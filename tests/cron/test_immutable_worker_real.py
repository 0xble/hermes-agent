"""Real S1 detached workers retain their loaded source across S2 pointer changes."""
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli.immutable_releases import promote, stage_release


@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_detached_cron_worker_stays_on_release_A_then_fresh_B(tmp_path, monkeypatch):
    from cron import executions
    from cron.executions import latest_execution
    from cron.jobs import create_job, use_cron_store

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for real release staging")
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(repo), str(source)], check=True)
    # Distinct real revisions: archive refuses synthetic names such as A/B.
    probe = source / "cron" / "s2_release_probe.py"
    revisions = []
    for name in ("A", "B"):
        probe.write_text(f"IDENTITY = {name!r}\n")
        subprocess.run(["git", "-C", str(source), "add", str(probe)], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=S2",
                        "-c", "user.email=s2@example.test", "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", f"test release {name}"], check=True)
        revisions.append(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                               text=True).strip())
    from hermes_cli import immutable_releases as releases
    monkeypatch.setattr(releases, "restore_active_distributions", lambda *args, **kwargs: None)
    a, _ = stage_release(source, home, sha=revisions[0], uv=uv)
    b, _ = stage_release(source, home, sha=revisions[1], uv=uv)
    for name, release in (("A", a), ("B", b)):
        # Git staging archives HEAD; run the candidate scheduler without committing it.
        shutil.copy2(repo / "cron" / "scheduler.py", release / "cron" / "scheduler.py")
        (release / "cron" / "s2_release_probe.py").write_text(f"IDENTITY = {name!r}\n")
        # sitecustomize runs in the detached worker, not the dispatcher. Identify
        # the job from its handoff payload so even an incorrectly selected B worker
        # records the A job's observed release before the late import.
        (release / "sitecustomize.py").write_text(
            "import json, os, pathlib, sys, threading, time\n"
            "def probe():\n"
            "    if '--external-worker-file' not in sys.argv: return\n"
            "    payload=pathlib.Path(sys.argv[sys.argv.index('--external-worker-file')+1])\n"
            "    job=json.loads(payload.read_text())['job']['name'].rsplit(' ',1)[-1]\n"
            "    home=pathlib.Path(os.environ['HERMES_HOME'])\n"
            "    trigger=home/f'probe-{job}'\n"
            "    deadline=time.monotonic()+40\n"
            "    while time.monotonic()<deadline:\n"
            "        if trigger.exists() and 'cron.scheduler' in sys.modules:\n"
            "            import cron.scheduler, cron.jobs, cron.s2_release_probe\n"
            "            from hermes_cli import immutable_releases\n"
            "            paths={k:str(sys.modules[k].__file__) for k in "
            "('cron.scheduler','cron.jobs','cron.s2_release_probe','hermes_cli.immutable_releases')}\n"
            "            (home/f'observed-{job}.json').write_text(json.dumps({"
            "'pid':os.getpid(),'exe':sys.executable,'cwd':os.getcwd(),"
            "'identity':cron.s2_release_probe.IDENTITY,'paths':paths}))\n"
            "            break\n"
            "        time.sleep(.05)\n"
            "threading.Thread(target=probe,daemon=True).start()\n"
        )
    promote(home, a)
    scripts = home / "scripts"
    scripts.mkdir()
    for name in ("A", "B"):
        (scripts / f"s2_{name}.py").write_text(
            "import pathlib, os, time\n"
            "home=pathlib.Path(os.environ['HERMES_HOME'])\n"
            f"(home/'started-{name}').write_text('started')\n"
            f"target=home/'observed-{name}.json'\n"
            "deadline=time.monotonic()+40\n"
            "while not target.exists() and time.monotonic()<deadline: time.sleep(.05)\n"
            "if not target.exists(): raise SystemExit('probe timeout')\n"
            f"(home/'finished-{name}').write_text('done')\n"
        )

    def await_file(path, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return path
            time.sleep(.05)
        raise AssertionError(f"missing {path}")

    def dispatch(name, release):
        with use_cron_store(home):
            job = create_job(prompt=None, schedule="every 1h", name=f"S2 {name}",
                             script=f"s2_{name}.py", no_agent=True)
        payload = home / f"job-{name}.json"
        payload.write_text(json.dumps(job))
        code = (
            "import json,os,pathlib\n"
            f"os.environ['HERMES_HOME']={str(home)!r}\n"
            "os.environ['HERMES_LAUNCHD_LABEL']='ai.hermes.s2spike.cron'\n"
            "from cron import scheduler\n"
            "from tools import process_registry\n"
            "process_registry._is_supervised_gateway_process=lambda: True\n"
            f"job=json.loads(pathlib.Path({str(payload)!r}).read_text())\n"
            f"pathlib.Path({str(home / f'dispatcher-ready-{name}')!r}).touch()\n"
            f"barrier=pathlib.Path({str(home / f'dispatch-{name}')!r})\n"
            "import time\n"
            "deadline=time.monotonic()+40\n"
            "while not barrier.exists() and time.monotonic()<deadline: time.sleep(.05)\n"
            "assert barrier.exists(), 'dispatch barrier timeout'\n"
            "assert scheduler.run_one_job(job,adapters=None,loop=None)\n"
        )
        # Import A through the moving symlink itself. Its loaded __file__ can
        # retain `current/...`; dispatch must use the import-time physical root.
        dispatch_cwd = home if name == "A" else release
        import_root = home / "current" if name == "A" else release
        return job, subprocess.Popen([str(release / ".venv" / "bin" / "python"), "-c", code],
                                     cwd=dispatch_cwd,
                                     env={**os.environ, "HERMES_HOME": str(home),
                                          "PYTHONPATH": str(import_root)},
                                     start_new_session=True)

    parents = []
    worker_pids = []
    try:
        job_a, parent_a = dispatch("A", a)
        parents.append(parent_a)
        await_file(home / "dispatcher-ready-A")
        promote(home, b)
        (home / "dispatch-A").touch()
        await_file(home / "started-A")
        with use_cron_store(home):
            row = latest_execution(job_a["id"])
        assert row and row["status"] == "running"
        worker_pids.append(row["pid"])
        (home / "probe-A").write_text("go")
        a_observed = json.loads(await_file(home / "observed-A.json").read_text())
        assert a_observed["pid"] == row["pid"]
        assert a_observed["identity"] == "A"
        parent_a.wait(timeout=40)
        assert parent_a.returncode == 0
        await_file(home / "finished-A")

        (home / "probe-B").write_text("go")
        job_b, parent_b = dispatch("B", b)
        parents.append(parent_b)
        await_file(home / "dispatcher-ready-B")
        (home / "dispatch-B").touch()
        await_file(home / "started-B")
        b_observed = json.loads(await_file(home / "observed-B.json").read_text())
        assert b_observed["identity"] == "B"
        assert b_observed["pid"] != a_observed["pid"]
        worker_pids.append(b_observed["pid"])
        parent_b.wait(timeout=40)
        assert parent_b.returncode == 0
        await_file(home / "finished-B")
        for name, release, result in (("A", a, a_observed), ("B", b, b_observed)):
            assert Path(result["exe"]) == release / ".venv" / "bin" / "python"
            for path in [result["cwd"], *result["paths"].values()]:
                assert Path(path).resolve().is_relative_to(release), (name, path)
        with use_cron_store(home):
            assert latest_execution(job_a["id"])["status"] == "completed"
            assert latest_execution(job_b["id"])["status"] == "completed"
    finally:
        for parent in parents:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGTERM)
                parent.wait(timeout=5)
        for pid in worker_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
