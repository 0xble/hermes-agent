"""Fail-closed completion booking for cron runs (#93820).

The scheduler booked every finished run as ``cron_complete`` based on the run
lifecycle alone: a job whose agent turn died after a tool call, mid-API-wait,
or without any assistant text still surfaced as a healthy run (one audited
day held 10 such silently-failed sessions). The fix classifies the session's
LAST message row through the existing ``session_lifecycle_statuses`` helper
before ``end_session``: only a real assistant reply — a plain answer or the
``[SILENT]`` sentinel, both assistant-text rows — books as ``cron_complete``;
anything else books as ``cron_incomplete_no_output``. Classification is
best-effort: a probe failure keeps the historical reason rather than
mislabeling a healthy run.
"""

import hashlib
import os
from typing import Callable

import pytest

import cron.scheduler as cron_scheduler
from gateway.session_context import reset_session_vars


_on_agent_run: Callable[[], None] | None = None


class _FakeCronAgent:

    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt, **_kwargs):
        callback = _on_agent_run
        if callback is not None:
            callback()
        return {
            "completed": True,
            "failed": False,
            "final_response": "done",
            "turn_exit_reason": "",
        }

    def close(self):
        pass


class _RecordingSessionDB:
    """SessionDB double with a configurable lifecycle classification."""

    def __init__(self, *args, **kwargs):
        self.ended: list[tuple[str, str]] = []
        self.lifecycle = type(self).next_lifecycle

    next_lifecycle = "complete"

    def set_session_title(self, *args, **kwargs):
        return True

    def get_compression_tip(self, session_id):
        return None

    def session_lifecycle_statuses(self, session_ids):
        if isinstance(type(self).next_lifecycle, Exception):
            raise type(self).next_lifecycle
        return {sid: type(self).next_lifecycle for sid in session_ids}

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        pass


def _run_booked_job(
    monkeypatch, tmp_path, *, live_job_updates=None, **job_updates
):
    import hermes_state
    import run_agent

    instances: list[_RecordingSessionDB] = []
    real_init = _RecordingSessionDB.__init__

    def _capture_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(_RecordingSessionDB, "__init__", _capture_init)
    monkeypatch.setattr(hermes_state, "SessionDB", _RecordingSessionDB)
    monkeypatch.setattr(run_agent, "AIAgent", _FakeCronAgent)
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", lambda *_a, **_k: None
    )
    # The runtime key is read from the environment (never a literal here);
    # AIAgent and SessionDB are fakes above, so the value is never used.
    monkeypatch.setenv("HERMES_TEST_RUNTIME_KEY", "unused-placeholder")

    def _fake_runtime(**_kwargs):
        return {
            "api_key": os.environ.get("HERMES_TEST_RUNTIME_KEY", ""),
            "base_url": None,
            "provider": "test-provider",
            "api_mode": None,
            "command": None,
            "args": None,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime
    )
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(
        cron_scheduler, "_guard_job_credential_exfil", lambda _job: None
    )
    job = {
        "id": "verify-complete",
        "name": "Verification",
        "prompt": "Do the thing",
        "schedule_display": "manual",
        "enabled": True,
    }
    job.update(job_updates)
    def _live_job(_ref):
        live = dict(job)
        if live_job_updates:
            live.update(live_job_updates)
        return live

    monkeypatch.setattr(cron_scheduler, "resolve_job_ref", _live_job)
    if job.get("completion_script") and not job.get("completion_script_sha256"):
        script_path = tmp_path / "scripts" / job["completion_script"]
        job["completion_script_sha256"] = hashlib.sha256(
            script_path.read_bytes()
        ).hexdigest()
    result = cron_scheduler.run_job(job)
    return instances, result


@pytest.fixture(autouse=True)
def _clean_state():
    global _on_agent_run
    reset_session_vars()
    _RecordingSessionDB.next_lifecycle = "complete"
    _on_agent_run = None
    yield
    reset_session_vars()


def test_run_without_final_assistant_message_books_incomplete(monkeypatch, tmp_path):
    """Last row a tool result / pending call (lifecycle 'interrupted') must
    not surface as a healthy complete run."""
    _RecordingSessionDB.next_lifecycle = "interrupted"

    instances, _ = _run_booked_job(monkeypatch, tmp_path)

    assert instances, "SessionDB was never constructed"
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_incomplete_no_output"]


def test_run_with_final_assistant_reply_books_complete(monkeypatch, tmp_path):
    """A real assistant reply (plain answer or [SILENT] — both assistant
    text rows) keeps the healthy booking."""
    instances, _ = _run_booked_job(monkeypatch, tmp_path)

    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


def test_classification_probe_failure_keeps_historical_reason(monkeypatch, tmp_path):
    """Best-effort metadata: a failing classifier must not mislabel a run."""
    _RecordingSessionDB.next_lifecycle = RuntimeError("db busy")

    instances, _ = _run_booked_job(monkeypatch, tmp_path)

    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


def test_completion_script_can_verify_agent_result(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    verifier = scripts / "verify.py"
    verifier.write_text("print('candidate and rollout verified')\n", encoding="utf-8")

    _, result = _run_booked_job(
        monkeypatch,
        tmp_path,
        completion_script="verify.py",
    )

    success, output, final_response, error = result
    assert success is True
    assert "candidate and rollout verified" in output
    assert final_response == "done"
    assert error is None


def test_completion_script_failure_cannot_surface_as_healthy(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    verifier = scripts / "verify.py"
    verifier.write_text(
        "import sys\nprint('published SHA does not match runtime')\nsys.exit(7)\n",
        encoding="utf-8",
    )

    instances, result = _run_booked_job(
        monkeypatch,
        tmp_path,
        completion_script="verify.py",
    )

    success, output, final_response, error = result
    assert success is False
    assert "completion verification failed" in output.lower()
    assert "published SHA does not match runtime" in output
    assert "verification failed" in final_response.lower()
    assert error is not None
    assert "published SHA does not match runtime" in error
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_completion_failed"]


def test_agent_cannot_replace_its_completion_verifier(monkeypatch, tmp_path):
    global _on_agent_run
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    verifier = scripts / "verify.py"
    verifier.write_text(
        "import sys\nprint('original verifier ran')\nsys.exit(9)\n",
        encoding="utf-8",
    )

    def replace_verifier():
        verifier.write_text("print('replacement bypass')\n", encoding="utf-8")

    _on_agent_run = replace_verifier
    _, result = _run_booked_job(
        monkeypatch,
        tmp_path,
        completion_script="verify.py",
    )

    success, output, _, error = result
    assert success is False
    assert "original verifier ran" in output
    assert "replacement bypass" not in output
    assert error is not None


def test_agent_cannot_disable_completion_verification_mid_run(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    verifier = scripts / "verify.py"
    verifier.write_text("print('verifier ran')\n", encoding="utf-8")

    _, result = _run_booked_job(
        monkeypatch,
        tmp_path,
        completion_script="verify.py",
        live_job_updates={"enabled": False},
    )

    success, output, _, error = result
    assert success is False
    assert "disabled during its run" in output
    assert "verifier ran" not in output
    assert error is not None
