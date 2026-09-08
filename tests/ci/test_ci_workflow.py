"""Automatic CI selection and final-result handling."""
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    # BaseLoader constructs strings/containers only, never Python objects.
    return yaml.load((ROOT / '.github/workflows/ci.yaml').read_text(), Loader=yaml.BaseLoader)


def result(needs, event='pull_request'):
    step = workflow()['jobs']['result']['steps'][0]
    script = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('PY', 1)[0]
    return subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                          env={**os.environ, 'NEEDS': json.dumps(needs), 'EVENT': event})


def needs(python=False, scan=False, deps=False, lock_scan=False):
    data = {key: {'result': 'skipped'} for key in workflow()['jobs']['result']['needs']}
    outputs = {key: 'false' for key in ('python', 'frontend', 'site', 'installer', 'rust', 'docker_meta', 'uv_lock', 'scan', 'deps', 'lock_scan', 'risk_full')}
    outputs['python'] = str(python).lower()
    outputs['scan'] = str(scan).lower()
    outputs['deps'] = str(deps).lower()
    outputs['lock_scan'] = str(lock_scan).lower()
    data['smoke'] = {'result': 'success', 'outputs': outputs}
    data['history']['result'] = 'success'
    if python:
        data['tests']['result'] = data['lint']['result'] = 'success'
    return data


def test_optional_lanes_can_skip_but_relevant_tests_cannot():
    assert result(needs()).returncode == 0
    assert result(needs(python=True)).returncode == 0
    data = needs(python=True)
    data['tests']['result'] = 'skipped'
    assert result(data).returncode != 0


def test_failed_and_cancelled_required_checks_never_pass():
    for outcome in ('failure', 'cancelled', 'skipped'):
        data = needs()
        data['smoke']['result'] = outcome
        assert result(data).returncode != 0
    data = needs(python=True)
    data['tests']['result'] = 'failure'
    assert result(data).returncode != 0


def test_manual_full_run_requires_every_lane():
    data = needs()
    assert result(data, 'workflow_dispatch').returncode != 0
    for job in data.values():
        job['result'] = 'success'
    assert result(data, 'workflow_dispatch').returncode == 0


def test_pull_request_requires_a_common_ancestor_check():
    data = needs()
    data['history']['result'] = 'failure'
    assert result(data).returncode != 0
    data['history']['result'] = 'success'
    assert result(data).returncode == 0


def test_changed_dependency_surfaces_require_blocking_security_checks():
    data = needs(scan=True)
    assert result(data).returncode != 0
    data['supply-chain']['result'] = 'success'
    assert result(data).returncode == 0

    data = needs(lock_scan=True)
    assert result(data).returncode != 0
    data['osv-scanner']['result'] = 'success'
    assert result(data).returncode == 0


def test_smoke_requires_no_missing_toolchain_and_no_label_reapproval():
    data = workflow()
    scripts = '\n'.join(step.get('run', '') for step in data['jobs']['smoke']['steps'])
    assert '--profile smoke' in scripts
    assert 'ci:full' not in json.dumps(data)
    assert data['concurrency']['cancel-in-progress'] == 'true'
    assert 'affected' not in data['jobs']
