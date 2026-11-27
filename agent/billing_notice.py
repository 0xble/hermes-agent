"""Best-effort daily provider-credit notices for unattended Hermes runs."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import subprocess

logger = logging.getLogger(__name__)


def notify_provider_out_of_credits(provider: str) -> None:
    """Send one deduplicated priority-0 notice through the installed notify sender."""
    provider = str(provider or "").strip()
    if not provider:
        return
    day = datetime.now(timezone.utc).date().isoformat()
    title = f"{provider} out of credits"
    key = f"hermes:provider-credits:{provider}:{day}"
    args = (
        "notify", "--json", "--yes", "push", "--key", key, "--kind", "alert",
        "--priority", "0", "--title", title, "--message", title, "--no-url",
    )

    def _send() -> None:
        try:
            subprocess.run(
                args,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("Provider credit notice could not be sent", exc_info=True)

    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(_send, name="hermes-credit-notice").start()
