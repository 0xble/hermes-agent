"""Regression coverage for target APIs restored during fork synchronization."""

import inspect
import json

from gateway.control_socket import GatewayControlServer
from gateway.platforms.webhook import _WebhookDeliveryIdentity
from gateway.run import start_gateway
from gateway.run_turn_progress import GatewayTurnProgressMixin
from gateway.session_execution import GatewaySessionAgentMixin
from gateway.run_turn_runner import TurnRunner
from tools.process_registry_notifications import (
    HEARTBEAT_OUTPUT_CHARS,
    heartbeat_output_tail,
    join_heartbeat_outputs,
)


def test_restored_gateway_entrypoints_and_turn_mixins():
    assert "idle_exit" in inspect.signature(start_gateway).parameters
    assert issubclass(TurnRunner, GatewayTurnProgressMixin)
    assert issubclass(TurnRunner, GatewaySessionAgentMixin)


def test_restored_webhook_identity_round_trips_route_names():
    chat_id = _WebhookDeliveryIdentity.from_parts("profile", "route:with/slash", "delivery:1").session_chat_id
    assert chat_id.startswith("webhook:v2:")
    assert _WebhookDeliveryIdentity.route_from_chat_id(chat_id) == "route:with/slash"


def test_restored_control_socket_private_and_ticket_surfaces(tmp_path):
    server = GatewayControlServer(tmp_path)
    server.private_handlers["private"] = lambda params, peer: {"peer": peer, "params": params}

    private = json.loads(server.handle_request_line(json.dumps({
        "protocol": 1, "verb": "private", "id": 7, "params": {"ok": True},
    }).encode(), "uid:501"))
    assert private["result"] == {"peer": "uid:501", "params": {"ok": True}}

    class TicketStore:
        instance_id = "instance-1"

        def mint(self, **kwargs):
            return "ticket-1"

        def revoke(self):
            pass

    server.ticket_store = TicketStore()  # type: ignore[assignment]
    ticket = json.loads(server.handle_request_line(json.dumps({
        "protocol": 1, "verb": "session-ticket", "id": 8,
        "params": {"profile_id": "default", "instance_id": "instance-1", "purpose": "test"},
    }).encode(), "uid:501"))
    assert ticket["result"]["ticket"] == "ticket-1"


def test_restored_heartbeat_output_contract():
    tail, omitted = heartbeat_output_tail("x" * (HEARTBEAT_OUTPUT_CHARS + 3))
    assert len(tail) == HEARTBEAT_OUTPUT_CHARS
    assert omitted == 3
    assert join_heartbeat_outputs({"output": "a", "omitted": 0}, {"output": "b", "omitted": 0}) == {
        "output": "ab", "omitted": 0}
