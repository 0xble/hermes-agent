from __future__ import annotations

import dataclasses
import subprocess
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
        process_exists=lambda pid: fake.old_alive,
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


def test_active_role_errors_and_reverse_handover_is_supported(tmp_path):
    def query(home, verb, *, slot, timeout=2):
        if verb == "identify":
            return {"standby": False, "slot": slot}
        return {"standby": False, "ready": True}
    events = []
    effects = HandoverEffects(query=query, bootout=lambda label: events.append(label))
    assert run_handover(home=tmp_path, timeout=1, effects=effects) == 1

    class ReverseFake(FakeGateway):
        def __init__(self):
            super().__init__()
            self.booted = False
            self.old_alive = True
            self.handover_states = iter([{"accepted": True, "token_lock_released": False},
                                         {"accepted": True, "token_lock_released": True}])

        def query(self, home, verb, *, slot, timeout=2.0):
            self.calls.append((verb, slot))
            if slot == "b":
                if verb == "identify":
                    return {"slot": "b", "standby": False, "pid": 202}
                if verb == "status":
                    return {"slot": "b", "standby": False, "ready": True}
                if verb == "handover":
                    return {"accepted": True}
                if verb == "handover-status":
                    return next(self.handover_states)
            if slot == "a":
                if verb == "identify":
                    return ({"slot": "a", "standby": True, "ready": self.standby_ready}
                            if self.booted else None)
                if verb == "status":
                    return ({"slot": "a", "standby": not self.promoted, "ready": self.standby_ready}
                            if self.booted else None)
                if verb == "promote":
                    self.promoted = True
                    self.old_alive = False
                    return {"promoted": True, "failed": []}
            return None

    fake, events = ReverseFake(), []
    effects = _effects(fake, events)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    assert [event[0] for event in events] == ["plist", "bootstrap", "bootout"]
    assert events[1][1].endswith(".b") is False
    assert events[-1][1].endswith(".b")
    assert any(slot == "b" and verb == "handover" for verb, slot in fake.calls)
    assert any(slot == "a" and verb == "promote" for verb, slot in fake.calls)


def test_active_dies_before_release_still_promotes_idle_and_only_boots_out_active(tmp_path):
    class CrashFake(FakeGateway):
        def __init__(self):
            super().__init__()
            self.crashed = False

        def query(self, home, verb, *, slot, timeout=2.0):
            self.calls.append((verb, slot))
            if slot == "a":
                if verb == "identify":
                    return {"slot": "a", "standby": False, "pid": 101} if not self.crashed else None
                if verb == "status":
                    return {"slot": "a", "standby": False, "ready": True} if not self.crashed else None
                if verb == "handover":
                    return {"accepted": True}
                if verb == "handover-status":
                    self.crashed = True
                    return {"accepted": True, "token_lock_released": False}
            if slot == "b":
                if verb == "identify":
                    return ({"slot": "b", "standby": True, "ready": self.standby_ready}
                            if self.booted else None)
                if verb == "status":
                    return ({"slot": "b", "standby": not self.promoted, "ready": self.standby_ready}
                            if self.booted else None)
                if verb == "promote":
                    self.promoted = True
                    return {"promoted": True, "failed": []}
            return None

    fake, events = CrashFake(), []
    effects = HandoverEffects(
        query=fake.query,
        bootstrap=lambda label, path: (events.append(("bootstrap", label)), setattr(fake, "booted", True)),
        bootout=lambda label: events.append(("bootout", label)),
        process_exists=lambda pid: fake.old_alive,
        monotonic=lambda: 0.0,
        sleep=lambda _: setattr(fake, "standby_ready", True),
        write_plist=lambda path, text: events.append(("plist", path.name, "b" in text)),
    )
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    bootouts = [event[1] for event in events if event[0] == "bootout"]
    assert len(bootouts) == 1 and not bootouts[0].endswith(".b")
    assert not any(event[1].endswith(".b") for event in events if event[0] == "bootout")
    assert any(verb == "promote" and slot == "b" for verb, slot in fake.calls)


def test_old_active_drains_past_the_budget_and_promoted_standby_is_never_booted_out(tmp_path):
    """After promotion B owns Telegram: A may take longer than any budget to finish its own turns,
    and no path may boot B out (that would be a cut). Only A's label is booted out, once A exits."""
    fake, events = FakeGateway(), []
    ticks = {"n": 0}

    def promote_keeps_old_alive(home, verb, *, slot, timeout=2.0):
        result = FakeGateway.query(fake, home, verb, slot=slot, timeout=timeout)
        if verb == "promote":
            fake.old_alive = True  # A still finishing an in-flight turn
        if slot == "a" and verb == "identify" and fake.promoted:
            ticks["n"] += 1
            if ticks["n"] > 20:  # long after the 5s budget below has expired
                fake.old_alive = False
                return None
        return result

    clock = {"t": 0.0}

    def sleep(_):
        fake.standby_ready = True
        clock["t"] += 1.0

    effects = HandoverEffects(
        query=promote_keeps_old_alive,
        bootstrap=lambda label, path: (events.append(("bootstrap", label)), setattr(fake, "booted", True)),
        bootout=lambda label: events.append(("bootout", label)),
        process_exists=lambda pid: True,
        monotonic=lambda: clock["t"],
        sleep=sleep,
        write_plist=lambda path, text: None,
    )
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    bootouts = [label for kind, label in events if kind == "bootout"]
    assert len(bootouts) == 1 and not bootouts[0].endswith(".b")
    assert clock["t"] > 5


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


def test_drill_home_gets_its_own_slot_labels_not_the_live_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-zero-cut"))
    active, standby = get_launchd_slot_labels()
    assert active.startswith("ai.hermes.gateway-") and active != "ai.hermes.gateway"
    assert standby == f"{active}.b"


def test_cli_budget_does_not_follow_a_zero_restart_drain_timeout(monkeypatch):
    """restart_drain_timeout 0 is a valid live setting; it must not abort every handover instantly."""
    from hermes_cli import gateway_handover

    seen = {}
    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "0")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"gateway": {"zero_cut_handover": {"enabled": True}}})
    monkeypatch.setattr(gateway_handover, "run_handover", lambda **kw: seen.update(kw) or 0)
    assert gateway_handover.cmd_handover(None) == 0
    assert seen["timeout"] == gateway_handover.STANDBY_READY_BUDGET_S > 0


def test_lost_promote_answer_never_boots_out_the_standby(tmp_path):
    """Promotion outlasts a short query; once A released Telegram, booting B out leaves nothing serving
    (found in a real drill: 0 active slots after A exited)."""
    fake, events = FakeGateway(), []
    real_query = fake.query
    timeouts = []

    def slow_promote(home, verb, *, slot, timeout=2.0):
        if verb == "promote":
            timeouts.append(timeout)
            real_query(home, verb, slot=slot, timeout=timeout)  # takes effect...
            return None                                        # ...but the answer is lost
        return real_query(home, verb, slot=slot, timeout=timeout)

    effects = dataclasses.replace(_effects(fake, events), query=slow_promote)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    assert timeouts and timeouts[0] >= 60
    assert not any(kind == "bootout" and label.endswith(".b") for kind, label, *_ in events)


def test_refused_promotion_after_release_leaves_standby_running(tmp_path):
    fake, events = FakeGateway(), []
    real_query = fake.query

    def refuse(home, verb, *, slot, timeout=2.0):
        if verb == "promote":
            return {"promoted": False, "refused": "not_ready"}
        return real_query(home, verb, slot=slot, timeout=timeout)

    effects = dataclasses.replace(_effects(fake, events), query=refuse)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 1
    assert not any(kind == "bootout" for kind, *_ in events)


def test_stale_loaded_idle_label_is_booted_out_and_bootstrapped_again(tmp_path):
    """Real drill: slot a exited 0 after handing over and stayed loaded, so the reverse handover's
    bootstrap failed with EIO and every crash drill aborted before starting the standby."""
    fake, events = FakeGateway(), []
    base = _effects(fake, events)
    tries = []

    def bootstrap(label, path):
        tries.append(label)
        if len(tries) == 1:
            raise subprocess.CalledProcessError(5, ["launchctl", "bootstrap"])
        base.bootstrap(label, path)

    assert run_handover(home=tmp_path, timeout=5, effects=dataclasses.replace(base, bootstrap=bootstrap)) == 0
    kinds = [event[0] for event in events]
    assert kinds[:3] == ["plist", "bootout", "bootstrap"] and len(tries) == 2
    assert events[1][1] == tries[0]  # only the idle label was booted out before the retry


def test_slow_release_after_acceptance_never_boots_out_the_standby(tmp_path):
    """Review: release shared the standby-ready budget, so a slow boot plus a ~15 s updater stop timed
    out and booted out B while A, having accepted, still released Telegram and exited."""
    fake, events = FakeGateway(), []
    clock = [0.0]
    states = iter([{"accepted": True, "token_lock_released": False}] * 50 +
                  [{"accepted": True, "token_lock_released": True}])
    fake.handover_states = states
    base = _effects(fake, events)

    def sleep(seconds):
        clock[0] += 1.0  # each poll costs a second: release lands long after the 5 s budget
        fake.standby_ready = True

    effects = dataclasses.replace(base, monotonic=lambda: clock[0], sleep=sleep)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    assert not any(kind == "bootout" and label.endswith(".b") for kind, label, *_ in events)


def test_lost_handover_answer_that_was_accepted_carries_on(tmp_path):
    fake, events = FakeGateway(), []
    real_query = fake.query

    def lossy(home, verb, *, slot, timeout=2.0):
        answer = real_query(home, verb, slot=slot, timeout=timeout)
        return None if verb == "handover" else answer  # accepted, but the answer never arrives

    fake.handover_states = iter([{"accepted": True, "token_lock_released": True}] * 5)
    effects = dataclasses.replace(_effects(fake, events), query=lossy)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 0
    assert not any(kind == "bootout" and label.endswith(".b") for kind, label, *_ in events)


def test_handover_that_was_never_accepted_boots_out_the_standby(tmp_path):
    fake, events = FakeGateway(), []
    real_query = fake.query

    def refused(home, verb, *, slot, timeout=2.0):
        if verb == "handover":
            return {"accepted": False, "refused": "disabled"}
        if verb == "handover-status":
            return {"accepted": False}
        return real_query(home, verb, slot=slot, timeout=timeout)

    effects = dataclasses.replace(_effects(fake, events), query=refused)
    assert run_handover(home=tmp_path, timeout=5, effects=effects) == 1
    assert any(kind == "bootout" and label.endswith(".b") for kind, label, *_ in events)
