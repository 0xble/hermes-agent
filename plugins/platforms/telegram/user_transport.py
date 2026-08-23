"""Narrow policy and types for Telegram user-authorized topic operations."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


IMPLEMENTED_CAPABILITIES = frozenset({"topic.read", "topic.icon.write"})
UNSET_EXPECTED_ICON = object()
_MAX_SIGNED_LONG = 2**63 - 1


class TelegramUserTransportError(RuntimeError):
    """Sanitized failure at the user-transport boundary."""


class TelegramTopicReadbackMismatch(TelegramUserTransportError):
    """The exact topic readback did not match the requested mutation."""


@dataclass(frozen=True)
class TelegramUserIdentity:
    user_id: int
    username: str | None
    is_premium: bool


@dataclass(frozen=True)
class TopicSnapshot:
    peer_id: int
    topic_id: int
    title: str
    icon_emoji_id: int | None
    closed: bool
    hidden: bool


@dataclass(frozen=True)
class VerifiedTopicMutation:
    peer_id: int
    topic_id: int
    requested_icon_emoji_id: int
    observed_icon_emoji_id: int
    verified_at: float


class TelegramUserTransport(Protocol):
    async def identity(self) -> TelegramUserIdentity: ...

    async def get_topic(self, *, peer_id: int, topic_id: int) -> TopicSnapshot: ...

    async def set_topic_icon(
        self,
        *,
        peer_id: int,
        topic_id: int,
        icon_emoji_id: int,
        expected_icon_emoji_id: int | None | object = UNSET_EXPECTED_ICON,
    ) -> VerifiedTopicMutation: ...


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TelegramUserTransportError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class TelegramUserTransportConfig:
    enabled: bool = False
    implementation: str = "telethon"
    expected_user_id: int | None = None
    require_premium: bool = True
    capabilities: frozenset[str] = frozenset()
    allowed_bot_peer_ids: frozenset[int] = frozenset()
    connect_timeout_seconds: float = 15.0
    rpc_timeout_seconds: float = 20.0
    max_flood_wait_seconds: float = 5.0
    failure_cooldown_seconds: float = 300.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TelegramUserTransportConfig":
        data = dict(raw or {})
        enabled = data.get("enabled") is True
        implementation = str(data.get("implementation") or "telethon").strip()
        if implementation != "telethon":
            raise TelegramUserTransportError(
                "user transport implementation must be 'telethon'"
            )

        capabilities_raw = data.get("capabilities") or []
        if not isinstance(capabilities_raw, list):
            raise TelegramUserTransportError("capabilities must be a list")
        capabilities = frozenset(str(value).strip() for value in capabilities_raw)
        unknown = capabilities - IMPLEMENTED_CAPABILITIES
        if unknown:
            raise TelegramUserTransportError(
                f"Unknown capability: {sorted(unknown)[0]}"
            )

        peers_raw = data.get("allowed_bot_peer_ids") or []
        if not isinstance(peers_raw, list):
            raise TelegramUserTransportError("allowed bot peers must be a list")
        if any(value == "*" for value in peers_raw):
            raise TelegramUserTransportError("peer wildcard is not allowed")
        peers = frozenset(
            _positive_int(value, "allowed bot peer ID") for value in peers_raw
        )

        expected = data.get("expected_user_id")
        if expected is not None:
            expected = _positive_int(expected, "expected user ID")
        if enabled:
            if expected is None:
                raise TelegramUserTransportError(
                    "expected_user_id is required when user transport is enabled"
                )
            if not capabilities:
                raise TelegramUserTransportError(
                    "at least one implemented capability is required"
                )
            if not peers:
                raise TelegramUserTransportError(
                    "at least one allowed bot peer ID is required"
                )

        def bounded_float(name: str, default: float, minimum: float = 0.1) -> float:
            try:
                value = float(data.get(name, default))
            except (TypeError, ValueError) as exc:
                raise TelegramUserTransportError(f"{name} must be numeric") from exc
            if value < minimum or value > 3600:
                raise TelegramUserTransportError(f"{name} is outside safe bounds")
            return value

        return cls(
            enabled=enabled,
            implementation=implementation,
            expected_user_id=expected,
            require_premium=data.get("require_premium", True) is not False,
            capabilities=capabilities,
            allowed_bot_peer_ids=peers,
            connect_timeout_seconds=bounded_float("connect_timeout_seconds", 15.0),
            rpc_timeout_seconds=bounded_float("rpc_timeout_seconds", 20.0),
            max_flood_wait_seconds=bounded_float("max_flood_wait_seconds", 5.0, 0.0),
            failure_cooldown_seconds=bounded_float("failure_cooldown_seconds", 300.0),
        )


class TelegramUserPolicy:
    """Fail-closed authorization for the two V1 topic capabilities."""

    def __init__(
        self,
        config: TelegramUserTransportConfig,
        *,
        active_bot_peer_id: int,
    ) -> None:
        self.config = config
        self.active_bot_peer_id = active_bot_peer_id

    def require(
        self,
        capability: str,
        *,
        peer_id: int,
        topic_id: int,
        custom_emoji_id: int | None = None,
    ) -> None:
        if not self.config.enabled:
            raise TelegramUserTransportError("user transport is disabled")
        if capability not in IMPLEMENTED_CAPABILITIES:
            raise TelegramUserTransportError("capability is not implemented")
        if capability not in self.config.capabilities:
            raise TelegramUserTransportError("capability is not configured")
        peer_id = _positive_int(peer_id, "peer ID")
        if peer_id not in self.config.allowed_bot_peer_ids:
            raise TelegramUserTransportError("peer is not allowed")
        if peer_id != self.active_bot_peer_id:
            raise TelegramUserTransportError(
                "peer does not match the active Bot API identity"
            )
        _positive_int(topic_id, "topic ID")
        if capability == "topic.icon.write":
            icon_id = _positive_int(custom_emoji_id, "custom emoji ID")
            if icon_id > _MAX_SIGNED_LONG:
                raise TelegramUserTransportError(
                    "custom emoji ID must fit a positive signed 64-bit integer"
                )


def telegram_user_state_dir(hermes_home: Path) -> Path:
    return Path(hermes_home) / "state" / "telegram-user"


def telegram_user_session_path(hermes_home: Path) -> Path:
    return telegram_user_state_dir(hermes_home) / "telethon.session"


def telegram_user_lock_path(hermes_home: Path) -> Path:
    return telegram_user_state_dir(hermes_home) / "session.lock"


def _require_owned(path: Path) -> None:
    getuid = getattr(os, "getuid", None)
    if os.name == "nt" or getuid is None:
        return
    if path.stat(follow_symlinks=False).st_uid != getuid():
        raise TelegramUserTransportError("Telegram user session path has unsafe ownership")


def _validate_session_storage(hermes_home: Path, *, create: bool) -> Path:
    """Validate fixed profile-local session paths without following symlinks."""
    home = Path(hermes_home)
    state_dir = home / "state"
    root = telegram_user_state_dir(home)
    session = telegram_user_session_path(home)
    for candidate in (home, state_dir, root):
        if candidate.exists() and candidate.is_symlink():
            raise TelegramUserTransportError(
                "Telegram user session ancestors must not be symlinks"
            )
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not root.exists():
        return session
    if not root.is_dir():
        raise TelegramUserTransportError("Telegram user session directory is not a directory")
    resolved_home = home.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_relative_to(resolved_home):
        raise TelegramUserTransportError(
            "Telegram user session path escapes the active profile"
        )
    _require_owned(root)
    if os.name != "nt":
        mode = stat.S_IMODE(root.stat(follow_symlinks=False).st_mode)
        if mode != 0o700:
            if not create:
                raise TelegramUserTransportError(
                    "Telegram user session directory mode must be 0700"
                )
            root.chmod(0o700)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{session}{suffix}")
        if candidate.is_symlink():
            raise TelegramUserTransportError(
                "Telegram user session path must not be a symlink"
            )
        if not candidate.exists():
            continue
        info = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise TelegramUserTransportError(
                "Telegram user session is not a regular file"
            )
        _require_owned(candidate)
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise TelegramUserTransportError(
                "Telegram user session and sidecar modes must be 0600"
            )
    return session


def inspect_safe_session_storage(hermes_home: Path) -> Path:
    """Read-only validation used by status and doctor."""
    return _validate_session_storage(hermes_home, create=False)


def ensure_safe_session_storage(hermes_home: Path) -> Path:
    """Create and validate the fixed profile-local Telethon session path."""
    return _validate_session_storage(hermes_home, create=True)
