"""Non-live contracts for native overlap assertions and failure evidence."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_overlap_launchd_rehearsal import (
    _assert_no_recovery_guidance,
    _native_failure_diagnostics,
)


def test_recovery_guidance_cannot_hide_behind_normal_fake_answer():
    normal = [{"text": "old-turn-complete"}]
    requests = [{"messages": [{"role": "assistant", "content": "old-turn-complete"}]}]
    _assert_no_recovery_guidance(normal, requests)
    for text in ("⏳ Gateway restarting", "Operation interrupted",
                 "The previous turn was interrupted", "Session restored"):
        with pytest.raises(AssertionError):
            _assert_no_recovery_guidance(normal + [{"text": text}], requests)
    with pytest.raises(AssertionError):
        _assert_no_recovery_guidance(normal, requests + [{"messages": [
            {"role": "user", "content": "[System note: The previous turn was interrupted. Resume."}
        ]}])


def test_failure_diagnostics_bound_logs_without_deleting_fixture_state(tmp_path):
    root = tmp_path / "overlap"
    root.mkdir()
    (root / "a.err").write_bytes(b"x" * 8000 + b"exact failure tail")
    (root / "b.out").write_text("successor output")
    profile = root / "profile"
    profile.mkdir()
    coordinator = profile / "fixture-state.db"
    coordinator.write_bytes(b"retained coordinator fixture")
    api = SimpleNamespace(lock=threading.Lock(), maximum=1,
                          errors=list(range(30)), sent=[{"text": "answer"}])
    evidence = json.loads(_native_failure_diagnostics(root, api))
    assert evidence["fixture_root"] == str(root)
    assert evidence["metrics"] == {"max_pollers": 1, "errors": list(range(10, 30)),
                                   "sent_count": 1}
    assert len(evidence["logs"]["a.err"].encode()) == 4000
    assert evidence["logs"]["a.err"].endswith("exact failure tail")
    assert evidence["logs"]["b.out"] == "successor output"
    assert coordinator.read_bytes() == b"retained coordinator fixture"
