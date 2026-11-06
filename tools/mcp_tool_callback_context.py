"""Callback ownership for a multiplexed MCP receive loop (never trust ambient context).

MCP notifications carry no owning RPC id. Overlapping calls therefore make the
whole busy cohort ambiguous, even after one exits; late private callbacks fail closed for their source lifetime. Standalone handler users without a server thunk retain caller context.
"""
from __future__ import annotations

import asyncio
import contextvars
from typing import Any
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger("tools.mcp_tool")

_rpc_target: contextvars.ContextVar[Any] = contextvars.ContextVar("mcp_rpc_transport", default=None)


def rpc_target(server):
    target = _rpc_target.get()
    return target[1] if target is not None and target[0] is server else server


@asynccontextmanager
async def private_rpc_transport(server):
    from agent.incognito import is_incognito
    from tools.mcp_tool import MCPServerTask
    # Generic in-process _dispatch users have no transport lifetime to fork. All
    # production MCP connections are MCPServerTask instances (subclasses included).
    if not is_incognito() or not isinstance(server, MCPServerTask):
        yield server
        return
    # Never register/reuse a private connection. Teardown includes its SDK task
    # group, including callbacks that outlive the client request or cancellation.
    private = type(server)(server.name)
    private._private_transport = True
    private._callback_source_private = True
    private.tool_timeout = server.tool_timeout
    token = _rpc_target.set((server, private))
    try:
        await private.start(dict(server._config))
        yield private
    finally:
        try:
            await private.shutdown()
        finally:
            _rpc_target.reset(token)



def begin_rpc_owner(server):
    from agent.incognito import is_incognito
    if is_incognito():
        server._callback_source_private = True
    owners = getattr(server, "_rpc_call_contexts", None)
    if owners is None:
        owners = server._rpc_call_contexts = {}
    if not owners:
        server._rpc_context_ambiguous = False
    else:
        server._rpc_context_ambiguous = True
    token = object()
    owners[token] = contextvars.copy_context()
    server._pending_call_context = None if server._rpc_context_ambiguous else owners[token]
    return token


def end_rpc_owner(server, token):
    owners = server._rpc_call_contexts
    owners.pop(token, None)
    server._pending_call_context = (
        next(iter(owners.values())) if len(owners) == 1 and not server._rpc_context_ambiguous else None)
    if not owners:
        server._rpc_context_ambiguous = False


async def run_owned_callback(call_context, callback, *args, on_unowned=lambda: None, private_allowed=True):
    """Replay ownership around admission, parsing, exceptions and log emission alike."""
    captured = contextvars.copy_context() if call_context is None else call_context()
    if captured is None:
        logger.info("MCP callback suppressed without an unambiguous RPC owner")
        return on_unowned()
    if not private_allowed:
        from agent.incognito import is_incognito
        if captured.run(is_incognito):
            return on_unowned()  # do not parse or schedule a persistence-capable notification
    return await captured.copy().run(asyncio.create_task, callback(*args))


def callback_owner(server):
    """Connection-owned admission; never assign late private A to normal B."""
    from agent.incognito import is_incognito
    captured = getattr(server, '_pending_call_context', None)
    private = getattr(server, '_callback_source_private', False)
    if captured is not None:
        return captured if not private or captured.run(is_incognito) else None
    if not private and not getattr(server, '_rpc_call_contexts', {}):
        return contextvars.copy_context()  # normal unsolicited notifications remain supported
    return None
