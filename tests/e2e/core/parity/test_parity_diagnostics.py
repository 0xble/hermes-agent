from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from tests.e2e.core.parity._helpers import ParityHome, failure_log_tail


def test_failure_log_tail_filters_and_caps_isolated_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text(
        "noise should be filtered\n"
        + "\n".join(f"plugin event {i}" for i in range(45))
        + "\n",
        encoding="utf-8",
    )
    (logs / "errors.log").write_text(
        "unrelated warning\n"
        + "\n".join(f"discovery error {i}" for i in range(45))
        + "\n",
        encoding="utf-8",
    )

    detail = failure_log_tail(cast(ParityHome, SimpleNamespace(hermes_home=tmp_path)))

    assert "noise should be filtered" not in detail
    assert "unrelated warning" not in detail
    assert "plugin event 44" in detail
    assert "discovery error 44" in detail
    assert len(detail.splitlines()) <= 60
