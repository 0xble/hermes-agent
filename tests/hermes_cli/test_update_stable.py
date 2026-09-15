"""Real Git boundaries for the operator-promoted stable pointer."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_revision
from tests.hermes_cli.test_update_revision import _repo, _advance, _git, _git_run


def test_stable_freezes_remote_once_retains_rollback_and_refuses_unsafe_refs(tmp_path):
    seed, checkout, first = _repo(tmp_path)
    with pytest.raises(RuntimeError, match='stable is unavailable'):
        update_revision.prepare_stable_target(_git_run, ['git'], checkout)
    candidate = _advance(seed)
    _git(['push', 'origin', f'{candidate}:refs/heads/stable'], seed)
    resolutions = []
    def moving_git_run(git_cmd, command, cwd, **kwargs):
        result = _git_run(git_cmd, command, cwd, **kwargs)
        if 'ls-remote' in command:
            resolutions.append(result.stdout)
            _git(['commit', '--allow-empty', '-m', 'remote advanced during update'], seed)
            _git(['push', 'origin', 'HEAD:refs/heads/main', 'HEAD:refs/heads/stable'], seed)
        return result
    target = update_revision.prepare_stable_target(moving_git_run, ['git'], checkout)
    assert len(resolutions) == 1
    assert target.sha == candidate
    rollback = update_revision.retain_precheckout_rollback(_git_run, ['git'], checkout, target)
    update_revision.checkout_revision(_git_run, ['git'], checkout, target)
    assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == candidate
    assert _git(['rev-parse', rollback.ref], checkout).stdout.strip() == first
    assert rollback.manifests['pyproject.toml']
    assert json.loads(Path(rollback.receipt_path).read_text(encoding='utf-8'))['rollback_sha'] == first
    _git(['fetch', 'origin', 'main'], checkout)
    _git(['checkout', '--detach', 'origin/main'], checkout)
    _git(['push', '--force', 'origin', f'{first}:refs/heads/stable'], seed)
    with pytest.raises(RuntimeError, match='downgrade'):
        update_revision.prepare_stable_target(_git_run, ['git'], checkout)
    _git(['checkout', '-b', 'divergent-release', first], seed)
    _git(['commit', '--allow-empty', '-m', 'divergent release development'], seed)
    _git(['push', '--force', 'origin', 'HEAD:refs/heads/stable'], seed)
    with pytest.raises(RuntimeError, match='not an ancestor'):
        update_revision.prepare_stable_target(_git_run, ['git'], checkout)


@pytest.mark.parametrize('moving', [False, True])
def test_shallow_history_uses_frozen_objects(tmp_path, moving):
    seed, _, first = _repo(tmp_path)
    remote = tmp_path / 'remote.git'
    shallow = tmp_path / 'shallow'
    candidate = _advance(seed)
    _git(['push', 'origin', f'{candidate}:refs/heads/stable'], seed)
    _git(['commit', '--allow-empty', '-m', 'main ahead of stable'], seed)
    _git(['push', 'origin', 'HEAD:main'], seed)
    frozen_main = _git(['rev-parse', 'HEAD'], seed).stdout.strip()
    _git(['clone', '--depth', '1', '--branch', 'main', remote.as_uri(), str(shallow)], tmp_path)
    assert _git(['rev-parse', '--is-shallow-repository'], shallow).stdout.strip() == 'true'
    _git(['fetch', 'origin', first], shallow)
    _git(['checkout', '--detach', first], shallow)
    calls = []
    def runner(git_cmd, command, cwd, **kwargs):
        result = _git_run(git_cmd, command, cwd, **kwargs)
        calls.append(command)
        if command[0] == 'ls-remote' and moving:
            _git(['commit', '--allow-empty', '-m', 'moving tips'], seed)
            _git(['push', 'origin', 'HEAD:main', 'HEAD:stable'], seed)
        return result
    target = update_revision.prepare_stable_target(runner, ['git'], shallow)
    assert target.sha == candidate
    assert len([c for c in calls if c[0] == 'ls-remote']) == 1
    history_fetch = next(c for c in calls if '--unshallow' in c)
    assert history_fetch == ['fetch', '--unshallow', 'origin', frozen_main, candidate]
    assert _git(['rev-parse', 'HEAD'], shallow).stdout.strip() == first
    update_revision.checkout_revision(_git_run, ['git'], shallow, target)
    assert _git(['rev-parse', 'HEAD'], shallow).stdout.strip() == candidate


def test_stable_dirty_refuses_before_network(tmp_path):
    _, checkout, first = _repo(tmp_path)
    (checkout / 'dirty').write_text('local work', encoding='utf-8')
    def runner(git_cmd, command, cwd, **kwargs):
        assert not kwargs.get('network'), command
        return _git_run(git_cmd, command, cwd, **kwargs)
    with pytest.raises(RuntimeError, match='dirty'):
        update_revision.prepare_stable_target(runner, ['git'], checkout)
    assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == first


@pytest.mark.parametrize('compatible', [True, False])
def test_stable_checkout_through_production_compatibility_guard(tmp_path, compatible):
    from hermes_cli.update_cmd import _git_run as production_git_run
    from hermes_cli.update_compatibility import RuntimeCompatibilityError
    seed, checkout, first = _repo(tmp_path)
    if not compatible:
        (seed / 'runtime-compatibility.json').write_text(
            json.dumps({'schema': 1, 'capabilities': ['delegation-admitted-v1']}), encoding='utf-8')
    candidate = _advance(seed)
    _git(['push', 'origin', f'{candidate}:refs/heads/stable'], seed)
    target = update_revision.prepare_stable_target(production_git_run, ['git'], checkout)
    rollback = update_revision.retain_precheckout_rollback(production_git_run, ['git'], checkout, target)
    assert _git(['rev-parse', rollback.ref], checkout).stdout.strip() == first
    if compatible:
        update_revision.checkout_revision(production_git_run, ['git'], checkout, target)
        assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == candidate
    else:
        with pytest.raises(RuntimeCompatibilityError, match='capability contract'):
            update_revision.checkout_revision(production_git_run, ['git'], checkout, target)
        assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == first
        assert not (checkout / 'after.txt').exists()
    assert _git(['status', '--porcelain'], checkout).stdout.strip() == ''


@pytest.mark.parametrize('snapshot', ['ok', 'failed', 'disabled'])
def test_configured_stable_uses_pinned_pipeline_only_after_snapshot(tmp_path, monkeypatch, snapshot):
    from hermes_cli import update_cmd, config, main
    from hermes_cli.main_install_repair import _resolve_update_branch
    seed, checkout, first = _repo(tmp_path)
    _git(['push', 'origin', f'{first}:refs/heads/stable'], seed)
    cfg = {'updates': {'channel': 'stable'}}
    monkeypatch.setattr(config, 'read_config_mapping_strict', lambda path: cfg)
    args = SimpleNamespace(revision=None, branch=None, no_backup=snapshot == 'disabled')
    assert _resolve_update_branch(args) == 'stable'
    assert _resolve_update_branch(SimpleNamespace(branch='main')) == 'main'
    seen = []
    def backup(_args):
        seen.append('snapshot')
        return None if snapshot == 'failed' else 'snapshot-id'
    monkeypatch.setattr(main, 'PROJECT_ROOT', checkout)
    monkeypatch.setattr(main, '_run_pre_update_backup', backup)
    monkeypatch.setattr(main, '_pause_windows_gateways_for_update', lambda: None)
    monkeypatch.setattr(main, '_resume_windows_gateways_after_update', lambda _: None)
    monkeypatch.setattr(update_cmd, '_resolve_update_options',
        lambda *a: SimpleNamespace(gw_input_fn=None, assume_yes=True))
    monkeypatch.setattr(update_cmd, '_begin_update_receipt_and_plan', lambda *a: None)
    monkeypatch.setattr(update_cmd, '_prepare_git_command', lambda **k: (False, ['git'], True))
    monkeypatch.setattr(update_cmd, '_git_run', _git_run)
    monkeypatch.setattr(update_cmd, '_run_pinned_revision_update',
        lambda git, target, *a, **k: seen.append(target.sha))
    if snapshot == 'ok':
        update_cmd._cmd_update_impl(args, False)
        assert seen == ['snapshot', first]
        assert args.revision == first
    else:
        with pytest.raises(SystemExit) as error:
            update_cmd._cmd_update_impl(args, False)
        assert error.value.code == 1
        assert seen == ([] if snapshot == 'disabled' else ['snapshot'])
    assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == first


def test_shallow_unrelated_head_refuses_without_source_change(tmp_path):
    seed, _, first = _repo(tmp_path)
    candidate = _advance(seed)
    _git(['push', 'origin', f'{candidate}:refs/heads/stable'], seed)
    _git(['checkout', '--orphan', 'unrelated'], seed)
    _git(['commit', '-m', 'unrelated root'], seed)
    _git(['push', 'origin', 'HEAD:unrelated'], seed)
    shallow = tmp_path / 'unrelated-shallow'
    _git(['clone', '--depth', '1', '--branch', 'unrelated',
          (tmp_path / 'remote.git').as_uri(), str(shallow)], tmp_path)
    before = _git(['rev-parse', 'HEAD'], shallow).stdout.strip()
    with pytest.raises(RuntimeError, match='downgrade or diverge'):
        update_revision.prepare_stable_target(_git_run, ['git'], shallow)
    assert _git(['rev-parse', 'HEAD'], shallow).stdout.strip() == before
    assert _git(['status', '--porcelain'], shallow).stdout.strip() == ''
