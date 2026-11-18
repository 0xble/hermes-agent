"""External drain control methods for GatewayRunner."""

from __future__ import annotations

import asyncio
import logging

from gateway.zero_cut_handover_drain import handover_owns_external_drain

logger = logging.getLogger("gateway.run")


class GatewayShutdownExternalDrainMixin:
    """Refuse new turns while an external drain marker is present."""

    def _enter_external_drain(self) -> None:
        """Begin external drain: refuse NEW turns while in-flight work finishes."""
        if self._external_drain_active:
            return
        self._external_drain_active = True
        logger.info(
            "External drain ENGAGED (.drain_request.json present) — refusing "
            "new turns; %d in-flight turn(s) will finish. Process stays up.", self._active_work_count(),
        )
        self._update_runtime_status("draining")

    def _exit_external_drain(self) -> None:
        """Cancel external drain without resurrecting a stopping gateway."""
        if not self._external_drain_active:
            return
        if handover_owns_external_drain(self):
            # The handover owns shared quiescence until it fails or becomes a real shutdown.
            return
        self._external_drain_active = False
        if self._draining or not self._running:
            logger.info(
                "External drain marker cleared during shutdown — not reverting "
                "to running (shutdown takes precedence)."
            )
            return
        logger.info(
            "External drain RELEASED (.drain_request.json removed) — "
            "re-accepting new turns; gateway_state -> %s.", self._serving_state(),
        )
        self._update_runtime_status(self._serving_state())

    async def _drain_control_watcher(self, interval: float = 1.0) -> None:
        """Poll the external drain marker and switch reversible admission."""
        from gateway.drain_control import drain_requested

        while self._running:
            try:
                # Marker I/O can stall under host pressure, so keep it off the gateway loop.
                if await asyncio.to_thread(drain_requested):
                    self._enter_external_drain()
                    # API and cron work also contributes to this reversible drain's status.
                    self._persist_active_agents()
                else:
                    self._exit_external_drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Drain-control watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)
