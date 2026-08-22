from __future__ import annotations

import json
import os

import pytest

from plugins.platforms.telegram.mtproto_telethon import (
    _render_login_qr,
    authorize_attended,
    logout_attended,
)
from plugins.platforms.telegram.user_transport import (
    TelegramUserTransportConfig,
    TelegramUserTransportError,
    telegram_user_session_path,
)
from tests.hermes_cli.test_telegram_namespace import _parser


class _PasswordNeeded(Exception):
    pass


class _Qr:
    url = "tg://login?token=secret-token"

    async def wait(self, timeout):
        raise _PasswordNeeded()


class _Client:
    def __init__(self, session_path):
        self.session_path = session_path
        self.passwords = []
        self.disconnected = False

    async def connect(self):
        telegram_user_session_path(self.session_path.parents[2]).touch(mode=0o600)

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return False

    async def qr_login(self):
        return _Qr()

    async def sign_in(self, *, password):
        self.passwords.append(password)

    async def get_me(self):
        return type("Me", (), {"id": 101, "username": "safe", "premium": True})()


def _config(expected=101):
    return TelegramUserTransportConfig.from_mapping(
        {
            "enabled": True,
            "expected_user_id": expected,
            "capabilities": ["topic.read"],
            "allowed_bot_peer_ids": [202],
        }
    )


@pytest.mark.asyncio
async def test_attended_login_renders_qr_and_prompts_for_2fa_locally_hidden(tmp_path):
    rendered = []
    prompts = []
    client_box = []

    def factory(session, *_args, **kwargs):
        assert kwargs["receive_updates"] is False
        client = _Client(telegram_user_session_path(tmp_path))
        client_box.append(client)
        return client

    identity = await authorize_attended(
        config=_config(),
        api_id=123,
        api_hash="api-secret",
        hermes_home=tmp_path,
        client_factory=factory,
        password_needed_exception=_PasswordNeeded,
        qr_renderer=lambda value: rendered.append(value),
        password_prompt=lambda prompt: prompts.append(prompt) or "2fa-secret",
    )

    assert identity.user_id == 101
    assert rendered == ["tg://login?token=secret-token"]
    assert prompts == ["Telegram cloud 2FA password: "]
    assert client_box[0].passwords == ["2fa-secret"]
    assert client_box[0].disconnected is True


@pytest.mark.asyncio
async def test_attended_login_removes_temporary_qr_artifact(tmp_path):
    artifact = tmp_path / "login-qr.png"

    def renderer(_value):
        artifact.write_bytes(b"temporary login token")
        return artifact

    await authorize_attended(
        config=_config(),
        api_id=123,
        api_hash="api-secret",
        hermes_home=tmp_path,
        client_factory=lambda *_args, **_kwargs: _Client(
            telegram_user_session_path(tmp_path)
        ),
        password_needed_exception=_PasswordNeeded,
        qr_renderer=renderer,
        password_prompt=lambda _prompt: "2fa-secret",
    )

    assert not artifact.exists()


@pytest.mark.asyncio
async def test_attended_login_removes_new_session_on_identity_mismatch(
    tmp_path, monkeypatch
):
    client = _Client(telegram_user_session_path(tmp_path))
    cleanup_events = []

    def remove_after_disconnect(path):
        assert client.disconnected is True
        cleanup_events.append(path)
        path.unlink(missing_ok=True)

    monkeypatch.setattr(
        "plugins.platforms.telegram.mtproto_telethon._remove_incomplete_session",
        remove_after_disconnect,
    )
    with pytest.raises(TelegramUserTransportError, match="identity mismatch"):
        await authorize_attended(
            config=_config(expected=999),
            api_id=123,
            api_hash="api-secret",
            hermes_home=tmp_path,
            client_factory=lambda *_args, **_kwargs: client,
            password_needed_exception=_PasswordNeeded,
            qr_renderer=lambda _value: None,
            password_prompt=lambda _prompt: "2fa-secret",
        )

    assert not telegram_user_session_path(tmp_path).exists()
    assert cleanup_events == [telegram_user_session_path(tmp_path)]


def test_user_capabilities_and_peers_are_local_bounded_json(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
platforms:
  telegram:
    extra:
      user_transport:
        enabled: true
        expected_user_id: 101
        capabilities: [topic.read]
        allowed_bot_peer_ids: [202]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    args = _parser().parse_args(("telegram", "user", "capabilities", "--json"))
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] == ["topic.read"]
    assert payload["implemented"] == ["topic.icon.write", "topic.read"]

    args = _parser().parse_args(("telegram", "user", "peers", "--json"))
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["peers"] == [{"peer_id": 202, "verified": None}]


@pytest.mark.asyncio
async def test_logout_verifies_revocation_then_removes_session(tmp_path):
    session = telegram_user_session_path(tmp_path)
    session.parent.mkdir(parents=True)
    session.touch(mode=0o600)

    class Client(_Client):
        authorized = True

        async def is_user_authorized(self):
            return self.authorized

        async def log_out(self):
            self.authorized = False
            return True

    client = Client(session)
    result = await logout_attended(
        config=_config(),
        api_id=123,
        api_hash="api-secret",
        hermes_home=tmp_path,
        client_factory=lambda *_args, **_kwargs: client,
    )

    assert result["authorized"] is False
    assert result["removed"] is True
    assert not session.exists()


@pytest.mark.asyncio
async def test_logout_remains_available_when_transport_is_disabled(tmp_path):
    session = telegram_user_session_path(tmp_path)
    session.parent.mkdir(parents=True)
    session.touch(mode=0o600)
    dangling = session.with_name(f"{session.name}-wal")
    dangling.symlink_to(tmp_path / "missing-wal-target")

    class Client:
        authorized = True

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def get_me(self):
            return type("Me", (), {"id": 101})()

        async def is_user_authorized(self):
            return self.authorized

        async def log_out(self):
            self.authorized = False
            return True

    result = await logout_attended(
        config=TelegramUserTransportConfig.from_mapping({"enabled": False}),
        api_id=123,
        api_hash="api-secret",
        hermes_home=tmp_path,
        client_factory=lambda *_args, **_kwargs: Client(),
    )
    assert result == {"schema_version": 1, "authorized": False, "removed": True}
    assert not os.path.lexists(dangling)


def test_qr_renderer_rejects_preexisting_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.platforms.telegram.mtproto_telethon.sys.platform", "darwin")
    target = tmp_path / "outside.txt"
    target.write_text("do not truncate", encoding="utf-8")
    (tmp_path / "login-qr.png").symlink_to(target)

    with pytest.raises(TelegramUserTransportError, match="QR path"):
        _render_login_qr("tg://login?token=synthetic", output_dir=tmp_path)
    assert target.read_text(encoding="utf-8") == "do not truncate"


def test_qr_renderer_removes_partial_artifact_when_preview_launch_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("plugins.platforms.telegram.mtproto_telethon.sys.platform", "darwin")

    def fail_open(*_args, **_kwargs):
        raise OSError("synthetic Preview failure")

    monkeypatch.setattr(
        "plugins.platforms.telegram.mtproto_telethon.subprocess.run", fail_open
    )
    with pytest.raises(TelegramUserTransportError, match="rendered safely"):
        _render_login_qr("tg://login?token=synthetic", output_dir=tmp_path)
    assert not (tmp_path / "login-qr.png").exists()


def test_local_doctor_reports_unsafe_session_storage_without_creating_files(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "state").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.telegram_user import local_diagnostics

    checks = {check["name"]: check for check in local_diagnostics()}
    assert checks["session_storage_safe"]["ok"] is False
    assert not (outside / "telegram-user").exists()


def test_global_doctor_aggregates_telegram_user_transport_checks(monkeypatch):
    from hermes_cli import doctor, telegram, telegram_user

    monkeypatch.setattr(
        telegram,
        "_local_status",
        lambda: {"user_transport": {"enabled": True}},
    )
    monkeypatch.setattr(
        telegram_user,
        "local_diagnostics",
        lambda _status=None: [
            {"name": "session_storage_safe", "ok": False, "detail": "unsafe"}
        ],
    )
    issues = []
    doctor._check_telegram_user_transport(issues)
    assert issues == ["Telegram user transport session_storage_safe: unsafe"]


def test_configured_lifecycle_keeps_explicit_timeout_bounds(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
gateway:
  platforms:
    telegram:
      extra:
        user_transport:
          enabled: true
          expected_user_id: 101
          capabilities: [topic.read]
          allowed_bot_peer_ids: [202]
          connect_timeout_seconds: 7
          rpc_timeout_seconds: 8
          max_flood_wait_seconds: 2
          failure_cooldown_seconds: 9
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.telegram_user import _configured

    config = _configured()
    assert config.connect_timeout_seconds == 7
    assert config.rpc_timeout_seconds == 8
    assert config.max_flood_wait_seconds == 2
    assert config.failure_cooldown_seconds == 9
