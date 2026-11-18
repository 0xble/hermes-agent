"""Recovery of paid Nous credentials after an entitlement refresh."""

from __future__ import annotations

def _try_refresh_nous_paid_entitlement_credentials(agent) -> bool:
    """Refresh Nous runtime credentials after a fresh paid-entitlement check."""
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        if get_nous_portal_account_info(force_fresh=True).paid_service_access is not True:
            return False
        return agent._try_refresh_nous_client_credentials(force=True)
    except Exception:
        return False
