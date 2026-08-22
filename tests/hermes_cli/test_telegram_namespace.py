from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _parser() -> argparse.ArgumentParser:
    from hermes_cli.telegram import register_cli

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    register_cli(subparsers)
    return parser


def test_telegram_namespace_registers_the_approved_command_tree():
    parser = _parser()

    cases = {
        ("telegram", "setup"),
        ("telegram", "status"),
        ("telegram", "doctor"),
        ("telegram", "user", "login"),
        ("telegram", "user", "status"),
        ("telegram", "user", "doctor"),
        ("telegram", "user", "logout"),
        ("telegram", "user", "capabilities"),
        ("telegram", "user", "peers"),
        ("telegram", "topic-icons", "status"),
        ("telegram", "topic-icons", "catalog"),
        ("telegram", "topic-icons", "resolve", "🧹"),
        ("telegram", "topic-icons", "verify", "--topic", "1"),
        (
            "telegram",
            "topic-icons",
            "set",
            "--topic",
            "1",
            "--emoji",
            "🧹",
            "--yes",
        ),
    }

    for argv in cases:
        args = parser.parse_args(argv)
        assert callable(args.func), argv


def test_local_telegram_status_is_json_and_does_not_build_live_services(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    from hermes_cli import telegram

    def forbidden():
        raise AssertionError("local status must not build a live Telegram service")

    monkeypatch.setattr(telegram, "build_live_service", forbidden)
    args = _parser().parse_args(("telegram", "status", "--json"))

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["live"] is False
    assert payload["user_transport"]["enabled"] is False
    assert payload["topic_icons"]["provider"] == "telegram_default"
    assert payload["topic_icons"]["fallback"] == "none"


def test_live_cli_registers_telegram_as_a_builtin_with_completion(tmp_path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "profile")
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "telegram", "--help"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "topic-icons" in result.stdout
    assert "user" in result.stdout

    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "telegram" in _BUILTIN_SUBCOMMANDS


def test_status_reads_canonical_gateway_platform_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
gateway:
  platforms:
    telegram:
      enabled: true
      token: ${TELEGRAM_BOT_TOKEN}
      extra:
        user_transport:
          enabled: true
          expected_user_id: 101
          capabilities: [topic.read, topic.icon.write]
          allowed_bot_peer_ids: [202]
        topic_icon_provider: telegram_custom_packs
        topic_icon_custom_packs: [AppleObjectsHomeTools]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")

    args = _parser().parse_args(("telegram", "status", "--json"))
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bot_api"] == {"enabled": True, "configured": True}
    assert payload["user_transport"]["expected_user_id"] == 101
    assert payload["topic_icons"]["configured_packs"] == ["AppleObjectsHomeTools"]


def test_live_status_and_doctor_merge_bounded_service_results(monkeypatch, capsys):
    from hermes_cli import telegram

    class Service:
        async def status(self, *, live=True):
            assert live is True
            return {
                "schema_version": 1,
                "live": True,
                "provider": "telegram_custom_packs",
                "configured_packs": ["AppleObjectsHomeTools"],
                "total_count": 122,
                "user_id": 101,
                "premium": True,
            }

    monkeypatch.setattr(telegram, "build_live_service", lambda: Service())
    monkeypatch.setattr(
        telegram,
        "_local_status",
        lambda: {
            "schema_version": 1,
            "live": False,
            "bot_api": {"enabled": True, "configured": True},
            "user_transport": {"enabled": True},
            "topic_icons": {"provider": "telegram_custom_packs"},
        },
    )

    status = _parser().parse_args(("telegram", "status", "--live", "--json"))
    assert status.func(status) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["user_transport"]["user_id"] == 101
    assert payload["topic_icons"]["total_count"] == 122

    monkeypatch.setattr(
        "hermes_cli.telegram_user.local_diagnostics",
        lambda _payload=None: [{"name": "local", "ok": True}],
    )
    doctor = _parser().parse_args(("telegram", "doctor", "--live", "--json"))
    assert doctor.func(doctor) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} == {"local", "live_ready"}


def test_setup_rejects_custom_icons_without_explicit_user_transport_and_preserves_config(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    home.mkdir()
    original = """
gateway:
  platforms:
    telegram:
      extra:
        user_transport:
          enabled: true
          expected_user_id: 101
          capabilities: [topic.read, topic.icon.write]
          allowed_bot_peer_ids: [202]
"""
    path = home / "config.yaml"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    args = _parser().parse_args(
        (
            "telegram",
            "setup",
            "--non-interactive",
            "--enable-custom-icons",
            "--pack",
            "AppleObjectsHomeTools",
        )
    )
    assert args.func(args) == 2
    assert path.read_text(encoding="utf-8") == original


def test_setup_writes_only_scoped_gateway_telegram_config(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: keep-me\nunrelated:\n  nested: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    args = _parser().parse_args(
        (
            "telegram",
            "setup",
            "--non-interactive",
            "--enable-user-transport",
            "--expected-user-id",
            "101",
            "--bot-peer-id",
            "202",
            "--enable-custom-icons",
            "--pack",
            "AppleObjectsHomeTools",
        )
    )
    assert args.func(args) == 0

    from hermes_cli.config import read_user_config_raw

    raw = read_user_config_raw(home / "config.yaml")
    assert raw["model"]["default"] == "keep-me"
    assert raw["unrelated"] == {"nested": True}
    extra = raw["gateway"]["platforms"]["telegram"]["extra"]
    assert extra["user_transport"]["expected_user_id"] == 101
    assert extra["user_transport"]["allowed_bot_peer_ids"] == [202]
    assert extra["topic_icon_provider"] == "telegram_custom_packs"
    assert extra["topic_icon_custom_packs"] == ["AppleObjectsHomeTools"]
