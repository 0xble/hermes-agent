"""Short post-interrupt bounds shared by restart and signal shutdown."""
from __future__ import annotations


def bounded_stop_requested(runner) -> bool:
    # The unplanned-signal flag controls revival, not stop latency: operator
    # SIGTERM/SIGINT deliberately leave it false but still race the supervisor.
    return bool(runner._restart_requested or getattr(runner, "_stop_requested_by_signal", False)
                or getattr(runner, "_signal_initiated_shutdown", False))


class GatewayShutdownDeadlinesMixin:
    def _restart_shutdown_bound(self) -> float:
        """Keep restart/signal post-interrupt cleanup within a short process-exit bound."""
        launchd_budget = getattr(self, "_launchd_exit_timeout_s", None)
        if isinstance(launchd_budget, (int, float)) and launchd_budget > 0:
            return max(1.0, min(3.0, float(launchd_budget) - 1.0))
        return 3.0

    def _restart_agent_finalize_bound(self, bound: float) -> float:
        """Reserve the final restart slice for adapter/token teardown."""
        return max(0.1, bound - min(1.0, max(0.5, bound / 3.0)))
