"""Privacy boundary tests use real sinks and fake network clients, never live services."""

import asyncio
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.incognito import (
    incognito_scope,
    is_incognito,
    register_session,
    tool_refusal,
)
from agent.incognito_agent import IncognitoMixin
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class Provider(MemoryProvider):
    @property
    def name(self):
        return "privacy-test"

    def get_tool_schemas(self):
        return []

    def __init__(self):
        self.writes = []
        self.recalls = []

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self._session_id = session_id

    def prefetch(self, query, *, session_id=""):
        self.recalls.append(query)
        return "remembered"

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        self.writes.append((user_content, assistant_content))

    def on_session_end(self, messages):
        self.writes.append(messages)

    def on_pre_compress(self, messages):
        self.writes.append(messages)

    def on_memory_write(self, action, target, content):
        self.writes.append(content)


@pytest.fixture(autouse=True)
def isolated_boundary():
    yield
    for session_id in (
        "privacy-test",
        "private-parent",
        "private-agent",
        "private-init",
    ):
        register_session(session_id, False)


def test_scope_nesting_and_thread_bound_context():
    from tools.thread_context import propagate_context_to_thread as ctx_bound

    results = []
    assert not is_incognito()
    with incognito_scope():
        with incognito_scope(False):
            assert is_incognito()
        worker = threading.Thread(
            target=ctx_bound(lambda: results.append(is_incognito()))
        )
        worker.start()
        worker.join()
    assert results == [True]
    assert not is_incognito()


def test_session_registry_crosses_raw_thread_and_stays_ram_only(tmp_path):
    before = set(tmp_path.rglob("*"))
    register_session("privacy-test")
    result = []
    worker = threading.Thread(
        target=lambda: result.append(is_incognito("privacy-test"))
    )
    worker.start()
    worker.join()
    assert result == [True]
    assert set(tmp_path.rglob("*")) == before


def test_private_ram_state_isolated_between_topics_and_discarded_on_exit():
    from agent.incognito import private_state

    register_session("private-parent")
    register_session("privacy-test")
    with incognito_scope(session_id="private-parent"):
        private_state("health")["private"] = True
        with incognito_scope(False):
            assert private_state("health") == {"private": True}
    with incognito_scope(session_id="privacy-test"):
        assert private_state("health") == {}
    with incognito_scope(session_id="private-parent"):
        assert private_state("health") == {"private": True}
    assert private_state("health") is None
    register_session("private-parent", False)
    register_session("private-parent")
    with incognito_scope(session_id="private-parent"):
        assert private_state("health") == {}
    with incognito_scope():
        private_state("health")["anonymous"] = True
    with incognito_scope():
        assert private_state("health") == {}


@pytest.mark.parametrize("changed_session", ["work-parent", "work-child"])
def test_revoked_work_keeps_ancestry_and_ram_local_without_poisoning_fresh_work(
    changed_session,
):
    from contextvars import ContextVar, copy_context
    from agent.incognito import private_state, shared_worker_context

    other_context = ContextVar("incognito-test-profile-and-secrets")
    marker = object()
    token = other_context.set(marker)
    try:
        with incognito_scope(False, "work-parent"):
            with incognito_scope(False, "work-child"):
                stale = copy_context()
        register_session(changed_session)
        register_session(changed_session, False)

        def finish_old_work():
            assert is_incognito()
            with incognito_scope(False, "work-child"):
                assert is_incognito()
                state = private_state("health")
                assert state is not None
                state["obsolete"] = True
                neutral = shared_worker_context()
                assert not neutral.run(is_incognito)
                assert neutral.run(other_context.get) is marker
                assert is_incognito()

        stale.run(finish_old_work)
        for session_id in ("work-parent", "work-child", "unrelated-work"):
            with incognito_scope(False, session_id):
                assert not is_incognito()
                assert private_state("health") is None
        register_session(changed_session)
        with incognito_scope(False, changed_session):
            assert private_state("health") == {}
        stale.run(finish_old_work)
        with incognito_scope(False, changed_session):
            assert private_state("health") == {}
    finally:
        register_session("work-parent", False)
        register_session("work-child", False)
        other_context.reset(token)


def test_logging_redacts_message_exception_and_stack_but_off_is_unchanged():
    stream = io.StringIO()
    logger = logging.getLogger("incognito-canary")
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("normal %s", "content")
        with incognito_scope():
            try:
                raise ValueError("PRIVATE_EXCEPTION_CANARY")
            except ValueError:
                logger.exception(
                    "PRIVATE_MESSAGE_CANARY %s", "PRIVATE_ARG_CANARY", stack_info=True
                )
        logger.warning("normal after")
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    assert "normal content" in text and "normal after" in text
    assert "PRIVATE_" not in text
    assert "Traceback" not in text
    assert "Incognito" in text


def test_memory_reads_continue_but_every_lifecycle_write_is_gated():
    provider = Provider()
    manager = MemoryManager(read_only=True)
    manager.add_provider(provider)
    manager.initialize_all("privacy-test")
    assert manager.prefetch_all("read query", session_id="privacy-test")
    manager.sync_all("private user", "private assistant")
    manager.on_memory_write("add", "memory", "private")
    manager.on_pre_compress([{"content": "private"}])
    manager.on_session_end([{"content": "private"}])
    provider.sync_turn("direct-private", "direct-private")
    assert provider.recalls == ["read query"]
    assert provider.writes == []
    manager.shutdown_all()


def test_boundary_keeps_preexisting_public_write_queue(monkeypatch):
    """Writes queued while public are public turns; a boundary delivers rather than drops them."""
    monkeypatch.setattr(MemoryManager, "_PRIVACY_FLUSH_TIMEOUT_S", 0.01)
    provider = Provider()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("privacy-test")
    queued = []

    class HeldExecutor:
        def submit(self, work):
            from concurrent.futures import Future

            future = Future()
            queued.append((work, future))
            return future

    manager._sync_executor = HeldExecutor()
    manager._submit_background(
        lambda: provider.sync_turn("queued", "old"), kind="write"
    )
    manager.set_read_only(True)
    manager.set_read_only(False)
    for work, future in queued:
        work()
    assert provider.writes == [("queued", "old")]
    provider.sync_turn("normal", "after")
    assert provider.writes == [("queued", "old"), ("normal", "after")]
    manager._sync_executor = None


def test_entering_privacy_delivers_queued_public_writes_first():
    from threading import Event, Thread

    provider = Provider()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("privacy-test")
    started, release = Event(), Event()

    def busy_worker():
        started.set()
        assert release.wait(5)

    # The single worker is busy, so this public turn is still queued at the boundary.
    manager._submit_background(busy_worker, kind="write")
    assert started.wait(5)
    manager._submit_background(lambda: provider.sync_turn("public before on", "kept"), kind="write")
    entering = Thread(target=manager.set_read_only, args=(True,))
    entering.start()
    try:
        release.set()
        entering.join(5)
        assert not entering.is_alive()
        assert provider.writes == [("public before on", "kept")]
        manager.sync_all("private user", "private assistant")
        assert provider.writes == [("public before on", "kept")]
    finally:
        release.set()
        manager.set_read_only(False)
        manager.shutdown_all()


def test_builtin_memory_loads_but_refuses_all_mutations(tmp_path, monkeypatch):
    from tools.memory_tool import MemoryStore

    memory = tmp_path / "MEMORY.md"
    memory.write_text("saved fact\n")
    store = MemoryStore(read_only=True)
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store.load_from_disk()
    assert "saved fact" in str(store.memory_entries)
    assert not store.add("memory", "PRIVATE_CANARY")["success"]
    assert not store.remove("memory", "saved fact")["success"]
    assert not store.replace("memory", "saved fact", "PRIVATE_CANARY")["success"]
    assert memory.read_text() == "saved fact\n"


def test_hindsight_direct_retain_central_sink_and_queue_are_fenced():
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._bank_id = "test-bank"
    provider._mode = "cloud"
    provider._retain_queue = __import__("queue").Queue()
    provider._ensure_writer = MagicMock()
    provider._register_atexit = MagicMock()
    job = MagicMock()
    provider._enqueue_retain(job)
    assert provider._retain_queue.qsize() == 1
    provider.read_only = True
    provider.read_only = False
    provider._retain_queue.get_nowait()()
    job.assert_called_once()  # queued while public: public work survives the boundary
    provider._session_turns = ["old"]
    provider.read_only = True
    assert provider._session_turns == ["old"]  # already retained public history is kept
    with patch.object(provider, "_run_hindsight_operation") as network:
        assert (
            provider._retain_batch({"content": "private"}, bank_id="test-bank") is None
        )
        assert (
            "incognito"
            in json.loads(
                provider.handle_tool_call("hindsight_retain", {"content": "private"})
            )["error"]
        )
    network.assert_not_called()
    provider._writer_stopped = True


def test_shared_hindsight_writer_keeps_public_work_and_refuses_private_work(
    monkeypatch,
):
    from threading import Event
    from plugins.memory.hindsight import HindsightMemoryProvider, _WRITER_SENTINEL

    provider = HindsightMemoryProvider()
    provider._session_id = "shared-retain-work"
    monkeypatch.setattr(provider, "_register_atexit", lambda: None)
    entered, release, completed = Event(), Event(), Event()
    delivered = []
    private_job = MagicMock()

    def already_started():
        entered.set()
        assert release.wait(5)

    def public_after():
        delivered.append("public after")
        completed.set()

    try:
        with incognito_scope(False, provider._session_id):
            provider._enqueue_retain(already_started)
            provider._enqueue_retain(lambda: delivered.append("public queued before on"))
        assert entered.wait(5)
        original_writer = provider._writer_thread
        register_session(provider._session_id)
        with incognito_scope(False, provider._session_id):
            provider._enqueue_retain(private_job)
        register_session(provider._session_id, False)
        with incognito_scope(False, provider._session_id):
            provider._enqueue_retain(public_after)
        release.set()
        assert completed.wait(5)
        assert provider._writer_thread is original_writer
        assert delivered == ["public queued before on", "public after"]
        private_job.assert_not_called()
    finally:
        release.set()
        register_session(provider._session_id, False)
        provider._shutting_down.set()
        provider._retain_queue.put(_WRITER_SENTINEL)
        if provider._writer_thread is not None:
            provider._writer_thread.join(timeout=5)
            assert not provider._writer_thread.is_alive()


def test_hindsight_recall_tool_remains_available():
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._mode = "cloud"
    provider._bank_id = "test"
    provider._session_id = "test-session"
    provider.read_only = True
    import hashlib
    from hindsight_client_api.models.recall_response import RecallResponse
    from plugins.memory.hindsight.private_reads import PrivateReadResponse

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    response = PrivateReadResponse(
        value=RecallResponse.model_validate(
            {"results": [{"id": "saved-fact", "text": "Saved fact"}]}
        ),
        query_sha256=digest("old memory"),
        bank_sha256=digest("test"),
        session_sha256=digest("test-session"),
        epoch=provider._privacy_epoch,
        result_nonempty=True,
    )
    with patch.object(
        provider, "_run_hindsight_operation", return_value=response
    ) as recall:
        payload = json.loads(
            provider.handle_tool_call("hindsight_recall", {"query": "old memory"})
        )
    assert payload == {
        "result": "1. Saved fact",
        "_hermes_private_read": {
            "no_store": True,
            "session_sha256": digest("test-session"),
            "epoch": provider._privacy_epoch,
            "result_nonempty": True,
            "bank_sha256": digest("test"),
            "query_sha256": digest("old memory"),
            "result_text_sha256": digest("1. Saved fact"),
        },
    }
    recall.assert_called_once()


def test_toggle_restores_saved_history_and_persistence_settings():
    class Agent(IncognitoMixin):
        pass

    agent = Agent()
    agent.session_id = "private-agent"
    agent.incognito = False
    agent._session_messages = [{"role": "user", "content": "saved"}]
    agent._cached_system_prompt = "frozen pre-incognito prompt"
    database = object()
    agent._session_db = database
    agent._persist_disabled = False
    agent.save_trajectories = True
    agent.skip_background_review = False
    agent.compression_checkpoint_required = True
    agent._checkpoint_mgr = SimpleNamespace(enabled=True)
    agent.context_compressor = None
    agent._memory_manager = MemoryManager()
    agent._memory_manager.add_provider(Provider())
    agent._memory_store = None
    agent.set_incognito(True)
    assert agent._session_db is None and agent._persist_disabled
    assert not agent._checkpoint_mgr.enabled
    assert not agent.save_trajectories
    agent._session_messages.append({"content": "PRIVATE_CANARY"})
    agent._cached_system_prompt = "private-only prompt cache"
    agent.set_incognito(False)
    assert agent._session_messages == [{"role": "user", "content": "saved"}]
    assert agent._cached_system_prompt == "frozen pre-incognito prompt"
    assert agent._session_db is database
    assert agent.save_trajectories and agent.compression_checkpoint_required
    assert agent._checkpoint_mgr.enabled


def test_agent_born_private_restores_compressor_store_on_off():
    from agent.incognito_agent import private_init

    database = object()

    class Agent(IncognitoMixin):
        @private_init
        def __init__(self, *, session_id, incognito=False, session_db=None):
            self.session_id = session_id
            self.incognito = incognito
            self._incognito_session_db = session_db if incognito else None
            self._session_db = None if incognito else session_db
            self.context_compressor = SimpleNamespace(_session_db=self._session_db)
            self._memory_manager = None
            self._memory_store = None

    agent = Agent(session_id="private-init", incognito=True, session_db=database)
    assert agent._incognito_born and agent.context_compressor._session_db is None
    agent.set_incognito(False)
    assert agent._session_db is database
    assert agent.context_compressor._session_db is database


@pytest.mark.parametrize(
    "name,args",
    [
        ("memory", {"action": "add"}),
        ("skill_manage", {"operations": []}),
        ("cronjob", {"action": "create"}),
        ("goal_set", {"action": "set"}),
        ("loop_set", {"action": "set"}),
        ("mcp__hindsight__retain", {"content": "private"}),
        ("terminal", {"persist_on_release": True}),
        ("terminal", {"survive_restart": True}),
    ],
)
def test_durable_tools_refuse_with_one_line(name, args):
    assert tool_refusal(name, args) is None
    with incognito_scope():
        assert "incognito" in tool_refusal(name, args)
        assert "\n" not in tool_refusal(name, args)
        assert tool_refusal("hindsight_recall", {"query": "read"}) is None
        assert tool_refusal("terminal", {"command": "pwd"}) is None


def test_request_dump_and_trajectory_sinks_do_not_create_files(tmp_path):
    from agent.agent_runtime_helpers import dump_api_request_debug
    from agent.session_persistence import SessionPersistenceMixin

    agent = SimpleNamespace(
        incognito=True, session_id="private-agent", logs_dir=tmp_path, model="test"
    )
    before = set(tmp_path.rglob("*"))
    assert (
        dump_api_request_debug(
            agent, {"messages": [{"content": "PRIVATE_CANARY"}]}, reason="privacy test"
        )
        is None
    )
    with incognito_scope():
        SessionPersistenceMixin._save_trajectory(
            agent, [{"content": "PRIVATE_CANARY"}], "private", True
        )
    assert set(tmp_path.rglob("*")) == before


def test_private_config_and_scratch_reads_do_not_initialize_home(tmp_path, monkeypatch):
    from hermes_cli.config import load_config
    import hermes_constants

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "_scratch_pruned_once", False)
    with incognito_scope():
        assert isinstance(load_config(), dict)
        assert hermes_constants.get_scratch_dir() == home / "cache" / "scratch"
    assert not home.exists()
    assert isinstance(load_config(), dict)
    assert (home / "SOUL.md").is_file()
    hermes_constants.get_scratch_dir()
    assert (home / "cache" / "scratch" / ".last_prune").is_file()


def test_private_logs_never_enter_file_queue_and_public_logging_resumes(tmp_path):
    import hermes_logging

    logger = logging.getLogger("incognito-file-boundary")
    hermes_logging._reset_queued_handlers()
    try:
        hermes_logging.setup_logging(hermes_home=tmp_path, log_level="INFO", force=True)
        logger.warning("public before")
        hermes_logging.flush_log_queue()
        before = {
            p: p.read_bytes() for p in (tmp_path / "logs").iterdir() if p.is_file()
        }
        with incognito_scope():
            assert (
                hermes_logging.setup_logging(hermes_home=tmp_path / "private")
                == tmp_path / "private" / "logs"
            )
            logger.warning("PRIVATE_LOG_CANARY")
            held = logger.makeRecord(
                logger.name,
                logging.WARNING,
                __file__,
                0,
                "PRIVATE_HELD_CANARY",
                (),
                None,
            )
        logger.handle(held)
        hermes_logging.flush_log_queue()
        assert {
            p: p.read_bytes() for p in (tmp_path / "logs").iterdir() if p.is_file()
        } == before
        assert not (tmp_path / "private").exists()
        logger.warning("public after")
        hermes_logging.flush_log_queue()
        assert b"public after" in (tmp_path / "logs" / "agent.log").read_bytes()
    finally:
        hermes_logging._reset_queued_handlers()
