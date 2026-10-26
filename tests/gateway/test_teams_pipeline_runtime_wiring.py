"""Tests for Teams pipeline runtime wiring into the gateway."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.teams_pipeline.runtime import (
    bind_gateway_runtime,
    build_pipeline_runtime,
    build_pipeline_runtime_config,
)


def test_gateway_runner_skips_wiring_without_msgraph_adapter(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: MagicMock()}
    runner._teams_pipeline_runtime_error = None

    called = False

    def _bind(_gateway_runner):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("plugins.teams_pipeline.runtime.bind_gateway_runtime", _bind)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"plugins": {"enabled": ["teams_pipeline"]}},
    )

    GatewayRunner._wire_teams_pipeline_runtime(runner)

    assert called is False


def test_build_pipeline_runtime_skips_sender_when_adapter_layer_is_unavailable(monkeypatch):
    gateway = SimpleNamespace(
        config=SimpleNamespace(
            platforms={
                Platform("teams"): PlatformConfig(
                    enabled=True,
                    extra={
                        "delivery_mode": "graph",
                        "team_id": "team-1",
                        "channel_id": "channel-1",
                    },
                ),
            }
        )
    )

    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.build_graph_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.resolve_teams_pipeline_store_path",
        lambda: "/tmp/teams-pipeline-store.json",
    )
    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.TeamsPipelineStore",
        lambda path: {"path": path},
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.platforms.teams.adapter",
        ModuleType("plugins.platforms.teams.adapter"),
    )

    runtime = build_pipeline_runtime(gateway)

    assert runtime.teams_sender is None


@pytest.fixture
async def recovery_runtime(monkeypatch, tmp_path):
    """Real binding, durable store and sink writers with offline provider collaborators."""
    from gateway.config import GatewayConfig
    from gateway.platforms.msgraph_webhook import MSGraphWebhookAdapter
    from plugins.platforms.teams.summary_writer import TeamsSummaryWriter
    from plugins.teams_pipeline import runtime as runtime_module
    from plugins.teams_pipeline.models import TeamsMeetingSummaryPayload
    from plugins.teams_pipeline.pipeline import LinearWriter, NotionWriter
    from plugins.teams_pipeline.store import TeamsPipelineStore

    store_path = tmp_path / 'teams-store.json'
    monkeypatch.setenv('MSGRAPH_WEBHOOK_STORE_PATH', str(store_path))
    real_build = runtime_module.build_pipeline_runtime
    adapters, effects_created = [], []
    notification = {
        'clientState': 'synthetic-client-state', 'changeType': 'created',
        'resource': 'users/synthetic-organizer/onlineMeetings/synthetic-meeting/transcripts/synthetic-transcript',
        'resourceData': {'meetingId': 'synthetic-meeting', 'organizerUserId': 'synthetic-organizer'},
    }

    class Effects:
        def __init__(self, stage, mode='incoming_webhook'):
            self.stage, self.mode = stage, mode
            self.calls, self.commits = [], []
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.thread_release, self.thread_finished = threading.Event(), asyncio.Event()
            self.thread_entered = False
            self.loop = asyncio.get_running_loop()
            effects_created.append(self)

        async def block(self, stage):
            if self.stage == stage:
                self.entered.set()
                await self.release.wait()

        async def get_json(self, path, **kwargs):
            self.calls.append(('graph_get', path))
            await self.block('resolving_meeting')
            return {'id': 'synthetic-meeting', 'subject': 'Synthetic Meeting'}

        async def collect_paginated(self, path, **kwargs):
            self.calls.append(('graph_list', path))
            if path.endswith('/transcripts'):
                await self.block('fetching_transcript')
                return [] if self.stage in {'downloading_recording', 'transcribing_audio'} else [
                    {'id': 'synthetic-transcript', 'displayName': 'synthetic.vtt', 'status': 'available'}]
            assert path.endswith('/recordings'), path
            await self.block('downloading_recording')
            return [{'id': 'synthetic-recording', 'displayName': 'synthetic.wav'}]

        async def download_to_file(self, path, destination, **kwargs):
            self.calls.append(('graph_download', path))
            Path(destination).write_text('Synthetic transcript with a decision and next action. ' * 8)
            return {'size_bytes': Path(destination).stat().st_size}

        def transcribe(self, path, model):
            self.thread_entered = True
            self.loop.call_soon_threadsafe(self.entered.set)
            try:
                assert self.thread_release.wait(timeout=30), 'fixture STT was not released'
                return {'success': True, 'transcript': 'Synthetic offline transcript. ' * 8}
            finally:
                self.loop.call_soon_threadsafe(self.thread_finished.set)

        async def summarize(self, *, resolved_meeting, transcript_text, artifacts):
            self.calls.append(('summary', resolved_meeting.meeting_id))
            await self.block('summarizing')
            return TeamsMeetingSummaryPayload(meeting_ref=resolved_meeting, title='Synthetic',
                                              summary='Synthetic summary', transcript_text=transcript_text)

        async def transport(self, request):
            stage = {'api.notion.com': 'writing_notion', 'api.linear.app': 'writing_linear',
                     'synthetic.invalid': 'sending_teams'}[request.url.host]
            self.calls.append((stage, request.method))
            self.commits.append(stage)
            # The synthetic provider accepted the effect before cancellation obscures its reply.
            await self.block(stage)
            if stage == 'writing_notion':
                return httpx.Response(200, json={'id': 'synthetic-page'})
            if stage == 'writing_linear':
                return httpx.Response(200, json={'data': {'issueCreate': {'issue': {'id': 'synthetic-issue'}}}})
            return httpx.Response(200, json={'delivered': True})

        async def post_json(self, path, *, json_body):
            self.calls.append(('sending_teams', path))
            self.commits.append('sending_teams')
            await self.block('sending_teams')
            return {'id': 'synthetic-message'}

    def bind(effects):
        adapter = MSGraphWebhookAdapter(PlatformConfig(enabled=True, extra={
            'host': '127.0.0.1', 'client_state': 'synthetic-client-state'}))
        adapters.append(adapter)
        teams = PlatformConfig(enabled=True, extra={
            'delivery_mode': effects.mode, 'incoming_webhook_url': 'https://synthetic.invalid/webhook',
            'chat_id': 'synthetic-chat', 'meeting_pipeline': {
                'tmp_dir': str(tmp_path / 'recordings'),
                'notion': {'enabled': True, 'database_id': 'synthetic-database'},
                'linear': {'enabled': True, 'team_id': 'synthetic-team'},
                'teams_delivery': {'force_resend': True},
            }})
        gateway = SimpleNamespace(adapters={Platform.MSGRAPH_WEBHOOK: adapter},
                                  config=GatewayConfig(platforms={Platform('teams'): teams}),
                                  _teams_pipeline_runtime=None, _teams_pipeline_runtime_error=None)
        monkeypatch.setattr(runtime_module, 'build_graph_client', lambda: effects)

        def build(gw):
            pipeline = real_build(gw)
            pipeline.summarize_fn, pipeline.transcribe_fn = effects.summarize, effects.transcribe
            transport = httpx.MockTransport(effects.transport)
            pipeline.notion_writer = NotionWriter(api_key='synthetic-notion', transport=transport)
            pipeline.linear_writer = LinearWriter(api_key='synthetic-linear', transport=transport)
            pipeline.teams_sender = TeamsSummaryWriter(platform_config=teams, graph_client=effects, transport=transport).write_summary
            return pipeline

        monkeypatch.setattr(runtime_module, 'build_pipeline_runtime', build)
        assert bind_gateway_runtime(gateway)
        return adapter, gateway._teams_pipeline_runtime

    async def post(adapter, events):
        body = json.dumps({'value': events}).encode()

        async def read():
            return body

        request = SimpleNamespace(query={}, remote='127.0.0.1', content_length=len(body), read=read)
        return await adapter._handle_notification(request)

    async def cancel(task, effects):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        effects.release.set()
        effects.thread_release.set()
        if effects.thread_entered:
            await asyncio.wait_for(effects.thread_finished.wait(), timeout=10)

    try:
        yield SimpleNamespace(Effects=Effects, bind=bind, post=post, cancel=cancel,
                              notification=notification, reopen=lambda: TeamsPipelineStore(store_path))
    finally:
        for effects in effects_created:
            effects.release.set()
            effects.thread_release.set()
        tasks = {task for adapter in adapters for task in adapter._background_tasks}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for effects in effects_created:
            if effects.thread_entered:
                await asyncio.wait_for(effects.thread_finished.wait(), timeout=10)


@pytest.mark.anyio
@pytest.mark.parametrize('status', [
    'received', 'resolving_meeting', 'fetching_transcript',
    'downloading_recording', 'transcribing_audio', 'summarizing'])
async def test_binding_recovers_safe_durable_work_once_without_waiting_for_processing(recovery_runtime, monkeypatch, status):
    p = recovery_runtime
    effects = p.Effects(status)
    adapter, pipeline = p.bind(effects)
    response = await p.post(adapter, [p.notification, p.notification])
    assert response.status == 202
    assert effects.calls == [] and len(adapter._background_tasks) == 1
    task = next(iter(adapter._background_tasks))
    job_id = next(iter(p.reopen().list_jobs()))
    if status != 'received':
        await asyncio.wait_for(effects.entered.wait(), timeout=10)
    assert p.reopen().get_job(job_id)['status'] == status
    await p.cancel(task, effects)

    fresh = p.Effects('summarizing')
    adapter, pipeline = p.bind(fresh)
    assert len(adapter._background_tasks) == 1, 'binding did not consume existing durable work'
    assert (await p.post(adapter, [p.notification, p.notification])).status == 202
    assert len(adapter._background_tasks) == 1
    await asyncio.wait_for(fresh.entered.wait(), timeout=10)
    assert fresh.calls.count(('summary', 'synthetic-meeting')) == 1
    fresh.release.set()
    await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), timeout=10)
    assert list(p.reopen().list_jobs()) == [job_id]
    assert p.reopen().get_job(job_id)['status'] == 'completed'
    assert fresh.commits == ['writing_notion', 'writing_linear', 'sending_teams']
    assert (await p.post(adapter, [p.notification])).status == 202
    assert not adapter._background_tasks
    calls = list(fresh.calls)

    def refuse(*args, **kwargs):
        raise OSError('synthetic-accept-refusal')

    monkeypatch.setattr(pipeline.store, 'upsert_job', refuse)
    assert (await p.post(adapter, [{**p.notification, 'id': 'synthetic-refused'}])).status == 503
    assert not adapter._background_tasks and fresh.calls == calls
    assert list(p.reopen().list_jobs()) == [job_id]


@pytest.mark.anyio
@pytest.mark.parametrize('status,mode', [
    ('writing_notion', 'incoming_webhook'), ('writing_linear', 'incoming_webhook'),
    ('sending_teams', 'incoming_webhook'), ('sending_teams', 'graph')])
@pytest.mark.parametrize('prior_receipt', [False, True], ids=['missing-receipt', 'old-receipt'])
async def test_interrupted_external_effects_stay_held_even_with_an_old_receipt(recovery_runtime, status, mode, prior_receipt):
    p = recovery_runtime
    effects = p.Effects(status, mode)
    adapter, pipeline = p.bind(effects)
    if prior_receipt:
        for sink, receipt in [('notion', {'page_id': 'old-page'}), ('linear', {'issue_id': 'old-issue'}),
                              ('teams', {'message_id': 'old-message'})]:
            pipeline.store.upsert_sink_record(f'{sink}:synthetic-meeting', receipt)
    assert (await p.post(adapter, [p.notification])).status == 202
    task = next(iter(adapter._background_tasks))
    job_id = next(iter(p.reopen().list_jobs()))
    await asyncio.wait_for(effects.entered.wait(), timeout=10)
    assert p.reopen().get_job(job_id)['status'] == status
    assert effects.commits[-1] == status
    await p.cancel(task, effects)
    receipts = {sink: p.reopen().get_sink_record(f'{sink}:synthetic-meeting')
                for sink in ('notion', 'linear', 'teams')}

    fresh = p.Effects(None, mode)
    adapter, pipeline = p.bind(fresh)
    startup_tasks = set(adapter._background_tasks)
    held = await pipeline.run_job(job_id)
    assert held.status == status, 'direct replay repeated an unconfirmed external operation'
    assert len(startup_tasks) == 1, 'binding did not surface the interrupted durable job'
    await asyncio.wait_for(asyncio.gather(*startup_tasks), timeout=10)
    assert fresh.calls == []
    record = p.reopen().get_job(job_id)
    assert record['status'] == status
    assert 'unconfirmed' in record['error_info']['message'].lower()
    assert 'held' in record['error_info']['message'].lower()
    assert (await p.post(adapter, [p.notification, p.notification])).status == 202
    await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), timeout=10)
    assert p.reopen().get_job(job_id) == record
    assert fresh.calls == []
    assert {sink: p.reopen().get_sink_record(f'{sink}:synthetic-meeting')
            for sink in ('notion', 'linear', 'teams')} == receipts
