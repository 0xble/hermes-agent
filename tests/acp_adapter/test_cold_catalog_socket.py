"""ACP registry/label discovery must not open sockets on the caller."""
import socket
import threading

import pytest


@pytest.fixture(autouse=True)
def _offline_model_inventory():
    """Exercise the real inventory rather than ACP conftest's stub."""


def test_cold_acp_catalog_does_not_open_foreground_socket(monkeypatch, tmp_path):
    from acp_adapter.model_catalog import build_model_state
    from agent import models_dev
    from hermes_cli import model_switch_providers as picker

    cfg = {"model": {"provider": "xai", "default": "fixture-current"},
           "nous": {"guest": False}}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "fixture-key")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)
    monkeypatch.setattr(models_dev, "_load_disk_cache", lambda: {})
    monkeypatch.setattr(picker, "prewarm_picker_cache_async", lambda: None)
    monkeypatch.setattr(picker, "_overlay_has_creds", lambda b, pid, slug, overlay: slug == "xai")
    attempts = []
    caller = threading.current_thread()

    def denied(*args, **kwargs):
        # Background warmers are allowed; deny their sockets too, but only the
        # attaching thread is forbidden to make a foreground attempt.
        if threading.current_thread() is caller:
            attempts.append((threading.current_thread().name, str(args[0])))
        raise OSError("cold discovery sockets blocked by test")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    state = build_model_state("fixture-current", "xai", "")
    assert state is not None
    assert state.current_model_id in {model.model_id for model in state.available_models}
    assert attempts == [], f"ACP foreground discovery reached sockets: {attempts}"
