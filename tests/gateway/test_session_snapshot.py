"""Resident-only snapshots exercise the real socket protocol, SQLite and memory routing."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from agent.incognito import register_session
from agent.incognito_agent import IncognitoMixin
from agent.memory_manager import MemoryManager
from agent.tool_dispatch_helpers import make_tool_result_message
from gateway.config import GatewayConfig, Platform
from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.session import SessionSource, SessionStore
from gateway.session_snapshot import _digest, _json, make_session_snapshot_handler
from gateway.session_state import SessionState
from hermes_state import SessionDB
from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.private_reads import PrivateReadResponse
from tests.home_io_guard import HomeIOGuard

MARKER = "resident-snapshot-fixture-marker"
FACT = "The fixture owner plays the bassoon."
QUERY = "Which instrument does the fixture owner play?"
CALL = "resident-snapshot-fixture-call"


class ResidentAgent(IncognitoMixin):
    def __init__(self, session_id, messages, manager):
        self.session_id = session_id
        self._session_messages = messages
        self._memory_manager = manager
        self._cached_system_prompt = "A frozen fixture prompt"
        self.model = "fixture-public-model"
        self.provider = "fixture-provider"
        self.incognito = False


@pytest.fixture
def resident(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    store = SessionStore(home / "sessions", GatewayConfig())
    store._db = db
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="17", thread_id="23", user_id="4"
    )
    entry = store.get_or_create_session(source)
    original_session_id = entry.session_id
    for message in (
        {"role": "user", "content": "Public prefix"},
        {"role": "assistant", "content": "Public reply"},
    ):
        store.append_to_transcript(entry.session_id, message)
    saved = db.get_messages_as_conversation(entry.session_id, repair_alternation=True)
    manager = MemoryManager()
    manager._session_id = entry.session_id
    provider = HindsightMemoryProvider()
    provider._mode = "local_external"
    provider._session_id = entry.session_id
    provider._bank_id = "fixture-bank"
    manager.add_provider(provider)
    agent = ResidentAgent(entry.session_id, deepcopy(saved), manager)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = store
    runner._sessions = {entry.session_key: SessionState()}
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = OrderedDict({entry.session_key: (agent, "fixture-signature")})
    case = SimpleNamespace(
        home=home,
        db=db,
        store=store,
        source=source,
        entry=entry,
        runner=runner,
        agent=agent,
        manager=manager,
        provider=provider,
        saved=saved,
    )
    yield case
    register_session(entry.session_key, False)
    register_session(original_session_id, False)
    register_session(entry.session_id, False)
    db.close()


def enter_and_recall(case):
    case.store.set_incognito(case.entry.session_key, True)
    case.agent.set_incognito(True)
    case.agent.model = "fixture-private-model"
    response = PrivateReadResponse(
        None,
        _digest(QUERY),
        _digest(case.provider._bank_id),
        _digest(case.entry.session_id),
        case.provider._privacy_epoch,
        True,
    )
    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": CALL,
                "type": "function",
                "function": {
                    "name": "hindsight_recall",
                    "arguments": json.dumps({"query": QUERY}),
                },
            }
        ],
    }
    case.agent._session_messages += [
        {"role": "user", "content": MARKER},
        call,
        make_tool_result_message("hindsight_recall", response.tool_result(FACT), CALL),
    ]
    case.store._incognito_histories[case.entry.session_id] = deepcopy(
        case.agent._session_messages
    )


async def request(case, *, probe=True):
    handler = make_session_snapshot_handler(case.runner, asyncio.get_running_loop())
    server = GatewayControlServer(
        home=case.home, verb_handlers={"session-snapshot": handler}
    )
    params = {"session_key": case.entry.session_key}
    if probe:
        params["recall_probe"] = {"marker": MARKER, "expected_fact": FACT}
    raw = json.dumps({"id": 1, "verb": "session-snapshot", "params": params}).encode()
    return json.loads(await asyncio.to_thread(server.handle_request_line, raw))


def test_real_protocol_witness_is_content_free_and_observational(resident, monkeypatch):
    enter_and_recall(resident)
    before = resident.db._conn.total_changes
    statements = []
    resident.db._conn.set_trace_callback(statements.append)

    def forbidden(*args, **kwargs):
        raise AssertionError("Snapshot attempted a loader, resolver or new connection")

    with monkeypatch.context() as guarded:
        for name in (
            "lookup_by_session_key",
            "load_transcript",
            "_load_saved_transcript",
            "_private_history",
            "_db_for_key",
            "_open_session_db_for_active_scope",
            "_profile_home_for_key",
        ):
            guarded.setattr(resident.store, name, forbidden)
        guarded.setattr(resident.db, "_get_read_conn", forbidden)
        guarded.setattr(resident.db, "_reopen_after_close_locked", forbidden)
        guard = HomeIOGuard(lambda: (resident.home,))
        guard.install(guarded)
        response = asyncio.run(request(resident))
    resident.db._conn.set_trace_callback(None)
    assert response["ok"] is True
    observed = response["result"]
    assert (
        observed["found"] is True
        and observed["incognito"] is True
        and observed["busy"] is False
    )
    assert observed["agent"]["model"] == "fixture-private-model"
    assert observed["agent"]["route_source"] == "cached"
    assert observed["history"]["count"] == len(resident.agent._session_messages)
    assert observed["history"]["source"] == "private_ram"
    assert (
        resident.db.get_messages_as_conversation(
            resident.entry.session_id, repair_alternation=True
        )
        == resident.saved
    )
    assert all(
        observed["recall"][name] is True
        for name in (
            "matched",
            "private_boundary",
            "provider_read_only",
            "result_nonempty",
            "expected_fact_match",
        )
    )
    assert all(
        value not in json.dumps(response)
        for value in (MARKER, FACT, QUERY, CALL, "A frozen fixture prompt")
    )
    assert resident.db._conn.total_changes == before
    assert statements and all(
        sql.lstrip().upper().startswith("SELECT ") for sql in statements
    )


def test_native_socket_transport_keeps_the_snapshot_off_the_loop(resident):
    enter_and_recall(resident)

    async def scenario():
        handler = make_session_snapshot_handler(
            resident.runner, asyncio.get_running_loop()
        )
        server = GatewayControlServer(
            home=resident.home, verb_handlers={"session-snapshot": handler}
        )
        assert await server.start()
        try:
            result = await asyncio.to_thread(
                query_gateway_control,
                resident.home,
                "session-snapshot",
                params={
                    "session_key": resident.entry.session_key,
                    "recall_probe": {"marker": MARKER, "expected_fact": FACT},
                },
            )
            assert result is not None
            assert result["recall"]["matched"] is True
            with pytest.raises(ValueError, match="^Session snapshot unavailable$"):
                handler({"session_key": resident.entry.session_key})
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_off_restores_same_session_prefix_prompt_and_zero_target_stores(resident):
    before = asyncio.run(request(resident, probe=False))["result"]
    enter_and_recall(resident)
    private = asyncio.run(request(resident))["result"]
    assert (
        private["private"]["store_count"] > 0
        and private["private"]["history_present"] is True
    )
    resident.agent.set_incognito(False)
    resident.store.set_incognito(resident.entry.session_key, False)
    resident.agent.model = "fixture-public-model"
    after = asyncio.run(request(resident, probe=False))["result"]
    for key in ("session_id", "history", "agent"):
        assert after[key] == before[key]
    assert after["private"] == {"history_present": False, "store_count": 0}
    assert after["incognito"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "unloaded",
        "missing_handle",
        "closed_handle",
        "store_locked",
        "missing_ram",
        "missing_registry",
    ],
)
def test_unavailable_state_never_becomes_an_empty_valid_snapshot(resident, failure):
    enter_and_recall(resident)
    held = False
    if failure == "unloaded":
        resident.store._loaded = False
    elif failure == "missing_handle":
        resident.store._db = None
    elif failure == "closed_handle":
        resident.db.close()
    elif failure == "store_locked":
        resident.store._lock.acquire()
        held = True
    elif failure == "missing_ram":
        resident.store._incognito_histories.clear()
    elif failure == "missing_registry":
        register_session(resident.entry.session_id, False)
    try:
        response = asyncio.run(request(resident))
        assert response["ok"] is False
        assert response["error"] == "ValueError: Session snapshot unavailable"
        assert all(value not in json.dumps(response) for value in (MARKER, FACT, QUERY))
    finally:
        if held:
            resident.store._lock.release()


@pytest.mark.parametrize(
    "mutation",
    ["turn_aba", "model_aba", "agent", "provider_epoch", "interval_aba", "session"],
)
def test_capture_rejects_changed_generations_and_aba_transitions(
    resident, monkeypatch, mutation
):
    enter_and_recall(resident)
    original = resident.db.get_messages_as_conversation
    fired = False

    def changed(*args, **kwargs):
        nonlocal fired
        rows = original(*args, **kwargs)
        if fired:
            return rows
        fired = True
        state = resident.runner._sessions[resident.entry.session_key]
        if mutation == "turn_aba":
            state.persistent.run_generation += 2
        elif mutation == "model_aba":
            state.persistent.model_override_epoch += 2
        elif mutation == "agent":
            replacement = ResidentAgent(
                resident.entry.session_id,
                deepcopy(resident.agent._session_messages),
                resident.manager,
            )
            resident.runner._agent_cache[resident.entry.session_key] = (
                replacement,
                "same-route",
            )
        elif mutation == "provider_epoch":
            resident.provider._privacy_epoch += 1
        elif mutation == "interval_aba":
            from agent.incognito import shared_worker_context

            context = shared_worker_context()
            context.run(resident.store.set_incognito, resident.entry.session_key, False)
            context.run(resident.store.set_incognito, resident.entry.session_key, True)
        elif mutation == "session":
            resident.entry.session_id = "different-fixture-session"
        return rows

    monkeypatch.setattr(resident.db, "get_messages_as_conversation", changed)
    response = asyncio.run(request(resident))
    assert (
        response["ok"] is False
        and response["error"] == "ValueError: Session snapshot unavailable"
    )


def test_uses_routed_provider_not_an_unrouted_provider_with_the_right_name(resident):
    enter_and_recall(resident)
    assert resident.provider in resident.manager._providers
    resident.manager._tool_to_provider.pop("hindsight_recall")
    response = asyncio.run(request(resident))
    assert response["ok"] is True
    assert response["result"]["recall"]["matched"] is False


def test_unknown_agent_boundary_and_busy_turn_cannot_witness(resident):
    enter_and_recall(resident)
    resident.agent._incognito_snapshot = None
    assert asyncio.run(request(resident))["result"]["recall"]["matched"] is False
    resident.agent._incognito_snapshot = {"_session_messages": resident.saved}
    resident.runner._sessions[resident.entry.session_key].turn.agent = resident.agent
    observed = asyncio.run(request(resident))["result"]
    assert observed["busy"] is True and observed["recall"]["matched"] is False


def test_private_model_replacement_with_empty_known_snapshot_uses_saved_prefix(
    resident,
):
    enter_and_recall(resident)
    resident.agent._incognito_snapshot = {"_session_messages": []}
    assert asyncio.run(request(resident))["result"]["recall"]["matched"] is True


def test_other_private_topics_do_not_change_target_cleanup_counts(resident):
    from agent.incognito import incognito_scope, private_state

    other = "unrelated-private-fixture-topic"
    register_session(other, True)
    try:
        with incognito_scope(True, other):
            state = private_state("fixture")
            assert state is not None
            state["value"] = "unrelated private fixture"
        observed = asyncio.run(request(resident, probe=False))["result"]
        assert observed["private"]["store_count"] == 0
    finally:
        register_session(other, False)


def test_resident_profile_handles_follow_a_b_a_and_fail_closed_on_a_cache_miss(
    resident,
):
    from gateway.session_persistence import _DB_UNPINNED

    store = resident.store
    store._db = _DB_UNPINNED
    store._db_handles[resident.home / "state.db"] = resident.db
    store.config.multiplex_profiles = True
    other_home = resident.home.parent / "secondary"
    other_home.mkdir()
    other_db = SessionDB(db_path=other_home / "state.db")
    store._profile_home_cache["fixture-secondary"] = other_home
    store._db_handles[other_home / "state.db"] = other_db
    try:
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="17",
            thread_id="24",
            user_id="4",
            profile="fixture-secondary",
        )
        other_entry = store.get_or_create_session(source)
        store.append_to_transcript(
            other_entry.session_id,
            {"role": "user", "content": "Secondary public prefix"},
        )
        other_saved = other_db.get_messages_as_conversation(
            other_entry.session_id, repair_alternation=True
        )
        assert other_saved and other_saved != resident.saved
        other = SimpleNamespace(
            home=resident.home, entry=other_entry, runner=resident.runner
        )
        first = asyncio.run(request(resident, probe=False))["result"]
        second = asyncio.run(request(other, probe=False))["result"]
        last = asyncio.run(request(resident, probe=False))["result"]
        assert first == last
        assert second["history"]["sha256"] == _digest(_json(other_saved))
        assert second["session_id"] != first["session_id"]
        assert second["agent"]["route_source"] == "none"
        store._profile_home_cache.clear()
        denied = asyncio.run(request(other, probe=False))
        assert (
            denied["ok"] is False
            and denied["error"] == "ValueError: Session snapshot unavailable"
        )
        assert asyncio.run(request(resident, probe=False))["result"] == first
    finally:
        other_db.close()


def test_resident_only_read_uses_no_pool_and_refuses_closed_or_locked_handles(
    resident, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Resident read attempted to open or reopen a connection")

    monkeypatch.setattr(resident.db, "_get_read_conn", forbidden)
    monkeypatch.setattr(resident.db, "_reopen_after_close_locked", forbidden)
    assert (
        resident.db.get_messages_as_conversation(
            resident.entry.session_id, repair_alternation=True, resident_only=True
        )
        == resident.saved
    )
    resident.db._lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="Resident session read unavailable"):
            resident.db.get_messages_as_conversation(
                resident.entry.session_id, resident_only=True
            )
    finally:
        resident.db._lock.release()
    resident.db.close()
    with pytest.raises(RuntimeError, match="Resident session read unavailable"):
        resident.db.get_messages_as_conversation(
            resident.entry.session_id, resident_only=True
        )
    with pytest.raises(ValueError, match="do not discover ancestors"):
        resident.db.get_messages_as_conversation(
            resident.entry.session_id, include_ancestors=True, resident_only=True
        )
