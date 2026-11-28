"""Best-effort cleanup of process-owned incognito intervals during gateway shutdown."""

import logging
from contextlib import suppress

logger = logging.getLogger("gateway.run")


def cleanup_incognito_private_dirs() -> None:
    """Run in the exit-state phase, after private-kernel shutdown, before removing intervals."""
    with suppress(Exception):
        from agent.incognito_private_dir import discard_owned_private_dirs
        removed = discard_owned_private_dirs()
        if removed:
            logger.info("Incognito shutdown cleanup removed %d private directories", removed)
