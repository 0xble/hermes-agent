"""Ordinary immutable update and marker-bearing in-band restart acceptance.

Only the top-level operator runs this module through scripts/zero_cut_drills.py.
Fakes exercise its contract but can never produce a native acceptance receipt.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
import uuid

from tests.drills.zero_cut_handover import harness as h
from tests.drills.zero_cut_handover import native_witnesses as w


NATIVE_ROUTES = frozenset({"update", "request_restart", "slash_restart"})

def validate_plan(plan):
    passes = plan.get("passes", [])
    if len(passes) != 3:
        raise ValueError("one native suite requires exactly 3 distinct route scenarios")
    if not plan.get("reservation") or not Path(plan.get("source", "")).is_absolute():
        raise ValueError("parent reservation and absolute source checkout required")
    topics = plan.get("topics", {})
    if set(topics) != {"work", "approval", "control", "admission"} or len(set(topics.values())) != 4 or not all(isinstance(t, int) and t > 0 for t in topics.values()):
        raise ValueError("4 distinct parent-prepared Telegram topic IDs required")
    previous = None
    for update in passes:
        if not all(re.fullmatch(r"[0-9a-f]{40}", update.get(k, "")) for k in ("before", "after")):
            raise ValueError("literal full source/release SHA required")
        route = update.get("route")
        if route not in NATIVE_ROUTES:
            raise ValueError("unavailable native route")
        if (route == "update") == (update["before"] == update["after"]):
            raise ValueError("update needs a genuine revision change, restart must preserve its release")
        if previous is not None and update["before"] != previous:
            raise ValueError("cycle release continuity mismatch")
        if route == "update" and not update.get("branch"):
            raise ValueError("ordinary update branch required")
        previous = update["after"]
    routes = {item["route"] for item in passes}
    if routes != NATIVE_ROUTES:
        raise ValueError("ordinary update, external request_restart and built-in /restart routes required")


def native_pass(result):
    return bool(result.get("passed") and result.get("complete") and result.get("native") and not result.get("xfail_reason"))


def accepted(results):
    """One complete route suite, not the 3 consecutive suites required before live."""
    routes = {result.get("route") for result in results}
    return (len(results) == 3 and routes == NATIVE_ROUTES
            and all(native_pass(result) for result in results))


def send_once(send, peer, message):
    result = send(peer, message)
    mid = h._message_id(result)
    if mid is None:
        raise RuntimeError("ambiguous Telegram send, not retried")
    return mid


def trigger(route, effects, peer, source, branch, token):
    if route == "update":
        # Execute the clean update authority's interpreter/module, not this dirty
        # candidate checkout, and do not bypass the ordinary updater's restart.
        return effects.run_command([str(source / ".venv" / "bin" / "python"), "-m", "hermes_cli.main",
                                    "update", "--yes", "--branch", branch], cwd=source,
                                   env={"HERMES_UPDATE_CORRELATION_ID": token}, background=True)
    if route == "slash_restart":
        return send_once(effects.send_as_user, peer, "/restart")
    if route == "request_restart":
        return send_once(effects.send_as_user, peer,
                         f"Call the installed external request_restart tool exactly once with reason '{token}'. "
                         "Do not use built-in /restart, shell signals, launchctl, gateway handover, or another route. "
                         "If the tool is absent or refuses, report that and stop. Do not retry an ambiguous result.")
    raise ValueError("unavailable native route")


def worker_command(release, directory, token, kind, seconds=0):
    return shlex.join([str(release / ".venv/bin/python"), str(Path(__file__).with_name("native_work.py")),
                       "--directory", str(directory), "--token", token, "--kind", kind, "--seconds", str(seconds)])


def arm_prompt(release, directory, token):
    commands = {k: worker_command(release, directory, token, k, 150 if k == "long" else 300 if k in {"background", "delegation"} else 0)
                for k in w.KINDS}
    return (f"Isolated native continuity experiment {token}. Do not restart/update/install anything. "
            "Never retry commands/effects. Use real native tools, not fabricated evidence. "
            f"First start terminal background=true notify=true command {commands['background']!r}. "
            f"Dispatch 1 background delegate whose task is to run {commands['delegation']!r} in a foreground terminal and return RESULT {token} delegation. "
            "Create a standing goal, a bounded 1-tick loop at 5m, and a one-shot cron due in 5m, each naming this token. "
            f"The goal must wait for the long tool to finish, then run {commands['goal']!r} and finish. "
            f"The loop must run {commands['loop']!r}, report RESULT {token} loop and end with LOOP_COMPLETE. "
            f"The cron must run {commands['cron']!r} once and deliver RESULT {token} cron to this topic. "
            f"Start the foreground terminal {commands['long']!r} with timeout >=180, not background. "
            f"After it returns report RESULT {token} long. Upon background/delegate completion report RESULT {token} background and RESULT {token} delegation exactly once. "
            f"Every real goal/loop/cron result must report its own RESULT {token} <kind> exactly once. "
            "Do not print a result until the corresponding native worker actually finished. Clean up your finite goal/loop/cron after results.")


def wait_for(effects, predicate, timeout, description):
    deadline = effects.clock() + timeout
    for _ in range(max(1, int(timeout / 2) + 1)):
        result = predicate()
        if result:
            return result
        if effects.clock() >= deadline:
            break
        effects.sleep(2)
    raise RuntimeError(f"incomplete witness: {description}")


class MarkerWatch:
    """A fast read-only watcher avoids missing the short-lived initiating marker."""
    def __init__(self, home):
        self.home, self.seen, self.stop = home, {}, threading.Event()
        self.baseline = {name: self.signature(name) for name in (".restart_notify.json", ".restart_last_processed.json")}
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def signature(self, name):
        try:
            path = self.home / name
            return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        except FileNotFoundError:
            return None

    def watch(self):
        while not self.stop.wait(0.02):
            for name, baseline in self.baseline.items():
                value = self.signature(name)
                if value and value != baseline:
                    self.seen[name] = value

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)


def preflight(effects, home, source, update):
    """No mutation if source/release/fleet identity is unknown or foreign."""
    def command(*argv):
        result = h._completed(effects.run_command(list(argv), cwd=source))
        if result.returncode:
            raise ValueError("native preflight command failed")
        return result.stdout.strip()
    if command("git", "status", "--porcelain"):
        raise ValueError("native update authority must be clean, no auto-stash allowed")
    if command("git", "rev-parse", "HEAD") != update["before"]:
        raise ValueError("source checkout is not the expected pre-update SHA")
    if command("git", "rev-parse", f"origin/{update['branch']}") != update["after"]:
        raise ValueError("qualified ordinary update branch target is not exact")
    command("git", "merge-base", "--is-ancestor", update["before"], update["after"])
    if not command("git", "diff", "--stat", update["before"], update["after"]):
        raise ValueError("empty revision exposure is not native update acceptance")
    script = ("import json;from dataclasses import asdict;"
              "from hermes_cli.update_inventory import collect_runtime_inventory;"
              "from hermes_cli.update_receipt import _profile_homes;"
              "print(json.dumps({'plan':asdict(collect_runtime_inventory()),'homes':[str(p.resolve()) for _,p in _profile_homes()]}))")
    inventory = json.loads(command(str(source / ".venv/bin/python"), "-c", script))
    if inventory["homes"] != [str(home.resolve())] or not inventory["plan"].get("runtimes"):
        raise ValueError("ordinary updater fleet is not exclusively the reserved test home")
    # A quiet/non-serving second slot is required, not a third slot workaround.
    snap = {"owners": [identity for slot in ("a", "b")
                       if (identity := h._control_query(effects, home, "identify", slot))]}
    if len(snap["owners"]) != 1:
        raise ValueError("occupied or unknown second slot")
    current = (home / "current").resolve(strict=True)
    if current != (home / "releases" / update["before"]).resolve(strict=True):
        raise ValueError("wrong immutable current release")
    return inventory


def run_step(home, peer, effects, plan, directory, route, update, *, native=False, snapshot=w.native_snapshot):
    token = uuid.uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    topics = plan["topics"]
    # A topic-aware effect has a peer argument for compatibility with the old harness.
    def topic_fx(name):
        return replace(effects, send_as_user=lambda p, s: effects.send_as_user(f"{p}:{topics[name]}", s))
    work_fx = topic_fx("work")
    control_fx = topic_fx("control")
    old_sha = update["before"] if route == "update" else update["after"]
    expected_sha = update["after"]
    release = home / "releases" / old_sha
    sends = []
    def get():
        snap = snapshot(home, directory, token, effects, peer)
        (directory / "latest-evidence.json").write_text(json.dumps(w.evidence(snap, token), indent=2) + "\n")
        return snap
    before = get()
    owner = w.active_owner(before, old_sha, home)
    if len(before["owners"]) != 1:
        raise ValueError("occupied second native slot")
    sends.append(send_once(work_fx.send_as_user, peer, arm_prompt(release, directory, token)))
    approval = plan["approval_command"]
    approval_command = approval.replace("{worker}", worker_command(release, directory, token, "approval"))
    sends.append(send_once(topic_fx("approval").send_as_user, peer,
                           f"Native approval experiment {token}. Run this command once through terminal, wait for approval, then report RESULT {token} approval only after success: {approval_command}"))
    def ready():
        snap = get()
        checks = w.armed(snap, token, owner)
        (directory / "armed.json").write_text(json.dumps(checks, indent=2) + "\n")
        return snap if all(checks.values()) else None
    boundary = wait_for(effects, ready, 180, "mixed work genuinely armed")
    queued_text = (f"/queue After original work, run {worker_command(release, directory, token, 'followup')} once and report RESULT {token} followup. Do not redirect the live tool.")
    sends.append(send_once(work_fx.send_as_user, peer, queued_text))
    def queued():
        snap = get()
        queue_ack = plan.get("queue_receipt", "Queued")
        observed = w.messages(snap.get("chat"))
        return snap if w.event_pair(snap, token, "long") and any(
            m.get("sender_id") == h.BOT_USER_ID and int(m.get("id", 0)) > int(sends[-1])
            and queue_ack in str(m.get("text", "")) for m in observed) else None
    wait_for(effects, queued, 30, "native /queue admission while original tool remains in flight")
    db_sends = list(sends)
    process = None
    with MarkerWatch(home) as marker:
        result = trigger(route, control_fx, peer, Path(plan["source"]), update.get("branch", ""), token)
        if route == "update":
            process = result
        else:
            sends.append(result)
            if route == "request_restart":
                db_sends.append(result)
        (directory / "operation.json").write_text(json.dumps({"route": route, "token": token,
            "pid": getattr(process, "pid", None), "message_id": None if route == "update" else result}) + "\n")
        def promoted():
            snap = get()
            try:
                successor = w.active_owner(snap, expected_sha, home)
            except ValueError:
                return None
            return (snap, successor) if successor["pid"] != owner["pid"] else None
        transition, successor = wait_for(effects, promoted, 240, "successor ownership")
        (directory / "transition-evidence.json").write_text(json.dumps(w.evidence(transition, token), indent=2) + "\n")
        # A 150s prompt or already-finished tool is not an in-flight witness.
        if not all(w.event_pair(transition, token, kind) for kind in ("long", "background", "delegation")):
            raise RuntimeError("150-second tool was not genuinely in flight at promotion")
        original = [o for o in transition["owners"] if o.get("pid") == owner["pid"]]
        if len(original) != 1 or original[0].get("code_sha") != old_sha or not original[0].get("handover", {}).get("token_lock_released"):
            raise RuntimeError("predecessor ownership/release not preserved at transition")
        sends.append(send_once(topic_fx("admission").send_as_user, peer,
                               f"Run {worker_command(home / 'releases' / expected_sha, directory, token, 'admission')} once and report RESULT {token} admission."))
        db_sends.append(sends[-1])
        sends.append(send_once(topic_fx("approval").send_as_user, peer, "/approve"))
        def finished():
            snap = get()
            checks = w.completed(snap, token, owner, successor, sends, db_sends=db_sends)
            (directory / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
            return (snap, checks) if all(checks.values()) else None
        final, checks = wait_for(effects, finished, 540, "exactly-once native effects, results and deliveries")
        def retired():
            snap = get()
            return snap if len(snap["owners"]) == 1 and snap["owners"][0]["pid"] == successor["pid"] else None
        final = wait_for(effects, retired, 60, "predecessor retirement after all owned work")
    if route != "update":
        checks["restart_marker"] = ".restart_notify.json" in marker.seen and not (home / ".restart_notify.json").exists()
        from agent.i18n import t
        receipt_text = plan.get("restart_receipt") or t("gateway.startup.restarted")
        checks["restart_delivery_once"] = sum(m.get("sender_id") == h.BOT_USER_ID and str(m.get("text", "")).strip() == receipt_text
                                                and int(m.get("id", 0)) > min(int(mid) for mid in sends)
                                                for m in w.messages(final.get("chat"))) == 1
        if route == "request_restart":
            checks["external_tool_route"] = w.restart_tool_executed(final, token)
        else:
            checks["slash_marker"] = ".restart_last_processed.json" in marker.seen
    if process is not None:
        checks["ordinary_update_exit"] = process.poll() == 0
        receipts = []
        for path in (home / "logs/update_receipts").glob("update_*.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("correlation_id") == token:
                receipts.append(receipt)
        checks["ordinary_update_receipt"] = len(receipts) == 1 and receipts[0].get("outcome") == "success" and receipts[0].get("pre_update", {}).get("sha") == old_sha and receipts[0].get("post_update", {}).get("sha") == expected_sha
    checks["retirement"] = len(final["owners"]) == 1 and final["owners"][0]["pid"] == successor["pid"]
    result = {"route": route, "token": token, "native": native, "complete": all(checks.values()),
              "passed": all(checks.values()), "checks": checks, "before": owner["code_sha"],
              "after": successor["code_sha"], "predecessor_pid": owner["pid"], "successor_pid": successor["pid"],
              "message_ids": sends, "armed_checks": w.armed(boundary, token, owner),
              "effect_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.glob(f"{token}-*.jsonl")}}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run_native(*, home, peer, plan, output, effects=None, live=False):
    validate_plan(plan)
    if not live or os.environ.get("ZERO_CUT_DRILLS_NATIVE_PARENT") != "1":
        raise ValueError("native route is top-level parent-only, explicitly opt in")
    if "{worker}" not in plan.get("approval_command", ""):
        raise ValueError("parent-qualified real approval command template required")
    native_execution = effects is None
    effects = effects or native_effects(home)
    h.safety_guard(home, peer, effects, peer_resolver=lambda fx, target: h.BOT_USERNAME
                   if str(target).lstrip("@").lower() in {str(h.BOT_USER_ID), h.BOT_USERNAME}
                   and any(m.get("sender_id") == h.BOT_USER_ID for m in w.messages(fx.read_chat(target, 20))) else None)
    if output.exists():
        raise ValueError("native artifact directory already exists, never replay a partial run")
    results = []
    for number, item in enumerate(plan["passes"], 1):
        route = item["route"]
        directory = output / str(number) / route
        try:
            if route == "update":
                preflight(effects, home, Path(plan["source"]), item)
            result = run_step(home, peer, effects, plan, directory, route, item, native=native_execution)
        except Exception as exc:
            result = {"route": route, "passed": False, "complete": False, "native": native_execution,
                      "error_type": type(exc).__name__, "reason": str(exc),
                      "recovery": "Reconcile existing effects. Do not retry automatically."}
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        results.append(result)
        if not native_pass(result):
            return {"passed": False, "passes": results}
    return {"passed": accepted(results), "passes": results}


def native_effects(home):
    from tools.environments.local import served_profile_child_env
    base = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG") if k in os.environ}
    env = served_profile_child_env(base=base, target_home=home, inherit_credentials=True)
    env.pop("PYTHONPATH", None)
    def run(argv, *, env=None, background=False, cwd=None):
        child = {**environment, **(env or {})}
        if background:
            process = subprocess.Popen(argv, cwd=cwd, env=child, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return process
        return subprocess.run(argv, cwd=cwd, env=child, check=False, capture_output=True, text=True)
    environment = env
    observer_env = served_profile_child_env(base=base, target_home=home, inherit_credentials=False)
    real = h.real_effects(home)
    def send(peer, message):
        target, _, topic = peer.partition(":")
        argv = ["--agent", "--yes", "send", target, message, "--silent"]
        if topic:
            argv.extend(["--topic", topic])
        # No retries, including FLOOD_WAIT: a send can be ambiguous.
        response = subprocess.run([h.TELEGRAM, *argv], check=False, capture_output=True, text=True, env=observer_env)
        return h._json_or_text(response.stdout) if response.returncode == 0 else {"error": "ambiguous send"}
    def read_chat(peer, limit):
        response = subprocess.run([h.TELEGRAM, "--agent", "messages", peer, "--limit", str(limit), "--raw-text"],
                                  check=False, capture_output=True, text=True, env=observer_env)
        if response.returncode:
            raise RuntimeError("Telegram observer unavailable, no continuity conclusion")
        return h._json_or_text(response.stdout)
    def launchctl(argv):
        return subprocess.run(["launchctl", *argv], check=False, capture_output=True, text=True, env=observer_env)
    return replace(real, run_command=run, send_as_user=send, read_chat=read_chat,
                   launchctl=launchctl, read_state_db=w.read_db)
