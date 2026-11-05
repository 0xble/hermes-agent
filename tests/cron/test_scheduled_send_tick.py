"""Credential-free full-tick canaries for the shared scheduled-send boundary.

The checker is a real subprocess and the payload is a real local script. It is
synthetic reader evidence, not provider coverage or person-transport proof.
"""
import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.mark.parametrize('stale', [False, True], ids=['fresh-reader', 'stale-reader'])
def test_full_tick_checks_before_local_payload(tmp_path, monkeypatch, stale):
    import httpx
    from cron import executions, jobs, scheduler, scheduler_worker_env
    import hermes_constants

    home = tmp_path / 'home'
    (home / 'cron').mkdir(parents=True)
    (home / 'scripts').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_TEST_ISOLATION', str(home))
    for key, value in {'HERMES_DIR': home, 'CRON_DIR': home / 'cron',
                       'JOBS_FILE': home / 'cron/jobs.json',
                       'OUTPUT_DIR': home / 'cron/output'}.items():
        monkeypatch.setattr(jobs, key, value)
    monkeypatch.setattr(scheduler, '_hermes_home', home)
    monkeypatch.setattr(scheduler, '_sweep_mcp_orphans', lambda: None)
    monkeypatch.setattr(scheduler, '_maybe_run_worktree_maintenance', lambda: None)
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: home)
    assert httpx.__file__ is not None
    dependencies = Path(httpx.__file__).resolve().parent.parent
    monkeypatch.setattr(scheduler_worker_env, '_committed_dependency_site_packages',
                        lambda root: dependencies)
    monkeypatch.setattr(scheduler.immutable_releases, 'LOADED_RELEASE_ROOT', None)
    monkeypatch.setattr(scheduler, '_running_job_ids', set())
    checked = home / 'checked.json'
    checker = home / 'synthetic_reader.py'
    checker.write_text(
        'import json, pathlib, sys\n'
        'request = json.load(sys.stdin)\n'
        f'pathlib.Path({str(checked)!r}).write_text(json.dumps(request))\n'
        f'print({json.dumps({"checked": True, "stale": stale, "reason": "newer reply" if stale else "synthetic quiet"})!r})\n')
    marker = home / 'payload.txt'
    script = home / 'scripts' / 'local-marker.sh'
    script.write_text('#!/bin/sh\nprintf exercised > ' + shlex.quote(str(marker))
                      + '\nprintf exercised\n')
    script.chmod(0o700)
    now = datetime.now(UTC)
    spec = {'channel': 'imessage', 'conversation': '42', 'recipient': 'synthetic-fixture',
            'approved_at': (now - timedelta(seconds=30)).isoformat(), 'script': str(checker)}
    job = jobs.create_job(prompt=None, schedule='in 1m', script=script.name,
                          no_agent=True, deliver='local', send_freshness=spec)
    records = jobs.load_jobs()
    records[0]['next_run_at'] = now.isoformat()
    records[0]['schedule']['run_at'] = now.isoformat()
    jobs.save_jobs(records)

    assert scheduler.tick(headless=True, verbose=False) == 1
    request = json.loads(checked.read_text())
    assert request['job_id'] == job['id']
    assert request['conversation'] == '42' and request['recipient'] == 'synthetic-fixture'
    assert request['scheduled_at'] == now.isoformat()
    assert 'script' not in request
    stored = jobs.get_job(job['id'])
    rows = executions.list_executions(job_id=job['id'])
    assert len(rows) == 1
    if stale:
        assert not marker.exists()
        assert stored is not None
        assert stored['state'] == 'missed' and stored['enabled'] is False
        assert stored['script'] == script.name
        assert stored['send_freshness'] == spec
        assert 'newer reply' in stored['missed_reason']
        assert rows[0]['status'] == 'failed'
        assert 'held; payload kept' in rows[0]['error']
    else:
        assert marker.read_text() == 'exercised'
        assert rows[0]['status'] == 'completed'
    # Both terminal paths are fenced: another tick cannot execute the payload.
    assert scheduler.tick(headless=True, verbose=False) == 0
    assert executions.list_executions(job_id=job['id']) == rows
