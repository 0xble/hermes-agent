"""The busy /stop route probes durable state off-loop but mounts authority state on-loop."""
from __future__ import annotations

import asyncio
from threading import get_ident
from types import SimpleNamespace

import pytest

from gateway.run_inbound import GatewayInboundMixin


@pytest.mark.asyncio
async def test_stop_route_mount_keeps_authority_mutation_on_gateway_loop(monkeypatch):
    loop_thread = get_ident()
    probe_threads: list[int] = []
    mount_threads: list[int] = []
    sessions: dict[str, object] = {}

    class Store:
        def peek_session_id(self, key):
            probe_threads.append(get_ident())
            return f"sid:{key}"

    authority = SimpleNamespace(
        runner=SimpleNamespace(session_store=Store()),
        sessions=sessions,
        logical_owner=lambda sid: sid,
    )
    runner = object.__new__(GatewayInboundMixin)

    def restore_routed_session(_authority, sid):
        mount_threads.append(get_ident())
        sessions[sid] = object()

    monkeypatch.setattr(
        "gateway.session_route_recovery.restore_routed_session",
        restore_routed_session,
    )

    sid = await asyncio.to_thread(
        runner._hm_mounted_owner_probe, authority, "route-key"
    )
    assert probe_threads and probe_threads[0] != loop_thread

    assert runner._hm_mounted_owner(authority, sid, "route-key") == sid
    assert mount_threads == [loop_thread]
    assert sessions == {sid: next(iter(sessions.values()))}
