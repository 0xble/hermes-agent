"""A finite owner's private endpoint is discoverable by its reserved workers only.

Reuse the durable worker-adoption claim (assignment, live PID/birth, principal and
owner-issued secret). Same-user socket access or a creator PID is not this grant.
"""
from pathlib import Path

from gateway.session_worker import _SCOPE, worker_identity_claim, verify_worker_identity_claim
from hermes_state_runtime import RuntimeStoreError


def worker_discovery_handler(runner, identify):
    def discover(params, subject):
        if set(params) != _SCOPE:
            raise RuntimeStoreError('invalid_params')
        registry = getattr(runner, 'session_authorities', None)
        profile = params.get('profile_id')
        if not isinstance(profile, str) or not Path(profile).is_absolute():
            raise RuntimeStoreError('profile_mismatch')
        authority = registry.for_home(profile) if registry is not None else None
        if authority is None or authority.profile_id != profile:
            raise RuntimeStoreError('profile_mismatch')
        from gateway.session_authorities import require_serving
        require_serving(authority)
        claim = worker_identity_claim(authority, params, subject)
        verify_worker_identity_claim(authority, params, claim, live_only=True)
        return identify()
    return discover
