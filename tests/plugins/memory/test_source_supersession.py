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


def accept_preexisting_versions(p, candidates):
    """Seed real accepted-operation tracking from a previous/concurrent producer.

    Recovery must still reconcile overlapping historical operations even though
    the current fresh-source producer serializes its own submissions.
    """
    for candidate in candidates:
        item = p._build_retain_kwargs(candidate.content, context=candidate.context,
                                     metadata=candidate.metadata, tags=list(candidate.tags))
        try:
            response = p._retain_batch(item, bank_id=p._bank_id,
                                       document_id=candidate.source_id, retain_async=True)
            p._track_retain_ops(response, p._bank_id, source_candidates=[candidate])
        except RuntimeError:
            p._source_candidate_failed(candidate)


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
        accept_preexisting_versions(parent, [old, new])
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
    accept_preexisting_versions(p, [old, new])
    assert len(server.calls) == 2
    server.statuses.update({"0": old_status, "1": "completed"})
    server.hash = new.content_hash
    if restart:
        p = provider(server)
    if old_status == "gone":
        from plugins.memory.hindsight.source_ledger import restore_source_ledger
        restore_source_ledger(p, p._bank_id)
        server.statuses["0"] = "completed"
        # Completion is known, but mismatched readback remains unresolved until
        # the newer operation is verified. Its subsequent eviction is safe.
        assert not p._is_retain_op_complete(p._bank_id, "0")
        server.statuses["0"] = "gone"
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
    accept_preexisting_versions(p, [old, new])
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
    accept_preexisting_versions(p, [new])
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
    accept_preexisting_versions(p, [new])
    server.statuses["1"] = "failed" if successor == "failed" else "completed"
    server.hash = new.content_hash if successor == "completed" else old.content_hash
    assert not p._wait_for_retains_drained(10)
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 2
    assert "0" in p._pending_retain_ops
    server.statuses["0"] = "completed"
    drained = p._wait_for_retains_drained(30)
    if successor == "completed":
        # The fresh observation was kept in memory until prior writes finished.
        assert not drained and len(server.calls) == 3
        server.statuses["2"] = "completed"
        server.hash = old.content_hash
        assert p._wait_for_retains_drained(30)
    else:
        assert drained
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == (3 if successor == "completed" else 2)


def test_discovered_repeated_version_remains_last_through_retention_and_restart():
    url = 'https://example.com/reverting-source'
    earlier, updated = versions(url)
    messages = [{'role': 'user', 'content': 'Read these source observations.'}]
    for index, candidate in enumerate([earlier, updated, earlier]):
        call_id = f'extract-{index}'
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{'id': call_id, 'type': 'function',
             'function': {'name': 'web_extract', 'arguments': json.dumps({'url': url})}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps({'results': [
                {'url': url, 'content': candidate.content, 'error': None}]})},
        ])
    discovered = discover_source_candidates(messages, retain_tool_sources=True)
    server = Server()
    p = provider(server)
    p._retain_source_candidates(discovered, p._bank_id)
    assert len(server.calls) == 1
    server.statuses['0'] = 'completed'
    server.hash = updated.content_hash
    assert not p._wait_for_retains_drained(30)
    assert server.calls[-1]['items'][0]['metadata']['content_hash'] == earlier.content_hash
    assert {call['items'][0]['content'] for call in server.calls} == {earlier.content, updated.content}
    server.statuses['1'] = 'completed'
    server.hash = earlier.content_hash
    assert p._wait_for_retains_drained(30)
    recovered = provider(server)
    assert recovered._wait_for_retains_drained(30)
    assert recovered._source_ledger[earlier.automatic_key]['status'] == 'completed'
    assert recovered._source_ledger[updated.automatic_key]['status'] == 'completed'


@pytest.mark.parametrize("finish_order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("latest", ["reversion", "successor"])
def test_pending_reversion_keeps_fresh_observation_until_async_writes_settle(finish_order, latest):
    class ApplyingServer(Server):
        def finish(self, operation):
            self.statuses[str(operation)] = "completed"
            self.hash = self.calls[operation]["items"][0]["metadata"]["content_hash"]

    old, new = versions()
    server = ApplyingServer()
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    accept_preexisting_versions(p, [new])
    # This is a fresh user-led turn's source discovery, not journal recovery.
    messages = [{"role": "user", "content": "Recheck the source now."},
        {"role": "assistant", "tool_calls": [{"id": "fresh", "type": "function",
         "function": {"name": "web_extract", "arguments": json.dumps({"url": "https://example.com/source"})}}]},
        {"role": "tool", "tool_call_id": "fresh", "content": json.dumps({"results": [
            {"url": "https://example.com/source", "content": old.content, "error": None}]})}]
    p._retain_source_candidates(discover_source_candidates(messages, retain_tool_sources=True), p._bank_id)
    if latest == "successor":
        p._retain_source_candidates([new], p._bank_id)
    assert len(server.calls) == 2  # pending operations are never duplicated
    assert not p._wait_for_retains_drained(10)
    for operation in finish_order:
        server.finish(operation)
    drained = p._wait_for_retains_drained(30)
    requires_write = latest == "reversion" or finish_order == (1, 0)
    if requires_write:
        assert len(server.calls) == 3  # latest fresh observation survives without rereading
        assert not drained
        server.finish(2)
        assert p._wait_for_retains_drained(30)
    else:
        assert len(server.calls) == 2 and drained
    assert server.hash == (old.content_hash if latest == "reversion" else new.content_hash)
    assert p._wait_for_retains_drained(30)
    assert len(server.calls) == (3 if requires_write else 2)
    # Recovery has operation references only, never a replayable deferred payload.
    recovered = provider(server)
    assert recovered._wait_for_retains_drained(30)
    assert len(server.calls) == (3 if requires_write else 2)


def test_deferred_fresh_payload_is_never_reconstructed_on_restart():
    old, new = versions()
    server = Server()
    p = provider(server)
    accept_preexisting_versions(p, [old, new])
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 2
    assert all(not entry["candidate"].content and not entry["candidate"].file_path
               for entry in p._source_journal.load().values())
    recovered = provider(server)
    server.statuses.update({"0": "completed", "1": "completed"})
    server.hash = new.content_hash
    assert recovered._wait_for_retains_drained(30)
    assert len(server.calls) == 2  # lost in-memory desire requires fresh observation


@pytest.mark.parametrize('prior_status', ['gone', 'not_found', 'processing', 'failed', 'cancelled'])
def test_deferred_reversion_requires_terminal_status_not_missing_status(prior_status):
    old, new = versions()
    server = Server()
    p = provider(server)
    accept_preexisting_versions(p, [old, new])
    p._retain_source_candidates([old], p._bank_id)
    server.statuses.update({'0': prior_status, '1': 'completed'})
    server.hash = 'unverified-remote-content'
    assert not p._wait_for_retains_drained(30)
    if prior_status in {'gone', 'not_found', 'processing'}:
        assert len(server.calls) == 2  # absent/unknown status cannot authorize another write
        assert '0' in p._pending_retain_ops
        server.statuses['0'] = 'completed'
        assert not p._wait_for_retains_drained(30)
    assert len(server.calls) == 3
    server.statuses['2'] = 'completed'
    server.hash = old.content_hash
    assert p._wait_for_retains_drained(30)
    assert not p._source_terminal_ops  # only live unresolved refs retain terminal evidence
    assert len(server.calls) == 3


@pytest.mark.parametrize('prior_status', ['completed', 'failed', 'gone'])
@pytest.mark.parametrize('repeat_old', [False, True])
@pytest.mark.parametrize('poll_between_observations', [False, True])
def test_fresh_versions_serialize_and_keep_intermediate_source_history(repeat_old, poll_between_observations, prior_status):
    old, new = versions('https://example.com/serialized')
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    if poll_between_observations:
        assert not p._wait_for_retains_drained(10)
    p._retain_source_candidates([new], p._bank_id)
    if repeat_old:
        p._retain_source_candidates([old], p._bank_id)
    # B cannot finish before A: it has not yet been submitted.
    assert len(server.calls) == 1
    if prior_status == 'gone':
        server.statuses['0'] = 'gone'
        server.hash = old.content_hash  # exact content is not proof the missing writer stopped
        assert not p._wait_for_retains_drained(30)
        assert len(server.calls) == 1
    expected = [old, new, old] if repeat_old else [old, new]
    for index, candidate in enumerate(expected):
        assert server.calls[index]['items'][0]['metadata']['content_hash'] == candidate.content_hash
        failed = prior_status == 'failed' and index == 0
        server.statuses[str(index)] = 'failed' if failed else 'completed'
        server.hash = 'no-source-yet' if failed else candidate.content_hash
        drained = p._wait_for_retains_drained(30)
        assert drained is (index == len(expected) - 1)
        assert len(server.calls) == min(index + 2, len(expected))
    assert server.hash == expected[-1].content_hash
    assert {call['items'][0]['content'] for call in server.calls} == {old.content, new.content}
    assert p._wait_for_retains_drained(30) and len(server.calls) == len(expected)


@pytest.mark.parametrize('failure', ['submission', 'operation'])
def test_serialized_successor_failure_is_not_an_automatic_retry_loop(failure):
    old, new = versions('https://example.com/serialized-failure')
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old, new], p._bank_id)
    assert len(server.calls) == 1
    if failure == 'submission':
        server.reject_hash = new.content_hash
    server.statuses['0'] = 'completed'
    server.hash = old.content_hash
    drained = p._wait_for_retains_drained(30)
    assert len(server.calls) == 2
    if failure == 'operation':
        assert not drained
        server.statuses['1'] = 'failed'
    assert p._wait_for_retains_drained(30) is (failure == "operation")
    assert p._wait_for_retains_drained(30) is (failure == "operation")
    assert p._source_ledger[new.automatic_key]['status'] == 'failed'
    assert server.hash == old.content_hash and len(server.calls) == 2


def test_deferred_exception_keeps_intermediate_payload_and_blocks_successor():
    from dataclasses import replace
    import hashlib

    old, middle = versions('https://example.com/deferred-exception')
    content = 'Latest source paragraph. ' * 80
    latest = replace(middle, content=content, content_hash=hashlib.sha256(content.encode()).hexdigest())
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old, middle, latest], p._bank_id)
    server.statuses['0'] = 'completed'
    server.hash = old.content_hash
    server.reject_hash = middle.content_hash
    assert not p._wait_for_retains_drained(30)
    assert len(server.calls) == 2
    assert [item[0].content for item in p._deferred_source_candidates[middle.source_id]] == [middle.content, latest.content]


def test_separate_providers_do_not_submit_overlapping_source_replacements():
    from plugins.memory.hindsight.source_ledger import restore_source_ledger

    old, new = versions('https://example.com/multi-provider')
    server = Server()
    first, second = provider(server), provider(server)
    assert first is not second
    # Both ordinary AIAgent instances can initialize before either sees a source.
    restore_source_ledger(first, first._bank_id)
    restore_source_ledger(second, second._bank_id)
    first._retain_source_candidates([old], first._bank_id)
    second._retain_source_candidates([new], second._bank_id)
    assert len(server.calls) == 1


def test_simultaneous_provider_admission_and_terminal_handoff(monkeypatch):
    import contextvars
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from plugins.memory.hindsight.source_ledger import SourceJournal, restore_source_ledger

    old, new = versions('https://example.com/simultaneous')
    server = Server()
    first, second = provider(server), provider(server)
    for p in (first, second):
        restore_source_ledger(p, p._bank_id)
    barrier = threading.Barrier(2)
    original = SourceJournal.reserve
    def simultaneous(self, candidate):
        barrier.wait(timeout=10)
        return original(self, candidate)
    monkeypatch.setattr(SourceJournal, 'reserve', simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contextvars.copy_context().run, p._retain_source_candidates, [c], p._bank_id)
                   for p, c in ((first, old), (second, new))]
        for future in futures:
            future.result(timeout=15)
    monkeypatch.setattr(SourceJournal, 'reserve', original)
    assert len(server.calls) == 1
    accepted_hash = server.calls[0]['items'][0]['metadata']['content_hash']
    waiter = second if accepted_hash == old.content_hash else first
    waiting = new if waiter is second else old
    assert not waiter._wait_for_retains_drained(10)
    assert len(server.calls) == 1
    server.statuses['0'] = 'completed'
    server.hash = accepted_hash
    assert not waiter._wait_for_retains_drained(30)
    assert len(server.calls) == 2
    assert server.calls[1]['items'][0]['content'] == waiting.content
    server.statuses['1'] = 'completed'
    server.hash = waiting.content_hash
    assert waiter._wait_for_retains_drained(30)


@pytest.mark.parametrize('crash_phase', ['reserved', 'accepted_without_receipt', 'response_lost'])
def test_unresolved_submission_survives_restart_without_replay(crash_phase):
    from plugins.memory.hindsight.source_ledger import restore_source_ledger

    old, new = versions('https://example.com/unknown-request')
    server = Server()
    first = provider(server)
    restore_source_ledger(first, first._bank_id)
    if crash_phase in {'reserved', 'accepted_without_receipt'}:
        assert first._source_journal.reserve(old)
        if crash_phase == 'accepted_without_receipt':
            item = first._build_retain_kwargs(old.content, metadata=old.metadata)
            server.aretain_batch(bank_id=first._bank_id, items=[item], document_id=old.source_id)
    else:
        original = server.aretain_batch
        def lose_response(**kwargs):
            original(**kwargs)
            raise TimeoutError('response lost after acceptance')
        server.aretain_batch = lose_response
        first._retain_source_candidates([old], first._bank_id)
        assert first._deferred_source_candidates[old.source_id][0][0].content == old.content
    calls = len(server.calls)
    restarted = provider(server)
    restarted._retain_source_candidates([old, new], restarted._bank_id)
    # Even a matching document is not terminal evidence for an unreceipted job.
    server.hash = new.content_hash
    for _ in range(3):
        assert not restarted._wait_for_retains_drained(30)
    assert len(server.calls) == calls
    assert [c.content for c, _ in restarted._deferred_source_candidates[old.source_id]] == [old.content, new.content]


def test_known_receipt_handoff_survives_restart_and_releases_on_terminal():
    old, new = versions('https://example.com/receipt-recovery')
    server = Server()
    first = provider(server)
    first._retain_source_candidates([old], first._bank_id)
    assert not first._source_journal.unresolved_submission(old.source_id)
    restarted = provider(server)
    restarted._retain_source_candidates([new], restarted._bank_id)
    assert len(server.calls) == 1
    server.statuses['0'] = 'completed'
    server.hash = old.content_hash
    assert not restarted._wait_for_retains_drained(30)
    assert len(server.calls) == 2


def test_pre_submit_validation_releases_reservation(monkeypatch):
    from dataclasses import replace
    old, _ = versions('https://example.com/invalid-file')
    old = replace(old, file_path='/nonexistent/source.txt')
    server = Server()
    p = provider(server)
    monkeypatch.setattr('plugins.memory.hindsight.read_verified_source_file', lambda c: None)
    p._retain_source_candidates([old], p._bank_id)
    assert not server.calls
    assert not p._source_journal.unresolved_submission(old.source_id)


def test_unknown_reservation_without_fresh_payload_does_not_report_drained():
    from plugins.memory.hindsight.source_ledger import restore_source_ledger
    old, _ = versions('https://example.com/unresolved-drain')
    server = Server()
    first = provider(server)
    restore_source_ledger(first, first._bank_id)
    assert first._source_journal.reserve(old)
    restarted = provider(server)
    assert not restarted._wait_for_retains_drained(30)
    assert not restarted._deferred_source_candidates
    assert not server.calls


def test_source_connection_error_does_not_retry_embedded_request(monkeypatch):
    from plugins.memory.hindsight import _SOURCE_SUBMISSION
    p = provider(Server())
    p._mode = 'local_embedded'
    clients = []
    p._get_client = lambda: clients.append(object()) or clients[-1]
    def fail(client):
        raise ConnectionError('connection reset by peer')
    token = _SOURCE_SUBMISSION.set(True)
    try:
        with pytest.raises(ConnectionError):
            HindsightMemoryProvider._run_hindsight_operation(p, fail)
    finally:
        _SOURCE_SUBMISSION.reset(token)
    assert len(clients) == 1


def test_deferred_pre_submit_failure_retries_only_on_later_drain(monkeypatch):
    from dataclasses import replace
    old, middle = versions('https://example.com/validation-retry')
    middle = replace(middle, file_path='/invalid/source.txt')
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old, middle], p._bank_id)
    checks = []
    monkeypatch.setattr('plugins.memory.hindsight.read_verified_source_file', lambda c: checks.append(c) or None)
    server.statuses['0'] = 'completed'
    server.hash = old.content_hash
    assert not p._wait_for_retains_drained(30)
    assert len(checks) == 1
    assert p._deferred_source_candidates[middle.source_id][0][0].content == middle.content
    assert not p._wait_for_retains_drained(30)
    assert len(checks) == 2
    assert len(server.calls) == 1


def test_refresh_keeps_unjournaled_acceptance_reconcilable(monkeypatch):
    from plugins.memory.hindsight.source_ledger import restore_source_ledger
    old, new = versions('https://example.com/journal-outage')
    server = Server()
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    server.statuses['0'] = 'completed'
    server.hash = old.content_hash
    assert p._wait_for_retains_drained(30)
    p._retain_source_candidates([new], p._bank_id)
    server.statuses['1'] = 'completed'
    server.hash = new.content_hash
    assert p._wait_for_retains_drained(30)
    original = p._source_journal.save
    def unavailable(*args, **kwargs):
        raise OSError('journal unavailable after acceptance')
    monkeypatch.setattr(p._source_journal, 'save', unavailable)
    p._retain_source_candidates([old], p._bank_id)
    assert '2' in p._source_ledger[old.automatic_key]['pending_operation_ids']
    restore_source_ledger(p, p._bank_id, refresh=True)
    assert '2' in p._source_ledger[old.automatic_key]['pending_operation_ids']
    assert p._source_retain_ops['2'].content == old.content
    assert p._source_journal.unresolved_submission(old.source_id)
    monkeypatch.setattr(p._source_journal, 'save', original)
    server.statuses['2'] = 'completed'
    server.hash = old.content_hash
    assert not p._wait_for_retains_drained(30)
    assert '2' in p._source_journal.load()[old.automatic_key]['terminal_operation_ids']
    assert len(server.calls) == 3


@pytest.mark.parametrize('failure_phase', ['refresh', 'dedup'])
@pytest.mark.parametrize('previously_completed', [False, True])
def test_pre_request_admission_failure_releases_real_reservation(monkeypatch, failure_phase, previously_completed):
    import sqlite3
    import plugins.memory.hindsight as hindsight
    from plugins.memory.hindsight.source_ledger import restore_source_ledger

    old, _ = versions('https://example.com/admission-failure')
    server = Server()
    p = provider(server)
    restore_source_ledger(p, p._bank_id)
    if previously_completed:
        p._retain_source_candidates([old], p._bank_id)
        server.statuses['0'] = 'completed'
        server.hash = old.content_hash
        assert p._wait_for_retains_drained(30)
    previous_calls = len(server.calls)
    original_restore = hindsight.restore_source_ledger
    original_dedup = p._source_candidate_already_submitted
    def fail_refresh(provider, bank_id, **kwargs):
        if provider._source_journal.unresolved_submission(old.source_id):
            raise sqlite3.OperationalError('database is locked after reserve')
        return original_restore(provider, bank_id, **kwargs)
    def fail_dedup(*args, **kwargs):
        assert p._source_journal.unresolved_submission(old.source_id)
        raise RuntimeError('dedup failed after reserve')
    if failure_phase == 'refresh':
        monkeypatch.setattr(hindsight, 'restore_source_ledger', fail_refresh)
    else:
        monkeypatch.setattr(p, '_source_candidate_already_submitted', fail_dedup)
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == previous_calls
    assert not p._source_journal.unresolved_submission(old.source_id)
    if previously_completed:
        assert p._source_ledger[old.automatic_key]['status'] == 'completed'
    monkeypatch.setattr(hindsight, 'restore_source_ledger', original_restore)
    monkeypatch.setattr(p, '_source_candidate_already_submitted', original_dedup)
    # A later fresh observation can progress: the failure never reached the SDK.
    p._retain_source_candidates([old], p._bank_id)
    assert len(server.calls) == 1
    assert server.calls[0]['items'][0]['content'] == old.content


@pytest.mark.parametrize('rejection', ['unauthorized', 'forbidden', 'schema'])
@pytest.mark.parametrize('file_upload', [False, True])
def test_definite_request_rejection_releases_source_admission(monkeypatch, rejection, file_upload):
    from dataclasses import replace
    from hindsight_client_api.exceptions import ApiException, UnauthorizedException, ForbiddenException
    old, _ = versions('https://example.com/definite-rejection')
    if file_upload:
        old = replace(old, file_path='/synthetic/source.txt')
        monkeypatch.setattr('plugins.memory.hindsight.read_verified_source_file', lambda c: b'bytes')
    server = Server()
    error = {
        'unauthorized': UnauthorizedException(status=401, body='{"detail":"Invalid API key"}'),
        'forbidden': ForbiddenException(status=403, body='{"detail":"Operation not allowed"}'),
        'schema': ApiException(status=422, body='{"detail":[{"type":"missing","loc":["body","items"],"msg":"Field required"}]}'),
    }[rejection]
    calls = []
    def rejected(**kwargs):
        calls.append(kwargs)
        raise error
    server.aretain_batch = rejected
    server._files_api = SimpleNamespace(file_retain=rejected)
    p = provider(server)
    p._retain_source_candidates([old], p._bank_id)
    assert len(calls) == 1
    assert not p._source_journal.unresolved_submission(old.source_id)
    assert old.automatic_key not in p._source_retain_verified
    # After an operator repairs credentials/input, one fresh attempt can proceed.
    def accepted(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(operation_id='accepted-after-fix')
    server.aretain_batch = accepted
    server._files_api.file_retain = accepted
    assert not p._wait_for_retains_drained(10)
    assert len(calls) == 2
    assert p._source_retain_ops['accepted-after-fix'].content == old.content


@pytest.mark.parametrize('failure', ['timeout', 'bad_request', 'server', 'unknown_422', 'defense_422', 'text_401', 'after_response'])
def test_uncertain_request_failure_keeps_exact_source_reservation(monkeypatch, failure):
    from hindsight_client_api.exceptions import ApiException, BadRequestException, UnauthorizedException
    old, _ = versions('https://example.com/uncertain-rejection')
    server = Server()
    error = {
        'timeout': TimeoutError('response lost'),
        'bad_request': BadRequestException(status=400, body='{"detail":"engine rejected late"}'),
        'server': ApiException(status=503),
        'unknown_422': ApiException(status=422, body='{"detail":"unknown validation stage"}'),
        'defense_422': ApiException(status=422, body='{"detail":{"violations":[]}}'),
        'text_401': RuntimeError('401 Unauthorized'),
        'after_response': UnauthorizedException(status=401),
    }[failure]
    p = provider(server)
    if failure == 'after_response':
        def fail_tracking(*args, **kwargs):
            raise error
        monkeypatch.setattr(p, '_track_retain_ops', fail_tracking)
    else:
        def fail_request(**kwargs):
            server.calls.append(kwargs)
            raise error
        server.aretain_batch = fail_request
    p._retain_source_candidates([old], p._bank_id)
    assert p._source_journal.unresolved_submission(old.source_id)
    assert len(server.calls) == 1
    assert p._deferred_source_candidates[old.source_id][0][0].content == old.content
    assert not p._wait_for_retains_drained(10)
    assert len(server.calls) == 1


def test_rejection_cleanup_does_not_release_a_different_token(monkeypatch):
    from hindsight_client_api.exceptions import UnauthorizedException
    old, _ = versions('https://example.com/rejection-token')
    server = Server()
    p = provider(server)
    def replaced_claim(**kwargs):
        import sqlite3
        with sqlite3.connect(p._source_journal.path) as db:
            db.execute('UPDATE source_submissions SET token=? WHERE scope=? AND source_id=?',
                       ('replacement-token', p._source_journal.scope, old.source_id))
        raise UnauthorizedException(status=401)
    server.aretain_batch = replaced_claim
    p._retain_source_candidates([old], p._bank_id)
    assert p._source_journal.unresolved_submission(old.source_id)
