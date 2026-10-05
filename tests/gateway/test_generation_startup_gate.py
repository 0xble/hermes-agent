"""The readiness canary traverses native admission and stores only synthetic output."""
import json
from types import SimpleNamespace

from tests.gateway.test_startup_gate import worker_provider  # noqa: F401

import pytest

from gateway.config import GatewayConfig
from gateway.generation import GenerationIdentity


def test_gate_agent_is_memory_and_tool_free(monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    created = []
    def factory(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(tools=[], valid_tool_names=set(), _build_assistant_message=lambda *args: None)
    source = SimpleNamespace(user_id='loopback', user_id_alt=None, user_name=None,
        chat_id='loopback', chat_name=None, chat_type='dm', thread_id=None)
    from gateway.startup_gate import _LoopbackTurn, _LoopbackAdapter
    import weakref
    turn_capability = _LoopbackTurn('fixture-nonce', 'configured', {}, None)
    adapter = _LoopbackAdapter(turn_capability)
    turn_capability.source = source
    source._transport_adapter_ref = weakref.ref(adapter)
    source._startup_gate_capability = turn_capability
    runner = SimpleNamespace(_prefill_messages=[{'role': 'user', 'content': 'old'}], _service_tier=None,
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
    assert (row['state'], row['verdict']) == ('exited', 'failed')
    evidence = json.loads(row['verdict_evidence'])
    assert evidence['reason'] == 'startup_gate_failed' and evidence['error'] == 'RuntimeError'
    assert db.leases() == []
    start.assert_not_awaited()
    with pytest.raises(SystemExit) as exc:
        run_generation._claim_process_generation(db, replace(process, pid=process.pid + 1))
    assert exc.value.code == 0 and db.generations() == [row]


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('entry', ['active', 'standby'])
@pytest.mark.parametrize('mode', ['wrapped', 'wrong-nonce', 'missing-nonce', 'late', 'timeout'])
async def test_both_production_entries_require_full_private_gate_before_poller(worker_provider, monkeypatch, entry, mode):
    from gateway import run, run_generation, startup_gate
    from gateway.generation import GenerationCoordinator
    runner, modes, _, spawned, write_config = worker_provider
    modes[0] = 'pass' if mode == 'late' else mode
    if mode == 'timeout':
        write_config(2)
    if mode == 'late':
        # Advance only the gate's measured clock, never asyncio's shared clock.
        measured = iter([0.0, 0.0, 46.0])
        monkeypatch.setattr(startup_gate, 'time', SimpleNamespace(monotonic=lambda: next(measured)))
    config = GatewayConfig(forward_only_handover_enabled=True)
    home = runner.config.sessions_dir.parent
    db = GenerationCoordinator(home)
    identity = GenerationIdentity.create(release_sha='new', label='successor', boot_id='boot')
    db.register(identity, state='standby')
    calls = []
    original_activate = run_generation._activate_cold_generation
    def activate(coordinator, candidate):
        from gateway.outbox import Outbox
        rows = Outbox(home).all_rows()
        assert len(rows) == 1 and rows[0].payload['content'].lower().find('hermes_ready') >= 0
        assert spawned and all(proc.returncode is not None for proc in spawned)
        calls.append('lease')
        return original_activate(coordinator, candidate)
    async def start_active(self):
        calls.append('poller')
    async def start_promoted(cfg, *, promoted_generation):
        assert promoted_generation == (identity, 1)
        calls.append('poller')
        return True
    monkeypatch.setattr(run_generation, '_activate_cold_generation', activate)
    monkeypatch.setattr(run_generation.ActiveGeneration, 'start', start_active)
    monkeypatch.setattr(run, 'start_gateway', start_promoted)
    start = run_generation.start_active_generation if entry == 'active' else run_generation.serve_standby_generation
    if mode == 'wrapped':
        await start(config, claimed_generation=(db, identity))
        assert calls == ['lease', 'poller']
        assert db.generations()[0]['verdict'] is None
    else:
        with pytest.raises(RuntimeError, match='startup gate'):
            await start(config, claimed_generation=(db, identity))
        assert calls == [] and db.leases() == []
        row = db.generations()[0]
        assert (row['state'], row['verdict']) == ('exited', 'failed')
        receipt = json.loads(row['verdict_evidence'])
        assert receipt['reason'] == 'startup_gate_failed'
        assert receipt['gate']['verdict'] == 'failed'
        if mode == 'late':
            assert receipt['gate']['elapsed_seconds'] == 46
            assert receipt['gate']['deadline_seconds'] == 45
        if mode in {'late', 'timeout'}:
            assert 'deadline exceeded' in receipt['gate']['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['active', 'standby'])
async def test_flag_off_production_entries_never_invoke_startup_gate(tmp_path, monkeypatch, entry):
    from unittest.mock import AsyncMock
    from gateway import run_generation
    from gateway.generation import GenerationCoordinator
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    forbidden = AsyncMock(side_effect=AssertionError('flag-off gate invoked'))
    monkeypatch.setattr('gateway.startup_gate.run_startup_gate', forbidden)
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha='legacy', label='legacy', boot_id='boot')
    db.register(identity, state='standby')
    config = GatewayConfig(overlap_handover_enabled=True)
    if entry == 'active':
        monkeypatch.setattr(run_generation.ActiveGeneration, 'start', AsyncMock())
        active = await run_generation.start_active_generation(config, claimed_generation=(db, identity))
        assert active.epoch == 1
    else:
        # Stop at the unchanged legacy socket boundary, after the gate site.
        monkeypatch.setattr(run_generation, '_ensure_generation_socket_parent',
                            lambda path: (_ for _ in ()).throw(RuntimeError('legacy socket reached')))
        with pytest.raises(RuntimeError, match='legacy socket reached'):
            await run_generation.serve_standby_generation(config, claimed_generation=(db, identity))
    forbidden.assert_not_awaited()
