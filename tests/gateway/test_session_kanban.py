"""Task claims, not caller-supplied launch policy, authorize Kanban execution."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _claimed_card(tmp_path, monkeypatch, **task_fields):
    """A claimed card on board ``owned`` assigned to the default profile, its workspace, and the
    owner connection and params ``build_kanban_policy`` takes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import SessionActor
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'pyproject.toml').write_text('[project]\nname = "task"\n')  # a code worktree
    from contextlib import closing
    kb.create_board('owned')  # explicit creation; connect() never creates a board (#43243)
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='Owned acceptance', body='Goal criteria', assignee='default',
            workspace_kind='dir', workspace_path=str(workspace), **task_fields)
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    return task, workspace, connection, params


def test_claim_freezes_task_policy_and_rejects_forgery(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    task, workspace, connection, params = _claimed_card(
        tmp_path, monkeypatch, skills=['owned-skill'], goal_mode=True, goal_max_turns=3,
        model_override='loop-model', provider_override='custom')
    from gateway.session_kanban import build_kanban_policy
    config = {'model': {'default': 'loop-model', 'provider': 'custom'}, 'platform_toolsets': {'cli': ['terminal', 'file']},
              'agent': {'coding_context': 'focus'}}  # the worktree must not collapse a worker into the coding posture
    policy, _secrets = build_kanban_policy(connection, params, config)
    context = json.loads(policy.kanban_json)
    assert policy.source == 'kanban' and policy.cwd == str(workspace)
    assert policy.model == task.model_override and policy.provider == task.provider_override
    assert {'kanban', 'terminal', 'file'} <= set(policy.toolsets)
    assert context['task_id'] == task.id and context['run_id'] == task.current_run_id
    assert context['board'] == 'owned' and context['claim_lock'] == task.claim_lock
    assert context['skills'] == ['owned-skill'] and context['goal_mode'] and context['goal_max_turns'] == 3
    assert context['accept_hooks'] is True and 'Goal criteria' in context['goal_text']
    for changed in ({'run_id': task.current_run_id + 1}, {'board': 'foreign'}, {'claim_lock': 'forged'}, {'cwd': '/tmp'}):
        with pytest.raises(RuntimeStoreError):
            build_kanban_policy(connection, params | changed, config)
    connection.native_owner = False
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        build_kanban_policy(connection, params, config)
    from gateway.session_policy import build_policy
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        build_policy({'source': 'kanban', 'cwd': str(workspace)}, config)


def test_alias_pinned_card_runs_the_aliases_route(tmp_path, monkeypatch):
    """A card pinned to a model alias runs the alias's model, provider and effort, resolved from the
    owner's config at dispatch; the card's own effort still wins."""
    aliases = {'model_aliases': {'opus': {'model': 'claude-opus-5-5', 'provider': 'custom:claude-proxy',
                                          'reasoning_effort': 'high'}}}
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda *a, **k: aliases)
    config = {'model': {'default': 'loop-model', 'provider': 'custom'},
              'platform_toolsets': {'cli': ['terminal', 'file']}, **aliases}
    from gateway.session_kanban import build_kanban_policy
    task, _workspace, connection, params = _claimed_card(tmp_path, monkeypatch, model_override='opus')
    policy, _secrets = build_kanban_policy(connection, params, config)
    assert (policy.model, policy.provider) == ('claude-opus-5-5', 'custom:claude-proxy')
    assert policy.config()['agent']['reasoning_effort'] == 'high'
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing(board='owned') as conn:
        kb.set_reasoning_effort(conn, task.id, 'low')
    policy, _secrets = build_kanban_policy(connection, params, config)
    assert policy.model == 'claude-opus-5-5' and policy.config()['agent']['reasoning_effort'] == 'low'



_PROFILE = {'model': {'default': 'loop-model', 'provider': 'custom', 'base_url': 'https://profile.invalid/v1',
                      'api_key': 'profile-secret'}, 'platform_toolsets': {'cli': ['terminal', 'file']}}


@pytest.mark.parametrize('entry, expect', [
    ({'model': 'm-keyed', 'base_url': 'https://alias.invalid/v1', 'api_key': 'alias-secret'}, 'api_key'),
    ({'model': 'm-env', 'base_url': 'https://alias.invalid/v1', 'key_env': 'ALIAS_KEY'}, 'key_env'),
    ({'model': 'm-bare', 'base_url': 'https://alias.invalid/v1'}, None),
], ids=['api_key', 'key_env', 'no-key'])
def test_an_alias_owns_its_credential_and_never_carries_the_profile_key(tmp_path, monkeypatch, entry, expect):
    """An alias with its own endpoint runs on its own key (literal or key_env pointer); the
    profile's key never pairs with the alias's host, even on the same provider label."""
    monkeypatch.setenv('ALIAS_KEY', 'env-secret')
    aliases = {'model_aliases': {'Edge': entry}}
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda *a, **k: aliases)
    from gateway.session_kanban import build_kanban_policy
    _task, _workspace, connection, params = _claimed_card(tmp_path, monkeypatch, model_override='edge')
    policy, private = build_kanban_policy(connection, params, {**_PROFILE, **aliases})
    frozen = json.loads(policy.config_json)
    assert policy.model == entry['model'] and frozen['model']['base_url'] == 'https://alias.invalid/v1'
    assert 'profile-secret' not in policy.config_json and 'profile-secret' not in private.values()
    if expect == 'api_key':
        assert frozen['model']['api_key_from'] == ['model_aliases', 'Edge', 'api_key']
        assert private[('model_aliases', 'Edge', 'api_key')] == 'alias-secret'
    elif expect == 'key_env':
        assert frozen['model']['key_env'] == 'ALIAS_KEY' and 'api_key_from' not in frozen['model']
    else:
        assert not {'api_key', 'api_key_from', 'key_env', 'api_key_env'} & {k for k, v in frozen['model'].items() if v}


def test_an_alias_pin_that_no_longer_resolves_blocks_the_card(tmp_path, monkeypatch):
    """The stored ``@`` keeps an alias pin an alias: once the owner's config drops it, dispatch
    blocks the card with the reason instead of sending the name as a model id. A raw id stays raw
    even when an alias of that name is added later."""
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from gateway.session_kanban import build_kanban_policy
    aliases = {'model_aliases': {'opus': {'model': 'claude-opus-5-5', 'provider': 'custom:claude-proxy'}}}
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda *a, **k: aliases)
    task, workspace, connection, params = _claimed_card(tmp_path, monkeypatch, model_override='opus')
    assert task.model_override == '@opus'
    with pytest.raises(RuntimeStoreError, match='invalid_kanban_claim'):
        build_kanban_policy(connection, params, dict(_PROFILE))
    with connect_closing(board='owned') as conn:
        blocked = kb.get_task(conn, task.id)
    assert blocked.status == 'blocked' and blocked.block_kind == 'needs_input'

    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda *a, **k: {})
    with connect_closing(board='owned') as conn:
        raw = kb.create_task(conn, title='raw pin', assignee='default', workspace_kind='dir',
                             workspace_path=str(workspace), model_override='sonnet')
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, raw)
    later = {'model_aliases': {'sonnet': {'model': 'claude-sonnet-5-5', 'provider': 'custom:claude-proxy'}}}
    policy, _secrets = build_kanban_policy(connection, dict(params, task_id=raw, run_id=claimed.current_run_id,
                                                            claim_lock=claimed.claim_lock), {**_PROFILE, **later})
    assert claimed.model_override == 'sonnet' and policy.model == 'sonnet'


@pytest.mark.parametrize('mode', ['complete', 'timeout', 'rate_limit', 'billing', 'crash'])
def test_real_dispatcher_lifecycle(tmp_path, mode):
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    from tests.gateway.fixtures.local_recovery_probe import child_env
    env = child_env()
    env.update(HOME=str(tmp_path / 'home'), USERPROFILE=str(tmp_path / 'home'), HERMES_HOME=str(tmp_path / 'state'), PYTHONPATH=str(repo), KANBAN_PROBE_MODE=mode)
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' / 'kanban_owner_probe.py')],
        env=env, cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=150, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / 'state' / 'receipt.json').read_text())
    print(json.dumps(receipt))
    if mode != 'complete':
        assert receipt['task_status'] == 'ready'
        return
    assert receipt['task_status'] == 'done' and receipt['source'] == 'kanban'
    assert receipt['tools'] and receipt['task_context'] and receipt['skill_context'] and receipt['hook_effect']
    assert receipt['admissions'] == 1 and receipt['retry_same_session']
    assert receipt['goal_continuation'] and receipt['stable_prefix']
    print(json.dumps(receipt))


def test_late_launcher_cannot_stamp_replacement_claim(tmp_path, monkeypatch):
    from contextlib import closing
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    kb.create_board('owned')  # explicit creation; connect() never creates a board (#43243)
    with closing(connect(board='owned')) as conn:
        tid = kb.create_task(conn, title='Owned race', assignee='default')
        old = kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason='replace claim', expected_run_id=old.current_run_id)
        kb.unblock_task(conn, tid)
        new = kb.claim_task(conn, tid)
        dispatch._set_worker_pid(conn, tid, 424242, run_id=old.current_run_id, claim_lock=old.claim_lock)
        assert kb.get_task(conn, tid).worker_pid is None
        assert conn.execute('SELECT worker_pid FROM task_runs WHERE id=?', (new.current_run_id,)).fetchone()[0] is None


def test_claim_reclaimed_before_admission_marker_is_refused(tmp_path, monkeypatch):
    """A claim that stops being current between policy/session creation and the marker
    transaction is refused: no owner_admitted row, no resume, no submit."""
    import asyncio
    from contextlib import closing
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import SessionActor
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    kb.create_board('owned')  # explicit creation; connect() never creates a board (#43243)
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='Owned race', assignee='default', workspace_kind='dir', workspace_path=str(workspace))
        kb.recompute_ready(conn)
        old = kb.claim_task(conn, task_id)
    config = {'model': {'default': 'loop-model', 'provider': 'custom'}, 'platform_toolsets': {'cli': ['terminal']}}
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: config)
    calls = []
    def create_local_session(authority, actor, params, *, trusted_policy=None, trusted_secrets=None):
        # Another process reclaims the task while the owner session is being created.
        with closing(connect(board='owned')) as conn:
            kb.block_task(conn, task_id, reason='replace claim', expected_run_id=old.current_run_id)
            kb.unblock_task(conn, task_id)
            kb.claim_task(conn, task_id)
        return SimpleNamespace(session_id='owner-session', profile_id=str(tmp_path))
    monkeypatch.setattr('gateway.session_local.create_local_session', create_local_session)
    async def submit(actor, submission, **kwargs):
        calls.append(('submit', submission))
    async def resume(ref, params):
        calls.append(('resume', ref))
    db = SimpleNamespace(get_session=lambda sid: None, db_path=str(tmp_path / 'state.db'))
    authority = SimpleNamespace(profile_id=str(tmp_path), db=db, submit=submit)
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    params = dict(board='owned', task_id=task_id, run_id=old.current_run_id, claim_lock=old.claim_lock)
    from gateway.session_kanban import run_task
    with pytest.raises(RuntimeStoreError, match='stale_kanban_claim'):
        asyncio.run(run_task(connection, params))
    assert calls == []
    with closing(connect(board='owned')) as conn:
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'", (task_id,)).fetchone()[0] == 0
