"""Disposable failed-ack observer and retained fixture export, not a gate lane."""
from __future__ import annotations

import json
from pathlib import Path
import time
import traceback

import pytest

TARGET = "tests/e2e/core/cli_tmux/test_classic_cli_main_parity.py"
NODE = TARGET + "::test_classic_cli_matches_main_baseline_across_fifteen_steps"
TOKEN = "failure-boundary-10a52eee"
OPTION = "--composer-shard-diagnostic=" + TOKEN
ROOT = Path(__file__).resolve().parents[2]
RETAINED = ROOT / ".ci/composer-shard/retained"
_ACTIVE = False


def pytest_addoption(parser):
    parser.addoption("--composer-shard-diagnostic", default="", help="Disposable target observer opt-in")


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    # The canonical runner launches exactly one explicit file per subprocess.
    # Only the original target receives pytest's supported --basetemp option.
    global _ACTIVE
    target_args = (TARGET, str(ROOT / TARGET))
    if OPTION not in args or not any(value in target_args for value in args):
        return
    if any(value.startswith("--basetemp") for value in args):
        raise pytest.UsageError("Refuse a second basetemp; no diagnostic retry")
    if RETAINED.exists() or RETAINED.is_symlink():
        raise pytest.UsageError("Refuse existing retained state; no diagnostic retry")
    # Supported basetemp may delete an existing directory. Only this new,
    # dedicated hosted-checkout directory is eligible, never operator state.
    if RETAINED.resolve().is_relative_to(Path.home().resolve() / ".hermes"):
        raise pytest.UsageError("Diagnostic basetemp must be outside operator Hermes home")
    args.append("--basetemp=" + str(RETAINED))
    _ACTIVE = True


def pytest_collection_modifyitems(config, items):
    if not _ACTIVE or config.getoption("composer_shard_diagnostic") != TOKEN:
        return
    for item in items:
        if item.nodeid == NODE:
            item.fixturenames.append("composer_shard")


def _clock():
    return {"monotonic_ns": time.monotonic_ns(), "wall_time_ns": time.time_ns()}


def _export(data, artifacts, tmp_path):
    """Retain bounded facts under the target fixture, never HOME or environment."""
    data["finished"] = _clock()
    try:
        for path in sorted(artifacts.iterdir()):
            if path.is_file() and path.suffix in (".json", ".txt"):
                text = path.read_text(encoding="utf-8", errors="replace")
                limit = 64000 if path.name.startswith("capture-") else 16000
                start = max(0, len(text) - limit)
                data["files"][path.name] = {"text": text[start:], "original_chars": len(text),
                                            "start_char_offset": start, "truncated": start > 0}
        blob = json.dumps(data, ensure_ascii=True, default=repr)
        if len(blob) >= 512000:
            raise ValueError("diagnostic payload exceeded 512000 characters")
    except Exception:
        blob = json.dumps({"token": TOKEN, "nodeid": NODE, "files": {},
                           "export_error": traceback.format_exc()}, ensure_ascii=True)
    (tmp_path / "composer-export.json").write_text(blob + "\n", encoding="utf-8")


def _capture_failed_ack(pane, data):
    """Observe only after the original acknowledgement has already failed."""
    artifacts = Path(data["artifacts_path"])
    label = "ack-failed"
    request = artifacts / "composer.json.req"
    snapshot = artifacts / ("snapshot-" + label + ".json")
    error_path = artifacts / "snapshot-error.json"
    packet = {"acknowledgement": data["acknowledgement"],
              "interpretation": "Unclassified. Missing, late or non-aligned facts are inconclusive.",
              "captures": {}, "errors": [], "snapshot": None}
    spec = {"id": label, "acknowledged": False,
            "acknowledgement_decision": data["acknowledgement"]["decision"], **_clock()}
    packet["request"] = spec
    try:
        packet["request_write_start"] = _clock()
        pending = request.with_suffix(".pending")
        pending.write_text(json.dumps(spec), encoding="utf-8")
        pending.replace(request)
        packet["request_write_end"] = _clock()
        # This bounded diagnostic wait is AFTER the unchanged 10-second decision,
        # never a timeout extension, another acknowledgement or a retry.
        deadline = time.monotonic() + 1.5
        while not snapshot.exists() and not error_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        packet["snapshot_wait_end"] = _clock()
        packet["snapshot_available_at_wait_end"] = snapshot.exists()
        if error_path.exists():
            packet["snapshot_error"] = json.loads(error_path.read_text(encoding="utf-8"))
        if snapshot.exists():
            packet["snapshot_read_start"] = _clock()
            packet["snapshot"] = json.loads(snapshot.read_text(encoding="utf-8"))
            packet["snapshot_read_end"] = _clock()
        else:
            packet["errors"].append({"operation": "snapshot", "error": "missing_at_wait_end"})
    except Exception:
        packet["errors"].append({"operation": "snapshot_request_or_read", "failure": traceback.format_exc()})
    # No tmux query, state request or baseline write occurs on the healthy path.
    try:
        (artifacts / "baseline.txt").write_text(pane.baseline, encoding="utf-8")
    except Exception:
        packet["errors"].append({"operation": "baseline", "failure": traceback.format_exc()})
    for name, capture in (
        ("escaped", lambda: pane.tmux("capture-pane", "-p", "-e", "-t", "p")),
        ("plain", pane.screen),
        ("scrollback", pane.scrollback),
    ):
        record = {"start": _clock(), "tmux_exit_status_unavailable": True}
        try:
            text = capture()
            record["end"] = _clock()
            record["empty"] = not bool(text)
            record["file"] = "capture-" + name + "-" + label + ".txt"
            (artifacts / record["file"]).write_text(text, encoding="utf-8")
        except Exception:
            record["end"] = _clock()
            record["failure"] = traceback.format_exc()
        packet["captures"][name] = record
    packet["finished"] = _clock()
    data["failure_packet"] = packet
    (artifacts / ("bundle-" + label + ".json")).write_text(json.dumps(packet, indent=2), encoding="utf-8")


@pytest.fixture
def composer_shard(request, tmp_path, monkeypatch):
    # The plugin is inert for all 724 peers and for non-opted-in invocations.
    if not _ACTIVE or request.node.nodeid != NODE:
        raise RuntimeError("Refuse observer outside the original parity node")
    from tests.e2e.core.cli_tmux._classic import ClassicHome, Pane, PROMPT
    artifacts = tmp_path / ("composer-observations-" + TOKEN)
    artifacts.mkdir()
    data = {"tmp_path": str(tmp_path), "artifacts_path": str(artifacts), "started": _clock(),
            "token": TOKEN, "nodeid": NODE, "files": {}, "boundary_reached": False}
    original_env, original_wait = ClassicHome.env, Pane.wait

    def env(ch):
        result = original_env(ch)
        # Preserve the canonical per-file TMPDIR, HOME and test configuration.
        result["HERMES_DIAG_COMPOSER_SNAPSHOT"] = str(artifacts / "composer.json")
        return result

    def wait(pane, *args, **kwargs):
        # Original submit, tmux, predicates, timeout, freshness, suggestion
        # rejection, paste-gap and Enter remain completely untouched.
        result = original_wait(pane, *args, **kwargs)
        if args == ([f"{PROMPT} /compress"], 10) and kwargs.get("exact_line") == "/compress":
            data["boundary_reached"] = True
            data["acknowledgement"] = {"value": result, "decision": _clock(), "timeout_seconds": 10,
                                       "enter_not_sent_at_decision": True}
            if result is None:
                try:
                    _capture_failed_ack(pane, data)
                except Exception:
                    data["capture_error"] = traceback.format_exc()
        return result

    monkeypatch.setattr(ClassicHome, "env", env)
    monkeypatch.setattr(Pane, "wait", wait)
    try:
        yield data
    finally:
        # The original scenario owns pane.kill/ch.reap. No final pane queries.
        _export(data, artifacts, tmp_path)
