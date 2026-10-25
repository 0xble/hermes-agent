"""Each synthetic restart resume is admitted once under its own durable identity."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource


def _source(thread_id="224426"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="group", user_id="u",
        thread_id=thread_id, message_id="inbound",
    )


@pytest.mark.asyncio
async def test_resume_scheduler_admits_each_interruption_marker_once(tmp_path):
    """Synthetic resumes share their reply anchor but not their durable admission identity."""
    import sqlite3

    from gateway.run_inbound import _admit_outbox_event
    from gateway.run import GatewayRunner

    source = _source("resume-admission")
    source.message_id = "reply-anchor"
    entries = [
        SimpleNamespace(session_key=f"resume-key-{i}", origin=source, resume_turn_id=f"resume-{i}")
        for i in range(2)
    ]
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        restart_resume_policy="continue", durable_outbox_enabled=True,
    )
    runner._resume_pending_candidates = lambda _platform=None: entries
    runner._auto_resume_ready = lambda _entry: (adapter, source)
    runner._release_running_agent_state = lambda key: setattr(
        runner._session_state(key).turn, "agent", None
    )
    runner._persist_active_agents = lambda: None
    tasks = []
    runner._retain_background_task = lambda task: tasks.append(task) or task
    runner._resolve_profile_home_for_source = lambda _source: tmp_path

    class _Adapter:
        _session_tasks = {}

        async def handle_message(self, event):
            await _admit_outbox_event(runner, event, event.source)
            event._gateway_accepted = True
            # A real turn reports completion; "resumed" is only logged after it.
            event._agent_turn_succeeded = True

    adapter = _Adapter()
    scheduled = runner._schedule_resume_pending_sessions()
    assert scheduled == 2
    await asyncio.gather(*tasks)

    with sqlite3.connect(tmp_path / "gateway-outbox.db") as db:
        admitted = sorted(row[0] for row in db.execute(
            "SELECT transport_event_id FROM admissions"
        ))
    assert admitted == ["resume:resume-0", "resume:resume-1"]
