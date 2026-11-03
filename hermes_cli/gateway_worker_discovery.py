"""Authenticated discovery for the workers reserved by a client-owned gateway."""
import json
import os
from pathlib import Path
import time

from hermes_cli.gateway_runtime_discovery import DiscoveryError, connect_private, _identify_response


def discover_worker_gateway_endpoint(home, scope, *, timeout=1.0):
    from hermes_cli.gateway_runtime import GatewayDiscovery, _endpoint, gateway_profile_home
    from hermes_cli.gateway_runtime_multiplex import multiplexer_root_for

    home = gateway_profile_home(Path(home))
    if not scope or scope.get('profile_id') != str(home):
        return GatewayDiscovery('conflict', reason_code='client_owned')
    deadline = time.monotonic() + timeout
    root = multiplexer_root_for(home)
    candidates = [home] if root is None else [home, root]
    request = json.dumps({'protocol': 1, 'id': 1, 'verb': 'worker-identify', 'params': scope}).encode() + b'\n'
    for control_home in candidates:
        budget = deadline - time.monotonic()
        if budget <= 0:
            break
        try:
            if os.name == 'nt':
                from gateway.runtime_bootstrap_windows import query_runtime_control
                raw = query_runtime_control(control_home, request, budget)
            else:
                from gateway.control_socket import _read_response_line
                with connect_private(control_home, budget) as peer:
                    peer.sendall(request)
                    def read():
                        peer.settimeout(max(0.001, deadline - time.monotonic()))
                        return peer.recv(65536)
                    raw = _read_response_line(read, deadline)
            payload = _identify_response(raw or b'')
            # The SAME private socket verified the full durable adoption claim before
            # disclosing this descriptor. Neither public identify nor creator PID can
            # authorize the worker. WebSocket worker.adopt still rechecks its claim.
            return _endpoint(payload, home, control_home=control_home, _worker_authorized=True)
        except (DiscoveryError, OSError, ValueError, TimeoutError):
            continue
    return GatewayDiscovery('conflict', reason_code='client_owned')
