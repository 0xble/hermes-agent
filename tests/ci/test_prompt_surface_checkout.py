"""Exercise the prompt review job's checkout against real full-history Git snapshots."""
from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import hermes_yaml

ROOT = Path(__file__).resolve().parents[2]
SNAP = Path('tests/fixtures/prompt_surface')


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=cwd, check=True, text=True, capture_output=True,
    ).stdout.strip()


def test_prompt_review_fetches_history_without_unrelated_historical_blobs(tmp_path):
    workflow = hermes_yaml.safe_load((ROOT / '.github/workflows/prompt-surface-diff.yml').read_text())
    checkout = next(step for step in workflow['jobs']['diff']['steps'] if step.get('uses', '').startswith('actions/checkout@'))
    options = checkout['with']
    assert options['fetch-depth'] == 0, 'merge-base review requires complete commit ancestry'

    origin = tmp_path / 'origin'
    origin.mkdir()
    _git(origin, 'init', '-b', 'main')
    _git(origin, 'config', 'user.name', 'Prompt Review Test')
    _git(origin, 'config', 'user.email', 'prompt-review@example.invalid')
    _git(origin, 'config', 'uploadpack.allowFilter', 'true')
    bulky = origin / 'retired-artifact.bin'
    bulky.write_bytes(random.Random(0).randbytes(1024 * 1024))
    _git(origin, 'add', '.')
    _git(origin, 'commit', '-m', 'Historical artifact unrelated to prompt review')
    old_blob = _git(origin, 'rev-parse', 'HEAD:retired-artifact.bin')
    bulky.unlink()
    shutil.copytree(ROOT / SNAP, origin / SNAP)
    _git(origin, 'add', '-A')
    _git(origin, 'commit', '-m', 'Base prompt snapshot')
    base = _git(origin, 'rev-parse', 'HEAD')
    _git(origin, 'checkout', '-b', 'topic')
    prompt = origin / SNAP / 'prompts/cli.claude.txt'
    prompt.write_text(prompt.read_text().replace('# Finishing the job', '# Finish every job'))
    _git(origin, 'add', '.')
    _git(origin, 'commit', '-m', 'Reviewable prompt change')

    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    _git(candidate, 'init')
    _git(candidate, 'remote', 'add', 'origin', origin.as_uri())
    fetch = ['fetch', '--no-tags', '--prune', '--no-recurse-submodules']
    if options.get('filter'):
        fetch.append('--filter=' + options['filter'])
    _git(candidate, *fetch, 'origin', '+refs/heads/*:refs/remotes/origin/*')
    _git(candidate, 'checkout', '--detach', 'origin/topic')
    assert _git(candidate, 'merge-base', 'origin/main', 'HEAD') == base
    # Rev-list's missing-object traversal does not hydrate the blobs it reports.
    missing = _git(candidate, 'rev-list', '--objects', '--all', '--missing=print').splitlines()
    assert '?' + old_blob in missing, 'checkout downloaded unrelated historical artifact content'

    archive = tmp_path / 'base.tar'
    _git(candidate, 'archive', '--output=' + str(archive), base, str(SNAP))
    base_tree = tmp_path / 'base'
    with tarfile.open(archive) as bundle:
        bundle.extractall(base_tree, filter='data')
    summary = tmp_path / 'prompt-surface.md'
    subprocess.run([
        sys.executable, str(ROOT / 'scripts/ci/prompt_surface.py'), 'diff',
        str(base_tree / SNAP), str(candidate / SNAP), '--output', str(summary),
    ], check=True)
    rendered = summary.read_text()
    assert '| cli.claude |' in rendered and '+# Finish every job' in rendered
    # Reading exactly the base/head snapshots must not hydrate the unrelated artifact either.
    assert '?' + old_blob in _git(candidate, 'rev-list', '--objects', '--all', '--missing=print').splitlines()
