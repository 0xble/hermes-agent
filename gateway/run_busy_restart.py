"""Topical phases split from gateway/run_busy.py; original module bindings remain late-bound."""

from __future__ import annotations

from gateway.platforms.event import MessageEvent

class GatewayBusySessionMixin:
    def _is_stale_restart_redelivery(self, event: MessageEvent) -> bool:
        """True if this /restart is a Telegram re-delivery we already handled.

        The previous gateway wrote ``.restart_last_processed.json`` (platform + transport +
        update_id). Within that bot's stream, update_id <= that value is a redelivery when this
        process booted from that restart; otherwise the marker must be < 5 minutes old.
        Telegram only (numeric ordering); legacy markers retain their unscoped behavior.
        """
        from gateway.run_busy import json, time
        from gateway.run import _hermes_home
        if event is None or event.source is None or event.platform_update_id is None:
            return False
        try:
            if event.source.platform.value != "telegram":
                return False
        except Exception:
            return False

        try:
            marker_path = _hermes_home / ".restart_last_processed.json"
            if not marker_path.exists():
                # Missing marker: a redelivered /restart would otherwise re-restart forever. Suppress
                # ONLY when this process booted from a chat /restart AND is within a short post-boot
                # window; consume the flag one-shot so a later legitimate /restart is honored.
                if (
                    # Belt-and-suspenders for when the dedup marker goes missing (manually cleaned up, or
                    # the previous cycle's write failed). Without a marker the update_id comparison below
                    # can't run, so a redelivered /restart would sail through and re-restart the gateway —
                    # an infinite loop (issue #18528).
                    getattr(self, "_booted_from_restart", False)
                    and time.time() - getattr(self, "_startup_time", 0.0) < 60
                ):
                    self._booted_from_restart = False
                    return True
                return False
            data = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return False

        from gateway.session_identity import transport_profile_of

        # Telegram update IDs are ordered per bot, not across the multiplexed process.
        # Runtime profiles may be shared by several bots, so use the receiving transport.
        recorded_profile = data.get("transport_profile")
        if recorded_profile is not None and recorded_profile != transport_profile_of(event.source):
            return False

        recorded_uid = data.get("update_id")
        if (
            data.get("platform") != "telegram"
            or not isinstance(recorded_uid, int)
            or event.platform_update_id > recorded_uid
        ):
            return False

        # A service-managed restart can outlast the 5-minute trust window; consume the boot
        # signal one-shot.
        if getattr(self, "_booted_from_restart", False):
            self._booted_from_restart = False
            return True

        # Staleness guard: an old marker (crash recovery) must not swallow a fresh /restart.
        requested_at = data.get("requested_at")
        return not (isinstance(requested_at, (int, float)) and time.time() - requested_at > 300)
