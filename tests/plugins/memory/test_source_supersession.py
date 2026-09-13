"""Source-version settlement through real retention, restart and drain boundaries."""
import json
import itertools
from types import SimpleNamespace

import pytest

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
    result = HindsightMemoryProvider()
    result._bank_id = "source-test"
    result._observation_scopes = []
    result._run_hindsight_operation = lambda operation: operation(server)
    result._RETAIN_OP_POLL_INTERVAL_S = 0
    return result


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
    journal.save(dict(preserved, pending_operation_ids=[]))
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
