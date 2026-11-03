"""Live legacy control identity, shared by public and authenticated worker discovery."""


def make_runtime_identify(runner, build_identify_payload):
    def _identify_runtime():
        from gateway.drain_control import RESTART_WAIT_REASON
        payload = build_identify_payload()
        descriptor = getattr(runner, "session_runtime_descriptor", {})
        payload.update({key: descriptor[key] for key in (
            "instance_id", "runtime_protocol", "authority_epoch", "state", "api_origin",
            "served_profiles", "parked_profiles", "capabilities", "client_owned") if key in descriptor})
        if descriptor.get("client_owned"):
            from gateway.client_lifetime import client_lease_token
            payload["client_lease_token"] = client_lease_token()
        if getattr(runner, "_draining", False):
            payload.update(state="draining")
            # The restart's after-turn wait still owns its turns, so the endpoint stays dialable.
            if descriptor.get("drain_reason") != RESTART_WAIT_REASON:
                payload["capabilities"] = []
        if payload.get("state") == "draining" and descriptor.get("drain_reason"):
            payload["drain_reason"] = descriptor["drain_reason"]
        payload["supervisor"] = {"manual": "none", "desktop": "none"}.get(
            payload.get("supervisor"), payload.get("supervisor", "none"))
        return payload
    return _identify_runtime
