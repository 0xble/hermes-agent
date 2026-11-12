"""Real-gateway incognito leak scan.

A private interval is driven through the real Telegram adapter against the stand-in Bot API:
text, a captioned document, a caption-less voice memo (command STT), a spoken reply (command
TTS) and a terminal command that writes to ``$TMPDIR``. Each carries a canary generated in this
process, never a source literal. After ``/incognito off`` (and, separately, after a SIGKILL and
restart) no file under the run's temp root, the Hermes home or the private-directory parent may
contain it.

This is a recording probe rather than an xfail. ``scripts/run_tests.sh`` drops the environment
and hides passing output, so the mode switch and the result travel through
``HERMES_E2E_ARTIFACTS``, which the runner forwards: an ``incognito-leak-scan.baseline`` marker
there selects report-only mode (for code that predates the private directory), and each test
writes ``incognito-leak-scan-<name>.json`` there (digest prefix, booleans, counts and relative
paths only).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

import pytest

from tests.e2e.core.platforms import _contract as C
from tests.e2e.core.platforms._drv_telegram import TelegramDriver
from tests.e2e.core.platforms._suite import rig_fixtures
from tests.fakes.fake_llm_provider import ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.spawns_gateway_lookalike]
rig, rig_stream = rig_fixtures(TelegramDriver)

RESTART_LINE = "Hermes restarted, so the private messages before this were dropped. Incognito is still on."
AUDIO_SENDS = ("sendVoice", "sendAudio", "sendDocument")


def _artifacts() -> Path | None:
    value = os.environ.get("HERMES_E2E_ARTIFACTS", "").strip()
    return Path(value) if value else None


def _baseline() -> bool:
    root = _artifacts()
    return (os.environ.get("HERMES_INCOGNITO_LEAK_SCAN_BASELINE") == "1"
            or bool(root and (root / "incognito-leak-scan.baseline").exists()))


def _report(name: str, payload: dict) -> None:
    root = _artifacts()
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    body = {"mode": "baseline" if _baseline() else "final", **payload}
    (root / f"incognito-leak-scan-{name}.json").write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")


def _scan(root: Path, canary: bytes) -> list[str]:
    hits: list[str] = []
    if not root.exists():
        return hits
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink() and canary in path.read_bytes():
                hits.append(str(path.relative_to(root)))
        except OSError:
            pass
    return sorted(hits)


def _roots(gw, private_parent: Path) -> list[tuple[str, Path]]:
    """Every place the gateway and its subprocesses can write: the run's temp root (the child's
    inherited ``TMPDIR``), the rig root, the Hermes home and the private-directory parent."""
    roots: list[tuple[str, Path]] = []
    temproot = os.environ.get("PYTEST_DEBUG_TEMPROOT", "").strip()
    candidates = [("tmp", Path(temproot))] if temproot else []
    candidates += [("rig", gw.root), ("home", gw.hermes_home), ("private", private_parent)]
    for label, candidate in candidates:
        resolved = candidate.resolve()
        if not any(resolved == known or known in resolved.parents for _, known in roots):
            roots.append((label, resolved))
    return roots


def _hits(gw, private_parent: Path, canary: bytes) -> list[str]:
    return sorted(f"{label}:{rel}" for label, root in _roots(gw, private_parent) for rel in _scan(root, canary))


def _interval_dirs(private_parent: Path) -> list[Path]:
    if not private_parent.exists():
        return []
    return [path for path in private_parent.iterdir() if path.name.startswith("hermes-incognito-")]


def _leftovers(private_parent: Path) -> list[str]:
    if not private_parent.exists():
        return []
    return sorted(p.name[:17] for p in private_parent.iterdir()
                  if p.name.startswith("hermes-incognito-") or p.name == "unattributed")


def _command_config() -> dict:
    # The transcript carries a model-script token, so a transcribed memo is answered by name.
    emit_stt = shlex.quote(
        "import pathlib,sys; pathlib.Path(sys.argv[2]).write_text('[in:heard] voice-' + pathlib.Path(sys.argv[1]).read_bytes().hex())"
    )
    emit_tts = shlex.quote(
        "import pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(pathlib.Path(sys.argv[1]).read_bytes())"
    )
    return {
        "stt": {"provider": "fake-command", "providers": {
            "fake-command": {"type": "command", "command":
                f"{shlex.quote(sys.executable)} -c {emit_stt} {{input_path}} {{output_path}}"},
        }},
        "tts": {"provider": "fake-command", "providers": {
            "fake-command": {"type": "command", "format": "ogg", "command":
                f"{shlex.quote(sys.executable)} -c {emit_tts} {{input_path}} {{output_path}}"},
        }},
        "approvals": {"mode": "off"},
    }


def _scratch_command(canary: bytes) -> str:
    code = "import os,pathlib,sys; pathlib.Path(os.environ['TMPDIR'], 'incognito-scan.txt').write_text(sys.argv[1])"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {shlex.quote(canary.decode())}"


def _boots(gw) -> int:
    try:
        return (gw.hermes_home / "logs" / "gateway.log").read_text(errors="replace").count("Gateway running with")
    except OSError:
        return 0


def _reboot(rig, *, hard: bool = False) -> None:
    """Restart and wait for THIS life to serve. The rig's readiness probe is satisfied by the
    previous life's polls, and an inbound sent while the adapter boots is batched with the next."""
    before = _boots(rig.gw)
    if hard:
        rig.kill_and_start()
    else:
        rig.restart()
    wait_until(lambda: _boots(rig.gw) > before or not rig.gw.alive(), "restarted gateway serving",
               timeout=90.0, on_timeout=rig.ctx)
    assert rig.gw.alive(), rig.gw.tail()


def _configure_media(rig) -> None:
    if getattr(rig, "_leak_scan_configured", False):
        return
    import hermes_yaml as yaml
    path = rig.gw.hermes_home / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key, value in _command_config().items():
        current = data.get(key)
        data[key] = {**current, **value} if isinstance(current, dict) and isinstance(value, dict) else value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _reboot(rig)
    rig._leak_scan_configured = True


def _seen(drv, chat_id: str, needle: str) -> int:
    return sum(needle in C.norm(v.text) for v in drv.visible(chat_id))


def _slash(rig, text: str, expect: str, user_id: str | None = None) -> None:
    """Send a slash command once no turn is running and wait for a NEW reply containing ``expect``."""
    chat = str(user_id or rig.drv.user_id)
    rig.gw.wait_idle()
    before = _seen(rig.drv, chat, expect)
    rig.drv.dm(text, user_id=user_id)
    wait_until(lambda: _seen(rig.drv, chat, expect) > before, f"{text!r} answered with {expect!r}",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)


def _ask(rig, text: str, aid: str, user_id: str | None = None) -> None:
    rig.gw.wait_idle()
    inbound = rig.drv.dm(text, user_id=user_id)
    C.wait_reply(rig, inbound.chat_id, aid)


def _model_saw(rig, needle: str) -> bool:
    return any(needle in str(body) for bodies in rig.director.requests.values() for body in bodies)


def _audio_sends(drv, chat_id: str) -> int:
    return sum(str(call.params.get("chat_id")) == str(chat_id) and not call.faulted
               for call in drv.standin.calls_of(*AUDIO_SENDS))


def _stored(gw, needle: str) -> bool:
    try:
        wait_until(lambda: gw.user_rows(needle), "public turn stored", timeout=15.0)
        return True
    except AssertionError:
        return False


def test_private_interval_leaves_no_canary_after_exit(rig):
    canary = ("incognito-" + secrets.token_hex(24)).encode()
    word = canary.decode()
    digest = hashlib.sha256(canary).hexdigest()[:12]
    tag = digest[:8]
    transcript = "voice-" + canary.hex()
    drv, gw, director = rig.drv, rig.gw, rig.director
    parent = rig.private_parent
    assert parent is not None
    baseline = _baseline()
    chat = str(drv.user_id)
    _configure_media(rig)
    director.script(f"txt-{tag}", C.answer(f"A-txt-{tag}", "text ok"))
    director.script(f"cap-{tag}", C.answer(f"A-cap-{tag}", "caption ok"))
    director.script("heard", C.answer(f"A-heard-{tag}", "voice ok"))
    director.script(f"say-{tag}", ToolCall("text_to_speech", {"text": word}), C.answer(f"A-say-{tag}", "spoken"))
    director.script(f"tmp-{tag}", ToolCall("terminal", {"command": _scratch_command(canary)}),
                    C.answer(f"A-tmp-{tag}", "wrote"))
    director.script(f"pub-{tag}", C.answer(f"A-pub-{tag}", "public ok"))

    _slash(rig, "/incognito on", "Incognito ON")
    _ask(rig, f"{word} [in:txt-{tag}]", f"A-txt-{tag}")
    gw.wait_idle()
    caption = drv.document("caption.txt", canary, "text/plain", f"{word} [in:cap-{tag}]")
    C.wait_reply(rig, caption.chat_id, f"A-cap-{tag}")
    gw.wait_idle()
    drv.voice(canary)  # no caption: only a transcript can carry the script token
    C.barrier(rig, f"bv-{tag}")
    voice_answered = _seen(drv, chat, C.foot(f"A-heard-{tag}")) > 0
    transcribed = _model_saw(rig, transcript)
    audio_before = _audio_sends(drv, chat)
    _ask(rig, f"say it [in:say-{tag}]", f"A-say-{tag}")
    C.barrier(rig, f"bs-{tag}")
    audio_sent = _audio_sends(drv, chat) > audio_before
    _ask(rig, f"write it [in:tmp-{tag}]", f"A-tmp-{tag}")
    C.barrier(rig, f"bx-{tag}")

    intervals = _interval_dirs(parent)
    before_off = _hits(gw, parent, canary)
    private_hits = sorted(rel for interval in intervals for rel in _scan(interval, canary))

    _slash(rig, "/incognito off", "Incognito OFF")
    hits = _hits(gw, parent, canary)
    leftovers = _leftovers(parent)
    downloads = len(drv.standin.calls_of("file_download"))
    _ask(rig, f"public-survives-{digest} [in:pub-{tag}]", f"A-pub-{tag}")
    stored = _stored(gw, f"public-survives-{digest}")
    _report("exit", {
        "canary_sha256_prefix": digest, "durable_hits": hits, "hits_before_off": before_off,
        "private_hits_before_off": private_hits, "interval_dirs_before_off": len(intervals),
        "leftover_private_dirs": leftovers, "telegram_file_downloads": downloads,
        "voice_answered": voice_answered, "model_saw_transcript": transcribed,
        "audio_sent": audio_sent, "public_turn_stored": stored,
    })
    assert gw.alive(), gw.tail()
    assert stored, "a public turn after the interval was not stored"
    if baseline:
        return
    assert transcribed and voice_answered, f"the caption-less voice memo was not transcribed and answered\n{rig.ctx()}"
    assert audio_sent, f"the spoken reply never reached the chat\n{drv.describe()}"
    assert intervals, "no private interval directory was created"
    assert private_hits, f"the canary never reached a private artifact; pre-exit hits were {before_off}"
    assert not hits, f"canary outlived the interval at {hits}; pre-exit hits were {before_off}"
    assert not leftovers, f"private directories remain after /incognito off: {leftovers}"


def test_hard_kill_keeps_topic_private_and_sweeps_the_interval(rig):
    canary = ("incognito-" + secrets.token_hex(24)).encode()
    word = canary.decode()
    digest = hashlib.sha256(canary).hexdigest()[:12]
    tag = digest[:8]
    drv, gw, director = rig.drv, rig.gw, rig.director
    parent = rig.private_parent
    assert parent is not None
    baseline = _baseline()
    user = rig.fresh_user()  # its own private chat: independent of the other test's topic
    _configure_media(rig)
    director.script(f"k1-{tag}", ToolCall("terminal", {"command": _scratch_command(canary)}),
                    C.answer(f"A-k1-{tag}", "wrote"))
    director.script(f"k2-{tag}", C.answer(f"A-k2-{tag}", "after restart"))

    _slash(rig, "/incognito on", "Incognito ON", user)
    _ask(rig, f"{word} [in:k1-{tag}]", f"A-k1-{tag}", user)
    old = {path.name for path in _interval_dirs(parent)}
    before_kill = _hits(gw, parent, canary)

    _reboot(rig, hard=True)
    _slash(rig, "/incognito status", "Incognito ON", user)
    _ask(rig, f"still there [in:k2-{tag}]", f"A-k2-{tag}", user)
    restart_line_seen = _seen(drv, user, RESTART_LINE) > 0
    hits = _hits(gw, parent, canary)
    survivors = sorted(old.intersection(path.name for path in _interval_dirs(parent)))
    _slash(rig, "/incognito off", "Incognito OFF", user)
    _report("restart", {
        "canary_sha256_prefix": digest, "durable_hits": hits, "hits_before_kill": before_kill,
        "interval_dirs_before_kill": len(old), "old_interval_dirs_left": len(survivors),
        "restart_line_seen": restart_line_seen, "leftover_private_dirs_after_off": _leftovers(parent),
    })
    assert gw.alive(), gw.tail()
    if baseline:
        return
    assert old, "no private interval directory existed before the kill"
    assert before_kill, "the canary never reached disk before the kill, so the sweep proves nothing"
    assert not survivors, f"the startup sweep left {len(survivors)} interval directories from the killed life"
    assert restart_line_seen, f"the reply after the restart did not say the private tail was dropped\n{rig.ctx()}"
    assert not hits, f"canary survived a hard kill and restart at {hits}"
