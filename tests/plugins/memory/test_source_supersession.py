"""Source-version settlement through real retention, restart and drain boundaries."""
import json
import itertools
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider
from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.source_retention import discover_source_candidates


@pytest.fixture(autouse=True)
def bounded_clock(monkeypatch):
    import plugins.memory.hindsight as hindsight
    # Deterministic expiration through the real drain, not subsecond wall timing.
    clock = itertools.count()
    monkeypatch.setattr(hindsight, "time", SimpleNamespace(
        monotonic=lambda: next(clock), sleep=lambda delay: None))


def versions(url="https://example.com/source"):
    def discover(text):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "extract", "type": "function",
             "function": {"name": "web_extract", "arguments": json.dumps({"url": url})}}]},
            {"role": "tool", "tool_call_id": "extract", "content": json.dumps({"results": [
                {"url": url, "content": text * 80, "error": None}]})},
        ]
        return discover_source_candidates(messages, retain_tool_sources=True)[0]
    return discover("Earlier source paragraph. "), discover("Updated source paragraph. ")


class Server:
    """Only SDK I/O is replaced; no provider resolution or network calls."""
    def __init__(self):
        self.operations = self.documents = self
        self.statuses = {}
        self.hash: str | None = None
        self.calls = []
        self.reject_hash: str | None = None
        self.child: str | None = None

    def aretain_batch(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["items"][0]["metadata"]["content_hash"] == self.reject_hash:
            raise RuntimeError("Submission refused")
        op = str(len(self.statuses))
        self.statuses[op] = "processing"
        if op == "1" and self.child:
            self.statuses["child"] = self.child
            return SimpleNamespace(operation_ids=[op, "child"])
        return SimpleNamespace(operation_id=op)

    def get_operation_status(self, *, bank_id, operation_id):
        from hindsight_client_api.exceptions import NotFoundException
        status = self.statuses[operation_id]
        if status == "gone":
            raise NotFoundException()
        return SimpleNamespace(status=status)

    def get_document(self, *, bank_id, document_id):
        return SimpleNamespace(id=document_id, document_metadata={"content_hash": self.hash})


def provider(server):
    result = load_memory_provider("hindsight", register_skills=False)
    assert isinstance(result, HindsightMemoryProvider)
    result._bank_id = "source-test"
    result._observation_scopes = []
    result._run_hindsight_operation = lambda operation: operation(server)
    result._RETAIN_OP_POLL_INTERVAL_S = 0
    return result


@pytest.mark.parametrize("journal_state", ["absent", "empty", "pending"])
def test_read_only_prefetch_never_recovers_or_settles_source_journal(journal_state):
    import asyncio
    import inspect
    from agent.delegation_context import delegated_child_context
    from hermes_constants import get_hermes_home
    from plugins.memory.hindsight.source_ledger import SourceJournal

    server = Server()
    parent = provider(server)
    path = get_hermes_home() / "memories" / "hindsight-source-operations.sqlite"
    if journal_state == "empty":
        SourceJournal(parent._api_url, parent._bank_id)
    elif journal_state == "pending":
        old, new = versions()
        parent._retain_source_candidates([old, new], parent._bank_id)
        server.statuses.update({"0": "completed", "1": "completed"})
        server.hash = new.content_hash
    if path.exists():
        path.chmod(0o640)  # opening the writer journal would also change mode

    def snapshot():
        return {str(p.relative_to(path.parent)): (p.read_bytes(), p.stat().st_mode,
                p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                for p in path.parent.glob("*") if p.is_file()}

    before = snapshot()
    observed = []
    async def arecall(**kwargs):
        observed.append(kwargs["query"])
        return SimpleNamespace(results=[SimpleNamespace(text="fixture recall")])
    server.arecall = arecall
    child = provider(server)
    child._read_only = True
    def operation(call):
        result = call(server)
        return asyncio.run(result) if inspect.isawaitable(result) else result
    child._run_hindsight_operation = operation
    with delegated_child_context(read_only_knowledge=True):
        child.queue_prefetch("source query")
        child._prefetch_thread.join(timeout=5)
        assert not child._prefetch_thread.is_alive()
        result = child.prefetch("source query")
    assert observed == ["source query"] and "fixture recall" in result
    assert snapshot() == before
    assert not child._source_ledger and not child._source_retain_ops
    assert not child._pending_retain_ops and getattr(child, "_source_journal", None) is None
    # Recovery and settlement still belong to the writable parent lifecycle.
    if journal_state == "pending":
        writable = provider(server)
        assert writable._wait_for_retains_drained(30)
        assert writable._source_ledger[old.automatic_key]["status"] == "superseded"
        assert writable._source_ledger[new.automatic_key]["status"] == "completed"


@pytest.mark.parametrize("old_status", ["completed", "gone"])
@pytest.mark.parametrize("restart", [False, True])
def test_superseded_source_settles_without_verifying_stale_hash(old_status, restart):
    old, new = versions()
    assert old.source_id == new.source_id and old.automatic_key != new.automatic_key
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old, new], p._bank_id)
    assert len(server.calls) == 2
    server.statuses.update({"0": old_status, "1": "completed"})
    server.hash = new.content_hash
    if restart:
        p = provider(server)
    assert p._wait_for_retains_drained(30)
    assert p._source_ledger[old.automatic_key]["status"] == "superseded"
    assert p._source_ledger[old.automatic_key]["superseded_by"] == new.automatic_key
    assert old.automatic_key not in p._source_retain_verified
    assert new.automatic_key in p._source_retain_verified
    assert not p._pending_retain_ops and not p._source_retain_ops
    assert len(server.calls) == 2  # recovery reads only; never reruns retain
    p._retain_source_candidates([old, new], p._bank_id)
    assert len(server.calls) == 2  # superseded is terminal, not retryable failure
    again = provider(server)
    assert again._wait_for_retains_drained(30)
    assert again._source_ledger[old.automatic_key]["status"] == "superseded"
    # Journal contains references only; original source provenance/content is
    # still exactly what the source retain sent, never rewritten for settlement.
    assert server.calls[0]["document_id"] == old.source_id
    assert server.calls[0]["items"][0]["metadata"] == old.metadata
    assert all(not entry["candidate"].content and not entry["candidate"].file_path
               for entry in again._source_ledger.values())
    other_bank = provider(server)
    other_bank._bank_id = "other-bank"
    assert other_bank._wait_for_retains_drained(30)
    assert not other_bank._source_ledger
    other_endpoint = provider(server)
    other_endpoint._api_url = "https://other.example.invalid"
    assert other_endpoint._wait_for_retains_drained(30)
    assert not other_endpoint._source_ledger
    # A stale journal writer cannot drop an independently accepted operation or
    # resurrect one another writer has already terminally reconciled.
    journal = again._source_journal
    stale = journal.load()[new.automatic_key]
    accepted = journal.save(dict(stale, status="accepted", operation_ids=["concurrent"],
                                 pending_operation_ids=["concurrent"]), accepted=True)
    preserved = journal.save(stale)
    assert preserved["pending_operation_ids"] == ["concurrent"]
    assert preserved["status"] == "accepted"  # stale completion cannot settle a new acceptance
    journal.save(dict(preserved, status="completed", pending_operation_ids=[]))
    terminal = journal.save(accepted)
    assert not terminal["pending_operation_ids"]
    assert terminal["status"] == "completed"


@pytest.mark.parametrize("scenario", [
    "new_failed", "new_submit_failed", "new_processing", "old_processing",
    "old_finishes_last", "unknown_hash", "new_child_failed", "new_child_processing", "other_source",
    "journal_write_failed",
])
def test_unsettled_source_evidence_survives_timeout_and_restart(scenario, monkeypatch):
    old, new = versions()
    if scenario == "other_source":
        _, new = versions("https://example.com/other")
    server = Server()
    if scenario == "new_submit_failed":
        server.reject_hash = new.content_hash
    if scenario.startswith("new_child_"):
        server.child = scenario.removeprefix("new_child_")
    p = provider(server)
    p._retain_source_candidates([old, new], p._bank_id)
    server.statuses.update({"0": "completed", "1": "completed"})
    server.hash = new.content_hash
    if scenario == "journal_write_failed":
        def unavailable(*args, **kwargs):
            raise OSError("journal unavailable")
        monkeypatch.setattr(p._source_journal, "save", unavailable)
        with pytest.raises(OSError, match="journal unavailable"):
            p._wait_for_retains_drained(30)
        assert p._pending_retain_ops == {"0", "1"}
        assert all(entry["status"] == "accepted" for entry in p._source_journal.load().values())
        recovered = provider(server)
        assert recovered._wait_for_retains_drained(30)
        assert recovered._source_ledger[old.automatic_key]["status"] == "superseded"
        assert old.automatic_key not in recovered._source_retain_verified
        assert len(server.calls) == 2
        return
    if scenario == "new_failed":
        server.statuses["1"] = "failed"
    elif scenario == "new_processing":
        server.statuses["1"] = "processing"
    elif scenario in {"old_processing", "old_finishes_last"}:
        server.statuses["0"] = "processing"
    elif scenario == "unknown_hash":
        server.hash = "unrelated-content"
    assert not p._wait_for_retains_drained(10)
    assert "0" in p._pending_retain_ops
    assert p._source_ledger[old.automatic_key]["status"] == "accepted"
    p = provider(server)
    assert not p._wait_for_retains_drained(10)
    assert "0" in p._pending_retain_ops
    assert old.automatic_key not in p._source_retain_verified
    if scenario in {"new_failed", "new_submit_failed", "new_child_failed"}:
        assert p._source_ledger[new.automatic_key]["status"] == "failed"
    if scenario == "old_finishes_last":
        # A later poll is NOT a newer source version. The old operation can still
        # overwrite the document after newer readback; verify only what is there.
        server.statuses["0"] = "completed"
        server.hash = old.content_hash
        assert p._wait_for_retains_drained(30)
        assert p._source_ledger[old.automatic_key]["status"] == "completed"
        assert "superseded_by" not in p._source_ledger[old.automatic_key]
    assert len(server.calls) == 2


@pytest.mark.parametrize("old_settlement", ["completed", "superseded"])
@pytest.mark.parametrize("restart", [False, True])
def test_fresh_source_reversion_preserves_history_and_replay_dedup(old_settlement, restart):
    old, new = versions()
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    if old_settlement == "completed":
        server.statuses["0"] = "completed"
        server.hash = old.content_hash
        assert p._wait_for_retains_drained(30)
        p._retain_source_candidates([old], p._bank_id)
        assert len(server.calls) == 1
    p._retain_source_candidates([new], p._bank_id)
    server.statuses.update({"0": "completed", "1": "completed"})
    server.hash = new.content_hash
    assert p._wait_for_retains_drained(30)
    historical = p._source_journal.load()[old.automatic_key]
    assert historical["status"] == old_settlement
    previous_sequence = p._source_ledger[new.automatic_key]["sequence"]
    if restart:
        p = provider(server)
    p._retain_source_candidates([new], p._bank_id)
    p._retain_source_candidates([old, new], p._bank_id)
    assert len(server.calls) == 2
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 3
    assert old.automatic_key not in p._source_retain_verified
    entry = p._source_journal.load()[old.automatic_key]
    assert entry["sequence"] > previous_sequence
    assert entry["operation_ids"] == ["0", "2"]
    assert entry["pending_operation_ids"] == ["2"]
    assert "superseded_by" not in entry
    # A stale instance's terminal snapshot is not evidence about this acceptance.
    p._source_journal.save(historical)
    persisted = p._source_journal.load()[old.automatic_key]
    assert persisted["status"] == "accepted"
    assert "superseded_by" not in persisted
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 3  # the new acceptance is still pending
    server.statuses["2"] = "completed"
    server.hash = old.content_hash
    assert p._wait_for_retains_drained(30)
    p = provider(server)
    assert p._wait_for_retains_drained(30)
    p._retain_source_candidates([old], p._bank_id)
    p._retain_source_candidates([new, old], p._bank_id)
    assert len(server.calls) == 3
    assert all(call["document_id"] == old.source_id for call in server.calls)
    assert p._source_ledger[old.automatic_key]["operation_ids"] == ["0", "2"]


@pytest.mark.parametrize("successor", ["completed", "failed", "submit_failed"])
def test_reversion_waits_for_pending_evidence_before_fresh_observation(successor):
    old, new = versions()
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    if successor == "submit_failed":
        server.reject_hash = new.content_hash
    p._retain_source_candidates([new], p._bank_id)
    server.statuses["1"] = "failed" if successor == "failed" else "completed"
    server.hash = new.content_hash if successor == "completed" else old.content_hash
    assert not p._wait_for_retains_drained(10)
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 2
    assert "0" in p._pending_retain_ops
    server.statuses["0"] = "completed"
    assert p._wait_for_retains_drained(30)
    # Recovery never replays stored payloads. A new source observation can now
    # revert a successful successor, but a failed successor is no supersession.
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == (3 if successor == "completed" else 2)
