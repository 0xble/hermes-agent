"""Ambiguous standalone sends must never become reconnect replay candidates."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
import cron.scheduler_delivery as delivery
from cron import scheduler_delivery_reconnect as reconnect


@pytest.mark.parametrize("thread_fallback", [False, True])
def test_ambiguous_standalone_timeout_is_not_replayed(monkeypatch, thread_fallback):
    fields = {name: None for name in delivery._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "timeout-job"}, platform=Platform.TELEGRAM,
                  platform_name="telegram", chat_id="42", thread_id=None,
                  live_error="send_path_degraded", transport=None,
                  pconfig=SimpleNamespace())
    target = delivery._TargetDelivery(**fields)
    queued = []
    monkeypatch.setattr(delivery._sched, "_interpreter_shutting_down", lambda *args: False)
    monkeypatch.setattr(delivery, "_get_standalone_send_timeout", lambda: 1)
    monkeypatch.setattr(reconnect, "queue_for_live_reconnect",
                        lambda *args, **kwargs: queued.append(args))

    def run(coro):
        coro.close()
        if thread_fallback:
            raise RuntimeError("asyncio.run() cannot be called from a running event loop")
        raise TimeoutError()

    monkeypatch.setattr(delivery.asyncio, "run", run)
    pool = MagicMock()
    if thread_fallback:
        def submit(_context, _run, coro):
            coro.close()
            future = MagicMock()
            future.result.side_effect = TimeoutError()
            return future

        pool.submit.side_effect = submit
        monkeypatch.setattr(delivery.concurrent.futures, "ThreadPoolExecutor", lambda **kwargs: pool)
    target_errors, delivery_errors = [], []
    delivery._deliver_standalone(target, "answer", [], target_errors, delivery_errors)
    assert "may still be in flight" in target_errors[0]
    assert queued == []
    if thread_fallback:
        pool.shutdown.assert_called_once_with(wait=False)


@pytest.mark.parametrize("timed_out_payload", [0, 1, 2, 3])
def test_timeout_preserves_only_unattempted_payloads(monkeypatch, timed_out_payload):
    from gateway import delivery_ledger as ledger
    from gateway.copy_blocks import wrap_copy_block

    fields = {name: None for name in delivery._TargetDelivery.__dataclass_fields__}
    fields.update(job={"id": "multipart-timeout"}, platform=Platform.TELEGRAM,
                  platform_name="telegram", chat_id="42", thread_id=None,
                  live_error="send_path_degraded", transport=None,
                  pconfig=SimpleNamespace())
    target = delivery._TargetDelivery(**fields)
    copies, media = ["first copy", "second copy"], [("attachment.png", "image")]
    attempted = []

    def send(_target, content, files, **kwargs):
        attempted.append((content, files))
        if len(attempted) - 1 == timed_out_payload:
            return None, delivery._StandaloneTimeout("send may still be in flight")
        return {}, None

    monkeypatch.setattr(delivery, "_standalone_send", send)
    queue = MagicMock(wraps=reconnect.queue_for_live_reconnect)
    monkeypatch.setattr(reconnect, "queue_for_live_reconnect", queue)
    target_errors, delivery_errors = [], []
    delivery._deliver_standalone(target, "answer", media, target_errors,
                                 delivery_errors, copy_blocks=copies)

    assert len(attempted) == timed_out_payload + 1
    with ledger._connect() as conn:
        recovered = conn.execute("SELECT content, state FROM delivery_obligations").fetchall()
    remaining_copies = copies[max(0, timed_out_payload):]
    assert recovered == [(wrap_copy_block(block), "failed") for block in remaining_copies]
    if timed_out_payload < 3:
        queue.assert_called_once()
        assert queue.call_args.args[1:3] == ("", media)
        assert queue.call_args.kwargs["copy_blocks"] == remaining_copies
        row_label = "row" if len(remaining_copies) == 1 else "rows"
        assert f"queued {len(remaining_copies)} delivery {row_label} for telegram:42" in delivery_errors[-1]
        assert "1 attachment(s) not queued" in delivery_errors[-1]
    else:
        queue.assert_not_called()
    assert "may still be in flight" in target_errors[0]
