"""Once ``hermes update`` has committed the new code, the update never fails (contract C3).

One real HEAD install (``_git_world``: the real ``scripts/install.sh`` over real smart HTTP, every
command in a ``bwrap`` sandbox) is shared by the cells. Each cell publishes a hostile upstream
release and breaks something that runs AFTER the checkout moved, for real:

(a) the web UI build fails: the release's own ``scripts/build/web.mjs`` exits 1 while
    ``$HOME/.e2e-break-web-build`` exists, and the release changes a web build input (``web.mjs``
    itself lives under ``scripts/build``, plus a new file under ``web/``) so ``freshness.mjs`` reports
    the web UI stale and the updater must build it. A config migration is owed too
    (``_config_version`` lowered by one): one failed step must not skip later independent ones.
(b) a gateway that refuses to restart: a credential-free, platform-less (cron-only) ``hermes
    gateway run`` lives in the same PID namespace as the updater (``handoff._nshost``). The release
    makes ``hermes_cli.gateway.run_gateway`` exit 1 before booting while
    ``$HOME/.e2e-break-gateway-boot`` exists. The updater drains the OLD gateway (pre-update code,
    no fault) and its relaunch boots the NEW code, which refuses to start: the restart fails for real,
    no gateway serves the new commit. Removing the toggle and updating again must retry the owed
    restart.
(c) SIGKILL during the post-commit tail: the release's ``web.mjs`` writes
    ``$HOME/.e2e-web-build-started`` and sleeps 120 s while ``$HOME/.e2e-slow-web-build`` exists. The
    update runs in the shared namespace; once the slow build started, every process born after the
    update was spawned is SIGSTOPped then SIGKILLed (that is the whole update tree, including a
    completion child in its own session or reparented to the namespace init).
(d) a sticky profile (``hermes profile use e2epost``) must not move update.log / update receipts off
    the root hermes home, and ``hermes logs update`` under that profile reads the root's update.log;
    pm's own sync receipts stay where pm writes them and ``hermes pm status`` there still shows them.
(e) the dependency sync fails after the tree moved (contract amendment A6): the release's
    ``pm.client.ensure_tools_for_sync`` raises while ``$HOME/.e2e-break-deps-sync`` exists; it runs in
    the completion bootstrap from the NEW tree, after the checkout moved. Exit 0, a ``dependencies``
    follow-up, the tail obligation armed; the next update completes it.
(f) Ctrl-C after the commit point: SIGINT (what a terminal's Ctrl-C delivers to the foreground
    ``hermes update``; the completion child runs in its own session) while the slow web build runs.
    The user must read that the new code is in place and the rest is owed, never a failure.

What is asserted is what the user sees: the exit code, ``⚠`` lines, the receipt at
``<root>/logs/update_receipts/latest.json`` (``outcome``, ``followups``), the
``source-completion-pending`` obligation in the install-state dir, the config version, the
owed-restart warning a later command prints, and which gateway (by its control-socket ``identify``)
serves the checkout.
"""

from __future__ import annotations

from tests.e2e.core.upgrade.git._hostile_diagnostics import phase

import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

import hermes_yaml as yaml
from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _git_world as G
from tests.e2e.core.upgrade.handoff._nshost import NamespaceHost

pytestmark = G.PYTESTMARK

WARN = "⚠"
BREAK_WEB = ".e2e-break-web-build"
SLOW_WEB = ".e2e-slow-web-build"
WEB_STARTED = ".e2e-web-build-started"
BREAK_GATEWAY = ".e2e-break-gateway-boot"
BREAK_DEPS = ".e2e-break-deps-sync"
TOGGLES = (BREAK_WEB, SLOW_WEB, WEB_STARTED, BREAK_GATEWAY, BREAK_DEPS)
DEPS_FAULT_TEXT = "e2e: dependency sync fails on purpose"
DEPS_OWED = "dependencies not installed yet"  # update_completion._settle_after_commit
INTERRUPTED_AFTER_COMMIT = "Interrupted after the code was updated"  # update_receipt
WEB_FAULT_TEXT = "e2e: the web build fails on purpose"
GATEWAY_FAULT_TEXT = "e2e: gateway boot refused on purpose"
OWED_RESTART = "Could not restart every paused gateway"  # update_pause_record._resume_fenced
PROFILE = "e2epost"
UPDATE_TIMEOUT = 1500
_WEB_MARK = "// e2e (test_hostile_post): hostile build toggles"
_GATEWAY_MARK = "# e2e (test_hostile_post): hostile gateway boot toggle"
_DEPS_MARK = "# e2e (test_hostile_post): hostile dependency sync toggle"

_HOSTILE_WEB = f"""{_WEB_MARK}, keyed on files in $HOME.
import {{ existsSync as e2eExists, writeFileSync as e2eWrite }} from 'node:fs'
import {{ homedir as e2eHomedir }} from 'node:os'
const e2eHomes = [...new Set([process.env.HOME, e2eHomedir()].filter(Boolean))]
const e2eToggle = name => e2eHomes.some(home => e2eExists(`${{home}}/${{name}}`))
if (e2eToggle('{BREAK_WEB}')) {{
  console.error('{WEB_FAULT_TEXT} ({BREAK_WEB})')
  process.exit(1)
}}
if (e2eToggle('{SLOW_WEB}')) {{
  for (const home of e2eHomes) {{ try {{ e2eWrite(`${{home}}/{WEB_STARTED}`, String(process.pid)) }} catch {{}} }}
  console.log('e2e: slow web build started ({SLOW_WEB})')
  await new Promise(resolve => setTimeout(resolve, 120000))
}}
"""

_HOSTILE_GATEWAY = f"""    {_GATEWAY_MARK}
    if __import__("os").path.exists(__import__("os").path.join(__import__("os").path.expanduser("~"), "{BREAK_GATEWAY}")):
        print("{GATEWAY_FAULT_TEXT} ({BREAK_GATEWAY})", file=__import__("sys").stderr, flush=True)
        raise SystemExit(1)
"""

_HOSTILE_DEPS = f"""    {_DEPS_MARK}
    if __import__("os").path.exists(__import__("os").path.join(__import__("os").path.expanduser("~"), "{BREAK_DEPS}")):
        raise RuntimeError("{DEPS_FAULT_TEXT} ({BREAK_DEPS})")
"""


# -- the shared install --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def w(hostile_diagnostics) -> Iterator[G.World]:
    """A SHORT root (the gateway's AF_UNIX control socket lives under ``$HERMES_HOME``; pytest's deep
    ``tmp_path`` overflows ``sun_path``). Set ``HERMES_E2E_KEEP_ROOT=1`` to keep it for a post-mortem."""
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    root = Path(tempfile.mkdtemp(prefix="hp", dir=base))
    phase("module install start", root / "ns-agent.stderr")
    try:
        with G.world(root, base=I.head_sha()) as world:
            phase("module install complete", root / "ns-agent.stderr",
                  world.sb.hermes_home / "logs" / "update.log",
                  world.sb.hermes_home / "logs" / "update_receipts" / "latest.json",
                  world.sb.root / "update-c.log", world.sb.root / "update-f.log")
            _edit_config(world, lambda cfg: cfg.update(updates={**(cfg.get("updates") or {}), "check": False}))
            yield world
    finally:
        if not os.environ.get("HERMES_E2E_KEEP_ROOT"):
            shutil.rmtree(root, ignore_errors=True)


def _config_path(w: G.World) -> Path:
    return w.sb.hermes_home / "config.yaml"


def _load_config(w: G.World) -> dict:
    p = _config_path(w)
    data = yaml.safe_load(p.read_text(encoding="utf-8-sig")) if p.is_file() else None
    return data if isinstance(data, dict) else {}


def _edit_config(w: G.World, edit) -> None:
    cfg = _load_config(w)
    edit(cfg)
    _config_path(w).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _config_version(w: G.World) -> int | None:
    v = _load_config(w).get("_config_version")
    return int(v) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else None


def _toggle(w: G.World, name: str, on: bool) -> None:
    p = w.sb.home / name
    if on:
        p.write_text("on\n", encoding="utf-8")
    else:
        p.unlink(missing_ok=True)


def _clear_toggles(w: G.World) -> None:
    for name in TOGGLES:
        _toggle(w, name, False)


def _pending_markers(w: G.World) -> list[str]:
    """The ``source-completion-pending`` obligation(s) in the install-state dir(s)."""
    installs = w.sb.hermes_home / "installs"
    return sorted(str(p) for p in installs.glob("**/source-completion-pending")) if installs.is_dir() else []


def _followups(rec: dict) -> list[dict]:
    items = rec.get("followups") or []
    return [f for f in items if isinstance(f, dict)]


def _followup_steps(rec: dict) -> set[str]:
    return {str(f.get("step")) for f in _followups(rec)}


def _update_log(w: G.World) -> Path:
    return w.sb.hermes_home / "logs" / "update.log"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else ""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _warn_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if WARN in line]


def _obligation(w: G.World) -> str:
    """The host fleet-restart obligation record(s) anywhere in the sandbox (diagnostics only)."""
    found = []
    for base, dirs, files in os.walk(w.sb.root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "venv", ".venv", "hermes-agent")]
        if "host-update-restart.json" in files:
            path = Path(base) / "host-update-restart.json"
            found.append(f"{path}: {_read(path)[:600]}")
    return "; ".join(found) or "none"


def _facts(w: G.World, cp=None, **extra) -> str:
    """A one-glance summary heading every failure message."""
    rec = w.receipt()
    parts = [f"rc={cp.returncode}" if cp is not None else "rc=-",
             f"head={w.head()[:12]}",
             f"receipt.outcome={rec.get('outcome')!r} update_id={str(rec.get('update_id'))[:12]} "
             f"pid={rec.get('pid')} followups={_followups(rec)}",
             f"pending={_pending_markers(w)}",
             f"config_version={_config_version(w)}",
             f"obligation={_obligation(w)}"]
    if cp is not None:
        parts.append(f"⚠ lines={_warn_lines(G.output(cp))[:8]}")
    parts += [f"{k}={v}" for k, v in extra.items()]
    return "\n".join(parts) + "\n"


# -- hostile releases ----------------------------------------------------------------------------


def _head_file(w: G.World, rel: str) -> str:
    return w.git("show", f"HEAD:{rel}") + "\n"


def _hostile_web_mjs(w: G.World) -> str:
    """The checkout's ``scripts/build/web.mjs`` with the toggle block after its imports."""
    text = _head_file(w, "scripts/build/web.mjs")
    if _WEB_MARK in text:
        return text
    lines = text.splitlines(keepends=True)
    last_import = max(i for i, line in enumerate(lines) if line.startswith("import "))
    while " from " not in lines[last_import] and last_import + 1 < len(lines):  # a multi-line import
        last_import += 1
    out = "".join(lines[:last_import + 1]) + _HOSTILE_WEB + "".join(lines[last_import + 1:])
    assert out.count(_WEB_MARK) == 1
    return out


def _publish_web_release(w: G.World, tag: str) -> str:
    """A release whose web build obeys the toggles and whose web inputs changed (web UI stale)."""
    return w.publish(f"release: e2e hostile post web {tag}", {
        "scripts/build/web.mjs": _hostile_web_mjs(w),
        f"web/e2e-hostile-post-{tag}.txt": f"web input changed by release {tag}\n",
    })


def _publish_gateway_fault_release(w: G.World, tag: str) -> str:
    """A release whose ``hermes gateway run`` refuses to boot while the toggle exists."""
    text = _head_file(w, "hermes_cli/gateway.py")
    if _GATEWAY_MARK not in text:
        m = re.search(r"^def run_gateway\(.*?\):\n    \"\"\".*?\"\"\"\n", text, re.S | re.M)
        assert m, "premise: hermes_cli/gateway.py has no run_gateway() with a docstring to inject after"
        text = text[:m.end()] + _HOSTILE_GATEWAY + text[m.end():]
    return w.publish(f"release: e2e hostile post gateway {tag}", {
        "hermes_cli/gateway.py": text,
        f"docs/e2e-hostile-post-{tag}.txt": f"release {tag}\n",
    })


def _publish_deps_fault_release(w: G.World, tag: str) -> str:
    """A release whose dependency preparation (``ensure_tools_for_sync``) fails while the toggle exists."""
    text = _head_file(w, "pm/client.py")
    if _DEPS_MARK not in text:
        m = re.search(r"^def ensure_tools_for_sync\(\) -> None:\n    \"\"\".*?\"\"\"\n", text, re.S | re.M)
        assert m, "premise: pm/client.py has no ensure_tools_for_sync() with a docstring to inject after"
        text = text[:m.end()] + _HOSTILE_DEPS + text[m.end():]
    return w.publish(f"release: e2e hostile post deps {tag}", {
        "pm/client.py": text,
        f"docs/e2e-hostile-post-{tag}.txt": f"release {tag}\n",
    })


# -- one shared PID namespace (gateway / killable update) ----------------------------------------


@contextlib.contextmanager
def _namespace(w: G.World) -> Iterator[NamespaceHost]:
    """Every process of the cell in ONE sandbox, as on a user's machine: the updater's process scans
    see the gateway it restarts. Closing kills the namespace init, which takes everything with it."""
    phase("namespace start", w.sb.root / "ns-agent.stderr",
          w.sb.hermes_home / "logs" / "update.log",
          w.sb.hermes_home / "logs" / "update_receipts" / "latest.json",
          w.sb.root / "update-c.log", w.sb.root / "update-f.log")
    host = NamespaceHost(w.sb.root, w.sb.env)
    phase("namespace ready")
    try:
        yield host
    finally:
        phase("namespace close start")
        leaked = host.close()
        phase("namespace close complete")
        assert not leaked, f"processes outlived the sandbox: {leaked}"


def _ns_cli(host: NamespaceHost, w: G.World, *args: str, timeout: float = 600):
    phase(f"namespace CLI start: {args}")
    cp = host.run([w.sb.hermes, *args], timeout=timeout)
    phase(f"namespace CLI complete: rc={cp.returncode}")
    w.transcripts.append(f"$ hermes {' '.join(args)} -> rc={cp.returncode}\n{(cp.stdout or '')[-6000:]}\n"
                         f"{(cp.stderr or '')[-3000:]}")
    return cp


def _ns_update(host: NamespaceHost, w: G.World, *extra: str):
    return _ns_cli(host, w, "update", "--yes", "--branch", "main", *extra, timeout=UPDATE_TIMEOUT)


def _identify(w: G.World) -> dict | None:
    from gateway.control_socket import identify_gateway

    with contextlib.suppress(Exception):
        return identify_gateway(w.sb.hermes_home, timeout=5.0)
    return None


def _serving(w: G.World, sha: str) -> dict | None:
    ident = _identify(w)
    return ident if ident and ident.get("code_sha") == sha else None


def _kill_born_after(host: NamespaceHost, before: set[int]) -> list[int]:
    """SIGSTOP then SIGKILL every process born after ``before`` was taken (by PID, namespace view):
    the update, its re-exec'd interpreter, build children and any detached completion child (own
    session, or reparented to the namespace init). Repeats until none is left."""
    killed: list[int] = []
    for _ in range(10):
        live = [p["pid"] for p in host.procs() if p["pid"] not in before and p["state"] not in ("Z", "X")]
        if not live:
            break
        for pid in live:
            host.kill(pid, signal.SIGSTOP)
        for pid in live:
            host.kill(pid, signal.SIGKILL)  # windows-footgun: ok - linux-only (bwrap) module
            if pid not in killed:
                killed.append(pid)
        time.sleep(0.5)
    return killed


def _gateway_logs(w: G.World) -> str:
    logs = w.sb.hermes_home / "logs"
    chunks = [_read(p)[-4000:] for p in sorted(logs.glob("*.log"))] if logs.is_dir() else []
    chunks += [_read(p)[-4000:] for p in sorted(w.sb.root.glob("gateway-*.log"))]
    return "\n".join(chunks)


# -- cells ---------------------------------------------------------------------------------------


def _foreground_group(host: NamespaceHost, upd: int) -> list[int]:
    """What a terminal's Ctrl-C reaches: the update and its descendants, minus the completion child's
    subtree (it runs in its own session, so the terminal's SIGINT never reaches it)."""
    procs = {p["pid"]: p for p in host.procs() if p["state"] not in ("Z", "X")}
    group, frontier = [], [upd]
    while frontier:
        pid = frontier.pop()
        if pid not in procs or "update_completion.py" in " ".join(procs[pid]["cmdline"]):
            continue
        group.append(pid)
        frontier += [p for p, row in procs.items() if row["ppid"] == pid]
    return group
