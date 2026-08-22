"""Telethon 1.x adapter for the two authorized Telegram topic operations."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from plugins.platforms.telegram.user_transport import (
    TelegramTopicReadbackMismatch,
    TelegramUserIdentity,
    TelegramUserPolicy,
    TelegramUserTransportConfig,
    TelegramUserTransportError,
    TopicSnapshot,
    UNSET_EXPECTED_ICON,
    VerifiedTopicMutation,
    ensure_safe_session_storage,
    telegram_user_lock_path,
    telegram_user_session_path,
)


@contextlib.contextmanager
def _private_file_creation_umask():
    """Keep newly-created MTProto credential files private from first write."""
    if os.name == "nt":
        yield
        return
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


class _SessionLease:
    """Non-blocking interprocess lease for one profile-local session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise TelegramUserTransportError(
                "Telegram user session lock path must not be a symlink"
            )
        if self.path.exists():
            info = self.path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise TelegramUserTransportError(
                    "Telegram user session lock path must be a regular file"
                )
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise TelegramUserTransportError(
                "Telegram user session lock path is unsafe"
            ) from exc
        handle = os.fdopen(fd, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\n")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise TelegramUserTransportError(
                "Telegram user session is already in use by another process"
            ) from exc
        self.handle = handle

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


class _BoundedFloodWait(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__("bounded Telegram FloodWait")
        self.seconds = seconds


class _AmbiguousNetworkFailure(TelegramUserTransportError):
    """A network failure that may have occurred after Telegram accepted a write."""


def _load_telethon() -> tuple[Callable[..., Any], Any]:
    try:
        from telethon import TelegramClient
        from telethon.tl import functions
    except ImportError:
        try:
            from tools.lazy_deps import ensure

            ensure("platform.telegram-user")
            from telethon import TelegramClient
            from telethon.tl import functions
        except Exception as exc:
            raise TelegramUserTransportError(
                "Telethon is unavailable; install the telegram-user optional extra"
            ) from exc
    return TelegramClient, functions


def _render_login_qr(url: str, *, output_dir: Path | None = None) -> Path | None:
    try:
        import qrcode
    except ImportError as exc:
        raise TelegramUserTransportError(
            "QR rendering is unavailable; install the messaging dependencies"
        ) from exc
    qr = qrcode.QRCode(border=4)
    qr.add_data(url)
    qr.make(fit=True)
    if sys.platform == "darwin" and output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = output_dir / "login-qr.png"
        if os.path.lexists(path):
            if path.is_symlink():
                raise TelegramUserTransportError(
                    "Telegram login QR path must not be a symlink"
                )
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise TelegramUserTransportError(
                    "Telegram login QR path must be a regular file"
                )
            if os.name != "nt" and hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise TelegramUserTransportError(
                    "Telegram login QR path has unsafe ownership"
                )
            path.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise TelegramUserTransportError(
                "Telegram login QR could not be created safely"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                qr.make_image(fill_color="black", back_color="white").save(stream)
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise TelegramUserTransportError(
                    "Telegram login QR is not a regular file"
                )
            opened = subprocess.run(
                ["open", "-a", "Preview", str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, TelegramUserTransportError):
                raise
            raise TelegramUserTransportError(
                "Telegram login QR could not be rendered safely"
            ) from exc
        if opened.returncode == 0:
            print(f"Telegram login QR opened in Preview: {path}")
            return path
        path.unlink(missing_ok=True)
    qr.print_ascii(invert=True)
    return None


def _remove_incomplete_session(session_path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{session_path}{suffix}")
        try:
            if path.exists() and not path.is_symlink() and path.is_file():
                path.unlink()
        except OSError:
            pass


async def authorize_attended(
    *,
    config: TelegramUserTransportConfig,
    api_id: int,
    api_hash: str,
    hermes_home: Path,
    client_factory: Callable[..., Any] | None = None,
    password_needed_exception: type[BaseException] | None = None,
    qr_renderer: Callable[[str], Any] | None = None,
    password_prompt: Callable[[str], str] | None = None,
    qr_timeout_seconds: float = 120.0,
) -> TelegramUserIdentity:
    """Perform attended QR authorization with local hidden cloud-2FA input."""
    if not config.enabled or config.expected_user_id is None:
        raise TelegramUserTransportError(
            "Telegram user transport must be configured before login"
        )
    session_path = ensure_safe_session_storage(Path(hermes_home))
    session_existed = session_path.exists()
    lease = _SessionLease(telegram_user_lock_path(Path(hermes_home)))
    lease.acquire()
    client = None
    qr_artifact: Path | None = None
    remove_incomplete_session = False
    try:
        if client_factory is None or password_needed_exception is None:
            loaded_factory, _ = _load_telethon()
            try:
                from telethon.errors import SessionPasswordNeededError
            except ImportError as exc:
                raise TelegramUserTransportError(
                    "Telethon error types are unavailable"
                ) from exc
            client_factory = client_factory or loaded_factory
            password_needed_exception = (
                password_needed_exception or SessionPasswordNeededError
            )
        with _private_file_creation_umask():
            client = client_factory(
                str(session_path), api_id, api_hash, receive_updates=False
            )
            await asyncio.wait_for(
                client.connect(), timeout=config.connect_timeout_seconds
            )
        ensure_safe_session_storage(Path(hermes_home))
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=config.rpc_timeout_seconds
        )
        if not authorized:
            qr = await asyncio.wait_for(
                client.qr_login(), timeout=config.rpc_timeout_seconds
            )
            # The renderer receives the token locally. Hermes never prints or
            # persists the raw URL itself.
            renderer = qr_renderer or (
                lambda value: _render_login_qr(value, output_dir=session_path.parent)
            )
            rendered = renderer(qr.url)
            qr_artifact = rendered if isinstance(rendered, Path) else None
            try:
                await qr.wait(timeout=qr_timeout_seconds)
            except password_needed_exception:
                if password_prompt is None:
                    from getpass import getpass

                    password_prompt = getpass
                password = password_prompt("Telegram cloud 2FA password: ")
                try:
                    await asyncio.wait_for(
                        client.sign_in(password=password),
                        timeout=config.rpc_timeout_seconds,
                    )
                finally:
                    password = ""
        me = await asyncio.wait_for(
            client.get_me(), timeout=config.rpc_timeout_seconds
        )
        observed_id = getattr(me, "id", None)
        if not isinstance(observed_id, int) or isinstance(observed_id, bool):
            raise TelegramUserTransportError("Telegram user identity is invalid")
        if observed_id != config.expected_user_id:
            raise TelegramUserTransportError("Telegram user identity mismatch")
        premium = bool(getattr(me, "premium", False))
        if config.require_premium and not premium:
            raise TelegramUserTransportError(
                "Telegram user account does not satisfy Premium policy"
            )
        ensure_safe_session_storage(Path(hermes_home))
        return TelegramUserIdentity(
            user_id=observed_id,
            username=(str(getattr(me, "username", "") or "").strip() or None),
            is_premium=premium,
        )
    except Exception:
        if not session_existed:
            remove_incomplete_session = True
        raise
    finally:
        if qr_artifact is not None:
            qr_artifact.unlink(missing_ok=True)
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=min(config.connect_timeout_seconds, 5.0)
                )
            except Exception:
                pass
        if remove_incomplete_session:
            _remove_incomplete_session(session_path)
        lease.release()


async def logout_attended(
    *,
    config: TelegramUserTransportConfig,
    api_id: int,
    api_hash: str,
    hermes_home: Path,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Revoke the exact configured identity and remove residual local state."""
    home = Path(hermes_home)
    raw_session_path = telegram_user_session_path(home)
    # Sidecars are not authorization roots. Remove symlink entries before strict
    # storage validation so emergency logout can clean a tampered profile
    # without following the link target.
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{raw_session_path}{suffix}")
        if sidecar.is_symlink():
            sidecar.unlink()
    session_path = ensure_safe_session_storage(home)
    if not session_path.exists():
        return {"schema_version": 1, "authorized": False, "removed": True}
    if client_factory is None:
        client_factory, _ = _load_telethon()
    lease = _SessionLease(telegram_user_lock_path(home))
    lease.acquire()
    client = None
    try:
        client = client_factory(
            str(session_path), api_id, api_hash, receive_updates=False
        )
        await asyncio.wait_for(
            client.connect(), timeout=config.connect_timeout_seconds
        )
        ensure_safe_session_storage(home)
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=config.rpc_timeout_seconds
        )
        if authorized:
            me = await asyncio.wait_for(
                client.get_me(), timeout=config.rpc_timeout_seconds
            )
            if (
                config.expected_user_id is not None
                and getattr(me, "id", None) != config.expected_user_id
            ):
                raise TelegramUserTransportError("Telegram user identity mismatch")
            await asyncio.wait_for(
                client.log_out(), timeout=config.rpc_timeout_seconds
            )
        if await asyncio.wait_for(
            client.is_user_authorized(), timeout=config.rpc_timeout_seconds
        ):
            raise TelegramUserTransportError(
                "Telegram user session remained authorized after logout"
            )
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=min(config.connect_timeout_seconds, 5.0)
                )
            except Exception:
                pass
        lease.release()

    # Telethon normally removes its session during log_out(). Quarantine any
    # residual file inside the same protected profile directory before final
    # removal so no path outside HERMES_HOME is ever selected.
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{session_path}{suffix}")
        if not os.path.lexists(path):
            continue
        if path.is_symlink():
            path.unlink()
            continue
        quarantine = path.with_name(f".{path.name}.revoked")
        path.replace(quarantine)
        quarantine.unlink(missing_ok=True)
    if any(
        os.path.lexists(Path(f"{session_path}{suffix}"))
        for suffix in ("", "-journal", "-wal", "-shm")
    ):
        raise TelegramUserTransportError(
            "Telegram user session files remained after logout"
        )
    return {"schema_version": 1, "authorized": False, "removed": True}


def _entity_peer_id(entity: Any) -> int | None:
    for name in ("user_id", "channel_id", "chat_id", "id"):
        value = getattr(entity, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


class TelethonTelegramUserTransport:
    """Lazy profile-owned Telethon client with no update subscription."""

    def __init__(
        self,
        *,
        config: TelegramUserTransportConfig,
        active_bot_peer_id: int,
        active_bot_username: str | None = None,
        api_id: int,
        api_hash: str,
        hermes_home: Path,
        client_factory: Callable[..., Any] | None = None,
        functions_module: Any | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.policy = TelegramUserPolicy(
            config, active_bot_peer_id=active_bot_peer_id
        )
        self.active_bot_peer_id = active_bot_peer_id
        self.active_bot_username = (
            str(active_bot_username or "").strip().lstrip("@") or None
        )
        self.api_id = api_id
        self._api_hash = api_hash
        self.hermes_home = Path(hermes_home)
        self._client_factory = client_factory
        self._functions = functions_module
        self._clock = clock
        self._sleeper = sleeper
        self._client = None
        self._identity: TelegramUserIdentity | None = None
        self._connect_lock = asyncio.Lock()
        self._lease = _SessionLease(telegram_user_lock_path(self.hermes_home))
        self._failure_count = 0
        self._circuit_until = 0.0
        self._circuit_reason: str | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_home={str(self.hermes_home)!r}, "
            f"expected_user_id={self.config.expected_user_id!r})"
        )

    def circuit_status(self) -> dict[str, Any]:
        remaining = max(0.0, self._circuit_until - self._clock())
        return {
            "open": remaining > 0,
            "reason": self._circuit_reason if remaining > 0 else None,
            "retry_after_seconds": remaining,
            "consecutive_failures": self._failure_count,
        }

    def _check_circuit(self) -> None:
        if self._circuit_until > self._clock():
            raise TelegramUserTransportError(
                "Telegram user transport circuit is open; operator repair or cooldown is required"
            )
        if self._circuit_until:
            self._circuit_until = 0.0
            self._circuit_reason = None
            self._failure_count = 0

    def _open_circuit(self, reason: str) -> None:
        self._circuit_reason = reason
        self._circuit_until = self._clock() + self.config.failure_cooldown_seconds

    def _record_failure(self, reason: str) -> None:
        self._failure_count += 1
        if self._failure_count >= 2:
            self._open_circuit(reason)

    def _record_success(self) -> None:
        self._failure_count = 0

    async def _ensure_connected(self):
        self._check_circuit()
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is not None:
                return self._client
            try:
                session_path = ensure_safe_session_storage(self.hermes_home)
            except TelegramUserTransportError:
                self._open_circuit("unsafe_session")
                raise
            factory, functions = self._client_factory, self._functions
            if factory is None or functions is None:
                loaded_factory, loaded_functions = _load_telethon()
                factory = factory or loaded_factory
                functions = functions or loaded_functions
            self._lease.acquire()
            client = None
            try:
                with _private_file_creation_umask():
                    client = factory(
                        str(session_path),
                        self.api_id,
                        self._api_hash,
                        receive_updates=False,
                    )
                    await asyncio.wait_for(
                        client.connect(),
                        timeout=self.config.connect_timeout_seconds,
                    )
                ensure_safe_session_storage(self.hermes_home)
                if not await asyncio.wait_for(
                    client.is_user_authorized(),
                    timeout=self.config.rpc_timeout_seconds,
                ):
                    raise TelegramUserTransportError(
                        "Telegram user session is not authorized"
                    )
                me = await asyncio.wait_for(
                    client.get_me(), timeout=self.config.rpc_timeout_seconds
                )
                observed_id = getattr(me, "id", None)
                if not isinstance(observed_id, int) or isinstance(observed_id, bool):
                    self._open_circuit("identity_invalid")
                    raise TelegramUserTransportError("Telegram user identity is invalid")
                if observed_id != self.config.expected_user_id:
                    raise TelegramUserTransportError(
                        "Telegram user identity mismatch"
                    )
                premium = bool(getattr(me, "premium", False))
                if self.config.require_premium and not premium:
                    raise TelegramUserTransportError(
                        "Telegram user account does not satisfy Premium policy"
                    )
                self._identity = TelegramUserIdentity(
                    user_id=observed_id,
                    username=(str(getattr(me, "username", "") or "").strip() or None),
                    is_premium=premium,
                )
                self._client = client
                self._functions = functions
                ensure_safe_session_storage(self.hermes_home)
                return client
            except Exception as exc:
                self._client = None
                self._identity = None
                if client is not None:
                    try:
                        await asyncio.wait_for(
                            client.disconnect(),
                            timeout=min(self.config.connect_timeout_seconds, 5.0),
                        )
                    except Exception:
                        pass
                self._lease.release()
                if isinstance(exc, TelegramUserTransportError) and any(
                    marker in str(exc)
                    for marker in (
                        "not authorized",
                        "identity mismatch",
                        "Premium policy",
                    )
                ):
                    self._open_circuit("identity_or_authorization")
                raise

    async def identity(self) -> TelegramUserIdentity:
        await self._ensure_connected()
        assert self._identity is not None
        return self._identity

    async def _exact_peer(self, peer_id: int):
        client = await self._ensure_connected()
        try:
            peer = await asyncio.wait_for(
                client.get_input_entity(peer_id),
                timeout=self.config.rpc_timeout_seconds,
            )
        except Exception as numeric_exc:
            if not self.active_bot_username:
                raise TelegramUserTransportError(
                    "Configured Telegram bot peer could not be resolved"
                ) from numeric_exc
            try:
                peer = await asyncio.wait_for(
                    client.get_input_entity(self.active_bot_username),
                    timeout=self.config.rpc_timeout_seconds,
                )
            except Exception as username_exc:
                raise TelegramUserTransportError(
                    "Configured Telegram bot peer could not be resolved"
                ) from username_exc
        if _entity_peer_id(peer) != peer_id:
            raise TelegramUserTransportError(
                "Resolved Telegram bot peer did not match the configured ID"
            )
        return peer

    async def _invoke(self, request: Any) -> Any:
        client = await self._ensure_connected()
        try:
            result = await asyncio.wait_for(
                client(request), timeout=self.config.rpc_timeout_seconds
            )
            self._record_success()
            return result
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            wait = getattr(exc, "seconds", None)
            if isinstance(wait, (int, float)):
                if wait > self.config.max_flood_wait_seconds:
                    self._open_circuit("flood_wait")
                    raise TelegramUserTransportError(
                        "Telegram FloodWait exceeds the configured bound"
                    ) from exc
                raise _BoundedFloodWait(float(wait)) from exc
            self._record_failure("rpc_failures")
            if isinstance(exc, (ConnectionError, OSError)):
                raise _AmbiguousNetworkFailure(
                    "Telegram network operation had an ambiguous outcome"
                ) from exc
            raise TelegramUserTransportError(
                f"Telegram {type(request).__name__} failed"
            ) from exc

    @staticmethod
    def _require_topic(response: Any, topic_id: int) -> Any:
        topics = getattr(response, "topics", None)
        matches = [topic for topic in (topics or []) if getattr(topic, "id", None) == topic_id]
        if len(matches) != 1:
            raise TelegramUserTransportError(
                "Telegram exact topic readback returned no unique match"
            )
        return matches[0]

    async def _read_topic(self, *, peer_id: int, topic_id: int, peer: Any) -> TopicSnapshot:
        functions = self._functions
        if functions is None:
            raise TelegramUserTransportError("Telethon request types are unavailable")
        request = functions.messages.GetForumTopicsByIDRequest(
            peer=peer,
            topics=[topic_id],
        )
        try:
            response = await self._invoke(request)
        except _BoundedFloodWait as exc:
            await self._sleeper(exc.seconds)
            try:
                response = await self._invoke(request)
            except _BoundedFloodWait as repeated:
                self._record_failure("repeated_flood_wait")
                raise TelegramUserTransportError(
                    "Telegram FloodWait persisted after one bounded retry"
                ) from repeated
        topic = self._require_topic(response, topic_id)
        icon = getattr(topic, "icon_emoji_id", None)
        return TopicSnapshot(
            peer_id=peer_id,
            topic_id=topic_id,
            title=str(getattr(topic, "title", "") or ""),
            icon_emoji_id=icon if isinstance(icon, int) else None,
            closed=bool(getattr(topic, "closed", False)),
            hidden=bool(getattr(topic, "hidden", False)),
        )

    async def get_topic(self, *, peer_id: int, topic_id: int) -> TopicSnapshot:
        self.policy.require("topic.read", peer_id=peer_id, topic_id=topic_id)
        self._check_circuit()
        peer = await self._exact_peer(peer_id)
        return await self._read_topic(peer_id=peer_id, topic_id=topic_id, peer=peer)

    async def set_topic_icon(
        self,
        *,
        peer_id: int,
        topic_id: int,
        icon_emoji_id: int,
        expected_icon_emoji_id: int | None | object = UNSET_EXPECTED_ICON,
    ) -> VerifiedTopicMutation:
        self.policy.require(
            "topic.icon.write",
            peer_id=peer_id,
            topic_id=topic_id,
            custom_emoji_id=icon_emoji_id,
        )
        self._check_circuit()
        peer = await self._exact_peer(peer_id)
        # Confirm the exact topic exists under the verified peer before the
        # user-authorized mutation. The post-write read remains authoritative.
        pre_write = await self._read_topic(
            peer_id=peer_id, topic_id=topic_id, peer=peer
        )
        if (
            expected_icon_emoji_id is not UNSET_EXPECTED_ICON
            and pre_write.icon_emoji_id != expected_icon_emoji_id
        ):
            raise TelegramTopicReadbackMismatch(
                "Telegram topic icon changed immediately before mutation"
            )
        functions = self._functions
        if functions is None:
            raise TelegramUserTransportError("Telethon request types are unavailable")
        edit = functions.messages.EditForumTopicRequest(
            peer=peer,
            topic_id=topic_id,
            icon_emoji_id=icon_emoji_id,
        )
        retry_wait: float | None = None
        try:
            await self._invoke(edit)
        except _BoundedFloodWait as exc:
            retry_wait = exc.seconds
        except (TimeoutError, asyncio.TimeoutError, _AmbiguousNetworkFailure):
            # Ambiguous mutation: authoritative readback decides whether it
            # landed. Never issue a blind second write.
            pass
        observed = await self._read_topic(
            peer_id=peer_id, topic_id=topic_id, peer=peer
        )
        if (
            observed.icon_emoji_id != icon_emoji_id
            and retry_wait is not None
        ):
            # FloodWait is a known rejection, but the exact readback remains
            # authoritative. Only after it proves the write did not land may
            # the single bounded retry be issued.
            await self._sleeper(retry_wait)
            try:
                await self._invoke(edit)
            except _BoundedFloodWait as repeated:
                self._record_failure("repeated_flood_wait")
                observed = await self._read_topic(
                    peer_id=peer_id, topic_id=topic_id, peer=peer
                )
                if observed.icon_emoji_id != icon_emoji_id:
                    raise TelegramUserTransportError(
                        "Telegram FloodWait persisted after one bounded retry"
                    ) from repeated
            else:
                observed = await self._read_topic(
                    peer_id=peer_id, topic_id=topic_id, peer=peer
                )
        if observed.icon_emoji_id != icon_emoji_id:
            raise TelegramTopicReadbackMismatch(
                "Telegram topic icon exact readback did not match the request"
            )
        observed_icon_emoji_id = observed.icon_emoji_id
        if observed_icon_emoji_id is None:
            raise TelegramTopicReadbackMismatch(
                "Telegram topic icon exact readback did not include an icon"
            )
        return VerifiedTopicMutation(
            peer_id=peer_id,
            topic_id=topic_id,
            requested_icon_emoji_id=icon_emoji_id,
            observed_icon_emoji_id=observed_icon_emoji_id,
            verified_at=self._clock(),
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        self._identity = None
        try:
            if client is not None:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=min(self.config.connect_timeout_seconds, 5.0),
                )
        finally:
            self._lease.release()
