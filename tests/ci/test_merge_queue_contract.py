"""Execute queue admission/qualification scripts and reject weakened workflow wiring."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
ELIGIBLE = "${{ !github.event.pull_request.draft || (github.event.pull_request.user.login == 'mergify[bot]' && startsWith(github.event.pull_request.head.ref, 'mergify/merge-queue/')) }}"
FULL = "${{ needs.profile.outputs.full_gate == 'true' }}"
HEAD = "${{ github.event.pull_request.head.sha }}"


def workflow():
    return YAML(typ='base').load((ROOT / '.github/workflows/gate.yml').read_text())


def step(job, name):
    return next(s for s in job['steps'] if s.get('id') == name or s.get('name') == name)


def contract(w):
    jobs = w['jobs']
    assert w['on']['pull_request']['types'] == ['opened', 'synchronize', 'reopened', 'ready_for_review']
    assert jobs['profile']['if'].strip() == ELIGIBLE
    assert jobs['profile']['outputs']['full_gate'] == '${{ steps.profile.outputs.full_gate }}'
    select = step(jobs['profile'], 'profile')
    assert select['env']['BASE_SHA'] == '${{ github.event.pull_request.base.sha }}'
    assert 'git ls-tree "$BASE_SHA" -- .mergify.yml' in select['run']
    assert 'git ls-tree refs/remotes/origin/main -- .mergify.yml' in select['run']
    assert 'git cat-file -e "$BASE_SHA^{commit}"' in select['run']
    assert jobs['pr-checks']['needs'] == ['profile']
    assert jobs['pr-checks']['if'] == "${{ needs.profile.outputs.full_gate == 'false' }}"
    assert jobs['pr-checks']['runs-on'] == 'ubuntu-slim'
    assert step(jobs['pr-checks'], 'Bounded ready checks')['run'] == './bin/ci preflight'
    for name in ('linux', 'python-shard'):
        job = jobs[name]
        assert job['needs'] == ['profile'] and job['if'] == FULL
        checkout = next(s for s in job['steps'] if 'uses' in s)
        assert checkout['with']['ref'] == HEAD
        assert './bin/ci gate' in '\n'.join(s.get('run', '') for s in job['steps'])
    assert jobs['python-shard']['strategy']['fail-fast'] == 'false'
    assert jobs['python-shard']['strategy']['matrix']['shard'] == [str(i) for i in range(1, 11)]
    assert "--shard '${{ matrix.shard }}/10'" in step(jobs['python-shard'], 'Exact-SHA Python shard')['run']
    assert jobs['linux']['container'] == jobs['python-shard']['container']
    q = jobs['qualification']
    assert q['if'] == 'always()'
    assert q['needs'] == ['profile', 'linux', 'python-shard']
    assert step(q, 'Require every full gate job')['env']['FULL_GATE'] == '${{ needs.profile.outputs.full_gate }}'
    assert "os.environ.get('FULL_GATE') == 'true' and" in step(q, 'Require every full gate job')['run']


def test_workflow_contract():
    contract(workflow())


@pytest.mark.parametrize('mutation', [
    'draft', 'author', 'prefix', 'head-not-base', 'missing-base', 'linux-unconditional',
    'shards-unconditional', 'omit-shard', 'matrix-shortened', 'preflight-qualifies',
    'not-exact-head', 'no-ready-event', 'preflight-unconditional', 'stale-base-bootstraps',
])
def test_each_loosening_is_rejected(mutation):
    w = copy.deepcopy(workflow())
    j = w['jobs']
    if mutation in ('draft', 'author', 'prefix'):
        j['profile']['if'] = {'draft': '${{ true }}', 'author': "${{ !github.event.pull_request.draft || startsWith(github.event.pull_request.head.ref, 'mergify/merge-queue/') }}", 'prefix': "${{ !github.event.pull_request.draft || github.event.pull_request.user.login == 'mergify[bot]' }}"}[mutation]
    elif mutation == 'head-not-base':
        step(j['profile'], 'profile')['env']['BASE_SHA'] = HEAD
    elif mutation == 'stale-base-bootstraps':
        s = step(j['profile'], 'profile')
        s['run'] = s['run'].replace(' && [ -z "$(git ls-tree refs/remotes/origin/main -- .mergify.yml)" ]', '')
    elif mutation == 'missing-base':
        s = step(j['profile'], 'profile')
        s['run'] = s['run'].replace('git cat-file -e "$BASE_SHA^{commit}"', 'true')
    elif mutation in ('linux-unconditional', 'shards-unconditional'):
        j['linux' if mutation.startswith('linux') else 'python-shard']['if'] = '${{ true }}'
    elif mutation == 'omit-shard':
        j['qualification']['needs'].remove('python-shard')
    elif mutation == 'matrix-shortened':
        j['python-shard']['strategy']['matrix']['shard'].pop()
    elif mutation == 'preflight-qualifies':
        s = step(j['qualification'], 'Require every full gate job')
        s['run'] = s['run'].replace("os.environ.get('FULL_GATE') == 'true' and ", '')
    elif mutation == 'not-exact-head':
        next(s for s in j['linux']['steps'] if 'uses' in s)['with']['ref'] = '${{ github.sha }}'
    elif mutation == 'no-ready-event':
        w['on']['pull_request']['types'].remove('ready_for_review')
    else:
        j['pr-checks']['if'] = '${{ always() }}'
    with pytest.raises(AssertionError):
        contract(w)


@pytest.fixture
def bases(tmp_path):
    """A clone of a bare origin: bootstrap (no queue) and queued commits, main at queued."""
    origin, work = tmp_path / 'origin.git', tmp_path / 'work'
    # Developer hooks (global core.hooksPath) must not run inside the fixture.
    isolated = ['git', '-c', 'core.hooksPath=/dev/null']
    subprocess.check_call([*isolated, 'init', '-q', '--bare', str(origin)])
    subprocess.check_call([*isolated, 'clone', '-q', str(origin), str(work)], stderr=subprocess.DEVNULL)

    def git(*args):
        return subprocess.check_output([*isolated, *args], cwd=work, text=True).strip()
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Queue contract')
    git('checkout', '-q', '-b', 'main')
    git('commit', '-qm', 'bootstrap', '--allow-empty')
    bootstrap = git('rev-parse', 'HEAD')
    git('push', '-q', 'origin', 'main')
    (work / '.mergify.yml').write_text('queue_rules: []\n')
    git('add', '.')
    git('commit', '-qm', 'queue exists')
    queued = git('rev-parse', 'HEAD')
    return work, bootstrap, queued, git


def run_profile(bases, base, author='contributor', ref='feature', main=None):
    directory, _, queued, git = bases
    git('push', '-q', '--force', 'origin', f'{main or queued}:refs/heads/main')
    output = directory.parent / 'output'
    output.write_text('')
    run = subprocess.run(['bash', '-c', step(workflow()['jobs']['profile'], 'profile')['run']], cwd=directory,
                         env={**os.environ, 'BASE_SHA': base, 'PR_AUTHOR': author, 'HEAD_REF': ref, 'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
    return run.returncode, output.read_text().strip()


def qualification(full_gate, **results):
    return subprocess.run(['bash', '-c', step(workflow()['jobs']['qualification'], 'Require every full gate job')['run']],
                          env={**os.environ, 'FULL_GATE': full_gate, 'NEEDS_JSON': json.dumps({k: {'result': v} for k, v in results.items()})}, capture_output=True).returncode


def test_ready_pr_runs_only_preflight_and_cannot_qualify(bases):
    assert run_profile(bases, bases[2]) == (0, 'full_gate=false')
    assert qualification('false', profile='success', linux='skipped', **{'python-shard': 'skipped'}) != 0
    # Even fabricated successful lane results cannot qualify a preflight profile.
    assert qualification('false', profile='success', linux='success', **{'python-shard': 'success'}) != 0


def test_batch_draft_runs_complete_gate(bases):
    assert run_profile(bases, bases[2], 'mergify[bot]', 'mergify/merge-queue/abcdef1234') == (0, 'full_gate=true')
    assert qualification('true', profile='success', linux='success', **{'python-shard': 'success'}) == 0


def test_bootstrap_runs_full_gate_only_while_main_has_no_queue(bases):
    assert run_profile(bases, bases[1], main=bases[1]) == (0, 'full_gate=true')


def test_stale_pre_queue_base_cannot_bootstrap_after_cutover(bases):
    # A PR opened (or a run re-run) against the pre-queue base after the queue landed.
    assert run_profile(bases, bases[1]) == (0, 'full_gate=false')


@pytest.mark.parametrize('base', ['bad-sha', '0' * 40])
def test_unavailable_base_fails_closed(bases, base):
    assert run_profile(bases, base)[0] != 0


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped', 'timed_out', ''])
def test_every_shard_must_succeed(result):
    assert qualification('true', profile='success', linux='success', **{'python-shard': result}) != 0


def test_ordinary_draft_has_no_lane_and_fails_qualification():
    # The exact eligibility expression is validated above; both draft exceptions
    # require bot identity AND queue prefix, not either one independently.
    assert workflow()['jobs']['profile']['if'].strip() == ELIGIBLE
    assert qualification('', profile='skipped', linux='skipped', **{'python-shard': 'skipped'}) != 0


def mergify():
    return YAML(typ='safe').load((ROOT / '.mergify.yml').read_text())


def queue_contract(config):
    # No autoqueue / auto_merge_conditions / retries: admission is the explicit command only.
    assert set(config) == {'merge_queue', 'queue_rules'}
    assert config['merge_queue'] == {'max_parallel_checks': 1}
    sync, default = config['queue_rules']
    assert sync['name'] == 'upstream-sync' and sync['batch_size'] == 1
    assert 'head ~= ^(sync|candidate)/' in sync['queue_conditions']
    assert default['name'] == 'default' and default['batch_size'] == 5
    assert default['batch_max_wait_time'] == '1 min'
    for q in (sync, default):
        assert set(q) <= {'name', 'batch_size', 'batch_max_wait_time', 'merge_method', 'branch_protection_injection_mode', 'queue_conditions', 'merge_conditions'}
        assert q['merge_method'] == 'fast-forward'
        assert q['branch_protection_injection_mode'] == 'merge'
        assert {'base = main', '-draft', 'check-success = pr-checks',
                'check-success = landing/reviewed-gated'} <= set(q['queue_conditions'])
        assert q['merge_conditions'] == ['check-success = qualification']


def test_queue_is_command_only_and_preserves_tested_sha():
    queue_contract(mergify())


@pytest.mark.parametrize('mutation', [
    'autoqueue', 'auto-merge-conditions', 'retries', 'parallel', 'squash', 'no-qualification',
    'drafts-admitted', 'sync-batched', 'sync-after-default', 'injection-queue',
    'sync-unreviewed', 'default-unreviewed', 'status-success-unsupported',
])
def test_each_queue_loosening_is_rejected(mutation):
    config = copy.deepcopy(mergify())
    sync, default = config['queue_rules']
    if mutation == 'autoqueue':
        default['autoqueue'] = True
    elif mutation == 'auto-merge-conditions':
        config['merge_protections_settings'] = {'auto_merge_conditions': ['base = main']}
    elif mutation == 'retries':
        default['max_checks_retries'] = 1
    elif mutation == 'parallel':
        config['merge_queue']['max_parallel_checks'] = 2
    elif mutation == 'squash':
        default['merge_method'] = 'squash'
    elif mutation == 'no-qualification':
        default['merge_conditions'] = ['check-success = pr-checks']
    elif mutation == 'drafts-admitted':
        default['queue_conditions'].remove('-draft')
    elif mutation in ('sync-unreviewed', 'default-unreviewed'):
        q = sync if mutation == 'sync-unreviewed' else default
        q['queue_conditions'].remove('check-success = landing/reviewed-gated')
    elif mutation == 'status-success-unsupported':
        default['queue_conditions'].remove('check-success = landing/reviewed-gated')
        default['queue_conditions'].append('status-success = landing/reviewed-gated')
    elif mutation == 'sync-batched':
        sync['batch_size'] = 5
    elif mutation == 'sync-after-default':
        config['queue_rules'].reverse()
    else:
        sync['branch_protection_injection_mode'] = 'queue'
    with pytest.raises(AssertionError):
        queue_contract(config)
