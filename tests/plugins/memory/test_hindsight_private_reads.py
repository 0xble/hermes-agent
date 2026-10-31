"""Private reads exercise the real HTTP client and SDK validation, not SDK mocks."""

from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from agent.memory_manager import MemoryManager
from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.private_reads import PrivateReadClient, PrivateReadError
from tests.home_io_guard import HomeIOGuard

QUERY = "Which instrument does the fixture owner play?"
FACT = "The fixture owner plays the bassoon."
SESSION = "private-read-fixture-session"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def endpoint():
    state = SimpleNamespace(
        calls=[],
        capability={"features": {"private_reads": True}},
        version_status=200,
        read_status=200,
        acknowledged=True,
        response={
            "results": [{"id": "fixture-fact", "text": FACT, "type": "observation"}]
        },
        redirect=None,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def answer(self, method):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.calls.append((method, self.path, dict(self.headers), body))
            is_version = self.path == "/version"
            self.send_response(
                state.version_status if is_version else state.read_status
            )
            self.send_header("Content-Type", "application/json")
            if state.acknowledged and not is_version:
                self.send_header("X-Hindsight-Private-Read", "1")
            if state.redirect:
                self.send_header("Location", state.redirect)
            self.end_headers()
            payload = state.capability if is_version else state.response
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            self.answer("GET")

        def do_POST(self):
            self.answer("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def client(endpoint):
    return PrivateReadClient(
        base_url=endpoint.url, api_key=None, timeout=2, session_id=SESSION, epoch=7
    )


@pytest.mark.parametrize("operation", ["recall", "reflect"])
def test_bodyless_capability_before_private_payload(endpoint, operation):
    if operation == "reflect":
        endpoint.response = {"text": FACT}
    response = asyncio.run(
        getattr(client(endpoint), "a" + operation)(
            bank_id="fixture bank", query=QUERY, budget="mid"
        )
    )
    assert [(method, path, body) for method, path, _, body in endpoint.calls[:1]] == [
        ("GET", "/version", b"")
    ]
    method, path, headers, body = endpoint.calls[1]
    assert method == "POST"
    assert path == f"/v1/default/banks/fixture%20bank/private/{operation}"
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body)["query"] == QUERY
    assert response.result_nonempty is True
    tool = json.loads(response.tool_result(FACT))
    assert tool["result"] == FACT
    assert tool["_hermes_private_read"] == {
        "no_store": True,
        "session_sha256": digest(SESSION),
        "epoch": 7,
        "result_nonempty": True,
        "bank_sha256": digest("fixture bank"),
        "query_sha256": digest(QUERY),
        "result_text_sha256": digest(FACT),
    }
    assert QUERY not in json.dumps(tool["_hermes_private_read"])
    assert FACT not in json.dumps(tool["_hermes_private_read"])
    assert SESSION not in json.dumps(tool["_hermes_private_read"])


@pytest.mark.parametrize(
    "capability",
    [
        {},
        {"features": {}},
        {"features": {"private_reads": False}},
        {"features": {"private_reads": "true"}},
        [],
        {"features": None},
    ],
)
def test_unsupported_or_malformed_capability_never_sends_query(endpoint, capability):
    endpoint.capability = capability
    with pytest.raises(PrivateReadError):
        asyncio.run(client(endpoint).arecall(bank_id="fixture", query=QUERY))
    assert [(method, path, body) for method, path, _, body in endpoint.calls] == [
        ("GET", "/version", b"")
    ]


@pytest.mark.parametrize("stage", ["version", "read"])
def test_redirect_is_not_followed(endpoint, stage):
    endpoint.redirect = endpoint.url + "/public-redirect"
    setattr(endpoint, stage + "_status", 307)
    with pytest.raises(PrivateReadError):
        asyncio.run(client(endpoint).arecall(bank_id="fixture", query=QUERY))
    assert len(endpoint.calls) == (1 if stage == "version" else 2)
    assert all("redirect" not in path for _, path, _, _ in endpoint.calls)


@pytest.mark.parametrize("bank", ["", ".", "..", "nested/bank", "nested\\bank"])
def test_invalid_bank_fails_before_network(endpoint, bank):
    with pytest.raises(PrivateReadError):
        asyncio.run(client(endpoint).arecall(bank_id=bank, query=QUERY))
    assert endpoint.calls == []


@pytest.mark.parametrize(
    "fault", ["missing_ack", "http_error", "malformed_response", "missing_result_text"]
)
def test_no_public_fallback_or_private_error_contents(endpoint, fault, caplog):
    if fault == "missing_ack":
        endpoint.acknowledged = False
    elif fault == "http_error":
        endpoint.read_status = 500
    endpoint.response = (
        {"error": QUERY + FACT}
        if fault != "missing_result_text"
        else {"results": [{"id": "fixture-fact", "trace": QUERY + FACT}]}
    )
    with pytest.raises(PrivateReadError) as caught:
        asyncio.run(client(endpoint).arecall(bank_id="fixture", query=QUERY))
    assert len(endpoint.calls) == 2
    assert all(
        path == "/version" or "/private/" in path for _, path, _, _ in endpoint.calls
    )
    assert QUERY not in str(caught.value)
    assert FACT not in str(caught.value)
    assert QUERY not in caplog.text
    assert FACT not in caplog.text


@pytest.mark.parametrize(
    "operation,payload", [("recall", {"results": []}), ("reflect", {"text": "  "})]
)
def test_empty_typed_results_are_not_positive_recall_proof(
    endpoint, operation, payload
):
    endpoint.response = payload
    response = asyncio.run(
        getattr(client(endpoint), "a" + operation)(bank_id="fixture", query=QUERY)
    )
    assert response.result_nonempty is False
    assert (
        json.loads(response.tool_result("No relevant memories found."))[
            "_hermes_private_read"
        ]["result_nonempty"]
        is False
    )


@pytest.fixture
def provider(endpoint, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: home)
    config = home / "hindsight" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "mode": "local_external",
                "api_url": endpoint.url,
                "bank_id": "fixture",
                "auto_recall": False,
                "auto_retain": False,
            }
        ),
        encoding="utf-8",
    )
    provider = HindsightMemoryProvider()
    provider.initialize(session_id=SESSION, hermes_home=str(home), platform="cli")
    yield provider
    provider.shutdown()


@pytest.mark.parametrize("tool", ["hindsight_recall", "hindsight_reflect"])
def test_real_manager_private_dispatch_has_correlated_typed_result(
    endpoint, provider, tool
):
    if tool == "hindsight_reflect":
        endpoint.response = {"text": FACT}
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.set_read_only(True)
    payload = json.loads(manager.handle_tool_call(tool, {"query": QUERY}))
    assert FACT in payload["result"]
    provenance = payload["_hermes_private_read"]
    assert provenance["no_store"] is True
    assert provenance["session_sha256"] == digest(SESSION)
    assert provenance["epoch"] == provider._privacy_epoch
    assert provenance["query_sha256"] == digest(QUERY)
    assert provenance["result_text_sha256"] == digest(payload["result"])
    assert provenance["result_nonempty"] is True
    assert [path for _, path, _, _ in endpoint.calls] == [
        "/version",
        "/v1/default/banks/fixture/private/" + tool.removeprefix("hindsight_"),
    ]
    assert provider._client is None


def test_read_only_refuses_unsupported_server_without_creating_public_sdk(
    endpoint, provider
):
    endpoint.capability = {"features": {"private_reads": False}}
    provider.read_only = True
    text = provider.handle_tool_call("hindsight_recall", {"query": QUERY})
    assert json.loads(text) == {
        "error": "Failed to search memory: Hindsight does not support no-store reads."
    }
    assert QUERY not in text
    assert FACT not in text
    assert provider._client is None
    assert len(endpoint.calls) == 1
    assert endpoint.calls[0][0:2] == ("GET", "/version")


def test_private_client_uses_no_hermes_home_io(
    endpoint, provider, tmp_path, monkeypatch
):
    # Warm only optional dependency imports, never a private query or public SDK operation.
    from hindsight_client_api.models.recall_request import RecallRequest
    from hindsight_client_api.models.recall_response import RecallResponse

    assert RecallRequest and RecallResponse
    home = tmp_path / "home"
    provider.read_only = True
    guard = HomeIOGuard(lambda: (home,))
    with monkeypatch.context() as guarded:
        guard.install(guarded)
        response = provider.handle_tool_call("hindsight_recall", {"query": QUERY})
    payload = json.loads(response)
    assert FACT in payload["result"]
    assert payload["_hermes_private_read"]["no_store"] is True


def test_public_mode_keeps_the_existing_sdk_dispatch(provider):
    calls = []

    class PublicClient:
        async def arecall(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                results=[SimpleNamespace(text=FACT, context=None, type="observation")]
            )

        async def aclose(self):
            pass

    provider._client = PublicClient()
    text = provider.handle_tool_call("hindsight_recall", {"query": QUERY})
    assert isinstance(text, str)
    assert FACT in text
    assert len(calls) == 1
    assert calls[0]["bank_id"] == "fixture"


@pytest.mark.parametrize("tool", ["hindsight_recall", "hindsight_reflect"])
def test_private_provider_rejects_unacknowledged_public_sdk_result(
    provider, tool, monkeypatch
):
    provider.read_only = True
    response = SimpleNamespace(results=[SimpleNamespace(text=FACT)], text=FACT)
    monkeypatch.setattr(
        provider, "_run_hindsight_operation", lambda *args, **kwargs: response
    )
    payload = provider.handle_tool_call(tool, {"query": QUERY})
    prefix = (
        "Failed to search memory" if tool == "hindsight_recall" else "Failed to reflect"
    )
    assert json.loads(payload) == {
        "error": prefix + ": Private Hindsight read was not acknowledged."
    }
    assert FACT not in payload
    assert QUERY not in payload


def test_provenance_stays_bound_to_dispatch_epoch(endpoint):
    private_client = client(endpoint)
    response = asyncio.run(private_client.arecall(bank_id="fixture", query=QUERY))
    assert json.loads(response.tool_result(FACT))["_hermes_private_read"]["epoch"] == 7
    # Changing a later client cannot relabel a response from an obsolete interval.
    later = PrivateReadClient(
        base_url=endpoint.url,
        api_key=None,
        timeout=2,
        session_id="other-session",
        epoch=9,
    )
    assert later is not private_client
    assert json.loads(response.tool_result(FACT))["_hermes_private_read"][
        "session_sha256"
    ] == digest(SESSION)
    assert json.loads(response.tool_result(FACT))["_hermes_private_read"]["epoch"] == 7


class _RecordingPublicClient:
    def __init__(self, hold=None):
        self.recalls, self.retains = [], []
        self._hold = hold

    async def arecall(self, **kwargs):
        self.recalls.append(kwargs["query"])
        return SimpleNamespace(results=[SimpleNamespace(text=FACT)])

    async def aretain_batch(self, **kwargs):
        if self._hold is not None and not self.retains:
            await asyncio.get_running_loop().run_in_executor(None, self._hold.wait, 5)
        self.retains.append(json.dumps(kwargs["items"]))
        return SimpleNamespace()

    async def aclose(self):
        pass


def _until(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_queued_private_prefetch_never_takes_the_public_route_after_off(provider, monkeypatch):
    """A recall queued while private is dropped at the boundary, never sent publicly."""
    gate, waiting = threading.Event(), threading.Event()
    public = _RecordingPublicClient()
    provider._client = public
    provider._auto_recall = True
    provider._prefetch_waits_for_retain = True

    def held_drain(timeout):
        waiting.set()
        return gate.wait(5)

    monkeypatch.setattr(provider, "_wait_for_retains_drained", held_drain)
    provider.read_only = True
    provider.queue_prefetch("PRIVATE_PREFETCH_QUERY")
    assert waiting.wait(5)
    provider.read_only = False
    gate.set()
    provider._join_prefetch(5)
    assert public.recalls == []
    assert provider.prefetch("next public turn") == ""


def test_public_retains_survive_entering_incognito(provider):
    """Queued and buffered public turns still reach Hindsight; private turns never do."""
    release = threading.Event()
    public = _RecordingPublicClient(hold=release)
    provider._client = public
    provider._auto_retain = True
    provider._retain_async = False
    provider.sync_turn("PUBLIC_ONE question", "answer")
    _until(lambda: provider._retain_queue.unfinished_tasks == 1 and provider._writer_thread is not None)
    provider.sync_turn("PUBLIC_TWO question", "answer")  # queued behind the held send
    provider._retain_every_n_turns = 2
    provider.sync_turn("PUBLIC_THREE buffered", "answer")  # buffered, not yet sent
    provider.read_only = True
    provider.sync_turn("PRIVATE_TURN", "answer")
    release.set()
    _until(lambda: len(public.retains) == 3)
    sent = "".join(public.retains)
    assert "PUBLIC_ONE" in sent and "PUBLIC_TWO" in sent and "PUBLIC_THREE" in sent
    assert "PRIVATE_TURN" not in sent


def test_refused_private_auto_recall_is_reported_not_silently_empty(endpoint, provider):
    endpoint.capability = {"features": {"private_reads": False}}
    provider._auto_recall = True
    provider._recall_sync = True
    provider.read_only = True
    assert provider.prefetch(QUERY) == ""
    status = provider.recall_status()
    assert status is not None and status.detail == "private recall unavailable, no memories used"
    manager = MemoryManager()
    manager.add_provider(provider)
    assert "private recall unavailable" in manager.describe_recall()
    assert provider._client is None
