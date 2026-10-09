"""Real cron handoffs pin the gateway's physical release across profile and pointer flips."""
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli.immutable_releases import promote, stage_release
from tests.hermes_cli.immutable_test_helpers import _build_test_venv

# scripts/run_tests_parallel.py kills a file after 300s with no attribution. Fail
# inside the test first, naming the phase that ran out of budget, while leaving
# room for interpreter startup, collection and teardown.
AGGREGATE_BUDGET_SECONDS = 240.0
# Per-wait cap: one stuck handshake must not spend the whole budget silently.
PHASE_TIMEOUT_SECONDS = 60.0


class _Budget:
    """One aggregate deadline shared by every wait, with per-phase timings."""

    def __init__(self, total: float):
        self.total = total
        self.start = time.monotonic()
        self.deadline = self.start + total
        self.phases: list[tuple[str, float]] = []
        self._mark = self.start

    def lap(self, phase: str) -> None:
        now = time.monotonic()
        self.phases.append((phase, now - self._mark))
        self._mark = now
        self.check(phase)

    def check(self, phase: str) -> None:
        if time.monotonic() >= self.deadline:
            raise AssertionError(self._exhausted(phase))

    def timeout(self, phase: str, cap: float = PHASE_TIMEOUT_SECONDS) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(self._exhausted(phase))
        return min(cap, remaining)

    def _exhausted(self, phase: str) -> str:
        return (f"aggregate budget of {self.total:g}s exhausted during {phase!r} "
                f"(elapsed {time.monotonic() - self.start:.1f}s); phases: {self.report()}")

    def report(self) -> str:
        return ", ".join(f"{name}={seconds:.1f}s" for name, seconds in self.phases)


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_detached_cron_workers_pin_both_profiles_before_and_after_flip(tmp_path, monkeypatch):
    from cron.jobs import create_job, use_cron_store

    budget = _Budget(AGGREGATE_BUDGET_SECONDS)
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for real release staging")
    home = tmp_path / "home"
    home.mkdir()
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(repo), str(source)], check=True)
    probe = source / "cron" / "s2_release_probe.py"
    revisions = []
    for name in ("A", "B"):
        probe.write_text(f"IDENTITY = {name!r}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", str(probe)], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=S2",
                        "-c", "user.email=s2@example.test", "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", f"test release {name}"], check=True)
        revisions.append(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                               text=True).strip())
    budget.lap("clone and commit revisions")
    from hermes_cli import immutable_releases as releases
    # This test verifies immutable Python/cron worker identity, not the optional web bundle.
    # Building npm assets for each disposable revision dominated the native-file budget on
    # shared macOS runners and could consume the whole 300-second file deadline.
    monkeypatch.setattr(releases, "_build_candidate_web", lambda _staging: None)
    # Dependency isolation is out of scope here: the .pth reuses the runner's
    # site-packages. Real _build_venv coverage lives in test_immutable_releases.py:354 and :913.
    monkeypatch.setattr(releases, "_build_venv", _build_test_venv)
    monkeypatch.setattr(releases, "restore_active_distributions", lambda *args, **kwargs: None)
    a, _ = stage_release(source, home, sha=revisions[0], uv=uv)
    budget.lap("stage release A")
    b, _ = stage_release(source, home, sha=revisions[1], uv=uv)
    budget.lap("stage release B")
    for name, release in (("A", a), ("B", b)):
        # Exercise the candidate's scheduler and its job/deadline dependencies
        # together; copying only the facade mixes it with the cloned base API.
        for relative in ("cron/scheduler.py", "cron/scheduler_detached_worker.py", "cron/jobs.py",
                         "hermes_cli/immutable_releases.py"):
            shutil.copy2(repo / relative, release / relative)
        (release / "cron" / "s2_release_probe.py").write_text(f"IDENTITY = {name!r}\n", encoding="utf-8")
        # The worker's own startup hook waits for the script barrier. That keeps
        # a real detached process alive while current is changed under it.
        (release / "sitecustomize.py").write_text(
            "import json,os,pathlib,sys,threading,time,shutil\n"
            "def probe():\n"
            "    if '--external-worker-file' not in sys.argv: return\n"
            "    payload=pathlib.Path(sys.argv[sys.argv.index('--external-worker-file')+1])\n"
            "    job=json.loads(payload.read_text())['job']['name'].rsplit(' ',1)[-1]\n"
            "    h=pathlib.Path(os.environ['HERMES_HOME'])\n"
            "    trigger=h/f'probe-{job}'\n"
            "    deadline=time.monotonic()+60\n"
            "    while time.monotonic()<deadline:\n"
            "        if trigger.exists() and 'cron.scheduler' in sys.modules:\n"
            "            import hermes_cli,cron.s2_release_probe\n"
            "            import subprocess\n"
            "            actual={'pid':os.getpid(),'exe':sys.executable,'prefix':sys.prefix,"
            "'cwd':os.getcwd(),'module':str(pathlib.Path(hermes_cli.__file__).resolve()),"
            "'identity':cron.s2_release_probe.IDENTITY,'venv':os.getenv('VIRTUAL_ENV'),"
            "'release':os.getenv('HERMES_RELEASE'),'which_python':shutil.which('python'),"
            "'which_hermes':shutil.which('hermes')}\n"
            "            output=h/f'observed-{job}.json'\n"
            "            pending=output.with_suffix('.json.tmp')\n"
            "            pending.write_text(json.dumps(actual))\n"
            "            os.replace(pending,output)\n"
            "            break\n"
            "        time.sleep(.05)\n"
            "threading.Thread(target=probe,daemon=True).start()\n",
            encoding="utf-8",
        )
    promote(home, a)
    budget.lap("overlay candidate and promote A")
    parents = []
    worker_pids = []

    def await_file(path):
        deadline = time.monotonic() + budget.timeout(f"wait for {path.name}")
        while time.monotonic() < deadline:
            if path.exists():
                return path
            time.sleep(.05)
        budget.check(f"wait for {path.name}")
        raise AssertionError(f"missing {path} after {PHASE_TIMEOUT_SECONDS:g}s; phases: {budget.report()}")

    def await_json(path):
        deadline = time.monotonic() + budget.timeout(f"wait for {path.name}")
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.05)
        budget.check(f"wait for {path.name}")
        raise AssertionError(f"missing or incomplete JSON: {path}; phases: {budget.report()}")

    def dispatch(name, release, profile):
        profile_home = home if profile == "default" else home / "profiles" / "p"
        profile_home.mkdir(parents=True, exist_ok=True)
        with use_cron_store(profile_home):
            job = create_job(prompt=None, schedule="every 1h", name=f"S2 {name}",
                             script=f"s2_{name}.py", no_agent=True)
        scripts = profile_home / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / f"s2_{name}.py").write_text(
            "import pathlib,os,time\n"
            "h=pathlib.Path(os.environ['HERMES_HOME'])\n"
            f"(h/'started-{name}').touch()\n"
            f"barrier=h/'finish-{name}'\n"
            "deadline=time.monotonic()+60\n"
            "while not barrier.exists() and time.monotonic()<deadline: time.sleep(.05)\n"
            "assert barrier.exists()\n", encoding="utf-8")
        payload = profile_home / f"job-{name}.json"
        payload.write_text(json.dumps(job), encoding="utf-8")
        code = (
            "import json,os,pathlib,time\n"
            f"os.environ['HERMES_HOME']={str(profile_home)!r}\n"
            "os.environ['HERMES_LAUNCHD_LABEL']='ai.hermes.s2spike.cron'\n"
            "from cron import scheduler\n"
            "from tools import process_registry\n"
            "process_registry._is_supervised_gateway_process=lambda: True\n"
            f"job=json.loads(pathlib.Path({str(payload)!r}).read_text())\n"
            f"h=pathlib.Path({str(profile_home)!r})\n"
            f"(h/'dispatcher-ready-{name}').touch()\n"
            "deadline=time.monotonic()+60\n"
            f"while not (h/'dispatch-{name}').exists() and time.monotonic()<deadline: time.sleep(.05)\n"
            "assert scheduler.run_one_job(job,adapters=None,loop=None)\n"
        )
        # A imports through the lexical pointer; the module must resolve its
        # physical installation before the pointer flips, regardless of profile.
        import_root = home / "current" if release == a else release
        proc = subprocess.Popen([str(release / ".venv" / "bin" / "python"), "-c", code],
                                cwd=home, env={**os.environ, "HERMES_HOME": str(profile_home),
                                               "PYTHONPATH": str(import_root)},
                                start_new_session=True)
        parents.append(proc)
        await_file(profile_home / f"dispatcher-ready-{name}")
        return profile_home, proc

    try:
        initial = [dispatch("A-default", a, "default"), dispatch("A-profile", a, "p")]
        delayed = dispatch("A-delayed", a, "default")
        budget.lap("spawn A dispatchers")
        for profile_home, _ in initial:
            name = "A-default" if profile_home == home else "A-profile"
            (profile_home / f"dispatch-{name}").touch()
            await_file(profile_home / f"started-{name}")
        budget.lap("start A workers")
        promote(home, b)
        later = [delayed, dispatch("B-default", b, "default"), dispatch("B-profile", b, "p")]
        budget.lap("promote B and spawn B dispatchers")
        for (profile_home, parent), name in zip(initial + later,
                                                 ("A-default", "A-profile", "A-delayed", "B-default", "B-profile")):
            if name.startswith("B-") or name == "A-delayed":
                (profile_home / f"dispatch-{name}").touch()
                await_file(profile_home / f"started-{name}")
                budget.lap(f"start {name} worker")
            (profile_home / f"probe-{name}").touch()
            result = await_json(profile_home / f"observed-{name}.json")
            budget.lap(f"probe {name}")
            worker_pids.append(result["pid"])
            print(f"{name}: {json.dumps(result, sort_keys=True)}")
            release = a if name.startswith("A-") else b
            assert result["identity"] == name[0], (name, result)
            assert Path(result["exe"]) == release / ".venv" / "bin" / "python", (name, result)
            assert result["prefix"] == str(release / ".venv"), (name, result)
            assert result["venv"] == str(release / ".venv"), (name, result)
            assert result["release"] == str(release), (name, result)
            assert result["which_python"] == str(release / ".venv" / "bin" / "python"), (name, result)
            assert result["which_hermes"] == str(release / ".venv" / "bin" / "hermes"), (name, result)
            assert Path(result["module"]).is_relative_to(release), (name, result)
            assert Path(result["cwd"]) == release, (name, result)
            (profile_home / f"finish-{name}").touch()
            try:
                parent.wait(timeout=budget.timeout(f"finish {name}"))
            except subprocess.TimeoutExpired:
                budget.check(f"finish {name}")
                raise AssertionError(f"{name} dispatcher did not exit; phases: {budget.report()}")
            assert parent.returncode == 0, (name, parent.returncode)
            budget.lap(f"finish {name}")
        print(f"phase timings: {budget.report()}")
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
