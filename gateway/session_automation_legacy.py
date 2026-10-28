"""Authorize pre-ledger routes without fabricating a human admission."""
from copy import copy
from pathlib import Path

from gateway.platforms.event import MessageEvent
from hermes_state_runtime import RuntimeStoreError


def revalidate_legacy_route(authority, entry, adapter):
    """A persisted origin proposes a route, never permission or private relay/role proof."""
    from gateway.session_envelope import _validate_native
    from gateway.session_ingress_context import _binding

    runner = authority.runner
    if entry.origin is None or adapter is None:
        raise RuntimeStoreError('not_found', field='automation_envelope')
    restored = copy(entry)
    restored.origin = copy(entry.origin)
    source = runner._restored_source(restored)
    registered, profile = runner._owning_profile(adapter, source.platform)
    if not registered or runner._delivery_adapter_for(source) is not adapter:
        raise RuntimeStoreError('not_found', field='automation_envelope_adapter')
    # _binding can mint a relay seal, and the native validator can honor a live
    # adapter's upstream-auth flag. Neither is proof of a legacy caller's grant.
    if (source.role_authorized or source.delivered_via_upstream_relay or
            runner._adapter_flag(source.platform, 'authorization_is_upstream', profile)):
        raise RuntimeStoreError('permission_denied', field='automation_legacy_delegation')
    bound, _, runtime_home, provenance = _binding(runner, source, profile)
    if bound is not adapter or Path(runtime_home).resolve() != Path(authority.db.db_path).parent.resolve():
        raise RuntimeStoreError('profile_mismatch')
    source.message_id = None
    event = MessageEvent(text='', source=source)
    # Same source, authorization and connector checks as native ingress. No inbound
    # message, text or media is invented, and no native admission row is written.
    encoded = _validate_native(runner, event, provenance)
    return event, {'source': encoded, 'provenance': provenance}
