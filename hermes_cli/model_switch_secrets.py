"""Topical helpers extracted from hermes_cli/model_switch.py."""
from __future__ import annotations

def _scoped_key_env(name: str) -> str:
    """Read a provider key env var the way the chat path does, honouring the per-profile scope.

    With a secret scope installed (multiplexed gateway turn, dashboard/kanban workers) the scope's
    verdict is authoritative: a hit is this profile's key, a miss must not borrow another profile's
    value from the process env or the default ``.env``. Multiplexing on with no scope fails closed
    (``UnscopedSecretError`` -> ""). Otherwise resolve through ``get_env_prefer_dotenv`` — the
    chain ``client_lifecycle`` uses for the actual request — so a ``key_env`` that lives only in
    ``$HERMES_HOME/.env`` authenticates the ``/model`` verification probe (#109315) and a rotated
    ``.env`` beats a stale value inherited from the parent shell."""
    if not name:
        return ""
    try:
        from agent.secret_scope import current_secret_scope, get_secret, is_multiplex_active
        if current_secret_scope() is not None or is_multiplex_active():
            return (get_secret(name, "") or "").strip()
        from agent.credential_pool import get_env_prefer_dotenv
        return (get_env_prefer_dotenv(name) or "").strip()
    except Exception:
        return ""
