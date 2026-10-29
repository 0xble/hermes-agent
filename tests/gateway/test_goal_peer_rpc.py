"""The real owner dispatch, not a parallel ledger or test-only peer adapter."""
import json
from types import SimpleNamespace

import pytest
import hermes_bootstrap  # noqa: F401 -- initialize dependencies before the real-home I/O guard

from tests.gateway.test_session_viewer_automation import owner  # noqa: F401


@pytest.mark.asyncio
async def test_native_goal_peer_rpc_uses_authorized_parent_snapshot_and_arms(owner, monkeypatch):
    from hermes_cli.checkins import outstanding_work
    from gateway import run_checkins

    reconciled = []

    async def reconcile(runner, source, route):
        reconciled.append((source, route))
    monkeypatch.setattr(run_checkins, "reconcile_turn", reconcile)
    reply = await owner.call("goal", "set", "Return the verified artifact")
    instance = json.loads(reply["state"])["created_at"]
    reply = await owner.call("goal", "wait_on_peer", "hermes:default/peer", "existing-work",
                             "artifact readback", expected_instance=instance)
    assert reply["value"]["ok"], reply
    assert len(reconciled) == 1
    assert outstanding_work(owner.db, owner.target)[0]["kind"] == "peer"
    before = owner.db.get_meta("goal:" + owner.target)
    rejected = await owner.call("goal", "mark_done", "peer says done")
    assert rejected["error"]["type"] == "ValueError"
    assert owner.db.get_meta("goal:" + owner.target) == before
    proof = "pytest peer_artifact.py: 1 passed, artifact readback verified"
    owner.db.append_message(owner.target, "tool", proof, tool_name="terminal")
    reply = await owner.call("goal", "resolve_peer", "hermes:default/peer", "existing-work",
                             proof, expected_instance=instance)
    assert reply["value"]["ok"], reply
    assert not json.loads(reply["state"])["pending_peers"]
    assert len(reconciled) == 2


@pytest.mark.asyncio
async def test_terminal_peer_entrypoint_attaches_only_to_current_session(monkeypatch):
    from hermes_cli import goal_peer_command, gateway_client, gateway_runtime
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def rpc(self, method, **params):
            calls.append((method, params))
            return {"value": {"ok": True}, "state": None}

    endpoint = object()
    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint",
                        lambda home: SimpleNamespace(state="ready", endpoint=endpoint))
    monkeypatch.setattr(gateway_client, "connect_gateway", lambda **kw: Client())
    monkeypatch.setenv("HERMES_SESSION_ID", "current-parent")
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id("current-parent"):
        reply = await goal_peer_command.run_command('wait 123 hermes:other/peer existing-work "artifact readback"')
    assert reply["value"]["ok"]
    assert calls == [("session.resume", {"session_id": "current-parent"}),
                     ("session.automation", {"session_id": "current-parent", "kind": "goal", "call": "wait_on_peer",
                      "args": ["hermes:other/peer", "existing-work", "artifact readback"],
                      "kwargs": {"expected_instance": 123.0}})]
    with scoped_current_session_id(""):
        with pytest.raises(ValueError, match="current session"):
            await goal_peer_command.run_command("list")
    assert len(calls) == 2
