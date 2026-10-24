"""Session-scoped attended holds take effect at the next API boundary."""
from __future__ import annotations

import asyncio

from agent.i18n import t
from gateway.attended_model import friendly_model_name, parse_attended_config
from gateway.session_attended import session_attended_pinned


class GatewayAttendCommandsMixin:
    async def _handle_attend_command(self, event) -> str:
        from gateway.run import _load_gateway_config
        from gateway.run_session_readback import read_session_settings

        action = (event.get_command_args() or "").strip().lower()
        if action not in ("", "off", "status"):
            return t("gateway.attend.usage")
        config = await asyncio.to_thread(_load_gateway_config)
        cfg = parse_attended_config(config)
        if action == "" and not cfg.enabled:
            return t("gateway.attend.disabled")
        source = event.source
        entry = await self.async_session_store.get_or_create_session(source)
        key = entry.session_key
        async with self._session_model_override_lock(key):
            if action == "status":
                pinned = await asyncio.to_thread(session_attended_pinned, self.session_store, key)
                model, _runtime, _reasoning = await read_session_settings(
                    self, source=source, session_key=key, user_config=config,
                )
                return t("gateway.attend.status_pinned" if pinned else "gateway.attend.status_unpinned",
                         model=friendly_model_name(model))
            pinned = action != "off"
            saved = await asyncio.to_thread(
                self.session_store.set_attended_pinned, key, pinned, expected_session_id=entry.session_id,
            )
            if not saved:
                return t("gateway.attend.session_changed")
            conversation = self._session_state(key).conversation
            if not pinned:
                stamp = await asyncio.to_thread(self.session_store.get_last_user_activity_at, key)
                conversation.last_user_activity_at = stamp.timestamp()
            conversation.pending_attended_notice = None
            self._bump_session_model_override_epoch(key)
        if not pinned:
            return t("gateway.attend.released", minutes=f"{cfg.idle_revert_minutes:g}")
        return t("gateway.attend.pinned", model=friendly_model_name(cfg.model or t("gateway.attend.configured_model")))
