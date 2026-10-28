"""Real gateway, process, native FIFO and reply path. Loopback model/relay only."""
from tests.gateway.test_authority_automation import _probe


def test_native_supervisor_inspects_and_replies_in_topic_then_disarms(tmp_path):
    _probe(tmp_path, "checkin")
