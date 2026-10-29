"""Commit-build admission rejects mixed inputs before repository code runs."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from scripts.releases.commit_build import publish_receipt, receipt_tag
from scripts.releases.versioning import tag_record
from tests.ci.test_desktop_release_tag_admission import _child_env, _git, _seed_repo, _BASH












def environment(clone, commit):
    return _child_env(
        TAG='', BUILD_COMMIT=commit, RELEASE_PHASE='', UPLOAD_RELEASE='false',
        TERMUX_UPGRADE_FROM_TAG='', DEFAULT_BRANCH='main', GITHUB_REF='refs/heads/main',
        GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REPOSITORY='fixture/repo',
        GITHUB_WORKFLOW_REF='fixture/repo/.github/workflows/desktop-bundled-release.yml@refs/heads/main',
        GITHUB_ACTOR='maintainer', GITHUB_TRIGGERING_ACTOR='maintainer',
        GITHUB_OUTPUT=str(clone / 'outputs'), GH_TOKEN='fixture-token',
        GIT_ALLOW_PROTOCOL='file', PYTHONUTF8='1',
    )



def test_post_build_receipts_bind_kind_commit_and_run_without_same_second_collisions(tmp_path):
    _origin, clone = _seed_repo(tmp_path)
    commit = _git('rev-parse', 'HEAD', cwd=clone)
    created_at = '2026-09-22T01:23:45Z'
    assert receipt_tag('commit', '0.0.0', created_at, '123') == \
        'v0.0.0+commit.20260922T012345Z.123'
    assert receipt_tag('commit', '0.0.0', created_at, '124') != \
        receipt_tag('commit', '0.0.0', created_at, '123')

    def run(argv, repo=None):
        if argv[:2] == ['gh', 'api'] and argv[-1] == '.permission':
            return 'write'
        if argv[:2] == ['gh', 'api'] and '/actions/runs/' in argv[2]:
            run_id = argv[2].rsplit('/', 1)[-1]
            return json.dumps({
                'id': int(run_id), 'event': 'workflow_dispatch', 'status': 'in_progress',
                'head_branch': 'main', 'head_sha': commit, 'created_at': created_at,
            })
        return subprocess.check_output(argv, cwd=repo or clone, text=True, encoding='utf-8').strip()

    base = {
        **environment(clone, commit),
        'GITHUB_ACTIONS': 'true',
        'GITHUB_SHA': commit,
        'GITHUB_RUN_ID': '123',
    }
    first = publish_receipt(
        'commit', base, version='0.0.0', commit=commit,
        details={'bundleEnv': {}}, run=run, repo=clone,
    )
    second = publish_receipt(
        'commit', {**base, 'GITHUB_RUN_ID': '124'}, version='0.0.0', commit=commit,
        details={'bundleEnv': {}}, run=run, repo=clone,
    )
    assert [first['tag'], second['tag']] == [
        'v0.0.0+commit.20260922T012345Z.123',
        'v0.0.0+commit.20260922T012345Z.124',
    ]
    assert tag_record(_git('tag', '-l', first['tag'], '--format=%(contents)', cwd=clone)) == first
    assert _git('rev-parse', f"{first['tag']}^{{commit}}", cwd=clone) == commit
    assert publish_receipt(
        'commit', base, version='0.0.0', commit=commit,
        details={'bundleEnv': {}}, run=run, repo=clone,
    ) == first
