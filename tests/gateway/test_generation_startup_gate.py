"""The readiness canary traverses native admission and stores only synthetic output."""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.generation import GenerationIdentity
from gateway.outbox import Outbox


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['HERMES_READY', '  **`hermes_ready`**.  ', '"HERMES_READY!"', None, 'HERMES_UNRELATED', 'HERMES_READY extra'])
async def test_gate_requires_native_runner_reply_without_recoverable_egress(tmp_path, monkeypatch, reply):
    from gateway import run
    from gateway.startup_gate import run_startup_gate
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    seen = []
    def forbidden_startup(self):
        raise AssertionError('loopback must not install software')
    monkeypatch.setattr(run.GatewayRunner, '_init_startup_checks', forbidden_startup)
    monkeypatch.setattr(run.GatewayRunner, '_get_proxy_url', lambda self: 'remote-proxy')
    async def agent(self, message, context_prompt, history, source, session_id, **kwargs):
        seen.append(source.chat_id)
        assert self._resolve_turn_toolsets({}, source, 'local') == ([], [])
        assert self._get_proxy_url() is None
        assert self.hooks.loaded_hooks == []
        return {'final_response': reply, 'messages': [], 'completed': reply is not None,
                'api_calls': 1, 'interrupted': False}
    monkeypatch.setattr(run.GatewayRunner, '_run_agent', agent)
    identity = GenerationIdentity.create(release_sha='new', label='new', boot_id='boot')
    if reply in {'HERMES_READY', '  **`hermes_ready`**.  ', '"HERMES_READY!"'}:
        await run_startup_gate(GatewayConfig(forward_only_handover_enabled=True), identity)
        rows = Outbox(tmp_path).all_rows()
        assert len(rows) == 1 and rows[0].state == 'delivered'
        assert rows[0].payload['destination'] == 'loopback'
        assert not Outbox(tmp_path).pending() and not Outbox(tmp_path).scheduled()
        with Outbox(tmp_path)._connect() as conn:
            row = conn.execute('SELECT attempts,send_status FROM outbox').fetchone()
            assert tuple(row) == (0, 'synthetic')
    else:
        with pytest.raises(RuntimeError, match='startup gate'):
            await run_startup_gate(GatewayConfig(forward_only_handover_enabled=True), identity)
    assert seen == [f'__hermes_startup_gate__:{identity.id}']


def test_gate_agent_is_memory_and_tool_free(monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    created = []
    def factory(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(tools=[])
    source = SimpleNamespace(user_id='loopback', user_id_alt=None, user_name=None,
        chat_id='loopback', chat_name=None, chat_type='dm', thread_id=None)
    runner = SimpleNamespace(_startup_gate_source=source, _prefill_messages=[{'role': 'user', 'content': 'old'}], _service_tier=None,
        _session_db=None, _refresh_fallback_model=lambda: {'model': 'fallback'})
    ctx = SimpleNamespace(AIAgent=factory, user_config={}, source=source, enabled_toolsets=[],
        disabled_toolsets=[], session_id='gate', session_key='gate')
    turn = object.__new__(TurnRunner)
    turn._runner, turn._ctx = runner, ctx
    turn._build_fresh_agent({'model': 'configured', 'runtime': {}}, 'local', '', 99, {}, {}, True)
    assert created[0]['enabled_toolsets'] == []
    assert created[0]['skip_memory'] and created[0]['skip_background_review']
    assert created[0]['skip_context_files'] and not created[0]['load_soul_identity']
    assert not created[0]['checkpoints_enabled']
    assert created[0]['max_iterations'] == 1
    assert not created[0]['prefill_messages'] and created[0]['fallback_model'] is None


@pytest.mark.asyncio
async def test_failed_cold_gate_parks_consumed_scope_without_polling(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from gateway import run, run_generation
    from gateway.generation import GenerationCoordinator
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'scope')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    process = GenerationIdentity.create(release_sha='r', label=db.service_label(), boot_id='boot')
    identity = run_generation._claim_process_generation(db, process)
    monkeypatch.setattr('gateway.startup_gate.run_startup_gate', AsyncMock(side_effect=RuntimeError('gate failed')))
    start = AsyncMock()
    monkeypatch.setattr(run, 'start_gateway', start)
    with pytest.raises(RuntimeError, match='gate failed'):
        await run_generation.serve_standby_generation(GatewayConfig(forward_only_handover_enabled=True),
            claimed_generation=(db, identity))
    row = db.generations()[0]
    assert (row['state'], row['verdict'], row['verdict_evidence']) == ('exited', 'failed', 'startup_gate_failed')
    assert db.leases() == []
    start.assert_not_awaited()
    with pytest.raises(SystemExit) as exc:
        run_generation._claim_process_generation(db, replace(process, pid=process.pid + 1))
    assert exc.value.code == 0 and db.generations() == [row]
