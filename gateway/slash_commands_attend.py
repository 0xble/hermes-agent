"""Session-scoped attended holds take effect at the next API boundary."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime

from agent.fast_mode import format_expiry_remaining
from agent.i18n import t
from gateway.attended_model import friendly_model_name, parse_attended_config, pin_is_active
from gateway.session_attended import session_attended_pin_until, session_attended_pinned
from hermes_cli.session_filters import parse_duration_seconds
from hermes_time import get_timezone


def _release_time(deadline: float) -> str:
    zone = get_timezone()
    return datetime.fromtimestamp(deadline, tz=zone).astimezone(zone).strftime("%Y-%m-%d %I:%M %p %Z")


class GatewayAttendCommandsMixin:
    async def _handle_attend_command(self, event) -> str:
        from gateway.run import _load_gateway_config
        from gateway.run_session_readback import read_session_settings

        action = (event.get_command_args() or "").strip().lower()
        pin_until = None
        release_time = ""
        if action not in ("", "off", "status"):
            try:
                duration = parse_duration_seconds(action) if any(c.isalpha() for c in action) else None
                if duration is None or not math.isfinite(duration) or duration <= 0:
                    raise ValueError
                pin_until = time.time() + duration
                release_time = _release_time(pin_until)
            except (ValueError, OverflowError, OSError):
                return t("gateway.attend.usage")
        config = await asyncio.to_thread(_load_gateway_config)
        cfg = parse_attended_config(config)
        if action not in ("off", "status") and not cfg.enabled:
            return t("gateway.attend.disabled")
        source = event.source
        entry = await self.async_session_store.get_or_create_session(source)
        key = entry.session_key
        async with self._session_model_override_lock(key):
            pinned = await asyncio.to_thread(session_attended_pinned, self.session_store, key)
            deadline = await asyncio.to_thread(session_attended_pin_until, self.session_store, key)
            pinned = pin_is_active(pinned, deadline, now=time.time())
            if action == "status" or (action == "off" and not pinned):
                model, _runtime, _reasoning = await read_session_settings(
                    self, source=source, session_key=key, user_config=config,
                )
                if pinned and deadline is not None:
                    return t("gateway.attend.status_timed", model=friendly_model_name(model),
                             remaining=format_expiry_remaining(deadline - time.time()), until=_release_time(deadline))
                return t("gateway.attend.status_pinned" if pinned else "gateway.attend.status_unpinned",
                         model=friendly_model_name(model))
            pinned = action != "off"
            saved = await asyncio.to_thread(
                self.session_store.set_attended_pinned, key, pinned,
                expected_session_id=entry.session_id, pin_until=pin_until,
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
        model = friendly_model_name(cfg.model or t("gateway.attend.configured_model"))
        if pin_until is not None:
            return t("gateway.attend.pinned_timed", model=model, duration=action, until=release_time)
        return t("gateway.attend.pinned", model=model)
