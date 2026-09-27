"""Replay shutdown spools under each owning gateway profile at startup."""

import logging
from pathlib import Path

from hermes_constants import get_routing_process_hermes_home
from gateway.session_recovery import SessionRecoveryMixin
from gateway.shutdown_flush import recover_pending_to_db

logger = logging.getLogger("gateway.run")


def pending_home_for_key(runner, session_key: str) -> Path | None:
    """Resolve queued keys with the SessionStore's canonical namespace parser.

    Without multiplexing, legacy ``agent:main`` belongs to the launch profile,
    including when that profile is named. With multiplexing, it names default.
    """
    launch = Path(get_routing_process_hermes_home())
    primary = getattr(runner, "_primary_profile_name", None) or "default"
    served = getattr(runner, "_served_profile_homes", None) or {}
    config = getattr(runner, "config", None)
    multiplex = getattr(config, "multiplex_profiles", len(served) > 1)
    owner = SessionRecoveryMixin._profile_from_session_key(session_key)
    if not multiplex:
        return launch if owner in (None, "default", primary) else None
    if owner == primary:
        return launch
    return Path(served[owner]) if owner in served else None


def recover_pending_shutdown_flush(runner) -> int:
    """Visit the launch home and every served home; leave failed spools for a later boot."""
    from gateway.run import _profile_runtime_scope

    launch_home = Path(get_routing_process_hermes_home())
    homes = [launch_home, *((getattr(runner, "_served_profile_homes", None) or {}).values())]
    recovered = 0
    for home in dict.fromkeys(Path(home) for home in homes):
        try:
            with _profile_runtime_scope(home, prepared_secret_scope={}):
                def resolve_here(key, *, not_after=None):
                    owner_home = pending_home_for_key(runner, key)
                    if owner_home is None or (home != launch_home and owner_home != home):
                        return None
                    # Before profile-owned spools, the shared primary adapter and runner both
                    # wrote routed secondary slots into the launch home's spool. Only that
                    # legacy location may cross homes, and only for a verified served owner.
                    with _profile_runtime_scope(owner_home, prepared_secret_scope={}):
                        return runner.session_store.resolve_session_id_for_key(key, not_after=not_after)

                recovered += recover_pending_to_db(session_resolver=resolve_here)
        except Exception:
            logger.warning("Pending-message recovery failed for profile home %s; spool retained", home,
                           exc_info=True)
    return recovered
