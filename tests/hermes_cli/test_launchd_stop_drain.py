"""`hermes gateway stop` on launchd waits out the gateway's drain instead of SIGKILLing after 5s."""

import subprocess

import pytest

import hermes_cli.gateway as gateway_cli


class TestLaunchdStopWaitsOutTheDrain:
    """A booted-out gateway drains (notify, then cron and delegation waits, about 34s on a busy host)
    and launchd SIGKILLs a job it owns at ExitTimeOut anyway. The CLI used to SIGKILL after 5s,
    cutting the drain short: interrupted turns were never marked for resume and the next boot paid
    unclean recovery."""

    @pytest.fixture
    def stop_env(self, tmp_path, monkeypatch):
        import hermes_cli.gateway_launchd as gateway_launchd
        state = {"bootout_rc": 0, "kills": [], "terms": [], "exit_at": 34.0, "now": 0.0,
                 "supervised": 77400, "subtree": {4242}}

        def fake_run(argv, *a, **k):
            if argv[:2] == ["launchctl", "bootout"] and state["bootout_rc"]:
                raise subprocess.CalledProcessError(state["bootout_rc"], argv, stderr="Boot-out failed")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def fake_sleep(seconds):
            state["now"] += max(seconds, 0.5)

        def cli_terminate(pid, force=False, **k):
            (state["kills"] if force else state["terms"]).append(pid)

        monkeypatch.setattr(gateway_launchd.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda *a, **k: state["supervised"])
        monkeypatch.setattr(gateway_cli, "_gateway_descendants_of",
                            lambda pid: set(state["subtree"]) if pid == state["supervised"] else set())
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
        monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
        monkeypatch.setattr(gateway_cli, "_mark_planned_stop", lambda *a, **k: None)
        monkeypatch.setattr("time.monotonic", lambda: state["now"])
        monkeypatch.setattr("time.sleep", fake_sleep)
        monkeypatch.setattr("gateway.status.get_running_pid",
                            lambda *a, **k: None if state["now"] >= state["exit_at"] or state["kills"] else 4242)
        monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1.0)
        monkeypatch.setattr("gateway.status.terminate_pid", cli_terminate)
        monkeypatch.setattr(gateway_cli, "terminate_pid", cli_terminate)
        return state

    def test_launchd_owned_gateway_finishes_its_drain_without_a_cli_sigkill(self, stop_env, capfd):
        gateway_cli.launchd_stop()  # the gateway is a grandchild of launchd's wrapper pid
        assert stop_env["kills"] == [] and stop_env["terms"] == []
        assert stop_env["now"] >= 34.0
        assert "did not exit gracefully" not in capfd.readouterr().out

    def test_cli_sigkill_is_only_a_backstop_past_launchd_exit_timeout(self, stop_env):
        stop_env["exit_at"] = 10_000.0  # a wedged gateway that ignores SIGTERM
        gateway_cli.launchd_stop()
        assert stop_env["kills"] == [4242]
        assert stop_env["now"] >= 60.0  # never before launchd's own ExitTimeOut clamp

    def test_detached_owner_beside_a_parked_job_gets_a_sigterm_and_its_drain(self, stop_env):
        stop_env["supervised"] = None  # job loaded but not running; a detached gateway owns the home
        stop_env["exit_at"] = 20.0
        gateway_cli.launchd_stop()
        assert stop_env["terms"] == [4242]
        assert stop_env["kills"] == []

    @pytest.mark.parametrize("rc", [3, 5])  # already unloaded; domain unsupported (detached fallback)
    def test_failed_bootout_keeps_the_short_pid_fallback(self, stop_env, rc):
        stop_env["bootout_rc"] = rc
        stop_env["exit_at"] = 10_000.0
        gateway_cli.launchd_stop()
        assert stop_env["kills"] == [4242]
        assert stop_env["now"] < 15.0
