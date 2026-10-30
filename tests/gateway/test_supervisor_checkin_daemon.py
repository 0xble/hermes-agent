"""Real gateway, process, native FIFO and reply path. Loopback model/relay only."""
import pytest

from tests.gateway.test_authority_automation import _probe


def test_native_supervisor_inspects_and_replies_in_topic_then_disarms(tmp_path):
    _probe(tmp_path, "checkin")


@pytest.mark.parametrize("failure", [False, True])
def test_native_peer_only_supervisor_writes_in_original_topic_and_keeps_unverified_work(tmp_path, failure):
    _probe(tmp_path, "peer-checkin-failure" if failure else "peer-checkin")


@pytest.mark.parametrize('boundary', ['admission', 'execution'])
def test_human_arrival_cancels_tick_and_native_fifo_keeps_draining(tmp_path, boundary):
    _probe(tmp_path, 'checkin-race-' + boundary)
