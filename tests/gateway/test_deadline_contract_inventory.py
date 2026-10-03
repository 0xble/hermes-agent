"""Mechanical blocking-site inventory for seamless-restart.md's deadline contract.

No imports of production modules, subprocesses, network, or live launchd jobs.
EXEMPT is intentionally site-specific: new unbudgeted sites fail closed. This is
an inventory, not data-flow proof that a supplied timeout is the correct budget;
behavioural lock/clock tests enforce that complementary part of the contract.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(os.environ.get("DEADLINE_INVENTORY_ROOT", Path(__file__).resolve().parents[2]))
MODULES = (
    "gateway/generation.py", "gateway/run_generation.py",
    "hermes_cli/gateway_forward_update.py", "hermes_cli/gateway_guardian.py",
    "hermes_cli/gateway_launchd_generation.py", "hermes_cli/gateway_launchd.py",
)
# The launchd module contains legacy install/stop/restart paths. Audit the shared
# forward bootstrap/probe helpers and the explicitly preserved legacy retry.
LAUNCHD_FUNCTIONS = {
    "_launchctl_bootstrap", "_launchctl_supervised_pid",
    "_launchctl_label_supervising_process", "_retry_launchctl_bootstrap_until_registered",
}
EXEMPT = {
    "gateway/generation.py:_deadline_connect:self.connect": "non-deadline caller fallback keeps the coordinator's default busy timeout",
    "gateway/generation.py:_begin_immediate:BEGIN IMMEDIATE": "helper owns the deadline-aware BEGIN; direct callers are legacy transactions",
    "gateway/generation.py:_initialize:self.connect": "schema initialization is outside a bounded handover window",
    "gateway/generation.py:_initialize:BEGIN IMMEDIATE": "schema initialization is outside a bounded handover window",
    "gateway/generation.py:register:self.connect": "registration is a non-deadline caller by design",
    "gateway/generation.py:register:BEGIN IMMEDIATE": "registration is a non-deadline caller by design",
    "gateway/generation.py:reserve_generation:self.connect": "reservation is a non-deadline caller by design",
    "gateway/generation.py:reserve_generation:BEGIN IMMEDIATE": "reservation is a non-deadline caller by design",
    "gateway/generation.py:register_unclaimed:self.connect": "legacy claim path is a non-deadline caller by design",
    "gateway/generation.py:register_unclaimed:BEGIN IMMEDIATE": "legacy claim path is a non-deadline caller by design",
    "gateway/generation.py:claim_generation:self.connect": "claim path is a non-deadline caller by design",
    "gateway/generation.py:claim_generation:BEGIN IMMEDIATE": "claim path is a non-deadline caller by design",
    "gateway/generation.py:_retire:self.connect": "retention cleanup is outside a bounded handover window",
    "gateway/generation.py:_retire:BEGIN IMMEDIATE": "retention cleanup is outside a bounded handover window",
    "gateway/generation.py:retire_dead_generation:self.connect": "retention cleanup is outside a bounded handover window",
    "gateway/generation.py:transition_state:self.connect": "state heartbeat path is a non-deadline caller by design",
    "gateway/generation.py:transition_state:BEGIN IMMEDIATE": "state heartbeat path is a non-deadline caller by design",
    "gateway/generation.py:observe_suspect:self.connect": "suspect observation is a non-deadline caller by design",
    "gateway/generation.py:observe_suspect:BEGIN IMMEDIATE": "suspect observation is a non-deadline caller by design",
    "gateway/generation.py:heartbeat:self.connect": "heartbeat is a non-deadline caller by design",
    "gateway/generation.py:heartbeat:BEGIN IMMEDIATE": "heartbeat is a non-deadline caller by design",
    "gateway/generation.py:record_poller_event:self.connect": "journal append is a non-deadline caller by design",
    "gateway/generation.py:record_poller_event:BEGIN IMMEDIATE": "journal append is a non-deadline caller by design",
    "gateway/generation.py:poller_journal:self.connect": "journal read is a non-deadline caller by design",
    "gateway/generation.py:transfer_receipts:self.connect": "receipt read is a non-deadline caller by design",
    "gateway/generation.py:project_active_summary:self.connect": "legacy projection is a non-deadline caller by design",
    "gateway/generation.py:project_active_summary:BEGIN IMMEDIATE": "legacy projection is a non-deadline caller by design",
    "gateway/generation.py:fence_draining_generation:self.connect": "drain fence is a non-deadline caller by design",
    "gateway/generation.py:fence_draining_generation:BEGIN IMMEDIATE": "drain fence is a non-deadline caller by design",
    "gateway/generation.py:project_stopped_summary:self.connect": "legacy projection is a non-deadline caller by design",
    "gateway/generation.py:project_stopped_summary:BEGIN IMMEDIATE": "legacy projection is a non-deadline caller by design",
    "gateway/generation.py:release_lease:self.connect": "ordinary release is a non-deadline caller by design",
    "gateway/generation.py:release_lease:BEGIN IMMEDIATE": "ordinary release is a non-deadline caller by design",
    "gateway/generation.py:generations:self.connect": "ordinary read is a non-deadline caller by design",
    "gateway/generation.py:leases:self.connect": "ordinary read is a non-deadline caller by design",
    "gateway/generation.py:remove_generation_files:sqlite3.connect": "read-only generation cleanup is outside a bounded window",
    "gateway/run_generation.py:_bootout_retired_generation:_gateway_domain": "legacy cold-start retirement is outside a bounded handover window",
    "gateway/run_generation.py:_bootout_retired_generation:_launch_state": "legacy cold-start retirement is outside a bounded handover window",
    "gateway/run_generation.py:_claim_legacy_process_generation:coordinator.connect": "legacy gateway claim is a non-deadline caller by design",
    "gateway/run_generation.py:_claim_legacy_process_generation:BEGIN IMMEDIATE": "legacy gateway claim is a non-deadline caller by design",
    "gateway/run_generation.py:_drain_after_transfer:asyncio.sleep": "drain watchdog sleep is outside the driver's bounded commit window",
    "gateway/run_generation.py:_generation_request:sock.connect": "socket timeout is installed on the socket before connect",
    "gateway/run_generation.py:_generation_request:sock.recv": "socket timeout is installed on the socket before recv",
    "gateway/run_generation.py:_generation_request:sock.sendall": "socket timeout is installed on the socket before send",
    "gateway/run_generation.py:_heartbeat:asyncio.sleep": "periodic heartbeat sleep is not a bounded handover operation",
    "gateway/run_generation.py:_socket_accepts_connections:probe.connect": "readiness probe socket has a caller-owned subprocess boundary",
    "gateway/run_generation.py:handover_to_generation:time.sleep": "post-commit proof sleep is inside the enclosing deadline loop",
    "gateway/run_generation.py:read_owner:self.coordinator.connect": "ordinary runtime read is a non-deadline caller by design",
    "gateway/run_generation.py:read_pending_work:self.coordinator.connect": "ordinary runtime read is a non-deadline caller by design",
    "gateway/run_generation.py:read_transfer:self.coordinator.connect": "ordinary runtime read is a non-deadline caller by design",
    "gateway/run_generation.py:serve_standby_generation:stop.wait": "standby service lifetime wait is not a bounded handover operation",
    "gateway/run_generation.py:take_over_legacy_gateway_resources:asyncio.sleep": "legacy takeover polling uses its own cooperative cap",
    "hermes_cli/gateway_forward_update.py:_await_wedge_proof:_sleep": "wedge proof sleep is inside its explicit stale-proof deadline",
    "hermes_cli/gateway_forward_update.py:_fresh_input:GenerationCoordinator().connect": "ordinary outbox read is a non-deadline caller by design",
    "hermes_cli/gateway_forward_update.py:_fresh_reply:db.connect": "ordinary outbox read is a non-deadline caller by design",
    "hermes_cli/gateway_forward_update.py:_frozen_transfer_tokens:db.connect": "ordinary transfer read is a non-deadline caller by design",
    "hermes_cli/gateway_forward_update.py:_refuse:db.connect": "refusal bookkeeping is a non-deadline caller by design",
    "hermes_cli/gateway_forward_update.py:_refuse:BEGIN IMMEDIATE": "refusal bookkeeping is a non-deadline caller by design",
    "hermes_cli/gateway_forward_update.py:_sleep:time.sleep": "helper is used by bounded callers and carries their computed remaining duration",
    "hermes_cli/gateway_guardian.py:_supervised_pid:_launchctl_supervised_pid": "compatibility fallback supports older injected test doubles",
    "hermes_cli/gateway_guardian.py:cli:_domain": "legacy guardian install/uninstall path is outside repair windows",
    "hermes_cli/gateway_guardian.py:cli:_launch_state": "legacy guardian install/uninstall path is outside repair windows",
    "hermes_cli/gateway_launchd.py:_retry_launchctl_bootstrap_until_registered:_gw()._launchctl_label_supervising_process": "legacy install/start retry preserves its byte-identical registration invariant",
    "hermes_cli/gateway_launchd.py:_retry_launchctl_bootstrap_until_registered:time.sleep": "legacy install/start retry preserves its byte-identical retry interval",
}


def _callee(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_callee(node.value)}.{node.attr}".strip(".")
    if isinstance(node, ast.BoolOp):
        # Injectable subprocess runners use (runner or subprocess.run)(...).
        return "runner"
    if isinstance(node, ast.Call):
        return _callee(node.func) + "()"
    return ""


def _blocking(call):
    callee = _callee(call.func)
    if callee == "asyncio.to_thread" and call.args:
        callee = _callee(call.args[0])
    leaf = callee.rsplit(".", 1)[-1]
    if leaf == "execute" and call.args and isinstance(call.args[0], ast.Constant):
        if isinstance(call.args[0].value, str) and call.args[0].value.strip().upper() == "BEGIN IMMEDIATE":
            return "BEGIN IMMEDIATE"
    if (leaf in {"connect", "_deadline_connect", "_begin_immediate", "_launch_state",
                 "_gateway_domain", "_domain", "_generation_request", "polling_status",
                 "request", "sleep", "_sleep", "wait", "wait_procs", "recv", "sendall",
                 "abort_transfer", "record_poller_stopped", "transfer_attempt_nonce"}
            or leaf.startswith("_launchctl_")
            or callee in {"subprocess.run", "runner", "self.runner", "launchctl", "launchctl_runner"}
            or (leaf in {"request", "get", "post"} and
                (callee.startswith("requests.") or callee.startswith("httpx.") or
                 callee.startswith("client.") or callee.startswith("session.")))):
        return callee
    return None


def _budgeted(call, callee, assignments):
    # Dynamic kwargs helpers still make the caller's deadline dependency visible
    # in the AST, while preserving compatibility with deadline-less test doubles.
    if any(isinstance(node, ast.Name) and node.id == "deadline" for node in ast.walk(call)):
        return True
    if any(k.arg and ("timeout" in k.arg or "deadline" in k.arg) and
           not (isinstance(k.value, ast.Constant) and k.value.value is None)
           for k in call.keywords):
        return True
    if callee.rsplit(".", 1)[-1] not in {"sleep", "_sleep", "_deadline_connect", "_begin_immediate"}:
        return False
    # Deadlines and sleep durations are positional. Follow simple local names
    # (_sleep = min(... remaining ...)), not arbitrary interprocedural flow.
    def has_budget(node, seen=frozenset()):
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if any(part in child.id for part in ("deadline", "remaining", "budget")):
                    return True
                if child.id not in seen and child.id in assignments:
                    if has_budget(assignments[child.id], seen | {child.id}):
                        return True
        return False
    return any(has_budget(arg) for arg in call.args)


def inventory(root=ROOT):
    sites = []
    for module in MODULES:
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        def visit(node, function="<module>"):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
                if module.endswith("gateway_launchd.py") and function not in LAUNCHD_FUNCTIONS:
                    return
            # Resolve only assignments in this lexical function, excluding nested
            # functions. A new wrapper remains a separately audited blocking site.
            assignments = {}
            def local(n):
                if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return
                if isinstance(n, ast.Assign):
                    for target in n.targets:
                        if isinstance(target, ast.Name):
                            assignments[target.id] = n.value
                for child in ast.iter_child_nodes(n):
                    local(child)
            local(node)
            def walk(n):
                if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(n, function)
                    return
                if isinstance(n, ast.Call):
                    callee = _blocking(n)
                    if callee:
                        sites.append((f"{module}:{function}:{callee}", n.lineno,
                                      _budgeted(n, callee, assignments)))
                for child in ast.iter_child_nodes(n):
                    walk(child)
            walk(node)
        visit(tree)
    return sites


def test_blocking_sites_have_budget_or_explicit_exemption():
    sites = inventory()
    missing = [f"{key}:{line}" for key, line, bounded in sites if not bounded and key not in EXEMPT]
    assert not missing, "Unbudgeted blocking calls:\n" + "\n".join(sorted(missing))
    assert all(reason.strip() and "\n" not in reason for reason in EXEMPT.values())
    unbudgeted = {key for key, _, bounded in sites if not bounded}
    assert not (EXEMPT.keys() - unbudgeted), "Remove stale exemptions: " + str(sorted(EXEMPT.keys() - unbudgeted))


def test_detector_covers_injected_runners_and_threaded_coordinator_calls():
    for expression in (
        "self.connect()", "db.connect()", "conn.execute('BEGIN IMMEDIATE')",
        "subprocess.run([])", "(runner or subprocess.run)([])", "self.runner([])",
        "_launch_state('d', 'l')", "_gateway_domain('l', None)", "self._domain('l')",
        "_launchctl_bootstrap('d', 'p', 'l')", "supervisor.request({}, 'polling_status')",
        "sock.connect('x')", "sock.recv(1)", "time.sleep(1)", "p.wait()", "psutil.wait_procs([])",
        "asyncio.to_thread(self.coordinator.record_poller_stopped, 'a', 1, 't', 0)",
        "asyncio.to_thread(self.coordinator.abort_transfer, 'a', 'b', 1)",
    ):
        call = ast.parse(expression, mode="eval").body
        assert _blocking(call), expression
        assert not _budgeted(call, _blocking(call), {}), expression
