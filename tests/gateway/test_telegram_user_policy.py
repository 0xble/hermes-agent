from __future__ import annotations

import os
from pathlib import Path

import pytest

from plugins.platforms.telegram.user_transport import (
    TelegramUserPolicy,
    TelegramUserTransportConfig,
    TelegramUserTransportError,
    ensure_safe_session_storage,
    telegram_user_session_path,
)


def _config(**overrides) -> TelegramUserTransportConfig:
    values = {
        "enabled": True,
        "implementation": "telethon",
        "expected_user_id": 101,
        "require_premium": True,
        "capabilities": ["topic.read", "topic.icon.write"],
        "allowed_bot_peer_ids": [202],
    }
    values.update(overrides)
    return TelegramUserTransportConfig.from_mapping(values)


def test_policy_allows_only_implemented_capabilities_and_exact_bot_peer():
    policy = TelegramUserPolicy(_config(), active_bot_peer_id=202)

    policy.require("topic.read", peer_id=202, topic_id=1)
    policy.require(
        "topic.icon.write",
        peer_id=202,
        topic_id=1,
        custom_emoji_id=2**63 - 1,
    )

    for capability in ("message.read", "rpc.call", "*"):
        with pytest.raises(TelegramUserTransportError, match="capability"):
            policy.require(capability, peer_id=202, topic_id=1)
    with pytest.raises(TelegramUserTransportError, match="peer"):
        policy.require("topic.read", peer_id=203, topic_id=1)
    with pytest.raises(TelegramUserTransportError, match="active Bot API"):
        TelegramUserPolicy(_config(), active_bot_peer_id=999).require(
            "topic.read", peer_id=202, topic_id=1
        )


@pytest.mark.parametrize("value", [0, -1, "1", None])
def test_policy_rejects_invalid_topic_ids_before_transport(value):
    with pytest.raises(TelegramUserTransportError, match="topic ID"):
        TelegramUserPolicy(_config(), active_bot_peer_id=202).require(
            "topic.read", peer_id=202, topic_id=value
        )


@pytest.mark.parametrize("value", [0, -1, 2**63, "2"])
def test_policy_rejects_invalid_custom_emoji_ids(value):
    with pytest.raises(TelegramUserTransportError, match="custom emoji ID"):
        TelegramUserPolicy(_config(), active_bot_peer_id=202).require(
            "topic.icon.write", peer_id=202, topic_id=1, custom_emoji_id=value
        )


def test_config_rejects_unknown_capabilities_wildcards_and_duplicate_packs():
    with pytest.raises(TelegramUserTransportError, match="Unknown capability"):
        _config(capabilities=["topic.read", "message.read"])
    with pytest.raises(TelegramUserTransportError, match="wildcard"):
        _config(allowed_bot_peer_ids=["*"])


def test_session_path_is_profile_local_and_rejects_symlinks(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_path = telegram_user_session_path(first)
    second_path = telegram_user_session_path(second)

    assert first_path == first / "state" / "telegram-user" / "telethon.session"
    assert second_path != first_path

    ensure_safe_session_storage(first)
    assert first_path.parent.stat().st_mode & 0o777 == 0o700

    target = tmp_path / "outside.session"
    target.write_text("not a session", encoding="utf-8")
    first_path.symlink_to(target)
    with pytest.raises(TelegramUserTransportError, match="symlink"):
        ensure_safe_session_storage(first)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_session_storage_rejects_group_readable_session(tmp_path):
    session = telegram_user_session_path(tmp_path)
    ensure_safe_session_storage(tmp_path)
    session.touch(mode=0o600)
    session.chmod(0o640)

    with pytest.raises(TelegramUserTransportError, match="0600"):
        ensure_safe_session_storage(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_session_storage_rejects_symlinked_state_ancestor(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(TelegramUserTransportError, match="ancestors"):
        ensure_safe_session_storage(home)
    assert not (outside / "telegram-user").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_session_storage_rejects_unsafe_sidecars(tmp_path):
    session = telegram_user_session_path(tmp_path)
    ensure_safe_session_storage(tmp_path)
    sidecar = session.with_name(f"{session.name}-wal")
    sidecar.touch(mode=0o600)
    sidecar.chmod(0o640)

    with pytest.raises(TelegramUserTransportError, match="sidecar modes"):
        ensure_safe_session_storage(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_session_lease_rejects_symlink(tmp_path):
    from plugins.platforms.telegram.mtproto_telethon import _SessionLease
    from plugins.platforms.telegram.user_transport import telegram_user_lock_path

    lock = telegram_user_lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    target = tmp_path / "outside.lock"
    target.write_text("do not modify", encoding="utf-8")
    lock.symlink_to(target)

    with pytest.raises(TelegramUserTransportError, match="lock path"):
        _SessionLease(lock).acquire()
    assert target.read_text(encoding="utf-8") == "do not modify"
