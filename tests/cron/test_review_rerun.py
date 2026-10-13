"""Reruns exercise the actual child entry point without agent or delivery effects."""
import pytest

from cron.scheduler_review import _bounded_review_rerun


def test_rerun_child_returns_new_output_without_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts/report.py"
    script.write_text("print('New rerun report')\n", encoding="utf-8")
    job = {"id": "rerun-test", "name": "Report", "prompt": "", "script": str(script),
           "no_agent": True, "deliver": "local", "review": {"contract": "x", "subject": "topic"}}
    success, _output, text, error = _bounded_review_rerun(job, 60)
    assert success, error
    assert text == "New rerun report"
    assert not (tmp_path / "cron/review-receipts.jsonl").exists()


def test_rerun_child_timeout_falls_back_and_kills_tree(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = SimpleNamespace(communicate=Mock(side_effect=subprocess.TimeoutExpired("rerun", 20)))
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
    kill, drain = Mock(), Mock()
    monkeypatch.setattr("cron.scheduler_script._terminate_cron_script_tree", kill)
    monkeypatch.setattr("cron.scheduler_script._drain_script_pipes", drain)
    with pytest.raises(TimeoutError, match="rerun deadline expired"):
        _bounded_review_rerun({"id": "job"}, 30)
    assert child.communicate.call_args.kwargs["timeout"] == 20
    kill.assert_called_once_with(child)
    drain.assert_called_once_with(child)
