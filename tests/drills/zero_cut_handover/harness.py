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
BOT_USER_ID = 8899335561  # @brianle_hermes_handover_bot; the CLI chat list carries id, not username
TELEGRAM = os.path.expanduser("~/.local/bin/telegram")
_CHAT_POLL_S = 2.0  # MTProto chat reads are FLOOD_WAIT-limited; poll the observer gently
ACK = "reply exactly ACK"


def _final_re(tag: str, kinds: str = "ACK|DONE") -> "re.Pattern[str]":
    """One answer for ``tag``; a reply may carry several (folded mid-turn inbound), on one line or many.
    Anchored to a line of answers, so the observer's own "... reply exactly DONE <tag>" never counts."""
    return re.compile(rf"(?im)^\s*(?:(?:ACK|DONE)\s+\S+\s+)*(?:{kinds})\s+{re.escape(tag)}(?=\s|$)")


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
    query_control: Callable[..., Mapping[str, Any] | None] | None = None


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

    repo_root = Path(__file__).resolve().parents[3]

    def run_command(argv: Sequence[str], *, env: Mapping[str, str] | None = None,
                    background: bool = False) -> Any:
        command = [str(part) for part in argv]
        if background:
            return subprocess.Popen(command, cwd=repo_root, env=env_for(env),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
        return subprocess.run(command, cwd=repo_root, env=env_for(env), check=False,
                              capture_output=True, text=True, encoding="utf-8")

    def launchctl(args: Sequence[str]) -> CommandResult:
        return _completed(subprocess.run(["launchctl", *map(str, args)], check=False,
                                         capture_output=True, text=True, encoding="utf-8"))

    def telegram_json(argv: Sequence[str]) -> Any:
        return _telegram_json(argv)

    def send_as_user(peer: str, text: str) -> Any:
        return telegram_json(["--agent", "--yes", "send", peer, text, "--silent"])

    def read_chat(peer: str, limit: int) -> Any:
        return telegram_json(["--agent", "messages", peer, "--limit", str(limit)])

    def read_state_db(path: Path) -> Mapping[str, Any]:
        return _read_state_db(path)

    return Effects(run_command, launchctl, send_as_user, read_chat, read_state_db,
                   kill=lambda pid: os.kill(pid, signal.SIGKILL))


def _telegram_json(argv: Sequence[str]) -> Any:
    """Run the telegram CLI observer. FLOOD_WAIT is retryable, so wait what Telegram asks. A send
    that errored for another reason is never retried blindly (it may have been delivered)."""
    data: Any = None
    for _attempt in range(6):
        result = subprocess.run([TELEGRAM, *argv], check=False, capture_output=True,
                                text=True, encoding="utf-8")
        data = _json_or_text(result.stdout)
        if data == "" and result.returncode != 0:
            # The CLI reports errors (FLOOD_WAIT included) as JSON on stderr; keep that evidence and
            # let a rate limit reach the retry below instead of reading as a failed send.
            data = _json_or_text((result.stderr or "").strip())
            if not isinstance(data, Mapping):
                data = {"error": {"code": "cli_failed", "returncode": result.returncode,
                                  "stderr": (result.stderr or "")[-400:]}}
        error = data.get("error") if isinstance(data, Mapping) else None
        if not (isinstance(error, Mapping) and error.get("code") == "rate_limited"):
            return data
        time.sleep(float(error.get("retry_after_seconds") or 3) + 0.5)
    return data


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


_PEER_VERIFIED: set[str] = set()  # one successful observer lookup per peer per process


def _peer_username(effects: Effects, peer: str) -> str | None:
    """Resolve ``peer`` to the drill bot only by its fixed user id (the observer is the telegram CLI,
    not code under test). Returns ``BOT_USERNAME`` when ``peer`` is that id or that username and the
    id is present in the user's chat list, else None."""
    if str(peer).lstrip("@").lower() not in {str(BOT_USER_ID), BOT_USERNAME}:
        return None
    if peer in _PEER_VERIFIED:
        return BOT_USERNAME
    data = _telegram_json(["--agent", "chats", "--limit", "200"])
    items = data if isinstance(data, list) else []
    if any(isinstance(c, Mapping) and c.get("id") == BOT_USER_ID for c in items):
        _PEER_VERIFIED.add(peer)
        return BOT_USERNAME
    return None


def _would_kill_production_gateway(effects: Effects, labels: tuple[str, str]) -> bool:
    production = _completed(effects.launchctl(["print", f"gui/{os.getuid()}/ai.hermes.gateway"]))
    production_pid = _pid_from_launchctl(production)
    if production_pid is None:
        return False
    return any(_pid_from_launchctl(_completed(effects.launchctl(
        ["print", f"gui/{os.getuid()}/{label}"]))) == production_pid for label in labels)


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
    if _would_kill_production_gateway(effects, (active, standby)):
        raise ValueError("refusing live drill: a drill slot shares the production gateway pid")
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
    if instruction == ACK:
        # Hermes folds a message that lands mid-turn into that turn and the model then answers the
        # newest one; asking for one ACK line per tag keeps every admitted inbound visibly answered.
        instruction = ("reply with one line 'ACK <tag>' for this tag and for every earlier ZC tag "
                       "in this chat you have not acknowledged yet, nothing else")
    text = f"ZC {drill}-{number} {instruction} {tag}"
    response = effects.send_as_user(peer, text)
    message_id = _message_id(response)
    if message_id is None:
        # The observer's own send failed (no id): nothing reached the bot, so this is a harness
        # error, never "lost inbound". Not retried: an errored send may still have been delivered.
        raise DrillObserverError(f"observer send for {tag} returned no message id: {str(response)[:200]}")
    if message_ids is not None:
        message_ids.append(message_id)
    tags.append(tag)
    return tag


class DrillObserverError(RuntimeError):
    """The test account's Telegram client failed; the drill result says nothing about the gateway."""


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
    """Run the checkout's CLI module, never an installed ``hermes`` on PATH."""
    repo_root = Path(__file__).resolve().parents[3]
    command = [sys.executable, "-m", "hermes_cli.main", *argv]
    merged_env = dict(env or {})
    merged_env.pop("PYTHONPATH", None)
    merged_env["HERMES_HOME"] = str(home)
    merged_env["ZERO_CUT_DRILLS_REPO_ROOT"] = str(repo_root)
    try:
        return effects.run_command(command, env=merged_env, background=background)
    except TypeError:
        return effects.run_command(command)


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
    attempts = 0
    while attempts < 100:
        _domain, result = _launchctl_label(effects, label)
        if result.returncode == 0:
            return True
        if attempts >= 99 or effects.clock() >= deadline:
            return False
        attempts += 1
        effects.sleep(0.1)
    return False


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
    return _command(effects, home, "gateway", "handover", background=background)


def _install(effects: Effects, home: Path) -> CommandResult:
    return _completed(_command(effects, home, "gateway", "install", "--if-missing"))


def _control_query(effects: Effects, home: Path, verb: str, slot: str) -> Mapping[str, Any] | None:
    if effects.query_control is not None:
        try:
            return effects.query_control(home, verb, slot=slot)
        except TypeError:
            return effects.query_control(home, verb, slot)
    from gateway.control_socket import query_gateway_control
    return query_gateway_control(home, verb, slot=slot)


def _starting_active_slot(effects: Effects, home: Path) -> str:
    from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY
    statuses = {slot: _control_query(effects, home, "status", slot)
                for slot in (SLOT_ACTIVE, SLOT_STANDBY)}
    active = [slot for slot, status in statuses.items()
              if isinstance(status, Mapping) and status.get("standby") is False]
    if len(active) != 1:
        raise RuntimeError(f"expected exactly one active slot at drill start, found {len(active)}")
    return active[0]


def _other_slot(slot: str) -> str:
    from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY
    return SLOT_STANDBY if slot == SLOT_ACTIVE else SLOT_ACTIVE


def _wait_handover_accepted(effects: Effects, home: Path, slot: str, *, timeout: float = 240.0) -> bool:
    deadline = effects.clock() + timeout
    attempts = 0
    while attempts < 2400:
        state = _control_query(effects, home, "handover-status", slot)
        if isinstance(state, Mapping) and state.get("accepted") is True:
            return True
        if effects.clock() >= deadline:
            return False
        attempts += 1
        effects.sleep(0.1)
    return False


def _process_done(process: Any) -> bool:
    poll = getattr(process, "poll", None)
    if callable(poll):
        return poll() is not None
    return False


def _kill_label_until_done(effects: Effects, label: str, process: Any, *, timeout: float = 240.0) -> None:
    deadline = effects.clock() + timeout
    killed: set[int] = set()
    attempts = 0
    while not _process_done(process) and attempts < 2400:
        _domain, result = _launchctl_label(effects, label)
        pid = _pid_from_launchctl(result)
        if pid is not None and pid not in killed:
            try:
                effects.kill(pid)
            except PermissionError:
                # launchd reports the pid while its root-owned xpcproxy is still exec'ing the job; the
                # same pid becomes our gateway a moment later, so retry it on the next poll.
                pid = None
            except ProcessLookupError:
                pass
            if pid is not None:
                killed.add(pid)
        if effects.clock() >= deadline:
            break
        attempts += 1
        effects.sleep(0.1)
    if not _process_done(process):
        raise RuntimeError(f"handover did not exit while killing {label}")
    _wait_process(process)


def _label_for_slot(home: Path, slot: str) -> str:
    labels = _slot_labels(home)
    return labels[0] if slot == "a" else labels[1]


def _pid_for_slot(effects: Effects, home: Path, slot: str) -> int | None:
    identity = _control_query(effects, home, "identify", slot)
    status = _control_query(effects, home, "status", slot)
    for value in (identity, status):
        if isinstance(value, Mapping) and value.get("pid") is not None:
            try:
                return int(value["pid"])
            except (TypeError, ValueError):
                pass
    return None


def _planned(effects: Effects, home: Path, peer: str, drill: str,
             message_ids: list[Any], active_slot: str) -> tuple[list[str], bool, str | None]:
    tags: list[str] = []

    def long_in_flight() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        _wait_process(_handover(effects, home))
        return True, None

    def follow_up() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        process = _handover(effects, home, background=True)
        standby = _label_for_slot(home, _other_slot(active_slot))
        if not _wait_slot(effects, standby):
            return False, f"standby {standby} did not boot"
        _send(effects, peer, drill, 2, "reply exactly ACK", tags, message_ids)
        _wait_process(process)
        return True, None

    def promotion_burst() -> tuple[bool, str | None]:
        process = _handover(effects, home, background=True)
        standby = _label_for_slot(home, _other_slot(active_slot))
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
        return second.returncode == 0, None if second.returncode == 0 else "reverse handover failed"

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
           message_ids: list[Any], active_slot: str) -> tuple[list[str], bool, str | None]:
    tags: list[str] = []
    standby_slot = _other_slot(active_slot)
    standby_label = _label_for_slot(home, standby_slot)
    active_label = _label_for_slot(home, active_slot)

    def kill_a() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "run terminal sleep 45 then reply exactly DONE", tags, message_ids)
        process = _handover(effects, home, background=True)
        if not _wait_handover_accepted(effects, home, active_slot):
            return False, "handover was not accepted before killing active slot"
        _kill_slot(effects, active_label)
        _wait_process(process)
        # The promoted slot must own the gateway before post-kill messages are sent.
        promoted = _control_query(effects, home, "status", standby_slot) or {}
        if promoted.get("standby") is not False:
            return False, "standby did not become active after killing active slot"
        for number in (2, 3):
            _send(effects, peer, drill, number, "reply exactly ACK", tags, message_ids)
        if not _wait_chat_finals(effects, peer, tags[1:]):
            return False, "post-kill messages did not receive finals"
        return True, None

    def kill_b() -> tuple[bool, str | None]:
        process = _handover(effects, home, background=True)
        _wait_process(process)
        promoted_slot = _other_slot(active_slot)
        promoted_label = _label_for_slot(home, promoted_slot)
        _send(effects, peer, drill, 1, "reply exactly ACK", tags, message_ids)
        old_pid = _pid_for_slot(effects, home, promoted_slot)
        _kill_slot(effects, promoted_label)
        deadline = effects.clock() + 240
        new_pid = old_pid
        attempts = 0
        while attempts < 2400 and effects.clock() < deadline:
            new_pid = _pid_for_slot(effects, home, promoted_slot)
            if new_pid is not None and new_pid != old_pid:
                break
            attempts += 1
            effects.sleep(0.1)
        if new_pid is None or new_pid == old_pid:
            return False, "promoted slot did not restart with a new pid"
        _send(effects, peer, drill, 2, "reply exactly ACK", tags, message_ids)
        if not _wait_chat_finals(effects, peer, tags):
            return False, "messages around promoted-slot restart did not receive finals"
        return True, None

    def failed_boot() -> tuple[bool, str | None]:
        _send(effects, peer, drill, 1, "reply exactly ACK", tags, message_ids)
        process = _handover(effects, home, background=True)
        _kill_label_until_done(effects, standby_label, process)
        result = _completed(process)
        if result.returncode == 0:
            return False, "standby boot failure unexpectedly succeeded"
        active_status = _control_query(effects, home, "status", active_slot) or {}
        if active_status.get("standby") is not False:
            return False, "original active slot lost ownership after standby boot failure"
        _send(effects, peer, drill, 2, "reply exactly ACK", tags, message_ids)
        if not _wait_chat_finals(effects, peer, tags):
            return False, "fresh ACK did not receive a final after standby failure"
        idle_domain, idle_result = _launchctl_label(effects, standby_label)
        del idle_domain
        if idle_result.returncode == 0:
            return False, "failed standby label remained loaded"
        return True, None

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
    terminal = {"sent", "delivered", "failed", "cancelled", "canceled", "expired", "completed", "done", "acknowledged", "skipped"}
    parked: list[Mapping[str, Any]] = []
    for row in tables.get("outbox", []) if isinstance(tables.get("outbox", []), list) else []:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("state", row.get("status", ""))).lower()
        if status not in terminal:
            parked.append({"table": "outbox", **row})
    for row in tables.get("gateway_routing", []) if isinstance(tables.get("gateway_routing", []), list) else []:
        if not isinstance(row, Mapping):
            continue
        raw = row.get("entry_json")
        try:
            entry = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            entry = {}
        if isinstance(entry, Mapping) and entry.get("active_turn_token") is not None:
            parked.append({"table": "gateway_routing", **row})
    return parked


def _queue_observation(state: Mapping[str, Any]) -> tuple[str, list[Any]]:
    found: list[Any] = []
    slots = state.get("slots", [])
    for slot in slots if isinstance(slots, list) else []:
        if not isinstance(slot, Mapping):
            continue
        for key in ("queue_depth", "pending_queue_depth", "pending_events", "queued_events", "adapter_pending", "pending_messages"):
            if key in slot:
                found.append({"slot": slot.get("slot"), key: slot[key]})
    return ("exposed" if found else "not_exposed", found)


def _state_message_evidence(state: Mapping[str, Any], tags: Sequence[str]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    tables = state.get("tables", state)
    rows = tables.get("messages", []) if isinstance(tables, Mapping) else []
    users = {tag: 0 for tag in tags}
    finals = {tag: 0 for tag in tags}
    done = {tag: 0 for tag in tags}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        role = str(row.get("role", "")).lower()
        content = str(row.get("content", "") or "")
        for tag in tags:
            if tag not in content:
                continue
            if role == "user":
                users[tag] += 1
            if role == "assistant" and _final_re(tag).search(content):
                finals[tag] += 1
            if role == "assistant" and _final_re(tag, "DONE").search(content):
                done[tag] += 1
    return users, finals, done


def assert_evidence(tags: Sequence[str], chat: Any, state: Mapping[str, Any], logs: str,
                    *, planned: bool = False, require_done: bool = False) -> dict[str, Any]:
    """Assert observer replies plus durable state.db rows; logs are never evidence."""
    del logs
    texts = [_message_text(message) for message in _walk_messages(chat)]
    finals = {tag: [text for text in texts if _final_re(tag).search(text)] for tag in tags}
    users, state_finals, done = _state_message_evidence(state, tags)
    lost = [tag for tag, matches in finals.items() if planned and not matches]
    duplicates = {tag: len(matches) for tag, matches in finals.items() if len(matches) > 1}
    bad_users = {tag: count for tag, count in users.items() if count != 1}
    bad_state_finals = {tag: count for tag, count in state_finals.items()
                        if (count != 1 if planned else count > 1)}
    # Only the long (sleep 45) turns owe a DONE: an ACK follow-up answers with ACK by design.
    missing_done = [tag for tag in tags if require_done and tag.endswith("-1") and done[tag] < 1]
    parked_rows = _parked_rows(state)
    queue_status, queue_values = _queue_observation(state)
    queued = [item for item in queue_values if any(value not in (0, None, False, "", [])
                                                   for key, value in item.items() if key != "slot")]
    slots = state.get("slots", []) if isinstance(state, Mapping) else []
    active_slots = [slot for slot in slots if isinstance(slot, Mapping) and slot.get("standby") is False]
    ownership_ok = len(active_slots) == 1 and bool(active_slots[0].get("owns_telegram", True))
    assertions = {
        "final_reply_exactly_once": {"passed": not lost and not duplicates and not bad_state_finals,
                                     "lost": lost, "duplicates": duplicates,
                                     "state_final_counts": state_finals, "bad_state_finals": bad_state_finals},
        "no_cut": {"passed": not missing_done, "missing_done": missing_done,
                    "state_done_counts": done},
        "no_auto_replay": {"passed": not bad_users, "user_message_counts": users,
                            "bad_user_counts": bad_users},
        "no_parking": {"passed": not parked_rows and not queued, "residue": parked_rows,
                        "queue_status": queue_status, "queue_values": queue_values},
        "standby_ownership": {"passed": ownership_ok, "active_slots": active_slots},
    }
    return {"passed": all(item["passed"] for item in assertions.values()), "assertions": assertions,
            "finals": finals}


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


def _wait_chat_finals(effects: Effects, peer: str, tags: Sequence[str], *, timeout: float = 240.0) -> bool:
    deadline = effects.clock() + timeout
    attempts = 0
    while attempts < 2400:
        chat = effects.read_chat(peer, max(50, len(tags) * 10))
        texts = [_message_text(message) for message in _walk_messages(chat)]
        if all(any(_final_re(tag).search(text) for text in texts) for tag in tags):
            return True
        if effects.clock() >= deadline:
            return False
        attempts += 1
        effects.sleep(_CHAT_POLL_S)
    return False


def _wait_planned_quiescence(effects: Effects, home: Path, peer: str, tags: Sequence[str],
                             *, timeout: float = 240.0) -> tuple[Any, Mapping[str, Any]]:
    deadline = effects.clock() + timeout
    attempts = 0
    chat: Any = None
    state: Mapping[str, Any] = {}
    while attempts < 2400:
        chat = effects.read_chat(peer, max(50, len(tags) * 10))
        state = dict(effects.read_state_db(home))
        texts = [_message_text(message) for message in _walk_messages(chat)]
        finals_present = all(any(_final_re(tag).search(text) for text in texts) for tag in tags)
        outbox_pending = any(row.get("state", row.get("status", "")).lower() in {"pending", "sending"}
                             for row in (state.get("tables", {}).get("outbox", [])
                                         if isinstance(state.get("tables", {}), Mapping) else [])
                             if isinstance(row, Mapping))
        if finals_present and not outbox_pending:
            return chat, state
        if effects.clock() >= deadline:
            break
        attempts += 1
        effects.sleep(_CHAT_POLL_S)
    return chat, state


def run_drill(*, home: Path, peer: str, drill: str, effects: Effects,
              live: bool = False, install: bool = False) -> dict[str, Any]:
    """Run one named drill and persist its JSON evidence result."""
    home = home.expanduser().resolve()
    if live:
        safety_guard(home, peer, effects)
    else:
        # Fake tests can use a temporary path while retaining the same sequence.
        if home.expanduser().resolve() == LIVE_HOME:
            safety_guard(home, peer, effects)
    if install:
        install_result = _install(effects, home)
        if install_result.returncode != 0:
            raise RuntimeError(f"gateway install failed: {install_result.stderr or install_result.stdout}")
    active_slot = _starting_active_slot(effects, home)
    started = effects.clock()
    message_ids: list[Any] = []
    planned = drill in {"long-in-flight", "follow-up-before-lock-release", "promotion-burst", "repeated-handovers"}
    if planned:
        tags, sequence_ok, xfail_reason = _planned(effects, home, peer, drill, message_ids, active_slot)
    else:
        tags, sequence_ok, xfail_reason = _crash(effects, home, peer, drill, message_ids, active_slot)
    if planned:
        chat, state_value = _wait_planned_quiescence(effects, home, peer, tags)
        state = dict(state_value)
    else:
        chat = effects.read_chat(peer, max(50, len(tags) * 10))
        state = dict(effects.read_state_db(home))
    if not isinstance(state.get("slots"), list):
        state["slots"] = _control_slots(home)
    evidence = assert_evidence(tags, chat, state, _logs(home), planned=planned,
                               require_done=drill in {"long-in-flight", "follow-up-before-lock-release"})
    result = {
        "drill": drill, "started_at": started, "finished_at": effects.clock(), "starting_active_slot": active_slot, "tags": tags,
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
    home = home.expanduser().resolve()
    if live or home == LIVE_HOME:
        safety_guard(home, peer, effects)  # before the install below, the suite's first mutation
    install = _install(effects, home)
    if install.returncode != 0:
        raise RuntimeError(f"gateway install failed: {install.stderr or install.stdout}")
    slot_a_label, slot_b_label = _slot_labels(home)
    # Roles are symmetric: after a handover the serving slot may be b, so the suite starts from
    # whichever label is loaded (run_drill still requires exactly one active slot).
    if not (_wait_slot(effects, slot_a_label) or _wait_slot(effects, slot_b_label)):
        raise RuntimeError(f"no drill slot launchd label is loaded: {slot_a_label}, {slot_b_label}")
    results: list[dict[str, Any]] = []
    for _ in range(max(1, repeat)):
        for name in names:
            try:
                results.append(run_drill(home=home, peer=peer, drill=name, effects=effects, live=live))
            except DrillObserverError as exc:
                # Not a gateway verdict: record it as unproven so the run still fails, and go on.
                results.append({"drill": name, "passed": False, "observer_error": str(exc)})
    return results


def real_effects(home: Path) -> Effects:
    return _real_effects(home.expanduser().resolve())
