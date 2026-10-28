"""Read-only node probes. Unknown evidence is never converted into a passing check."""
from __future__ import annotations

import json
import plistlib
import re
import shlex
import subprocess
from typing import Any

from tools.computer_use.node_routing import remaining
from tools.nodes import Node

_MARKER = "__HERMES_NODE_PROBE__"


class NodeUnavailable(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__(f"{report.get('code')}: {report.get('error')}")


def _probe_command(node: Node) -> str:
    driver = shlex.quote(node.cua_driver)
    probes = {
        "version": f"{driver} --version",
        "status": f"{driver} status",
        "permissions": f"{driver} permissions status --json",
        "console": "/usr/sbin/ioreg -n Root -d 1 -a",
        "power": "/usr/sbin/ioreg -r -n IOPMrootDomain -a",
    }
    return "; ".join(f"printf '\\n{_MARKER}{key}\\n'; {command} "
                     + ("2>&1" if key == "status" else "2>/dev/null")
                     for key, command in probes.items())


def _sections(text: str) -> dict[str, str]:
    return {part.split("\n", 1)[0]: part.partition("\n")[2].strip()
            for part in text.split(_MARKER)[1:]}


def _plist(text: str) -> list:
    try:
        value = plistlib.loads(text.encode())
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        return [row for row in rows if isinstance(row, dict)]
    except (ValueError, plistlib.InvalidFileException):
        return []


def _observe(value: Any, source: str) -> dict:
    return {"value": value, "source": source}


def _permissions(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _boolean(value):
    return value if type(value) is bool else None


def parse_observations(parts: dict[str, str]) -> dict:
    version = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b", parts.get("version", ""))
    permissions = _permissions(parts.get("permissions", ""))
    source = permissions.get("source", {})
    attributed = isinstance(source, dict) and source.get("attribution") == "driver-daemon"
    console = _plist(parts.get("console", ""))
    sessions = console[0].get("IOConsoleUsers", []) if console else []
    sessions = [s for s in sessions if isinstance(s, dict)] if isinstance(sessions, list) else []
    on_console = [s for s in sessions if s.get("kCGSSessionOnConsoleKey") is True
                  or s.get("IOConsoleSessionOnConsoleKey") is True]
    # Only the actual console session is evidence. An SSH login is not an unlocked desktop.
    locked = _boolean(console[0].get("IOConsoleLocked")) if console else None
    if locked is None and len(on_console) == 1:
        locked = _boolean(on_console[0].get("CGSSessionScreenIsLocked"))
    power = _plist(parts.get("power", ""))
    capabilities = power[0].get("System Capabilities") if power else None
    # IOKit/IOPM.h: kIOPMSystemCapabilityGraphics = 0x02. Unlike SSH
    # liveness, this distinguishes a GUI-capable wake from a network-only dark wake.
    awake = bool(capabilities & 0x02) if type(capabilities) is int else None
    status = parts.get("status", "")
    mode = re.search(r"^\s*permission mode: (standard|bounded|unrestricted)\b", status, re.MULTILINE)
    running = True if "Cua Driver daemon is running" in status else False if "daemon is not running" in status else None
    return {
        "reachable": _observe(True, "SSH command executed"),
        "daemon_running": _observe(running, "remote cua-driver status, public daemon"),
        "permission_mode": _observe(mode.group(1) if mode else None, "remote cua-driver status, immutable daemon authorization"),
        "locked": _observe(locked, "IORegistry Root IOConsoleLocked, or explicit console-session lock flag"),
        "awake": _observe(awake, "IOPMrootDomain System Capabilities graphics bit (GUI awake, not SSH liveness)"),
        "accessibility": _observe(_boolean(permissions.get("accessibility")) if attributed else None,
                                  "cua-driver permissions status, driver-daemon attribution required"),
        "screen_recording": _observe(_boolean(permissions.get("screen_recording")) if attributed else None,
                                     "cua-driver permissions status, driver-daemon attribution required"),
        "version": _observe(version.group() if version else None, "remote cua-driver --version"),
    }


def node_doctor(node: Node) -> dict:
    from tools.environments.local import served_profile_child_env, _sanitize_subprocess_env

    checks = {key: _observe(None, "not observed") for key in
              ("reachable", "daemon_running", "permission_mode", "locked", "awake", "accessibility", "screen_recording", "version")}
    report = {"node": node.name, "ssh": node.ssh, "checks": checks, "ok": False}
    if node.platform != "macos":
        report.update(code="node_platform_unsupported", error=f"No doctor probes for node platform {node.platform!r}")
        return report
    # SSH executes this shell fragment on the node, never on the controller.
    argv = node.ssh_argv()
    argv[-1:] = [_probe_command(node)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=remaining(), check=False,
                              env=_sanitize_subprocess_env(served_profile_child_env()))
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        report.update(code="node_unreachable", error=str(exc))
        return report
    parts = _sections(proc.stdout)
    if not parts:
        denied = "permission denied" in proc.stderr.lower()
        checks["reachable"] = _observe(True if denied else False, "SSH authentication refused" if denied else proc.stderr.strip())
        report.update(code="node_ssh_denied" if denied else "node_unreachable", error=proc.stderr.strip() or "SSH probe failed")
        return report
    checks = report["checks"] = parse_observations(parts)
    failures = (("daemon_running", False, "node_daemon_absent"), ("locked", True, "node_locked"), ("awake", False, "node_asleep"),
                ("accessibility", False, "node_permission_denied"), ("screen_recording", False, "node_permission_denied"))
    for key, bad, code in failures:
        if checks[key]["value"] is bad:
            report.update(code=code, error=f"{key}={bad}")
            return report
    actual_mode = checks["permission_mode"]["value"]
    if actual_mode is not None and actual_mode != node.cua_permission_mode:
        report.update(code="node_permission_mode_mismatch",
                      error=f"Expected {node.cua_permission_mode} public daemon, observed {actual_mode}. "
                            "The node startup owner must reconcile it. Hermes will not restart or reconfigure it")
        return report
    required = {"reachable": True, "daemon_running": True, "locked": False, "awake": True,
                "accessibility": True, "screen_recording": True}
    unknown = [key for key, wanted in required.items() if checks[key]["value"] is not wanted]
    if actual_mode is None:
        unknown.append("permission_mode")
    if checks["version"]["value"] is None:
        unknown.append("version")
    if unknown:
        report.update(code="node_state_unknown", error=f"Cannot verify {', '.join(unknown)}")
        return report
    report["ok"] = True
    return report


def run_node_doctor(node: Node, *, json_output: bool = False) -> int:
    report = node_doctor(node)
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Node: {node.name} ({node.ssh})")
        for name, check in report["checks"].items():
            value = "unknown" if check["value"] is None else str(check["value"]).lower()
            print(f"  {name}: {value} [{check['source']}]")
        if report.get("error"):
            print(f"  {report['code']}: {report['error']}")
    return 0 if report["ok"] else 1
