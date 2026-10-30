"""Standby Telegram deferral and in-place promotion seams."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import gateway_slot, zero_cut_standby as zcs
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.run import _start_gateway_shutdown_tail


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: {"standby": False} if slot == "a" else None)
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    zcs.reset_for_tests()
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "restore_completions", lambda: 0)
    adopted = []
    monkeypatch.setattr(zcs, "_start_adopting", adopted.append)  # covered by its own tests below
    yield
    zcs.reset_for_tests()
    gateway_slot.reset_gateway_slot_for_tests()


@pytest.mark.asyncio
async def test_standby_boot_leaves_delegation_ledger_and_promotion_replays_it(monkeypatch):
    from gateway.run import GatewayRunner
    from tools.process_registry import process_registry

    replays = []
    monkeypatch.setattr(process_registry, "restore_completions", lambda: replays.append("launch") or 0)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._profile_adapters = {}
    runner._start_secondary_profile_adapters = lambda: asyncio.sleep(0, result=0)
    await runner._start_secondary_profiles(0, [])
    assert replays == []  # standby never touches the shared ledger at boot

    promoted = SimpleNamespace(
        adapters={}, _standby_deferred={},
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.mark_ready()
    assert (await zcs.promote(promoted))["promoted"] is True
    assert replays == ["launch"]


@pytest.mark.asyncio
async def test_slot_a_can_defer_and_promote(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    zcs.reset_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "a")
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: {"standby": False} if slot == "b" else None)
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    platform = Platform.TELEGRAM
    adapter = object()
    runner = SimpleNamespace(
        adapters={}, _standby_deferred={}, _failed_platforms={},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: None,
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=True),
        _publish_primary_adapter=lambda p, a: runner.adapters.__setitem__(p, a),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    assert zcs.defer_platform(runner, platform, object())
    assert zcs.readiness()["slot"] == "a"
    zcs.mark_ready()
    assert (await zcs.promote(runner))["promoted"] is True


def test_slot_b_crash_restart_with_slot_a_down_is_active_and_does_not_defer_telegram(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    zcs.reset_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot", lambda home, slot, timeout: None)
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    runner = SimpleNamespace(_standby_deferred={})
    assert zcs.is_standby() is False
    assert zcs.defer_platform(runner, Platform.TELEGRAM, object()) is False
    assert runner._standby_deferred == {}


@pytest.mark.asyncio
async def test_prefilter_defers_telegram_before_adapter_creation(tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="secret")},
        sessions_dir=tmp_path / "sessions",
    )
    runner._failed_platforms = {}
    runner._abort_startup_if_shutdown_requested = lambda *args: asyncio.sleep(0, result=False)
    runner._create_adapter = lambda *args: pytest.fail("standby must not create Telegram")
    aborted, enabled, skipped, pending = await runner._start_prefilter_platforms()
    assert not aborted
    assert enabled == 0
    assert pending == []
    assert runner._failed_platforms == {}
    assert zcs.readiness()["deferred"] == ["telegram"]


@pytest.mark.asyncio
async def test_promote_connects_once_starts_cron_and_is_idempotent(monkeypatch):
    platform = Platform.TELEGRAM
    adapter = object()
    calls = []
    runner = SimpleNamespace(
        adapters={},
        _standby_deferred={(None, platform): object()},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: calls.append("wire"),
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=True),
        _publish_primary_adapter=lambda p, a: (calls.append("publish"), runner.adapters.__setitem__(p, a)),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0, result=calls.append("prime")),
        _start_finish_wiring=lambda count: asyncio.sleep(0, result=calls.append("finish")),
        _start_spawn_background_watchers=lambda: calls.append("watchers"),
        _safe_adapter_disconnect=lambda *args: asyncio.sleep(0),
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: ("stop", "provider", "cron", "house"))
    zcs.defer_platform(runner, platform, runner._standby_deferred[(None, platform)])
    zcs.mark_ready()
    first = await zcs.promote(runner)
    second = await zcs.promote(runner)
    assert first == {"promoted": True, "already": False, "failed": []}
    assert second == {"promoted": True, "already": True}
    assert runner.adapters[platform] is adapter
    assert runner._zero_cut_cron_handles == ("stop", "provider", "cron", "house")
    assert calls.count("publish") == 1
    assert calls.index("prime") < calls.index("publish")  # shared store opened before serving
    assert zcs.is_standby() is False


@pytest.mark.asyncio
async def test_promote_queues_failed_primary_connect_for_reconnect(monkeypatch):
    platform = Platform.TELEGRAM
    config = object()
    adapter = object()
    runner = SimpleNamespace(
        adapters={},
        _failed_platforms={},
        _standby_deferred={},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: None,
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=False),
        _startup_retry_entry=lambda p, a, c: {"adapter": a, "config": c},
        _publish_primary_adapter=lambda p, a: pytest.fail("failed connect must not publish"),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, config)
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result == {"promoted": True, "already": False, "failed": ["telegram"]}
    assert runner._failed_platforms[platform] == {"adapter": adapter, "config": config}
    assert runner.adapters == {}


@pytest.mark.asyncio
async def test_promote_that_raises_stays_standby_and_a_retry_completes(monkeypatch):
    platform = Platform.TELEGRAM
    adapter = object()
    primes = {"n": 0}

    async def prime():
        primes["n"] += 1
        if primes["n"] == 1:
            raise RuntimeError("state.db busy")

    runner = SimpleNamespace(
        adapters={},
        _standby_deferred={},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: None,
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=True),
        _publish_primary_adapter=lambda p, a: runner.adapters.__setitem__(p, a),
        _start_prime_session_db_after_ready=prime,
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, object())
    zcs.mark_ready()
    with pytest.raises(RuntimeError):
        await zcs.promote(runner)
    assert zcs.is_standby() is True
    assert zcs.readiness()["deferred"] == ["telegram"]  # nothing consumed, retry can serve it
    assert await zcs.promote(runner) == {"promoted": True, "already": False, "failed": []}
    assert runner.adapters[platform] is adapter
    assert zcs.is_standby() is False


@pytest.mark.asyncio
async def test_prefilter_does_not_defer_multiplex_telegram_without_credential(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="")},
        sessions_dir=tmp_path / "sessions",
    )
    runner._failed_platforms = {}
    runner._multiplex_on = lambda: True
    runner._abort_startup_if_shutdown_requested = lambda *args: asyncio.sleep(0, result=False)
    runner._create_adapter = lambda *args: pytest.fail("no credential must not create Telegram")
    monkeypatch.setattr("gateway.run._platform_has_bot_credential", lambda p, c: False)
    _aborted, _enabled, skipped, _pending = await runner._start_prefilter_platforms()
    assert skipped == [Platform.TELEGRAM]
    assert zcs.readiness()["deferred"] == []


def test_promote_verb_over_real_socket_refuses_until_ready(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="hgw-", dir="/tmp"))
    runner = SimpleNamespace(
        adapters={}, _standby_deferred={},
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)

    async def scenario():
        loop = asyncio.get_running_loop()
        verb = lambda: asyncio.run_coroutine_threadsafe(zcs.promote(runner), loop).result()
        server = GatewayControlServer(home, verb_handlers={"promote": verb})
        assert await server.start()

        def ask():
            return query_gateway_control(home, "promote", slot="b")
        try:
            before = await loop.run_in_executor(None, ask)
            zcs.mark_ready()
            first = await loop.run_in_executor(None, ask)
            again = await loop.run_in_executor(None, ask)
            return before, first, again
        finally:
            await server.stop()

    try:
        before, first, again = asyncio.run(scenario())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert before is not None and first is not None and again is not None
    assert before["promoted"] is False and before["refused"] == "not_ready"
    assert first["promoted"] is True and first["already"] is False
    assert again["promoted"] is True and again["already"] is True


def test_promote_refused_on_active_slot(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "a")
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    zcs.mark_ready()
    assert asyncio.run(zcs.promote(SimpleNamespace())) == {"promoted": False, "refused": "not_standby"}


@pytest.mark.asyncio
async def test_flag_off_slot_b_creates_telegram_as_today(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    gateway_slot.reset_gateway_slot_for_tests()
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": False}}})
    assert zcs.is_standby() is False
    created = []
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="secret")},
        sessions_dir=tmp_path / "sessions",
    )
    runner._failed_platforms = {}
    runner._abort_startup_if_shutdown_requested = lambda *args: asyncio.sleep(0, result=False)
    runner._create_adapter = lambda platform, config: created.append(platform)
    await runner._start_prefilter_platforms()
    assert created == [Platform.TELEGRAM]
    assert zcs.readiness()["deferred"] == []


@pytest.mark.asyncio
async def test_shutdown_tail_accepts_none_handles(monkeypatch):
    runner = SimpleNamespace(_zero_cut_cron_handles=(None, None, None, None), config=None)
    monkeypatch.setattr("gateway.run._settle_mcp_discovery", lambda r: asyncio.sleep(0))
    monkeypatch.setattr("gateway.run._shutdown_mcp_servers_nonblocking", lambda config=None: asyncio.sleep(0))
    monkeypatch.setattr("gateway.run._resolve_gateway_exit_verdict", lambda r, s: True)
    monkeypatch.setattr("gateway.run._best_effort", lambda *args, **kwargs: None)
    planned = SimpleNamespace(set=lambda: None, join=lambda timeout: None)
    assert await _start_gateway_shutdown_tail(
        runner, None, None, None, None, None, planned, planned, [False]
    ) is True


def test_promotion_does_not_spawn_a_second_reconnect_watcher():
    from gateway.run import GatewayRunner

    spawned, supervised = [], []
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._failed_platforms = {}
    runner._reconnect_watcher_task = None

    class LiveTask:
        def done(self):
            return False

    def spawn_reconnect():
        spawned.append("reconnect")
        runner._reconnect_watcher_task = LiveTask()

    runner._spawn_reconnect_watcher = spawn_reconnect
    runner._spawn_supervised = lambda fn, name, **kw: supervised.append(name)
    runner._scale_to_zero_should_arm = lambda: False
    runner._log_scale_to_zero_not_armed_reason = lambda: None
    runner._start_spawn_background_watchers()  # standby: reconnect watcher only
    assert spawned == ["reconnect"] and supervised == []
    zcs.mark_ready()
    zcs._promoted = True  # what promote() sets before spawning the deferred watchers
    runner._start_spawn_background_watchers()
    assert spawned == ["reconnect"]
    assert "drain_control_watcher" in supervised


def _secondary_runner(tmp_path, connect):
    """A real GatewayRunner shell whose secondary profiles start through _start_one_profile_adapters."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._failed_platforms = {}
    runner._profile_failed_platforms = {}
    runner._profile_adapters = {}
    runner._standby_deferred = {}
    runner._primary_profile_name = "default"
    runner._served_profile_homes = {"work": tmp_path / "work", "play": tmp_path / "play"}
    runner._start_prime_session_db_after_ready = lambda: asyncio.sleep(0)
    runner._restore_secondary_completion_ledgers = lambda homes: None
    runner._start_finish_wiring = lambda count: asyncio.sleep(0)
    runner._start_spawn_background_watchers = lambda: None
    runner._multiplex_on = lambda: False
    runner._sync_voice_mode_state_to_adapter = lambda adapter: None
    runner._configure_profile_adapter = lambda adapter, profile, platform: None
    runner._connect_initial_adapter_with_timeout = connect
    tokens = {"work": "tok-shared", "play": "tok-shared"}
    scopes = []

    async def load_config(profile, home):
        return GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token=tokens[profile])})

    def create(platform, config):
        from hermes_constants import get_hermes_home
        scopes.append(str(get_hermes_home()))
        return SimpleNamespace(platform=platform, config=config, token=config.token)

    runner._load_secondary_profile_config = load_config
    runner._create_adapter = create
    runner._adapter_credential_claim = staticmethod(lambda platform, adapter: (platform, adapter.token))
    runner._adapter_listener_claim = staticmethod(lambda platform, adapter: None)
    runner._adapter_credential_fingerprint = lambda adapter: None
    runner._duplicate_credential_origins = lambda *a: (None, None)
    reconnects = []
    runner._safe_adapter_disconnect = lambda adapter, platform: asyncio.sleep(0)
    runner._schedule_secondary_profile_startup_reconnect = (
        lambda profile, platform, adapter: reconnects.append((profile, platform)))
    runner.statuses = {}
    runner._update_platform_runtime_status = lambda key, **kw: runner.statuses.__setitem__(key, kw)
    return runner, scopes, reconnects


@pytest.mark.asyncio
async def test_promote_flags_a_secondary_whose_adapter_cannot_be_created(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    runner, _scopes, reconnects = _secondary_runner(tmp_path, lambda a, p: asyncio.sleep(0, result=True))
    runner._served_profile_homes = {"work": tmp_path / "work"}
    runner._create_adapter = lambda platform, config: None
    zcs.defer_platform(runner, Platform.TELEGRAM, object(), profile="work")
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result["failed"] == ["work:telegram"]
    assert runner.statuses["work:telegram"]["needs_attention"] is True  # visibly unserved, not dropped


@pytest.mark.asyncio
@pytest.mark.parametrize("heals", [True, False])
async def test_promote_primary_create_none_is_flagged_and_queued_when_it_can_heal(monkeypatch, heals):
    platform = Platform.TELEGRAM
    config = object()
    statuses = {}
    runner = SimpleNamespace(
        adapters={}, _failed_platforms={}, _standby_deferred={},
        _create_adapter=lambda p, c: None,
        _adapter_may_heal=lambda p, c: heals,
        _startup_retry_entry=lambda p, a, c: {"adapter": a, "config": c},
        _update_platform_runtime_status=lambda key, **kw: statuses.__setitem__(key, kw),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, config)
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result["failed"] == ["telegram"]
    assert statuses["telegram"]["needs_attention"] is True
    assert (platform in runner._failed_platforms) is heals


@pytest.mark.asyncio
async def test_promote_primary_create_raise_keeps_it_deferred_for_retry(monkeypatch):
    platform = Platform.TELEGRAM
    calls = {"n": 0}
    adapter = object()

    def create(p, c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("plugin import failed")
        return adapter

    runner = SimpleNamespace(
        adapters={}, _failed_platforms={}, _standby_deferred={},
        _create_adapter=create, _wire_adapter_handlers=lambda a: None,
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=True),
        _publish_primary_adapter=lambda p, a: runner.adapters.__setitem__(p, a),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, object())
    zcs.mark_ready()
    with pytest.raises(RuntimeError):
        await zcs.promote(runner)
    assert zcs.is_standby() and zcs.readiness()["deferred"] == ["telegram"]
    assert (await zcs.promote(runner))["failed"] == []
    assert runner.adapters[platform] is adapter


@pytest.mark.asyncio
async def test_promote_starts_secondaries_in_profile_scope_and_refuses_shared_token(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    runner, scopes, reconnects = _secondary_runner(tmp_path, lambda a, p: asyncio.sleep(0, result=True))
    for profile in ("work", "play"):
        zcs.defer_platform(runner, Platform.TELEGRAM, object(), profile=profile)
    zcs.mark_ready()
    result = await zcs.promote(runner)
    # Only the first profile in served order polls the shared token; the duplicate is refused.
    assert Platform.TELEGRAM in runner._profile_adapters["work"]
    assert Platform.TELEGRAM not in runner._profile_adapters.get("play", {})
    assert result["failed"] == ["play:telegram"]
    assert scopes == [str(tmp_path / "work"), str(tmp_path / "play")]  # created inside each profile scope
    assert reconnects == []


@pytest.mark.asyncio
async def test_promote_secondary_connect_raise_is_queued_for_reconnect(monkeypatch, tmp_path):
    async def boom(adapter, platform):
        raise RuntimeError("network down")

    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    runner, _scopes, reconnects = _secondary_runner(tmp_path, boom)
    runner._served_profile_homes = {"work": tmp_path / "work"}
    zcs.defer_platform(runner, Platform.TELEGRAM, object(), profile="work")
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result == {"promoted": True, "already": False, "failed": ["work:telegram"]}
    assert reconnects == [("work", Platform.TELEGRAM)]
    assert zcs.is_standby() is False


def _hold_lock(path):
    import fcntl
    handle = open(path, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


@pytest.mark.asyncio
async def test_promoted_slot_adopts_host_role_only_after_predecessor_exits(
        monkeypatch, tmp_path):
    """Review: promotion never took the host gateway role nor replayed the durable outbox, both skipped
    at standby boot. They wait for the predecessor, which holds the role and may still be sending."""
    calls = []
    monkeypatch.setattr(zcs, "_PREDECESSOR_POLL_S", 0.01)
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.run._claim_host_gateway_role", lambda force=False: calls.append("claim"))
    monkeypatch.setattr("gateway.run._refresh_host_gateway_record", lambda r: calls.append("record"))

    predecessor = _hold_lock(tmp_path / "gateway.lock")  # slot a still alive, finishing its turns
    runner = SimpleNamespace(config=SimpleNamespace())
    task = asyncio.create_task(zcs._adopt_predecessor_duties(runner))
    await asyncio.sleep(0.1)
    assert calls == []
    predecessor.close()  # slot a exits
    await asyncio.wait_for(task, 2)
    assert calls == ["claim", "record"]




def test_clients_follow_a_promoted_b_once_a_is_gone(monkeypatch, tmp_path):
    """Review: after a->b handover every unslotted reader still looked at slot a's (gone) files."""
    monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
    gateway_slot.reset_gateway_slot_for_tests()
    assert gateway_slot.serving_slot(tmp_path) == "a"  # flag-off shape: no b lock at all
    b = _hold_lock(tmp_path / "gateway.b.lock")
    a = _hold_lock(tmp_path / "gateway.lock")
    assert gateway_slot.serving_slot(tmp_path) == "a"  # both alive: a is still the home's gateway
    a.close()
    assert gateway_slot.serving_slot(tmp_path) == "b"
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: tmp_path)
    assert gateway_slot.process_slot_filename("gateway.pid") == "gateway.b.pid"
    b.close()
    assert gateway_slot.serving_slot(tmp_path) == "a"


def test_a_pinned_gateway_keeps_its_own_slot_files(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: tmp_path)
    b = _hold_lock(tmp_path / "gateway.b.lock")
    try:
        # _reset pinned this process to slot b; pin it to a and its own files never follow b.
        gateway_slot.reset_gateway_slot_for_tests()
        monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
        gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": False}}})
        assert gateway_slot.process_slot_filename("gateway.pid") == "gateway.pid"
    finally:
        b.close()


def test_serving_slot_opens_no_file_when_slot_b_has_no_lock(monkeypatch, tmp_path):
    """Hosted CI caught it: an adapter test patches ``Path.exists`` to True and ``open`` to a mock, so
    the probe opened (and closed) that mock for a b lock that does not exist, and the adapter's own
    log-file ``close()`` count came out 2."""
    import builtins
    from pathlib import Path
    monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
    gateway_slot.reset_gateway_slot_for_tests()
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(builtins, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    assert gateway_slot.serving_slot(tmp_path) == "a"
    assert opened == []
