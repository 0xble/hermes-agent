"""A known retirement permits transport retries, never protocol or ownership bypasses."""
import time

import pytest


@pytest.mark.parametrize("owner", [None, "finite", "idle"])
@pytest.mark.parametrize("failure", ["eof", "reset", "invalid", "unauthorized"])
def test_identify_retry_is_limited_to_known_retirement(tmp_path, monkeypatch, owner, failure):
    from hermes_cli import gateway_runtime as runtime
    from hermes_cli import gateway_runtime_discovery as discovery
    from hermes_cli import gateway_runtime_service as service
    from hermes_cli import gateway_runtime_start as start

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload = {"runtime_protocol": 1, "pid": 123, "supervisor": "none",
               "state": "draining" if owner == "idle" else "ready",
               "drain_reason": "idle_exit", "instance_id": "retiring", "authority_epoch": 1,
               "client_owned": owner == "finite", "client_lease_token": "other-client",
               "api_origin": "http://127.0.0.1:41817",
               "served_profiles": [{"profile_id": str(tmp_path), "home": str(tmp_path)}],
               "capabilities": ["session-authority-v1"]}
    calls = []

    def identify(*args, **kwargs):
        calls.append(kwargs["timeout"])
        if owner is not None and len(calls) == 1:
            return payload
        if failure == "reset":
            raise ConnectionResetError
        if failure == "unauthorized":
            raise discovery.DiscoveryError("unsafe_control_owner")
        raise discovery.DiscoveryError("incomplete_control_response" if failure == "eof"
                                       else "invalid_control_response")

    def forbidden(*args, **kwargs):
        pytest.fail("an incomplete identify must not start or replace an unverified owner")

    monkeypatch.setattr(discovery, "query_identify", identify)
    monkeypatch.setattr(runtime, "_served_by_multiplexer", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "discover_existing_gateway_service", forbidden)
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", forbidden)
    before = time.monotonic()
    observed = runtime.ensure_gateway_runtime(tmp_path, timeout=2)
    elapsed = time.monotonic() - before
    assert observed.endpoint is None
    if owner is not None and failure in {"eof", "reset"}:
        assert len(calls) > 2
        assert 2 <= elapsed < 5
        assert calls[-1] < calls[0]
        assert observed.reason_code == ("client_owned" if owner == "finite" else "deadline")
        assert observed.state == ("conflict" if owner == "finite" else "starting")
    else:
        assert len(calls) == (2 if owner is not None else 1)
        assert observed.state == "inaccessible"
        reasons = {"invalid": "invalid_control_response", "unauthorized": "unsafe_control_owner"}
        assert observed.reason_code == reasons.get(failure, "incomplete_control_response")
