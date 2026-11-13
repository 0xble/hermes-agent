"""Platform-bound authorization callback for adapter taps and inline queries.

Moved out of ``gateway.run_adapters`` so that facade can stay under its line
cap. The mixin method remains the public entry and delegates here.
"""

from __future__ import annotations

import functools
import weakref as _weakref
from pathlib import Path
from typing import Callable, Optional

from gateway.config import Platform
from gateway.session import SessionSource


def make_adapter_auth_check(
    runner, platform: Platform, profile_name: Optional[str] = None
) -> Callable[[str, Optional[str], Optional[str]], bool]:
    """Platform-bound auth callback for adapters (prompt-injection mitigation for fetched
    context); delegates to :meth:`_is_user_authorized`. ``profile_name`` binds a secondary to
    its scope; for the shared primary (None) the routed profile is stamped so its pairing store
    is consulted while allowlist reads stay under the transport home.

    Without this an inline-button caller approved only in the routed profile's pairing store was denied
    (#86296), because the adapter's callback source was never route-stamped. A secondary-owned bot's
    callback fires outside the profile runtime scope (straight off the adapter's event loop), so the
    check re-enters the owning profile's scope per call — the gate's scoped env read otherwise falls
    back to os.environ (the default profile's env) and denies the secondary's own allowlisted
    callers (#120639).
    """
    from gateway.run import get_hermes_home
    transport_home = Path(get_hermes_home()) if runner._multiplex_on() and profile_name is None else None
    # Resolved once; the scope is entered per call so an ``.env`` allowlist edit reaches the next
    # tap, matching the message path's per-message re-read.
    profile_home = runner._routed_profile_home(profile_name) if profile_name else None

    def check(
        user_id: str, chat_type: Optional[str] = None, chat_id: Optional[str] = None, *,
        is_bot: bool = False, thread_id: Optional[str] = None,
    ) -> bool:
        if not user_id:
            return False
        source = SessionSource(
            platform=platform, chat_id=chat_id or "", chat_type=chat_type or "group",
            user_id=user_id, thread_id=thread_id, is_bot=bool(is_bot), profile=profile_name,
        )
        # Same transport provenance as ``build_source``, so policy reads resolve the receiving adapter.
        registry = (
            (getattr(runner, "_profile_adapters", None) or {}).get(profile_name)
            if profile_name else getattr(runner, "adapters", None)
        ) or {}
        adapter = registry.get(platform)
        if adapter is not None:
            source._transport_adapter_ref = _weakref.ref(adapter)  # type: ignore[attr-defined]
        if transport_home is None:
            # Sync, on the adapter's event loop (per tap, per inline-query keystroke): never
            # hydrate external secret sources here — that takes the process-global source lock
            # (#99519). Startup and the message path hydrate off-loop; this reads their cache.
            from gateway.run import _profile_runtime_scope
            with runner._scope_or_null(
                    functools.partial(_profile_runtime_scope, hydrate_secrets=False), profile_home):
                return runner._is_user_authorized(source)
        # Canonicalize FIRST (callback sources never went through ``build_source``): the routed
        # profile's pairing store is consulted, allowlists read under the transport home.
        if runner._canonicalize(source, primary_home=transport_home) is None:
            return False  # fail-closed, like the ``_handle_message`` ingress gate
        return runner._is_user_authorized_for_source(source)
    return check
