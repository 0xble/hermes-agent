"""Owner prune client accepts the same nullable runtime budget as the store."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from hermes_cli import gateway_client, sessions_owner


def test_owner_prune_accepts_explicit_null_runtime_budget(monkeypatch):
    calls = []

    @asynccontextmanager
    async def connect(endpoint):
        async def rpc(method, **params):
            calls.append((method, params))
            return {"deleted": 0}
        yield SimpleNamespace(rpc=rpc)

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    assert sessions_owner.owner_prune(object(), max_seconds=None) == {"deleted": 0}
    assert calls == [("session.prune", {"_timeout": sessions_owner.PRUNE_RPC_TIMEOUT, "max_seconds": None})]
