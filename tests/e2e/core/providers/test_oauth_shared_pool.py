"""Two real agent processes share one auth.json pool row and hit its expiry at the same moment.

Both processes load the same Anthropic OAuth pool row (single-use refresh
token) and both get the vendor's 401 for the expired access token. The first to
refresh holds the cross-process auth-store lock while the (slow) vendor token
endpoint answers; the second must wait for it, re-read the rotated pair from
auth.json and adopt it instead of presenting the refresh token that was just
spent. Expected: exactly one refresh grant, no ``invalid_grant``, both turns
answered with the new bearer, and the refresh token on disk still live.

Everything is real Hermes (pool, persistence, locking, the Anthropic OAuth
refresh over TLS through the intercepting proxy); only the vendor endpoints are
loopback fakes. The single-process variant of the stale-writer bug (#120815, fixed)
is pinned by test_oauth_anthropic_refresh.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from tests.e2e.core.providers._oauth_helpers import (
    EXPIRED,
    OLD_ACCESS,
    Hold,
    credential,
    start_anthropic_rig,
    text,
    wait_until,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="tagged-tree cleanup uses /proc")

SLOW_TOKEN_ENDPOINT_S = 2.0  # vendor latency window while both processes contend, not synchronization


# This property is the auth-store lock across processes, not CLI transport sharing.
# A -z client now correctly refuses a sibling's finite gateway on the same home;
# a shared daemon would silently turn this into a same-process lock test instead.
# Run the production provider resolver + AIAgent in two explicit child processes.
POOL_AGENT = r'''
import os, sys
from hermes_cli.config import load_config
from hermes_cli.runtime_provider import resolve_runtime_provider
from run_agent import AIAgent
cfg = load_config()["model"]
runtime = resolve_runtime_provider(requested=cfg["provider"], target_model=cfg["default"])
assert runtime.get("credential_pool") is not None, "must exercise the real OAuth credential pool"
agent = AIAgent(model=cfg["default"], provider=runtime["provider"],
                api_key=runtime["api_key"], base_url=runtime["base_url"],
                api_mode=runtime["api_mode"], credential_pool=runtime["credential_pool"],
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                skip_background_review=True)
try:
    print(f"POOL_AGENT_PID={os.getpid()}", flush=True)
    result = agent.run_conversation(sys.argv[1])
    print(result.get("final_response") or "", flush=True)
    code = 2 if result.get("failed") else 0
finally:
    agent.close()
raise SystemExit(code)
'''


def run_pool_agent(fh, prompt, child_env):
    return subprocess.run([sys.executable, "-c", POOL_AGENT, prompt], env=fh.env(child_env),
                          cwd=fh.root, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", timeout=150, check=False)


def _who(record: dict) -> str:
    return "A" if "PROCESS-ALPHA" in str(record["body"].get("messages")) else "B"


def test_concurrent_refresh_across_processes_spends_the_token_once(tmp_path) -> None:
    b_arrived, both_rejected = threading.Event(), threading.Event()
    rejected: list[str] = []
    lock = threading.Lock()

    def reject(who: str):
        with lock:
            rejected.append(who)
            if len(rejected) >= 2:
                both_rejected.set()
        return EXPIRED

    def decide(record: dict):
        who, bearer = _who(record), credential(record)
        if bearer != OLD_ACCESS:
            return text(f"ANSWER-{who}")
        if who == "B":
            b_arrived.set()
            return lambda: reject("B")
        # A's 401 is released only once B's expired call is in, so both hit the expiry together.
        return Hold(b_arrived, lambda: reject("A"))

    rig = start_anthropic_rig(tmp_path, decide, title_generation=False)

    def slow_token_endpoint(_grant) -> None:
        both_rejected.wait(60)
        threading.Event().wait(SLOW_TOKEN_ENDPOINT_S)

    rig.tokens.before_refresh_response = slow_token_endpoint
    box: dict = {}
    thread_a = threading.Thread(target=lambda: box.setdefault("a", run_pool_agent(
        rig.fh, "PROCESS-ALPHA say hi", rig.child_env)), daemon=True)
    try:
        thread_a.start()
        wait_until(lambda: any(_who(r) == "A" for r in rig.messages.main_requests()), 90, "process A's first call")
        proc_b = run_pool_agent(rig.fh, "PROCESS-BRAVO say hi", rig.child_env)
        thread_a.join(150)
    finally:
        for ev in (b_arrived, both_rejected):
            ev.set()
        rig.stop()
    proc_a = box.get("a")
    assert proc_a is not None, "process A did not finish"

    def show(name: str, p) -> str:
        return f"{name}: exit={p.returncode}\nstdout:\n{p.stdout[-1200:]}\nstderr:\n{p.stderr[-1200:]}"

    grants = rig.tokens.refresh_grants()
    summary = [(g.presented, g.status, g.error) for g in grants]
    assert sorted(rejected) == ["A", "B"], f"setup: both processes must see the 401 (saw {rejected})"
    assert not rig.messages.schema_errors(), f"request bodies violate the SDK schema: {rig.messages.schema_errors()}"
    assert [g.status for g in grants] == [200], (
        f"the single-use refresh token must be spent exactly once across processes; grants={summary}\n"
        f"{show('A', proc_a)}\n{show('B', proc_b)}")
    new_access, new_refresh = grants[0].issued_access_token, grants[0].issued_refresh_token
    for name, proc in (("A", proc_a), ("B", proc_b)):
        assert proc.returncode == 0 and f"ANSWER-{name}" in proc.stdout, show(name, proc)
    child_pids = [int(line.split("=", 1)[1]) for proc in (proc_a, proc_b)
                  for line in proc.stdout.splitlines() if line.startswith("POOL_AGENT_PID=")]
    assert len(child_pids) == 2 and len(set(child_pids)) == 2
    assert os.getpid() not in child_pids, "rotation must race two independent agent processes"
    retried = {_who(r): credential(r) for r in rig.messages.main_requests() if credential(r) != OLD_ACCESS}
    assert retried == {"A": new_access, "B": new_access}, f"retries did not use the rotated bearer: {retried}"
    row = rig.pool_row("e1")
    assert row.get("refresh_token") == new_refresh and rig.tokens.is_live(new_refresh), (
        f"auth.json does not hold the live rotated refresh token: disk={row.get('refresh_token')!r}")