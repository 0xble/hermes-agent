"""Strict cooldown reads distinguish no outage from unavailable durable state."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import shared_primary_cooldown as cooldown
from agent.agent_runtime_helpers import _refresh_from_shared_cooldown


@pytest.mark.parametrize("failure", ["permission", "malformed", "wrong_shape"])
def test_unavailable_state_preserves_cached_deadline(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    route = ("custom", "https://primary.invalid/v1", "primary")
    record = cooldown.arm_cooldown(route, reason="rate_limit", reset_at=time.time() + 7200)
    assert record is not None
    path = cooldown._state_path()
    if failure == "permission":
        real_read = Path.read_text
        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("unavailable state")
            return real_read(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", denied)
    else:
        path.write_text("{" if failure == "malformed" else '[]', encoding="utf-8")
    deadline = time.monotonic() + 7200
    agent = SimpleNamespace(_primary_runtime=dict(zip(("provider", "base_url", "model"), route)),
                            _shared_primary_cooldown_record=record, _rate_limited_until=deadline)
    assert _refresh_from_shared_cooldown(agent) == "unavailable"
    assert agent._rate_limited_until == deadline
    assert agent._shared_primary_cooldown_record == record


def test_missing_state_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cooldown.read_cooldown(("custom", "https://primary.invalid/v1", "primary")) is None
