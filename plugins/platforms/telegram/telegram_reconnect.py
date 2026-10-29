"""Profile-scoped replacement adapter resolution and bounded egress reconnect wait."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from plugins.platforms.telegram.adapter import TelegramAdapter

logger = logging.getLogger("plugins.platforms.telegram.adapter")


class TelegramReconnectMixin:
    """Reconnect egress stays with the retiring adapter's transport owner."""

    def _replacement_telegram_adapter(self) -> Optional["TelegramAdapter"]:
        """Live replacement for this adapter's owning profile after a reconnect.

        ``runner.adapters`` belongs only to the primary profile. A secondary adapter
        must resolve from its own map; otherwise an in-flight delivery can use the
        wrong bot identity.
        """
        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            return None
        owner = getattr(self, "_owner_profile", None) or getattr(self, "_hermes_profile_name", None)
        resolve = getattr(runner, "_authorization_adapter", None)
        if callable(resolve):
            live = resolve(self.platform, owner)
        elif owner and owner != "default" and owner != getattr(runner, "_primary_profile_name", None):
            live = ((getattr(runner, "_profile_adapters", None) or {}).get(owner) or {}).get(self.platform)
        else:
            live = (getattr(runner, "adapters", None) or {}).get(self.platform)
        if live is not None and live is not self and getattr(live, "_bot", None):
            return live
        return None

    async def _wait_for_reconnection(self) -> bool:
        """Wait for ``_bot`` or a replacement adapter; False on expiry or permanent fatal."""
        if self._bot or self._replacement_telegram_adapter() is not None:
            return True
        if self._is_permanent_fatal():
            return False
        wait_s = float(getattr(self, "_RECONNECT_WAIT_SECONDS", 15.0))
        poll_s = float(getattr(self, "_RECONNECT_POLL_INTERVAL", 0.5))
        logger.info("[%s] Not connected — waiting for reconnection (up to %.0fs)", self.name, wait_s)
        waited = 0.0
        while waited < wait_s:
            await asyncio.sleep(poll_s)
            waited += poll_s
            if self._is_permanent_fatal():
                return False
            if self._bot or self._replacement_telegram_adapter() is not None:
                logger.info("[%s] Reconnected after %.1fs", self.name, waited)
                return True
        logger.warning("[%s] Still not connected after %.0fs", self.name, wait_s)
        return False
