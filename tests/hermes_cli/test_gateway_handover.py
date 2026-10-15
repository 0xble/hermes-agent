from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading

from gateway.control_socket import query_gateway_control
from hermes_cli.gateway_handover import HandoverEffects, run_handover
from hermes_cli.gateway_launchd import get_launchd_slot_labels


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.promoted = False
        self.old_alive = True
        self.standby_ready = False
        self.booted = False
        self.handover_states = iter([{"accepted": True, "token_lock_released": False},
                                     {"accepted": True, "token_lock_released": True}])

    def query(self, home, verb, *, slot, timeout=2.0):
        self.calls.append((verb, slot))
        if slot == "a":
            if verb == "identify":
                return {"slot": "a", "standby": False, "pid": 101} if self.old_alive else None
            if verb == "status":
                return {"slot": "a", "standby": False, "ready": True}
            if verb == "handover":
                return {"accepted": True}
            if verb == "handover-status":
                return next(self.handover_states)
        if slot == "b":
            if verb == "identify":
                return ({"slot": "b", "standby": True, "ready": self.standby_ready}
                        if self.booted else None)
            if verb == "status":
                return ({"slot": "b", "standby": not self.promoted, "ready": self.standby_ready}
                        if self.booted else None)
            if verb == "promote":
                self.promoted = True
                self.old_alive = False
                return {"promoted": True, "failed": []}
        return None


def _effects(fake, events):
    def bootstrap(label, path):
        events.append(("bootstrap", label, path.name))
        fake.booted = True

    return HandoverEffects(
        query=fake.query,
        bootstrap=bootstrap,
        bootout=lambda label: events.append(("bootout", label)),
        process_exists=lambda pid: False,
        monotonic=lambda: 0.0,
        sleep=lambda _: setattr(fake, "standby_ready", True),
        write_plist=lambda path, text: events.append(("plist", path.name, "b" in text)),
    )


def test_handover_a_to_b_ordering(tmp_path):
    fake, events = FakeGateway(), []
    assert run_handover(home=tmp_path, timeout=5, effects=_effects(fake, events)) == 0
    assert [event[0] for event in events] == ["plist", "bootstrap", "bootout"]
    assert events[-1][1].startswith("ai.hermes.gateway") and not events[-1][1].endswith(".b")
    verbs = [verb for verb, _slot in fake.calls]
    assert verbs.index("handover") < verbs.index("promote")


def test_standby_never_ready_leaves_active_untouched(tmp_path):
    fake, events = FakeGateway(), []
    clock = iter([0.0, 10.0])
    effects = _effects(fake, events)
    effects = HandoverEffects(**{**effects.__dict__, "monotonic": lambda: next(clock),
                                 "sleep": lambda _: None})
    assert run_handover(home=tmp_path, timeout=1, effects=effects) == 1
    assert any(event[0] == "bootout" and event[1].endswith(".b") for event in events)
    assert not any(verb == "handover" for verb, _slot in fake.calls)


def test_handover_failed_does_not_promote(tmp_path):
    fake, events = FakeGateway(), []
    fake.handover_states = iter([{"failed": "telegram lock failure"}])
    assert run_handover(home=tmp_path, timeout=5, effects=_effects(fake, events)) == 1
    assert not any(verb == "promote" for verb, _slot in fake.calls)
    assert any(event[0] == "bootout" for event in events)


def test_active_role_errors_and_reverse_are_refused(tmp_path):
    def query(home, verb, *, slot, timeout=2):
        if verb == "identify":
            return {"standby": False, "slot": slot}
        return {"standby": False, "ready": True}
    events = []
    effects = HandoverEffects(query=query, bootout=lambda label: events.append(label))
    assert run_handover(home=tmp_path, timeout=1, effects=effects) == 1

    def reverse(home, verb, *, slot, timeout=2):
        if verb == "identify":
            return {"standby": slot == "a", "slot": slot}
        return {"standby": slot == "a", "ready": True}
    assert run_handover(home=tmp_path, timeout=1,
                        effects=HandoverEffects(query=reverse, bootout=lambda label: None)) == 1


def test_control_socket_client_round_trip(tmp_path):
    home = Path(tempfile.mkdtemp(prefix="hgw-", dir="/tmp"))
    sock_path = home / "gateway.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b'{"ok":true,"result":{"slot":"a","standby":false}}\n')
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert query_gateway_control(home, "identify") == {"slot": "a", "standby": False}
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_zero_cut_drill_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-zero-cut"))
    assert get_launchd_slot_labels() == ("ai.hermes.gateway.zero-cut", "ai.hermes.gateway.zero-cut.b")
