"""Narrow priority admission must not become an agent or arbitrary script bypass."""

import json
import os
import threading

import pytest

from cron import executions, jobs, scheduler
from cron import scheduler_admission as admission
from cron import scheduler_script
from cron.scheduler_provider import _profile_cron_scope
from hermes_constants import hermes_home_key
from tests.cron.test_script_admission import due_job, home
from tests.waiting import wait_until


def declared_send(path, now, name="send"):
    script = path / "scripts" / f"{name}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(admission.PERSON_SEND_MARKER + b"\npass\n")
    return due_job(now, schedule="in 1m", repeat=1, no_agent=True, script=str(script))


@pytest.mark.parametrize("change", ["agent", "recurring", "delivery", "missing", "outside",
                                     "prefix", "late_marker", "invalid_path"])
def test_only_explicit_local_oneshot_scripts_qualify(home, tmp_path, change):
    path, now = home
    job = declared_send(path, now)
    assert admission.is_person_send(job)
    if change == "agent":
        job["no_agent"] = False
    elif change == "recurring":
        job["schedule"] = {"kind": "interval", "minutes": 1}
    elif change == "delivery":
        job["deliver"] = "origin"
    elif change == "missing":
        job["script"] = "missing.py"
    elif change == "outside":
        outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
        outside.write_bytes(admission.PERSON_SEND_MARKER + b"\npass\n")
        job["script"] = str(outside)
    elif change == "prefix":
        script = path / "scripts" / "prefix.py"
        script.write_bytes(admission.PERSON_SEND_MARKER + b"x\npass\n")
        job["script"] = str(script)
    elif change == "late_marker":
        script = path / "scripts" / "late.py"
        script.write_bytes(b"# ordinary\n" + admission.PERSON_SEND_MARKER + b"\npass\n")
        job["script"] = str(script)
    elif change == "invalid_path":
        job["script"] = "\0"
    assert not admission.is_person_send(job)


@pytest.mark.parametrize("symlink", [False, True])
def test_cloud_scripts_never_open_during_admission(home, tmp_path, monkeypatch, symlink):
    from pathlib import Path

    path, now = home
    job = declared_send(path, now)
    cloud_home = tmp_path / "Library" / "CloudStorage" / "profile"
    cloud_script = cloud_home / "scripts" / "send.py"
    cloud_script.parent.mkdir(parents=True)
    cloud_script.write_bytes(admission.PERSON_SEND_MARKER + b"\npass\n")
    if symlink:
        scripts = path / "scripts"
        (scripts / "send.py").unlink()
        scripts.rmdir()
        scripts.symlink_to(cloud_home / "scripts", target_is_directory=True)
        owner_home = path
    else:
        owner_home = cloud_home
        job["script"] = str(cloud_script)
    with _profile_cron_scope(owner_home):
        opened = []
        original_open = Path.open

        def record_open(candidate, *args, **kwargs):
            opened.append(candidate)
            return original_open(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "open", record_open)
        assert not admission.is_person_send(job)
        assert not opened, "admission tried to hydrate a cloud script"


@pytest.mark.parametrize("edit", ["script", "agent", "delivery", "recurring"])
def test_queued_edits_reroute_before_claim_without_losing_execution(home, monkeypatch, edit):
    path, now = home
    first = declared_send(path, now, "first")
    changed = declared_send(path, now, "changed")
    tail = declared_send(path, now, "tail")
    blocker = due_job(now, no_agent=edit != "agent")
    release_first, release_blocker = threading.Event(), threading.Event()
    first_started, blocker_started, changed_started, tail_done = (threading.Event() for _ in range(4))
    starts = []
    monkeypatch.setattr(scheduler, "_resolve_max_parallel_workers", lambda: 1)

    def run(item, **kwargs):
        starts.append((item["id"], threading.current_thread().name))
        if item["id"] == first["id"]:
            first_started.set()
            assert release_first.wait(10)
        elif item["id"] == blocker["id"]:
            blocker_started.set()
            assert release_blocker.wait(10)
        elif item["id"] == changed["id"]:
            changed_started.set()
        elif item["id"] == tail["id"]:
            tail_done.set()
        return True

    monkeypatch.setattr(scheduler, "run_one_job", run)
    try:
        assert scheduler.tick(sync=False) == 4
        assert first_started.wait(5) and blocker_started.wait(5)
        receipt = executions.latest_execution(changed["id"])
        if edit == "script":
            (path / "scripts" / "changed.py").write_text("pass\n", encoding="utf-8")
        elif edit == "agent":
            jobs.update_job(changed["id"], {"no_agent": False})
        elif edit == "delivery":
            jobs.update_job(changed["id"], {"deliver": "origin"})
        else:
            jobs.update_job(changed["id"], {"schedule": "every 10m"})
        release_first.set()
        assert tail_done.wait(5), "changed work occupied person-send capacity"
        assert not changed_started.is_set(), "changed work bypassed its occupied target lane"
        assert scheduler.is_job_running(changed["id"])
        persisted = jobs.get_job(changed["id"])
        assert persisted is not None and not persisted.get("fire_claim")
        assert scheduler.tick(sync=False) == 0, "reroute lost per-job admission ownership"
        release_blocker.set()
        assert changed_started.wait(5)
        assert wait_until(lambda: not scheduler.is_job_running(changed["id"]), timeout=5)
        matching = [name for job_id, name in starts if job_id == changed["id"]]
        assert len(matching) == 1
        assert not matching[0].startswith("cron-person-send")
        latest = executions.latest_execution(changed["id"])
        assert latest is not None and receipt is not None and latest["id"] == receipt["id"]
    finally:
        release_first.set()
        release_blocker.set()


def test_person_send_capacity_is_serial_profile_scoped_and_reclaimed(home, tmp_path):
    path, now = home
    first = declared_send(path, now, "first")
    second = declared_send(path, now, "second")
    release, entered = threading.Event(), threading.Event()
    starts = []

    def body(item):
        starts.append((item["id"], hermes_home_key(scheduler._get_hermes_home())))
        if item["id"] == first["id"]:
            entered.set()
            assert release.wait(10)
        return True

    futures = []
    other_home = tmp_path / "other"
    try:
        pool = scheduler._get_parallel_pool(1)
        futures += admission.submit_due_jobs([first, second], pool, body)
        assert entered.wait(5)
        assert second["id"] not in [job_id for job_id, _ in starts]
        assert admission.submit_due_jobs([first, second], pool, body) == []
        with _profile_cron_scope(other_home):
            other = declared_send(other_home, now)
            other_future = admission.submit_due_jobs([other], scheduler._get_parallel_pool(1), body)[0]
            assert other_future.result(timeout=5)
            assert starts[-1] == (other["id"], hermes_home_key(other_home))
        scheduler.discard_parallel_pools([hermes_home_key(other_home)])
        assert hermes_home_key(other_home) not in admission._lane_pools["person-send"]
        assert hermes_home_key(path) in admission._lane_pools["person-send"]
    finally:
        release.set()
        for future in futures:
            assert future.result(timeout=5)
    assert [job_id for job_id, key in starts if key == hermes_home_key(path)] == [first["id"], second["id"]]
    scheduler._shutdown_parallel_pool()
    assert not admission._lane_pools["person-send"]


def test_real_concurrent_scripts_keep_cwd_and_profile_environment_local(home, tmp_path, monkeypatch):
    """Cover the real subprocess/env boundary called out by upstream #73804."""
    path, now = home
    other_home = tmp_path / "profile-b"
    other_home.mkdir()
    workdirs = [path / "work-a", other_home / "work-b"]
    for workdir in workdirs:
        workdir.mkdir()
    # Both children receive the routed profile, not a sibling's process context.
    ambient_home = os.environ.get("HERMES_HOME")
    monkeypatch.setenv("TERMINAL_CWD", "launch-cwd")
    before_cwd = os.getcwd()
    original_run = scheduler_script._run_job_script
    barrier = threading.Barrier(2)

    def synchronized_run(*args, **kwargs):
        barrier.wait(timeout=5)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(scheduler_script, "_run_job_script", synchronized_run)
    futures = []
    results = []

    def body(item):
        result = scheduler.run_job(item)
        results.append(result)
        return result

    for index, owner_home in enumerate([path, other_home]):
        with _profile_cron_scope(owner_home):
            item = declared_send(owner_home, now)
            (owner_home / "scripts" / "send.py").write_text(
                admission.PERSON_SEND_MARKER.decode() + "\nimport json, os\n"
                "print(json.dumps({'cwd': os.getcwd(), 'home': os.environ['HERMES_HOME']}))\n",
                encoding="utf-8")
            item = jobs.update_job(item["id"], {"workdir": str(workdirs[index])})
            assert item is not None
            futures += admission.submit_due_jobs([item], scheduler._get_parallel_pool(1), body)
    for future in futures:
        success, _, output, error = future.result(timeout=10)
        assert success, error
        actual = json.loads(output)
        assert actual in [{"cwd": str(workdirs[0].resolve()), "home": str(path.resolve())},
                          {"cwd": str(workdirs[1].resolve()), "home": str(other_home.resolve())}]
    assert len(results) == 2
    assert os.getcwd() == before_cwd
    assert os.environ["TERMINAL_CWD"] == "launch-cwd"
    assert os.environ.get("HERMES_HOME") == ambient_home
