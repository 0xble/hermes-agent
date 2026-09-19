"""Hermes-managed Camofox state helpers.

With managed persistence enabled, Hermes sends a deterministic userId derived from the
active profile so Camofox maps it to the same persistent browser profile across restarts.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, Optional

from hermes_constants import get_hermes_home


def get_camofox_state_dir() -> Path:
    """Return the profile-scoped root directory for Camofox persistence."""
    return get_hermes_home() / "browser_auth" / "camofox"


def get_camofox_identity(task_id: Optional[str] = None) -> Dict[str, str]:
    """Stable Hermes-managed Camofox identity: userId is profile-scoped, session key is
    scoped to the logical browser task so new tabs in the same profile reuse it."""
    scope_root = str(get_camofox_state_dir())
    user_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-user:{scope_root}").hex[:10]
    session_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-session:{scope_root}:{task_id or 'default'}").hex[:16]
    return {"user_id": f"hermes_{user_digest}", "session_key": f"task_{session_digest}"}


# Default operator aliases for the personal profile. Company profiles (LPG, Meridian) MUST
# narrow this through ``browser.camofox.accounts``: a shared agent that advertises another
# owner's alias invites a cross-company login, which the migration contract forbids.
CAMOFOX_ACCOUNT_ALIASES = ("brianle", "lpg", "meridian")

# ``personal`` was the legacy Chrome-era alias. The replacement is a hard cutover to
# ``brianle`` with no compatibility alias, so config cannot reintroduce it.
_REFUSED_ACCOUNT_ALIASES = frozenset({"personal"})


def get_camofox_account_aliases() -> tuple:
    """Operator-facing Camofox aliases for the ACTIVE profile.

    ``browser.camofox.accounts`` overrides the defaults so each installation advertises only
    the identities it owns. Entries are normalized and deduplicated; a malformed or empty
    list falls back to the defaults rather than leaving a profile with no browser identity.
    """
    try:
        from hermes_cli.config import load_config
        configured = load_config().get("browser", {}).get("camofox", {}).get("accounts")
    except Exception:
        return CAMOFOX_ACCOUNT_ALIASES
    if not isinstance(configured, (list, tuple)):
        return CAMOFOX_ACCOUNT_ALIASES
    aliases: list = []
    for entry in configured:
        if not isinstance(entry, str):
            continue
        alias = entry.strip().lower()
        if not alias or alias in _REFUSED_ACCOUNT_ALIASES or alias in aliases:
            continue
        aliases.append(alias)
    return tuple(aliases) or CAMOFOX_ACCOUNT_ALIASES


def get_camofox_account_identity(account: str, task_id: Optional[str] = None) -> Dict[str, str]:
    """Return a stable identity for one named operator account.

    The alias is part of the profile-scoped derivation, so sibling accounts never
    share a Camofox userId while tasks using the same account reuse its browser
    profile. Raw Camofox IDs stay inside the client and are never model-facing.
    """
    allowed = get_camofox_account_aliases()
    if account not in allowed:
        raise ValueError(f"Unknown Camofox account {account!r}; choose one of: {', '.join(allowed)}")
    scope_root = str(get_camofox_state_dir())
    user_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-account:{scope_root}:{account}").hex[:10]
    session_digest = uuid.uuid5(
        uuid.NAMESPACE_URL, f"camofox-account-session:{scope_root}:{account}:{task_id or 'default'}"
    ).hex[:16]
    return {"user_id": f"hermes_{user_digest}", "session_key": f"{account}_{session_digest}"}


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

CAMOFOX_STATE_DIR_NAME = "browser_auth"

CAMOFOX_STATE_SUBDIR = "camofox"
# ---- END PLUGIN-COMPAT ----
