import pytest
from unittest.mock import AsyncMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE, is_global_startup_conflict
from gateway.run import GatewayRunner
from gateway.status import read_runtime_status


@pytest.mark.parametrize(
    "code, expected",
    [
        ("telegram-bot-token_lock", True),   # BasePlatformAdapter._acquire_platform_lock
        ("discord-bot-token_lock", True),
        ("whatsapp-session_lock", True),
        ("feishu_app_lock", True),
        ("lock_conflict", True),             # buzz / irc / line identity conflicts
        ("telegram_connect_error", False),
        ("telegram_auth_error", False),
        ("relay_membership_required", False),
        ("duplicate_credential", False),
        ("", False),
        (None, False),
    ],
)
def test_is_global_startup_conflict_matches_lock_code_families(code, expected):
    assert is_global_startup_conflict(code) is expected


class _RetryableFailureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._set_fatal_error(
            "telegram_connect_error",
            "Telegram startup failed: temporary DNS resolution failure.",
            retryable=True,
        )
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _DisabledAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=False, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        raise AssertionError("connect should not be called for disabled platforms")

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _SuccessfulAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_start_gateway_verbosity_imports_redacting_formatter(monkeypatch, tmp_path):
    """Verbosity != None must not crash with NameError on RedactingFormatter (#8044)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _CleanExitRunner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = True
            self.exit_reason = None
            self.exit_code = None
            self.adapters = {}

        async def start(self):
            assert self._platform_lock_takeover_on_start is False
            return True

        async def stop(self):
            return None

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _CleanExitRunner)

    from gateway.run import start_gateway

    # verbosity=1 triggers the code path that uses RedactingFormatter.
    # Before the fix this raised NameError.
    ok = await start_gateway(config=GatewayConfig(), replace=False, verbosity=1)

    assert ok is True


@pytest.mark.asyncio
async def test_start_gateway_replace_aborts_when_force_killed_pid_still_alive(
    monkeypatch, tmp_path
):
    """Regression for #19471 (duplicate-gateway half).

    If SIGKILL fails to reap the old gateway, --replace must NOT clear the PID
    file / scoped locks and start a fresh instance — that leaves two live
    gateways fighting over the same token. It should abort instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    calls = []
    removed_pid = False
    released_locks = False

    class _RunnerShouldNotStart:
        def __init__(self, config):
            raise AssertionError("replacement must not start while old PID is alive")

    def _mock_remove_pid_file():
        nonlocal removed_pid
        removed_pid = True

    def _mock_release_all_scoped_locks(**kwargs):
        nonlocal released_locks
        released_locks = True
        return 0

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 42)
    monkeypatch.setattr("gateway.status.remove_pid_file", _mock_remove_pid_file)
    monkeypatch.setattr(
        "gateway.status.release_all_scoped_locks",
        _mock_release_all_scoped_locks,
    )
    monkeypatch.setattr(
        "gateway.status.terminate_pid",
        lambda pid, force=False, **kwargs: calls.append((pid, force)),
    )
    # Ownership guard (#89315): legitimate same-home replace fixture — the
    # persisted record is bound to target pid 42 in this home.
    monkeypatch.setattr(
        "gateway.status._read_pid_record",
        lambda path=None: {
            "pid": 42,
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 0,
            "hermes_home": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        "gateway.status._get_process_start_time", lambda pid: 0 if pid == 42 else None
    )
    # _pid_exists never goes False — the force-kill did not take.
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 100)
    monkeypatch.setattr("gateway.run.os.kill", lambda pid, sig: None)
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _RunnerShouldNotStart)

    from gateway.run import start_gateway

    ok = await start_gateway(config=GatewayConfig(), replace=True, verbosity=None)

    assert ok is False
    assert calls == [(42, False), (42, True)]
    assert removed_pid is False
    assert released_locks is False


@pytest.mark.asyncio
async def test_start_gateway_replace_writes_takeover_marker_before_sigterm(
    monkeypatch, tmp_path
):
    """--replace must write a takeover marker BEFORE sending SIGTERM.

    The marker lets the target's shutdown handler identify the signal as a
    planned takeover (→ exit 0) rather than an unexpected kill (→ exit 1).
    Without the marker, PR #5646's signal-recovery path would revive the
    target via systemd Restart=on-failure, starting a flap loop.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Record the ORDER of marker-write + terminate_pid calls
    events: list[str] = []
    marker_paths_seen: list = []

    def record_write_marker(target_pid: int) -> bool:
        events.append(f"write_marker(target_pid={target_pid})")
        # Also check that the marker file actually exists after this call
        marker_paths_seen.append(
            (tmp_path / ".gateway-takeover.json").exists() is False  # not yet
        )
        # Actually write the marker so we can verify cleanup later
        from gateway.status import _get_takeover_marker_path, _write_json_file
        _write_json_file(_get_takeover_marker_path(), {
            "target_pid": target_pid,
            "target_start_time": 0,
            "replacer_pid": 100,
            "written_at": "2026-04-17T00:00:00+00:00",
        })
        return True

    def record_terminate(pid, force=False, **kwargs):
        events.append(f"terminate_pid(pid={pid}, force={force})")

    class _CleanExitRunner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = True
            self.exit_reason = None
            self.exit_code = None
            self.adapters = {}

        async def start(self):
            return True

        async def stop(self):
            return None

    _pid_state = {"alive": True}
    def _mock_get_running_pid():
        return 42 if _pid_state["alive"] else None
    def _mock_remove_pid_file():
        _pid_state["alive"] = False
    monkeypatch.setattr("gateway.status.get_running_pid", _mock_get_running_pid)
    monkeypatch.setattr("gateway.status.remove_pid_file", _mock_remove_pid_file)
    # Ownership guard (#89315): this test simulates a legitimate same-home
    # replace, so the persisted pid record must be a valid BOUND record for
    # the target pid in THIS home. start_time 0 matches the legacy fixture's
    # convention; the live probe is patched to agree.
    monkeypatch.setattr(
        "gateway.status._read_pid_record",
        lambda path=None: {
            "pid": 42,
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 0,
            "hermes_home": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        "gateway.status._get_process_start_time", lambda pid: 0 if pid == 42 else None
    )
    monkeypatch.setattr(
        "gateway.status.release_all_scoped_locks",
        lambda **kwargs: 0,
    )
    monkeypatch.setattr("gateway.status.write_takeover_marker", record_write_marker)
    monkeypatch.setattr("gateway.status.terminate_pid", record_terminate)
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 100)
    # Simulate old process exiting on first check so we don't loop into force-kill
    monkeypatch.setattr(
        "gateway.run.os.kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _CleanExitRunner)

    from gateway.run import start_gateway

    ok = await start_gateway(config=GatewayConfig(), replace=True, verbosity=None)

    assert ok is True
    # Ordering: marker written BEFORE SIGTERM
    assert events[0] == "write_marker(target_pid=42)"
    assert any(e.startswith("terminate_pid(pid=42") for e in events[1:])
    # Marker file cleanup: replacer cleans it after loop completes
    assert not (tmp_path / ".gateway-takeover.json").exists()


@pytest.mark.asyncio
async def test_start_gateway_replace_clears_marker_on_permission_denied(
    monkeypatch, tmp_path
):
    """If we fail to kill the existing PID (permission denied), clean up the
    marker so it doesn't grief an unrelated future shutdown."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def write_marker(target_pid: int) -> bool:
        from gateway.status import _get_takeover_marker_path, _write_json_file
        _write_json_file(_get_takeover_marker_path(), {
            "target_pid": target_pid,
            "target_start_time": 0,
            "replacer_pid": 100,
            "written_at": "2026-04-17T00:00:00+00:00",
        })
        return True

    def raise_permission(pid, force=False, **kwargs):
        raise PermissionError("simulated EPERM")

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 42)
    monkeypatch.setattr("gateway.status.write_takeover_marker", write_marker)
    monkeypatch.setattr("gateway.status.terminate_pid", raise_permission)
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 100)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)

    from gateway.run import start_gateway

    # Should return False due to permission error
    ok = await start_gateway(config=GatewayConfig(), replace=True, verbosity=None)

    assert ok is False
    # Marker must NOT be left behind
    assert not (tmp_path / ".gateway-takeover.json").exists()


@pytest.mark.asyncio
async def test_runner_degrades_gracefully_when_all_adapters_missing(monkeypatch, tmp_path, caplog):
    """When all enabled platforms have no adapter (missing library or credentials),
    the gateway should NOT return failure — it should warn and continue running for
    cron job execution, matching the behaviour of 'no platforms enabled' (#5196).

    In fleet deployments the same config.yaml is shared across nodes that may only
    have credentials for a subset of platforms.  Requiring perfect credentials on
    every node makes fleet operation impossible."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.DISCORD: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    # Simulate _create_adapter returning None for ALL platforms (missing library /
    # missing credentials — no connection attempt ever made).
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, cfg: None)

    import logging
    with caplog.at_level(logging.WARNING):
        ok = await runner.start()

    # Must NOT return False — gateway should keep running for cron.
    assert ok is True
    assert runner.should_exit_cleanly is False
    assert runner.adapters == {}
    # Runtime state must remain "running", not "startup_failed".
    state = read_runtime_status()
    assert state["gateway_state"] == "running"
    # A warning must be emitted explaining why no platforms connected.
    assert any(
        "No adapter could be created" in record.message
        for record in caplog.records
    ), "Expected degraded-mode warning when all adapters are missing"


class _NonRetryableFailureAdapter(BasePlatformAdapter):
    """Simulates a fatal config error like token collision."""
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._set_fatal_error(
            "discord-bot-token_lock",
            "Discord bot token already in use (PID 999). Stop the other gateway first.",
            retryable=False,
        )
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_runner_exits_with_ex_config_on_nonretryable_startup_error(monkeypatch, tmp_path):
    """Non-retryable startup errors (token collision, no platforms) must
    set exit_code to 78 (EX_CONFIG) so the s6 finish script can translate
    it to exit 125 (permanent failure).  See #51228."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(enabled=True, token="***")
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    monkeypatch.setattr(runner, "_create_adapter", lambda platform, platform_config: _NonRetryableFailureAdapter())

    ok = await runner.start()

    assert ok is True  # start() returns True (clean exit requested)
    assert runner.should_exit_cleanly is True
    assert runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE
    state = read_runtime_status()
    assert state["gateway_state"] == "startup_failed"


@pytest.mark.asyncio
async def test_start_gateway_propagates_fatal_config_exit_code(monkeypatch, tmp_path):
    """A clean exit carrying GATEWAY_FATAL_CONFIG_EXIT_CODE must surface as a
    process-level SystemExit(78) — NOT a truthy return — so main() exits 78
    and the s6 finish script can translate it to 125 (no restart).

    This guards the propagation gap: runner.start() stamps exit_code=78 and
    requests a clean exit, but start_gateway()'s clean-exit branch used to
    `return True` before the SystemExit(exit_code) site, so main() exited 0
    and s6 crash-looped anyway (#51228)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _FatalConfigRunner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = True
            self.exit_reason = "discord: Discord bot token already in use"
            self.exit_code = GATEWAY_FATAL_CONFIG_EXIT_CODE
            self.adapters = {}

        async def start(self):
            return True

        async def stop(self):
            return None

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _FatalConfigRunner)

    from gateway.run import start_gateway

    with pytest.raises(SystemExit) as exc_info:
        await start_gateway(config=GatewayConfig(), replace=False, verbosity=0)

    assert exc_info.value.code == GATEWAY_FATAL_CONFIG_EXIT_CODE


class _ForeignTokenLockAdapter(BasePlatformAdapter):
    """Connects exactly like telegram/discord do: production
    ``_acquire_platform_lock`` first, which emits ``{scope}_lock`` with
    ``retryable=True`` (so a mid-run reconnect can recover, #54167)."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return self._acquire_platform_lock(
            "telegram-bot-token", self.config.token, "Telegram bot token"
        )

    async def disconnect(self) -> None:
        self._release_platform_lock()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_live_foreign_token_lock_at_startup_exits_ex_config(monkeypatch, tmp_path):
    """Salvage of #83183 claim 1: a LIVE foreign holder of the bot token at
    zero-connected startup is a single-writer conflict, not a transient blip.

    ``_acquire_platform_lock`` deliberately emits the conflict retryable so a
    *mid-run* reconnect can recover once the holder exits.  The startup router
    used to key solely off that flag, so the gateway stayed alive, deaf, and
    retry-queued forever instead of exiting 78 (EX_CONFIG)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    # A live foreign holder: acquire_scoped_lock reports (False, record).
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (
            False,
            {"pid": 424242, "start_time": 1, "hermes_home": "/other/home", "profile": "other"},
        ),
    )
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    monkeypatch.setattr(
        runner, "_create_adapter", lambda platform, platform_config: _ForeignTokenLockAdapter()
    )

    ok = await runner.start()

    assert ok is True
    assert runner.should_exit_cleanly is True
    assert runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE
    assert runner._failed_platforms == {}
    state = read_runtime_status()
    assert state["gateway_state"] == "startup_failed"
    assert state["platforms"]["telegram"]["state"] == "fatal"
    assert state["platforms"]["telegram"]["error_code"] == "telegram-bot-token_lock"


@pytest.mark.asyncio
async def test_token_lock_plus_retryable_peer_stays_alive(monkeypatch, tmp_path):
    """A lock conflict alongside a genuinely transient peer failure is the
    NS-609 mixed mode: the lock is parked fatal, the peer keeps its retry, and
    the gateway stays alive (no exit 78)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (False, {"pid": 424242, "start_time": 1}),
    )
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.DISCORD: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    class _DiscordBlip(_RetryableFailureAdapter):
        def __init__(self):
            BasePlatformAdapter.__init__(
                self, PlatformConfig(enabled=True, token="***"), Platform.DISCORD
            )

    monkeypatch.setattr(
        runner,
        "_create_adapter",
        lambda platform, cfg: (
            _ForeignTokenLockAdapter() if platform is Platform.TELEGRAM else _DiscordBlip()
        ),
    )

    ok = await runner.start()
    try:
        assert ok is True
        assert runner.should_exit_cleanly is False
        assert runner.exit_code is None
        assert set(runner._failed_platforms) == {Platform.DISCORD}
        state = read_runtime_status()
        assert state["gateway_state"] == "running"
        assert state["platforms"]["telegram"]["state"] == "fatal"
        assert state["platforms"]["discord"]["state"] == "retrying"
    finally:
        await runner.stop()


class _MissingCredentialAdapter(BasePlatformAdapter):
    """An adapter whose bot token never reached the environment."""
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token=""), Platform.DISCORD)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._set_fatal_error("missing_credentials", "No bot token configured", retryable=False)
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


async def _run_startup(monkeypatch, tmp_path, adapter_factory, *, secrets_degraded: bool):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agent.secret_sources.registry.last_apply_had_transient_failure",
        lambda *_a, **_k: secrets_degraded,
    )
    config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="")},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    monkeypatch.setattr(runner, "_create_adapter", lambda p, pc: adapter_factory())
    await runner.start()
    return runner


@pytest.mark.asyncio
async def test_transient_secret_failure_does_not_claim_a_fatal_config_fault(monkeypatch, tmp_path):
    """A secrets backend that timed out must not be reported as broken configuration.

    The fetch budget drops the WHOLE source, so its credentials never reach the
    environment and the adapter marks itself non-retryable. Exiting with the
    fatal-config code tells systemd (RestartPreventExitStatus) to keep the gateway down
    permanently over a slow backend, when only a restart can refetch the token.
    """
    runner = await _run_startup(
        monkeypatch, tmp_path, _MissingCredentialAdapter, secrets_degraded=True
    )

    assert runner.should_exit_cleanly is True
    assert runner.exit_code != GATEWAY_FATAL_CONFIG_EXIT_CODE, (
        "a transient secrets failure must stay restartable under every supervisor"
    )
    assert runner.exit_code != 0
    assert read_runtime_status()["gateway_state"] == "startup_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_factory, secrets_degraded, why",
    [
        (_MissingCredentialAdapter, False, "secrets healthy: an absent token IS misconfiguration"),
        (_NonRetryableFailureAdapter, True, "an ownership conflict is not fixed by restarting"),
    ],
)
async def test_fatal_config_exit_is_preserved(monkeypatch, tmp_path, adapter_factory,
                                              secrets_degraded, why):
    """The transient-secrets escape hatch must not swallow genuine fatal conflicts.

    The second case is the one that sank the previous upstream attempt at this change:
    a live foreign token holder stayed fatal only because the classifier refused to
    generalise from 'credentials missing' to 'any non-retryable failure'.
    """
    runner = await _run_startup(
        monkeypatch, tmp_path, adapter_factory, secrets_degraded=secrets_degraded
    )

    assert runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE, why


@pytest.mark.parametrize(
    "code, expected",
    [
        ("missing_credentials", True),
        ("MISSING_CREDENTIALS", True),          # Teams, Photon
        ("yuanbao_missing_credentials", True),  # per-platform prefix
        ("missing_bot_token", True),
        ("discord-bot-token_lock", False),      # ownership conflict, must stay fatal
        ("missing_dependency", False),          # a real config fault, not a lost secret
        ("", False),
    ],
)
def test_missing_credential_codes_are_matched_as_a_family(code, expected):
    """Adapters spell the absent-credential code differently.

    An exact literal covered only two of them, and a second platform with a different
    spelling silently removed coverage the first one would have had alone.
    """
    assert GatewayRunner._is_missing_credential_code(code) is expected


@pytest.mark.asyncio
async def test_two_platforms_missing_credentials_keep_coverage(monkeypatch, tmp_path):
    """Differently-spelled missing-credential codes must not cancel each other out."""
    runner = await _run_startup(
        monkeypatch, tmp_path, _MissingCredentialAdapter, secrets_degraded=True
    )
    runner._startup_nonretryable_codes = {"missing_credentials", "MISSING_CREDENTIALS"}
    assert runner._missing_credentials_blamed_on_secrets() is True


def test_transient_failure_signal_is_owned_by_a_home(tmp_path):
    """A multiplexing gateway hydrates each secondary profile through apply_all during
    startup, so a bare global would be answered by whichever profile hydrated last."""
    from agent.secret_sources import registry

    registry._reset_registry_for_tests()
    primary, secondary = tmp_path / "primary", tmp_path / "secondary"
    from hermes_constants import hermes_home_key

    registry._TRANSIENT_FAILURE_HOMES[hermes_home_key(primary)] = True
    registry._TRANSIENT_FAILURE_HOMES[hermes_home_key(secondary)] = False

    assert registry.last_apply_had_transient_failure(primary) is True
    assert registry.last_apply_had_transient_failure(secondary) is False
    registry._reset_registry_for_tests()


@pytest.mark.asyncio
async def test_repeated_transient_failures_eventually_park(monkeypatch, tmp_path):
    """A permanently dead secrets backend must stop looping and park visibly.

    The generated systemd unit disables the generic start limiter and leans on the
    fatal-config exit as its only backstop, so an unbounded restartable exit would
    restart every RestartSec forever with no parked state for an operator to find.
    """
    from gateway.run_startup import _TRANSIENT_EXIT_STREAK_LIMIT

    codes = []
    for _ in range(_TRANSIENT_EXIT_STREAK_LIMIT):
        runner = await _run_startup(
            monkeypatch, tmp_path, _MissingCredentialAdapter, secrets_degraded=True
        )
        codes.append(runner.exit_code)

    assert all(c != GATEWAY_FATAL_CONFIG_EXIT_CODE for c in codes[:-1]), (
        "early attempts must stay restartable so a brief outage self-heals"
    )
    assert codes[-1] == GATEWAY_FATAL_CONFIG_EXIT_CODE, (
        "a backend that never comes back must park instead of looping"
    )


@pytest.mark.asyncio
async def test_a_successful_connect_forgives_the_streak(monkeypatch, tmp_path):
    """The budget is for CONSECUTIVE failures; recovering must reset it."""
    from gateway.run_startup import _TRANSIENT_EXIT_STREAK_LIMIT

    for _ in range(_TRANSIENT_EXIT_STREAK_LIMIT - 1):
        await _run_startup(monkeypatch, tmp_path, _MissingCredentialAdapter, secrets_degraded=True)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    GatewayRunner(GatewayConfig(platforms={}, sessions_dir=tmp_path / "s"))._reset_transient_exit_streak()

    runner = await _run_startup(
        monkeypatch, tmp_path, _MissingCredentialAdapter, secrets_degraded=True
    )
    assert runner.exit_code != GATEWAY_FATAL_CONFIG_EXIT_CODE
