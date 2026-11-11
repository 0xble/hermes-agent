"""Profile-local retry sweep failures, deferrals and recovery reporting."""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path

logger = logging.getLogger("gateway.run")


class GatewayDelegationRetrySweepMixin:
    def _collect_profile_retry_actions(self, profile: str) -> list:
        from hermes_constants import hermes_home_key
        from tools.async_delegation import ORPHAN_SWEEP_INTERVAL_S
        from tools.delegation_resume import RETRY_CLAIM_TTL_S, sweep_retries

        key = hermes_home_key()
        failures = getattr(self, "_delegation_retry_busy_sweeps", None)
        if failures is None:
            failures = self._delegation_retry_busy_sweeps = {}
        try:
            actions = sweep_retries()
        except sqlite3.OperationalError as exc:
            # Extended BUSY codes share the primary low byte. Other storage
            # errors are failures, not an expected competing-writer deferral.
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
                failures.pop(key, None)
                logger.warning("Delegation retry sweep failed for profile %s: %s", profile, exc)
                return []
            count = failures[key] = failures.get(key, 0) + 1
            threshold = max(1, math.ceil(RETRY_CLAIM_TTL_S / ORPHAN_SWEEP_INTERVAL_S))
            logger.log(
                logging.WARNING if count >= threshold else logging.INFO,
                "Delegation retry sweep deferred: database busy; retrying next sweep "
                "(profile=%s consecutive=%d warning_after=%d)", profile, count, threshold)
            return []
        except Exception as exc:
            failures.pop(key, None)
            logger.warning("Delegation retry sweep failed for profile %s: %s", profile, exc)
            return []
        if count := failures.pop(key, 0):
            logger.info("Delegation retry sweep recovered for profile %s after %d busy sweeps", profile, count)
        return [{**action, "profile": profile} for action in actions]

    def _collect_delegation_retry_actions(self) -> list:
        """A busy ledger defers only its own profile's atomic retry claims."""
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_active_profile_name

        primary = get_active_profile_name() or "default"
        actions = self._collect_profile_retry_actions(primary)
        for profile, home in (getattr(self, "_served_profile_homes", None) or {}).items():
            if profile in {primary, getattr(self, "_primary_profile_name", None)}:
                continue
            try:
                with _profile_runtime_scope(Path(home), {}):
                    actions.extend(self._collect_profile_retry_actions(profile))
            except Exception as exc:
                logger.warning("Delegation retry sweep failed for profile %s: %s", profile, exc)
        return actions
