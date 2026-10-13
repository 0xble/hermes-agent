"""Reruns exercise the actual child entry point without agent or delivery effects."""
import pytest
import threading

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
    from unittest.mock import Mock
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = Mock(communicate=Mock(side_effect=subprocess.TimeoutExpired("rerun", 20)),
                 poll=Mock(return_value=None))
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
    kill, drain = Mock(), Mock()
    monkeypatch.setattr("cron.scheduler_script._terminate_cron_script_tree", kill)
    monkeypatch.setattr("cron.scheduler_script._drain_script_pipes", drain)
    with pytest.raises(TimeoutError, match="rerun deadline expired"):
        _bounded_review_rerun({"id": "job"}, 30)
    assert child.communicate.call_args.kwargs["timeout"] == 20
    kill.assert_called_once_with(child)
    drain.assert_called_once_with(child)


def test_abandoned_review_thread_kills_its_rerun(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import Mock
    from cron import scheduler_review as review
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    child = Mock(communicate=Mock(side_effect=review.ReviewAbandoned("review abandoned")),
                 poll=Mock(return_value=None))
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
    kill, drain = Mock(), Mock()
    monkeypatch.setattr("cron.scheduler_script._terminate_cron_script_tree", kill)
    monkeypatch.setattr("cron.scheduler_script._drain_script_pipes", drain)
    with pytest.raises(review.ReviewAbandoned):
        review._bounded_review_rerun({"id": "job"}, 30)
    kill.assert_called_once_with(child)
    drain.assert_called_once_with(child)
    assert child not in review._rerun_children


@pytest.mark.platforms("posix")
def test_gateway_shutdown_kills_live_rerun_process_group(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time
    import psutil
    from cron import scheduler_review as review

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    real_popen = subprocess.Popen
    children = []
    ready = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])\n"
        f"Path({str(ready)!r}).write_text(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    def launch(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        child = real_popen([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(review.subprocess, "Popen", launch)
    errors = []
    def rerun():
        try:
            review._bounded_review_rerun({"id": "job"}, 60)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=rerun)
    worker.start()
    try:
        end = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < end:
            time.sleep(.01)
        assert ready.exists()
        grandchild = int(ready.read_text())
        review.stop_reviews()
        worker.join(5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], review.ReviewAbandoned)
        assert children[0].poll() is not None
        def alive(pid):
            try:
                return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False
        end = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < end:
            time.sleep(.01)
        assert not alive(grandchild)
        assert children[0] not in review._rerun_children
    finally:
        review.stop_reviews()
        worker.join(5)
