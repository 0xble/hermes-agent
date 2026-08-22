"""Active-profile composition for Telegram topic-icon operator commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home
from plugins.platforms.telegram.mtproto_telethon import (
    TelethonTelegramUserTransport,
)
from plugins.platforms.telegram.topic_icon_service import TelegramTopicIconService
from plugins.platforms.telegram.topic_icons import (
    CustomTopicIconCatalog,
    TopicIconCatalogError,
)
from plugins.platforms.telegram.user_transport import (
    TelegramUserTransportConfig,
    TelegramUserTransportError,
)


def _profile_inputs() -> tuple[dict[str, Any], tuple[str, ...], float]:
    from hermes_cli.config import load_config
    from hermes_cli.telegram import _telegram_extra

    config = load_config()
    extra = _telegram_extra(config if isinstance(config, dict) else {})
    user = extra.get("user_transport")
    user = user if isinstance(user, dict) else {}
    packs = extra.get("topic_icon_custom_packs")
    packs = tuple(packs) if isinstance(packs, list) else ()
    ttl = extra.get("topic_icon_catalog_ttl_seconds", 86400)
    return user, packs, float(ttl)


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret

    return str(get_secret(name, "") or "").strip()


class ProfileTopicIconService:
    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        packs: tuple[str, ...] | None = None,
        bot: Any | None = None,
        transport: Any | None = None,
        catalog: CustomTopicIconCatalog | None = None,
        state: Any | None = None,
        hermes_home: Path | None = None,
    ) -> None:
        if config is None or packs is None:
            profile_config, profile_packs, ttl = _profile_inputs()
            config = profile_config if config is None else config
            packs = profile_packs if packs is None else packs
        else:
            ttl = 86400.0
        self.config = TelegramUserTransportConfig.from_mapping(config)
        self.packs = tuple(packs)
        self._catalog = catalog or CustomTopicIconCatalog(
            self.packs, ttl_seconds=ttl
        )
        self._bot = bot
        self._transport = transport
        self._state = state
        self.hermes_home = Path(hermes_home or get_hermes_home())

    async def _bot_identity(self):
        if self._bot is None:
            token = _secret("TELEGRAM_BOT_TOKEN")
            if not token:
                raise TelegramUserTransportError(
                    "TELEGRAM_BOT_TOKEN is missing in the active profile"
                )
            try:
                from telegram import Bot  # type: ignore[attr-defined]
            except ImportError as exc:
                raise TelegramUserTransportError(
                    "Telegram Bot API dependencies are unavailable"
                ) from exc
            self._bot = Bot(token=token)
        try:
            identity = await self._bot.get_me()
        except Exception as exc:
            raise TelegramUserTransportError(
                "Active Bot API identity could not be verified"
            ) from exc
        bot_id = getattr(identity, "id", None)
        if bot_id not in self.config.allowed_bot_peer_ids:
            raise TelegramUserTransportError(
                "Active Bot API identity is not an allowed user-transport peer"
            )
        return identity

    async def _ensure_transport(self):
        identity = await self._bot_identity()
        if self._transport is None:
            api_id_raw = _secret("TELEGRAM_API_ID")
            api_hash = _secret("TELEGRAM_API_HASH")
            try:
                api_id = int(api_id_raw)
            except (TypeError, ValueError) as exc:
                raise TelegramUserTransportError(
                    "TELEGRAM_API_ID is missing or invalid in the active profile"
                ) from exc
            if not api_hash:
                raise TelegramUserTransportError(
                    "TELEGRAM_API_HASH is missing in the active profile"
                )
            self._transport = TelethonTelegramUserTransport(
                config=self.config,
                active_bot_peer_id=int(identity.id),
                active_bot_username=(
                    str(getattr(identity, "username", "") or "").strip() or None
                ),
                api_id=api_id,
                api_hash=api_hash,
                hermes_home=self.hermes_home,
            )
        return self._transport, int(identity.id)

    def _ensure_state(self):
        if self._state is None:
            from hermes_state import SessionDB

            self._state = SessionDB()
        return self._state

    async def _ensure_catalog(self, *, refresh: bool = False) -> None:
        if not self.packs:
            raise TopicIconCatalogError("no custom topic-icon packs are configured")
        if refresh or not self._catalog.rows:
            await self._bot_identity()
            await self._catalog.refresh(self._bot)

    async def _core(self) -> TelegramTopicIconService:
        await self._ensure_catalog()
        transport, peer_id = await self._ensure_transport()
        return TelegramTopicIconService(
            catalog=self._catalog,
            transport=transport,
            peer_id=peer_id,
            state_chat_id=str(self.config.expected_user_id),
            state=self._ensure_state(),
            bot=self._bot,
        )

    async def peers(self) -> dict[str, Any]:
        identity = await self._bot_identity()
        bot_id = int(identity.id)
        return {
            "schema_version": 1,
            "live": True,
            "peers": [
                {
                    "peer_id": peer_id,
                    "verified": peer_id == bot_id,
                    "username": (
                        str(getattr(identity, "username", "") or "").strip() or None
                    )
                    if peer_id == bot_id
                    else None,
                }
                for peer_id in sorted(self.config.allowed_bot_peer_ids)
            ],
        }

    async def user_status(self) -> dict[str, Any]:
        transport, _ = await self._ensure_transport()
        try:
            identity = await transport.identity()
            return {
                "schema_version": 1,
                "live": True,
                "enabled": self.config.enabled,
                "authorized": True,
                "user_id": identity.user_id,
                "username": identity.username,
                "premium": identity.is_premium,
            }
        finally:
            await transport.close()

    async def status(self, *, live: bool = True) -> dict[str, Any]:
        core = await self._core()
        try:
            return await core.status(live=live)
        finally:
            await self._transport.close()

    async def catalog(self, **kwargs) -> dict[str, Any]:
        refresh = bool(kwargs.pop("refresh", False))
        await self._ensure_catalog(refresh=refresh)
        core = TelegramTopicIconService(
            catalog=self._catalog,
            transport=self._transport,
            peer_id=next(iter(self.config.allowed_bot_peer_ids), 0),
            bot=self._bot,
        )
        return await core.catalog_rows(refresh=False, **kwargs)

    async def resolve(self, emoji: str, pack: str | None = None):
        await self._ensure_catalog()
        candidate = self._catalog.resolve(emoji, pack=pack)
        if candidate is None:
            raise TopicIconCatalogError(
                "Unicode selector does not resolve in the configured custom packs"
            )
        return candidate.to_dict()

    async def verify(self, topic_id: int):
        core = await self._core()
        try:
            return await core.verify(topic_id)
        finally:
            await self._transport.close()

    async def set(self, topic_id: int, emoji: str, pack: str | None = None):
        core = await self._core()
        try:
            return await core.set(topic_id, emoji, pack=pack)
        finally:
            await self._transport.close()
