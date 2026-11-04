"""An executor completing after cancellation still owes bounded child reaping."""
import subprocess
from unittest.mock import Mock, call

import pytest


@pytest.mark.parametrize("wedged", [False, True])
def test_late_spawn_is_reaped_with_the_same_timeout_and_kill_fallback(wedged):
    from hermes_cli.gateway_client_lifetime import ClientGatewayLease

    lease = ClientGatewayLease()
    lease.close()
    child = Mock()
    if wedged:
        child.wait.side_effect = [subprocess.TimeoutExpired("launcher", 5), 0]
    lease.own(child)
    child.stdin.close.assert_called_once()
    assert child.wait.call_args_list == ([call(timeout=5), call(timeout=5)] if wedged else [call(timeout=5)])
    assert child.kill.call_count == int(wedged)
    lease.close()
    assert not lease.children
