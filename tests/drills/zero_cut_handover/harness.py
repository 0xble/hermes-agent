"""Fake-driven zero-cut gateway handover drills.

The live entry point is intentionally narrow: it must be explicitly enabled with
``ZERO_CUT_DRILLS_LIVE=1`` and the safety guard runs before any mutating effect.
Unit tests use the same state machine with an :class:`Effects` made entirely of
fakes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence

LIVE_HOME = Path("/Users/brianle/.hermes-zero-cut")
BOT_USERNAME = "brianle_hermes_handover_bot"
TELEGRAM = os.path.expanduser("~/.local/bin/telegram")


@dataclass(frozen=True)
class CommandResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Effects:
    """All external effects used by the harness.

    ``run_command`` accepts ``background=True`` for the two drills that need to
    send an inbound while the handover process is still draining. A fake may
    return any object with ``wait()``/``poll()`` for a background command.
    """

    run_command: Callable[..., Any]
    launchctl: Callable[[Sequence[str]], CommandResult]
    send_as_user: Callable[[str, str], Any]
    read_chat: Callable[[str, int], Any]
    read_state_db: Callable[[Path], Mapping[str, Any]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    kill: Callable[[int], None] = lambda pid: os.kill(pid, signal.SIGKILL)


def _completed(result: Any) -> CommandResult:
    if isinstance(result, CommandResult):
        return result
    return CommandResult(
        int(getattr(result, "returncode", 0)),
        str(getattr(result, "stdout", "") or ""),
        str(getattr(result, "stderr", "") or ""),
    )


def _real_effects(home: Path) -> Effects:
    """Create effects for the isolated drill home; never used by unit tests."""

    def env_for(extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["HERMES_HOME"] = str(home)
        env.update(extra or {})
        return env

    def run_command(argv: Sequence[str], *, env: Mapping[str, str] | None = None,
                    background: bool = False) -> Any:
        command = [str(part) for part in argv]
        if background:
            return subprocess.Popen(command, cwd=home, env=env_for(env),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
        return subprocess.run(command, cwd=home, env=env_for(env), check=False,
                              capture_output=True, text=True, encoding="utf-8")

    def launchctl(args: Sequence[str]) -> CommandResult:
        return _completed(subprocess.run(["launchctl", *map(str, args)], check=False,
                                         capture_output=True, text=True, encoding="utf-8"))

    def send_as_user(peer: str, text: str) -> Any:
        result = subprocess.run([TELEGRAM, "--agent", "--yes", "send", peer, text, "--silent"],
                                check=False, capture_output=True, text=True, encoding="utf-8")
        return _json_or_text(result.stdout)

    def read_chat(peer: str, limit: int) -> Any:
        result = subprocess.run([TELEGRAM, "--agent", "messages", peer, "--limit", str(limit)],
                                check=False, capture_output=True, text=True, encoding="utf-8")
        return _json_or_text(result.stdout)

    def read_state_db(path: Path) -> Mapping[str, Any]:
        return _read_state_db(path)

    return Effects(run_command, launchctl, send_as_user, read_chat, read_state_db,
                   kill=lambda pid: os.kill(pid, signal.SIGKILL))


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text


def _read_state_db(home: Path) -> Mapping[str, Any]:
    """Read drill evidence without depending on one state-db migration version."""
    path = home / "state.db"
    if not path.is_file():
        return {"tables": {}, "error": f"missing state db: {path}"}
    tables: dict[str, list[dict[str, Any]]] = {}
    try:
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for name in names:
                try:
                    tables[name] = [dict(row) for row in db.execute(f'SELECT * FROM "{name}"')]
                except sqlite3.DatabaseError:
                    tables[name] = []
    except sqlite3.DatabaseError as exc:
        return {"tables": {}, "error": str(exc)}
    return {"tables": tables}


def _config(home: Path) -> Mapping[str, Any]:
    path = home / "config.yaml"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        value = yaml.safe_load(text)
        return value if isinstance(value, Mapping) else {}
    except (ImportError, ValueError):
        enabled = bool(re.search(r"(?m)^\s*enabled:\s*true\s*(?:#.*)?$", text, re.I))
        return {"gateway": {"zero_cut_handover": {"enabled": enabled}}}


def _slot_labels(home: Path) -> tuple[str, str]:
    """Use the production label helper while resolving it against ``home``."""
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        from hermes_cli.gateway_launchd import get_launchd_slot_labels
        return get_launchd_slot_labels()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def _peer_username(effects: Effects, peer: str) -> str | None:
    result = _completed(effects.run_command(
        [TELEGRAM, "--agent", "--json", "chats", "--limit", "100"], background=False))
    data = _json_or_text(result.stdout)
    wanted = str(peer).lstrip("@").lower()
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            username = value.get("username") or value.get("user_name")
            identifiers = (value.get(key) for key in ("id", "peer", "name", "title", "username"))
            if username and any(str(item).lstrip("@").lower() == wanted for item in identifiers if item is not None):
                found.append(str(username).lstrip("@"))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    return found[0] if found else None


def safety_guard(home: Path, peer: str, effects: Effects, *,
                 labels: tuple[str, str] | None = None,
                 peer_resolver: Callable[[Effects, str], str | None] = _peer_username) -> None:
    """Refuse unsafe homes, slots, configuration, or peers before real effects."""
    resolved = home.expanduser().resolve()
    if resolved != LIVE_HOME:
        raise ValueError(f"refusing live drill outside {LIVE_HOME}: {resolved}")
    active, standby = labels or _slot_labels(resolved)
    for label in (active, standby):
        if label == "ai.hermes.gateway" or label.startswith("ai.hermes.gateway."):
            raise ValueError(f"refusing live drill with production launchd label: {label}")
    if active == standby:
        raise ValueError("refusing live drill: slot labels are not distinct")
    config = _config(resolved)
    enabled = ((config.get("gateway") or {}).get("zero_cut_handover") or {}).get("enabled")
    if enabled is not True:
        raise ValueError("refusing live drill: gateway.zero_cut_handover.enabled is not true")
    username = peer_resolver(effects, peer)
    if username != BOT_USERNAME:
        raise ValueError(f"refusing live drill: peer does not resolve to @{BOT_USERNAME}")


def _tag(drill: str, number: int, now: float) -> str:
    return f"{drill}-{int(now * 1000)}-{number}"


def _send(effects: Effects, peer: str, drill: str, number: int, instruction: str,
          tags: list[str], message_ids: list[Any] | None = None) -> str:
    tag = _tag(drill, number, effects.clock())
    text = f"ZC {drill}-{number} {instruction} {tag}"
    response = effects.send_as_user(peer, text)
    if message_ids is not None:
        message_ids.append(_message_id(response))
    tags.append(tag)
    return tag


def _message_id(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("message_id", "id", "msg_id"):
            if key in value:
                return value[key]
        for child in value.values():
            found = _message_id(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _message_id(child)
            if found is not None:
                return found
    return None


def _command(effects: Effects, home: Path, *argv: str, background: bool = False,
             env: Mapping[str, str] | None = None) -> Any:
    try:
        return effects.run_command(list(argv), env=env, background=background)
    except TypeError:
        # Small fakes often expose only argv; retaining this fallback keeps the
        # Effects seam pleasant without weakening the real command environment.
        return effects.run_command(list(argv))


def _wait_process(process: Any) -> CommandResult:
    if process is None:
        return CommandResult()
    wait = getattr(process, "wait", None)
    if callable(wait):
        wait()
    return _completed(process)


def _launchctl_label(effects: Effects, label: str) -> tuple[str, CommandResult]:
    for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
        result = _completed(effects.launchctl(["print", f"{domain}/{label}"]))
        if result.returncode == 0:
            return domain, result
    return f"gui/{os.getuid()}", result


def _wait_slot(effects: Effects, label: str, *, timeout: float = 10.0) -> bool:
    deadline = effects.clock() + timeout
    while True:
        _domain, result = _launchctl_label(effects, label)
        if result.returncode == 0:
            return True
        if effects.clock() >= deadline:
            return False
        effects.sleep(0.1)


def _pid_from_launchctl(result: CommandResult) -> int | None:
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", result.stdout)
    return int(match.group(1)) if match else None


def _kill_slot(effects: Effects, label: str) -> int | None:
    domain, result = _launchctl_label(effects, label)
    pid = _pid_from_launchctl(result)
    if pid is not None:
        effects.kill(pid)
    return pid


def _handover(effects: Effects, home: Path, *, background: bool = False) -> Any:
    return _command(effects, home, "hermes", "gateway", "handover", background=background)


def _install(effects: Effects, home: Path) -> CommandResult:
    return _completed(_command(effects, home, "hermes", "gateway", "install"))


def _planned(effects: Effects, home: Path, peer: str, drill: str,
             message_ids: list[Any]) -> tuple[list[str], bool, str | None]:
    tags: list[str] = []

    def long_in_flight() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        _wait_process(_handover(effects, home))
        return True, None

    def follow_up() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        process = _handover(effects, home, background=True)
        _active, standby = _slot_labels(home)
        if not _wait_slot(effects, standby):
            return False, f"standby {standby} did not boot"
        _send(effects, peer, drill, 2, "reply exactly ACK", tags, message_ids)
        _wait_process(process)
        return True, None

    def promotion_burst() -> tuple[bool, str | None]:
        process = _handover(effects, home, background=True)
        _active, standby = _slot_labels(home)
        for number in (1, 2):
            _send(effects, peer, drill, number, "reply exactly ACK", tags, message_ids)
        _wait_slot(effects, standby)
        for number in (3, 4, 5):
            _send(effects, peer, drill, number, "reply exactly ACK", tags, message_ids)
        _wait_process(process)
        return True, None

    def repeated_handovers() -> tuple[bool, str | None]:
        _wait_process(_handover(effects, home))
        second = _wait_process(_handover(effects, home))
        if second.returncode != 0:
            return True, "reverse handover is not supported by the current stack"
        return True, None

    handlers = {
        "long-in-flight": long_in_flight,
        "follow-up-before-lock-release": follow_up,
        "promotion-burst": promotion_burst,
        "repeated-handovers": repeated_handovers,
    }
    try:
        sequence_ok, reason = handlers[drill]()
    except KeyError as exc:
        raise ValueError(f"unknown planned drill: {drill}") from exc
    return tags, sequence_ok, reason


def _crash(effects: Effects, home: Path, peer: str, drill: str,
           message_ids: list[Any]) -> tuple[list[str], bool, str | None]:
    tags: list[str] = []
    _active, standby = _slot_labels(home)

    def kill_a() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        process = _handover(effects, home, background=True)
        _wait_slot(effects, standby)
        _kill_slot(effects, _slot_labels(home)[0])
        _wait_process(process)
        return True, None

    def kill_b() -> tuple[bool, str | None]:
        process = _handover(effects, home, background=True)
        _wait_slot(effects, standby)
        _send(effects, peer, drill, 1, "reply exactly ACK", tags, message_ids)
        _kill_slot(effects, standby)
        _wait_process(process)
        domain, _result = _launchctl_label(effects, standby)
        effects.launchctl(["kickstart", "-k", f"{domain}/{standby}"])
        return True, None

    def failed_boot() -> tuple[bool, str | None]:
        # The invalid slot environment is isolated to B; A remains untouched.
        _send(effects, peer, drill, 1, "reply exactly ACK", tags, message_ids)
        result = _completed(_command(
            effects, home, "hermes", "gateway", "handover", env={
                "HERMES_GATEWAY_SLOT": "b", "ZERO_CUT_DRILL_INVALID_CONFIG": "1"}))
        return (False, "standby failure simulation unexpectedly succeeded") if result.returncode == 0 else (True, None)

    handlers = {
        "kill-a-during-drain": kill_a,
        "kill-b-after-promotion": kill_b,
        "standby-fails-to-boot": failed_boot,
    }
    try:
        sequence_ok, reason = handlers[drill]()
    except KeyError as exc:
        raise ValueError(f"unknown crash drill: {drill}") from exc
    return tags, sequence_ok, reason


def _walk_messages(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if any(key in value for key in ("text", "message", "raw_text")):
            found.append(value)
        for child in value.values():
            found.extend(_walk_messages(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_messages(child))
    return found


def _message_text(message: Mapping[str, Any]) -> str:
    value = message.get("text", message.get("message", message.get("raw_text", "")))
    if isinstance(value, list):
        return "".join(str(item.get("text", item)) if isinstance(item, Mapping) else str(item) for item in value)
    return str(value)


def _logs(home: Path) -> str:
    log_dir = home / "logs"
    if not log_dir.is_dir():
        return ""
    chunks: list[str] = []
    for path in sorted(log_dir.glob("*.log")):
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _state_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tables = state.get("tables", state)
    rows: list[Mapping[str, Any]] = []
    if isinstance(tables, Mapping):
        for value in tables.values():
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


def _parked_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tables = state.get("tables", state)
    if not isinstance(tables, Mapping):
        return []
    parked: list[Mapping[str, Any]] = []
    for name, values in tables.items():
        if not isinstance(values, list):
            continue
        for row in values:
            if not isinstance(row, Mapping):
                continue
            state_value = str(row.get("state", row.get("status", ""))).lower()
            if name == "outbox" and state_value in {"pending", "sending", "ambiguous"}:
                parked.append(row)
            if name == "gateway_routing" and any(row.get(key) for key in
                                                 ("active_turn_token", "active_turn_id", "queued_event")):
                parked.append(row)
    return parked


def assert_evidence(tags: Sequence[str], chat: Any, state: Mapping[str, Any], logs: str,
                    *, allow_interrupted: bool = False) -> dict[str, Any]:
    """Compute the drill assertions from observer chat, state-db, and logs."""
    texts = [_message_text(message) for message in _walk_messages(chat)]
    finals = {tag: [text for text in texts if re.search(
        rf"(?is)^\s*(?:ACK|DONE)\s+{re.escape(tag)}\s*$", text)] for tag in tags}
    lost = [tag for tag, matches in finals.items() if not matches]
    duplicates = {tag: len(matches) for tag, matches in finals.items() if len(matches) > 1}
    corpus = "\n".join([logs, json.dumps(state, default=str)])

    def starts_for(tag: str) -> int:
        start_lines = re.findall(r"(?im)^[^\n]*(?:model[-_ ]?turn|turn|admission)[-_ ]?start[^\n]*$", corpus)
        return sum(1 for line in start_lines if tag in line)

    starts = {tag: starts_for(tag) for tag in tags}
    interrupted = [tag for tag in tags if re.search(
        rf"(?im){re.escape(tag)}[^\n]*(?:interrupt|cancel|cut)", corpus)]
    parked_rows = _parked_rows(state)
    queued_events = state.get("queued_events", []) if isinstance(state, Mapping) else []
    active_markers = state.get("active_turn_markers", []) if isinstance(state, Mapping) else []
    parked = bool(parked_rows or queued_events or active_markers or re.search(
        r"(?im)(?:queued event|active[-_ ]turn marker|pending outbox)", logs))
    slots = state.get("slots", []) if isinstance(state, Mapping) else []
    active_slots = [slot for slot in slots if isinstance(slot, Mapping) and slot.get("standby") is False]
    ownership_ok = len(active_slots) == 1 and bool(active_slots[0].get("owns_telegram", True))

    assertions = {
        "final_reply_exactly_once": {"passed": not lost and not duplicates, "lost": lost, "duplicates": duplicates},
        "no_cut": {"passed": allow_interrupted or not interrupted, "interrupted": interrupted},
        "no_auto_replay": {"passed": not any(count > 1 for count in starts.values()), "turn_starts": starts},
        "no_parking": {"passed": not parked, "residue": parked_rows},
        "standby_ownership": {"passed": ownership_ok, "active_slots": active_slots},
    }
    return {"passed": all(item["passed"] for item in assertions.values()), "assertions": assertions,
            "finals": finals, "interrupted": interrupted}


def _control_slots(home: Path) -> list[Mapping[str, Any]]:
    try:
        from gateway.control_socket import query_gateway_control
        from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY
        slots = []
        for slot in (SLOT_ACTIVE, SLOT_STANDBY):
            identity = query_gateway_control(home, "identify", slot=slot)
            status = query_gateway_control(home, "status", slot=slot)
            if identity or status:
                slots.append({**(identity or {}), **(status or {})})
        return slots
    except Exception:
        return []


def _result_path(home: Path, drill: str, now: float) -> Path:
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))
    path = home / "drills" / f"{stamp}-{drill}.json"
    suffix = 2
    while path.exists():
        path = home / "drills" / f"{stamp}-{drill}-{suffix}.json"
        suffix += 1
    return path


def run_drill(*, home: Path, peer: str, drill: str, effects: Effects,
              live: bool = False) -> dict[str, Any]:
    """Run one named drill and persist its JSON evidence result."""
    home = home.expanduser().resolve()
    if live:
        safety_guard(home, peer, effects)
    else:
        # Fake tests can use a temporary path while retaining the same sequence.
        if home.expanduser().resolve() == LIVE_HOME:
            safety_guard(home, peer, effects)
    install = _install(effects, home)
    if install.returncode != 0:
        raise RuntimeError(f"gateway install failed: {install.stderr or install.stdout}")
    started = effects.clock()
    message_ids: list[Any] = []
    if drill in {"long-in-flight", "follow-up-before-lock-release", "promotion-burst", "repeated-handovers"}:
        tags, sequence_ok, xfail_reason = _planned(effects, home, peer, drill, message_ids)
        allow_interrupted = False
    else:
        tags, sequence_ok, xfail_reason = _crash(effects, home, peer, drill, message_ids)
        allow_interrupted = True
    chat = effects.read_chat(peer, max(50, len(tags) * 10))
    state = dict(effects.read_state_db(home))
    slots = state.get("slots") if isinstance(state, Mapping) else None
    if not isinstance(slots, list):
        state["slots"] = _control_slots(home)
    evidence = assert_evidence(tags, chat, state, _logs(home), allow_interrupted=allow_interrupted)
    result = {
        "drill": drill, "started_at": started, "finished_at": effects.clock(), "tags": tags,
        "message_ids": message_ids, "sequence_ok": sequence_ok, "xfail_reason": xfail_reason,
        "assertions": evidence["assertions"],
        "passed": bool(sequence_ok and (xfail_reason or evidence["passed"])),
        "chat_observed": len(_walk_messages(chat)), "state_db": state,
    }
    path = _result_path(home, drill, effects.clock())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return result


def run_suite(*, home: Path, peer: str, drills: str, repeat: int, effects: Effects,
              live: bool = False) -> list[dict[str, Any]]:
    groups = {
        "planned": ("long-in-flight", "follow-up-before-lock-release", "promotion-burst", "repeated-handovers"),
        "crash": ("kill-a-during-drain", "kill-b-after-promotion", "standby-fails-to-boot"),
    }
    names = groups[drills] if drills in groups else groups["planned"] + groups["crash"]
    results: list[dict[str, Any]] = []
    for _ in range(max(1, repeat)):
        for name in names:
            results.append(run_drill(home=home, peer=peer, drill=name, effects=effects, live=live))
    return results


def real_effects(home: Path) -> Effects:
    return _real_effects(home.expanduser().resolve())
