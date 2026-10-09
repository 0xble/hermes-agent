"""A cron payload the live lane rejected as reconnect-only is not lost when standalone fails (#125363).

``send_path_degraded`` means the adapter that owns the connection will deliver after it reconnects.
The router raises that rejection (``DeliveryRouter._deliver_to_platform`` -> RuntimeError), so it
reaches the cron lane on the EXCEPTION arm of ``_deliver_via_live_adapter``. A satellite profile's
cron worker has no platform token, so the standalone fallback fails; the payload must then wait in
the delivery ledger for that adapter's post-reconnect sweep. Any other failure still falls through
to standalone exactly as before and queues nothing.
"""

import asyncio
import threading

import pytest

import cron.scheduler_delivery as sd
from gateway import delivery_ledger as dl
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult


@pytest.fixture(autouse=True)
def _fresh_ledger(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda config=None: True)
    monkeypatch.setattr(sd, "_maybe_mirror_cron_delivery", lambda *a, **k: None)


@pytest.fixture
def gateway_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)


def _deliver_through_router(monkeypatch, loop, *, live_error: str):
    """Run the live lane against a transport whose send is rejected with ``live_error`` (the router
    raises it), then the standalone lane on a token-less worker. Returns (standalone calls, errors)."""
    class Transport:
        adapter = type("Adapter", (), {"_owner_profile": "satellite"})()
        is_relay = False

        async def send(self, platform, chat_id, content, metadata=None):
            return SendResult(success=False, error=live_error, retryable=live_error == "send_path_degraded")

    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "job-1"}, platform=Platform.TELEGRAM, platform_name="telegram", chat_id="-100",
                  thread_id="42", transport=Transport(), config=GatewayConfig(), loop=loop,
                  target_adapters={}, mirror_text="", origin={})
    t = sd._TargetDelivery(**fields)
    standalone_calls = []
    monkeypatch.setattr(
        sd, "_standalone_send",
        lambda t, content, media: standalone_calls.append(content) or (None, "You must pass the token from BotFather"))
    target_errors, delivery_errors = [], []
    assert not sd._deliver_via_live_adapter(
        t, "the report", [], target_errors=target_errors, delivery_errors=delivery_errors, unverified_targets=[])
    sd._deliver_standalone(t, "the report", [], target_errors, delivery_errors)
    return standalone_calls, delivery_errors


def test_reconnect_only_rejection_survives_a_failed_standalone_for_the_sweep(monkeypatch, gateway_loop):
    standalone_calls, errors = _deliver_through_router(monkeypatch, gateway_loop, live_error="send_path_degraded")
    assert standalone_calls == ["the report"]  # standalone still gets its chance first
    assert any("queued text for telegram:-100:42" in e for e in errors)
    claimed = dl.sweep_failed_for_runtime("telegram", profile="satellite")
    assert [(row["chat_id"], row["thread_id"], row["content"]) for row in claimed] == [("-100", "42", "the report")]


def test_live_partial_failure_falls_back_only_to_undelivered_parts(monkeypatch, gateway_loop):
    class Transport:
        adapter = type("Adapter", (), {"_owner_profile": "satellite"})()
        is_relay = False

        def __init__(self):
            self.calls = []

        async def send(self, platform, chat_id, content, metadata=None):
            self.calls.append((content, metadata or {}))
            if content == "second":
                return SendResult(success=False, error="blocked")
            return SendResult(success=True, message_id=str(len(self.calls)))

    transport = Transport()
    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "job-partial"}, platform=Platform.TELEGRAM, platform_name="telegram", chat_id="-100",
                  thread_id="42", transport=transport, config=GatewayConfig(), loop=gateway_loop,
                  target_adapters={}, mirror_text="", origin={})
    t = sd._TargetDelivery(**fields)
    fallback_calls = []
    monkeypatch.setattr(
        sd, "_standalone_send",
        lambda _t, content, media, **kwargs: fallback_calls.append((content, kwargs.get("copy_block")))
        or ({"success": True}, None),
    )
    target_errors, delivery_errors = [], []
    assert not sd._deliver_via_live_adapter(
        t, "reply", [], target_errors=target_errors, delivery_errors=delivery_errors,
        unverified_targets=[], copy_blocks=["first", "second"],
    )
    sd._deliver_standalone(
        t, "", [], target_errors, delivery_errors, copy_blocks=["second"])
    assert [content for content, _metadata in transport.calls] == ["reply", "first", "second"]
    assert fallback_calls == [("second", True)]


def test_reconnect_queue_preserves_copy_block_markers_and_order(monkeypatch):
    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "job-copy-queue"}, platform=Platform.TELEGRAM, platform_name="telegram", chat_id="-100",
                  thread_id="42", transport=None, config=GatewayConfig(), loop=None,
                  target_adapters={}, mirror_text="", origin={}, live_error="send_path_degraded")
    t = sd._TargetDelivery(**fields)
    errors = []
    sd._queue_for_live_reconnect(t, "reply", [], errors, copy_blocks=["first", "second"])
    claimed = dl.sweep_failed_for_runtime("telegram", profile=None)
    assert len(claimed) == 1
    assert claimed[0]["content"] == (
        "reply\n[[copy]]\nfirst\n[[/copy]]\n[[copy]]\nsecond\n[[/copy]]")


def test_reconnect_only_rejection_retries_live_before_standalone(monkeypatch, gateway_loop):
    """A reconnect-only refusal must keep the rich live lane in charge before fallback."""
    class Transport:
        adapter = type("Adapter", (), {"_owner_profile": "satellite"})()
        is_relay = False

        def __init__(self):
            self.calls = 0

        async def send(self, platform, chat_id, content, metadata=None):
            self.calls += 1
            if self.calls == 1:
                return SendResult(success=False, error="send_path_degraded", retryable=True)
            return SendResult(success=True, message_id="99")

    transport = Transport()
    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "job-retry"}, platform=Platform.TELEGRAM, platform_name="telegram", chat_id="-100",
                  thread_id="42", transport=transport, config=GatewayConfig(), loop=gateway_loop,
                  target_adapters={}, mirror_text="", origin={})
    t = sd._TargetDelivery(**fields)
    monkeypatch.setattr(sd, "_LIVE_RECONNECT_WAIT_BUDGET_SECS", 0.1, raising=False)
    monkeypatch.setattr(sd, "_LIVE_RECONNECT_BACKOFF_SECS", (0.001,), raising=False)
    standalone_calls = []
    monkeypatch.setattr(sd, "_standalone_send", lambda *args: standalone_calls.append(True) or ({"success": True}, None))

    target_errors, delivery_errors = [], []
    assert sd._deliver_via_live_adapter(
        t, "the rich report", [], target_errors=target_errors,
        delivery_errors=delivery_errors, unverified_targets=[])
    assert transport.calls == 2
    assert standalone_calls == []


def test_transient_fallback_marks_formatting_degraded(monkeypatch, gateway_loop):
    """Only a Telegram fallback after a transient live rejection is formatting-degraded."""
    class Transport:
        adapter = type("Adapter", (), {"_owner_profile": "satellite"})()
        is_relay = False

        async def send(self, platform, chat_id, content, metadata=None):
            return SendResult(success=False, error="send_path_degraded", retryable=True)

    job = {"id": "job-degraded"}
    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job=job, platform=Platform.TELEGRAM, platform_name="telegram", chat_id="-100",
                  thread_id="42", transport=Transport(), config=GatewayConfig(), loop=gateway_loop,
                  target_adapters={}, mirror_text="", origin={})
    t = sd._TargetDelivery(**fields)
    monkeypatch.setattr(sd, "_LIVE_RECONNECT_WAIT_BUDGET_SECS", 0.0, raising=False)
    monkeypatch.setattr(sd, "_standalone_send", lambda *args: ({"success": True}, None))

    target_errors, delivery_errors = [], []
    assert not sd._deliver_via_live_adapter(
        t, "the report", [], target_errors=target_errors,
        delivery_errors=delivery_errors, unverified_targets=[])
    sd._deliver_standalone(t, "the report", [], target_errors, delivery_errors)
    assert job["_formatting_degraded_targets"] == ["telegram:-100:42"]


def test_formatting_degraded_receipt_is_persisted(monkeypatch):
    job = {"id": "job-receipt", "_formatting_degraded_targets": ["telegram:-100:42"]}
    persisted = {}
    monkeypatch.setattr("cron.jobs.update_job", lambda job_id, values: persisted.update(values))

    sd._record_delivery_verification(job, [])

    assert job["last_delivery_formatting_degraded"] == ["telegram:-100:42"]
    assert persisted["last_delivery_formatting_degraded"] == ["telegram:-100:42"]


def test_non_telegram_reconnect_rejection_falls_back_without_waiting(monkeypatch, gateway_loop):
    """Discord also reports send_path_degraded; its cron delivery keeps the immediate fallback."""
    class Transport:
        adapter = type("Adapter", (), {"_owner_profile": "satellite"})()
        is_relay = False

        def __init__(self):
            self.calls = 0

        async def send(self, platform, chat_id, content, metadata=None):
            self.calls += 1
            return SendResult(success=False, error="send_path_degraded", retryable=True)

    transport = Transport()
    fields = {name: None for name in sd._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "job-discord"}, platform=Platform.DISCORD, platform_name="discord", chat_id="555",
                  thread_id=None, transport=transport, config=GatewayConfig(), loop=gateway_loop,
                  target_adapters={}, mirror_text="", origin={})
    t = sd._TargetDelivery(**fields)
    sleeps = []
    monkeypatch.setattr(sd.time, "sleep", sleeps.append)

    assert not sd._deliver_via_live_adapter(
        t, "the report", [], target_errors=[], delivery_errors=[], unverified_targets=[])
    assert transport.calls == 1
    assert sleeps == []


def test_reconnect_waits_use_the_whole_budget():
    """The default backoff schedule reaches the full reconnect budget instead of stopping at 63s."""
    waited, attempt = 0.0, 0
    while (wait := sd._short_reconnect_wait(RuntimeError("send_path_degraded"), waited, attempt)) is not None:
        assert wait > 0
        waited += wait
        attempt += 1
    assert waited == sd._LIVE_RECONNECT_WAIT_BUDGET_SECS == 120.0
    assert sd._short_reconnect_wait(RuntimeError("chat not found"), 0.0, 0) is None


def test_other_live_failures_still_fall_to_standalone_and_queue_nothing(monkeypatch, gateway_loop):
    standalone_calls, errors = _deliver_through_router(monkeypatch, gateway_loop, live_error="chat not found")
    assert standalone_calls == ["the report"]
    assert any("BotFather" in e for e in errors) and not any("queued" in e for e in errors)
    assert dl.sweep_failed_for_runtime("telegram", profile="satellite") == []
