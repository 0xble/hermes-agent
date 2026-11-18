"""Keep diagnostics inside the test home while profile-routing tests bind other homes."""

import contextvars
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_reply_latency(_hermetic_environment, monkeypatch):
    from gateway import reply_latency, reply_latency_sink

    isolated_home = os.environ['HERMES_HOME']
    # Context-local and pinned process homes are deliberately foreign in routing
    # tests. Neither may redirect diagnostic writes out of the test's environment.
    monkeypatch.setattr(reply_latency_sink, 'log_directory', lambda:
                        Path(os.environ.get('HERMES_HOME') or isolated_home) / 'logs/reply-latency')
    monkeypatch.setattr(reply_latency_sink, '_sinks', {})
    monkeypatch.setattr(reply_latency, '_current', contextvars.ContextVar('test_reply_receipt', default=None))
    monkeypatch.setattr(reply_latency, '_delivery_kind', contextvars.ContextVar('test_delivery_kind', default=None))
