"""API admission never blocks the shared owner loop on a held SQLite writer (every HTTP door)."""
import asyncio
import sqlite3
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEY = 'off-loop-admission-key-long-enough'
AUTH = {'Authorization': 'Bearer ' + KEY}

DOORS = [
    ('chat', '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]}, {}, 200),
    ('responses', '/v1/responses', {'input': 'hi'}, {'Idempotency-Key': 'resp-1'}, 200),
    ('runs', '/v1/runs', {'input': 'hi'}, {}, 202),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('name,path,body,headers,expected', DOORS, ids=[d[0] for d in DOORS])
async def test_admission_waits_for_the_writer_off_the_owner_loop(api, owner, monkeypatch, name, path, body,
                                                                headers, expected):
    from gateway.platforms import api_server_runs

    async def handle(event):
        from gateway.session_results import execution_result
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    async def no_execution(*args, **kwargs):
        pass
    owner.runner._handle_message = handle
    monkeypatch.setattr(api_server_runs, '_execute_run', no_execution)
    api._api_key = KEY
    app = web.Application()
    app.router.add_post(path, getattr(api, {'chat': '_handle_chat_completions', 'responses': '_handle_responses',
                                            'runs': '_handle_runs'}[name]))
    loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    contended = asyncio.Event()
    write_threads = []
    execute_write = owner.db._execute_write
    retry_write = owner.db._sleep_before_write_retry

    def off_loop_write(*args, **kwargs):
        thread = threading.get_ident()
        write_threads.append(thread)
        assert thread != owner_thread, 'blocking admission write ran on the owner loop thread'
        return execute_write(*args, **kwargs)

    def observed_retry(*args, **kwargs):
        # This is a real SQLite busy retry, not a synthetic delay in a mocked write.
        assert threading.get_ident() != owner_thread
        loop.call_soon_threadsafe(contended.set)
        return retry_write(*args, **kwargs)

    monkeypatch.setattr(owner.db, '_execute_write', off_loop_write)
    monkeypatch.setattr(owner.db, '_sleep_before_write_retry', observed_retry)
    async with TestClient(TestServer(app)) as client:
        holder = sqlite3.connect(owner.db.db_path, timeout=0, isolation_level=None)
        holder.execute('BEGIN IMMEDIATE')
        request = asyncio.create_task(client.post(path, json=body, headers={**AUTH, **headers}))
        observed = asyncio.create_task(contended.wait())
        try:
            # A busy retry must reach the owner loop while the actual writer is still held.
            # Thread identity, not wall-clock tick gaps, distinguishes admission blocking
            # from unrelated callbacks or descheduling on a shared CI runner.
            done, _ = await asyncio.wait({request, observed}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            assert observed in done, 'admission did not encounter the held SQLite writer off-loop'
            assert write_threads and all(thread != owner_thread for thread in write_threads)
            assert not request.done(), 'admission must await the write, not ACK before commit'
        finally:
            holder.rollback()
            holder.close()
            observed.cancel()
            await asyncio.gather(observed, return_exceptions=True)
            response = await asyncio.wait_for(request, 10)
        assert response.status == expected, await response.text()
