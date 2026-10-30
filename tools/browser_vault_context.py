"""Run a vault fill, or an automatic one-time-code entry, inside the selected backend's ``fill_context``
(a plugin backend's lock/snapshot/manager session).

Order, for every backend: the item's metadata is read and the page origin checked against it
(``_bound_page_origin``) BEFORE the context is entered, so a page on the wrong origin never opens a
backend's manager session or lock. Inside the context a backend with its own ``fill_context`` has the
item re-read, so the origins checked and the secret resolved come from one snapshot.

A plugin backend's failure arrives as ``PluginBackendError`` (fixed text, see
``agent/vault_backends/plugin.py``) and becomes a typed refusal; built-in backends behave as before.
"""
import json
import logging
from functools import wraps

logger = logging.getLogger(__name__)


def plugin_errors_as_refusal(fn):
    """A ``PluginBackendError`` raised anywhere in ``fn`` (metadata read, context enter/exit, resolve)
    becomes the ``backend_unavailable`` refusal."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from agent.vault_backends.plugin import PluginBackendError

        try:
            return fn(*args, **kwargs)
        except PluginBackendError as exc:
            return json.dumps({"success": False, "error_type": "backend_unavailable", "error": str(exc)})
    return wrapped


def saved_origins(meta) -> list[str]:
    """The exact origins a vault item may be filled on (several for manager items)."""
    return list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])


def _alias_origin(value) -> str | None:
    """One exact ``https://host[:port]`` origin, or None for anything else (http, a path, query, fragment,
    credentials or a wildcard): an alias never widens a binding beyond one named origin."""
    from urllib.parse import urlsplit

    from agent.vault_store import VaultError, normalize_origin

    if not isinstance(value, str) or "*" in value:
        return None
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            return None
        return normalize_origin(value)
    except (ValueError, VaultError):
        return None


def origin_aliases(meta) -> list[str]:
    """Extra exact origins the owner configured for a saved login under ``vault.origin_aliases.<handle>``,
    for a sign-in page on another host than the one the password manager item lists. Read on every call, so
    removing an entry takes effect at once; invalid entries are ignored with a warning."""
    from hermes_cli.config import load_config_readonly

    if meta.kind != "login":
        return []
    vault_cfg = load_config_readonly().get("vault")
    aliases = vault_cfg.get("origin_aliases") if isinstance(vault_cfg, dict) else None
    raw = aliases.get(meta.id) if isinstance(aliases, dict) else None
    if raw is None:
        return []
    out: list[str] = []
    for value in raw if isinstance(raw, list) else [raw]:
        origin = _alias_origin(value)
        if origin is None:
            logger.warning("Ignoring vault.origin_aliases entry for %s: %r is not an exact https origin",
                           meta.id, value)
        elif origin not in out:
            out.append(origin)
    return out


def snapshot_refusal(backend, handle, meta, page_origin):
    """Inside ``fill_context``: ``None`` when the item still binds ``page_origin``, else the refusal.
    Only a backend with its own context is re-read (the built-in managers' metadata read is a full
    listing, and their default context freezes nothing)."""
    from agent.vault_backends.base import LoginBackend

    if type(backend).fill_context is LoginBackend.fill_context:
        return None
    fresh = backend.get_meta(handle)
    if fresh is not None and fresh.kind == meta.kind and (
            page_origin in saved_origins(fresh) or page_origin in origin_aliases(fresh)):
        return None
    return {"success": False, "error_type": "origin_mismatch",
            "error": (f"Refused: the saved item changed before the fill and no longer binds {page_origin}. "
                      "Nothing was resolved or written.")}


def write_backend_code(backend, handle, meta, origin, write):
    """Mint the backend's one-time code for ``handle`` and ``write`` it, both inside ``fill_context``, as a
    password fill is. The caller has already checked ``origin`` against ``meta``. ``None`` when the backend
    has no code (or failed to give one): the caller asks the user."""
    from agent.vault_backends.plugin import PluginBackendError

    written = None
    try:
        with backend.fill_context():
            refusal = snapshot_refusal(backend, handle, meta, origin)
            if refusal is not None:
                return json.dumps(refusal)
            try:
                code = backend.resolve_otp(handle)
            except Exception:  # health: allow BLE001 -- no automatic code; the user is asked instead
                code = None
            if code:
                written = write(code)
    except PluginBackendError:  # the plugin's context failed to open or close; a code already written stands
        pass
    return written
