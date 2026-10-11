"""Ambiguous standalone sends must never become reconnect replay candidates."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
import cron.scheduler_delivery as delivery


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
    monkeypatch.setattr(delivery, "_queue_for_live_reconnect",
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
