"""Keep TUI worker thread fakes local to the surface that owns them."""

import threading
from types import SimpleNamespace

from agent import memory_provider

_SESSION_WORK_PREFIXES = ("prompt-turn-", "auto-continue-", "side-agent-")


def patch_server_threads(monkeypatch, server, factory):
    """Replace surface workers without changing process-wide infrastructure threads.

    Session work uses spawn_context_thread, which must keep its context binding;
    only its three surface-worker name families use the supplied test factory.
    """
    surface = SimpleNamespace(**vars(server.threading))
    surface.Thread = factory
    monkeypatch.setattr(server, "threading", surface)
    context_threads = SimpleNamespace(**vars(memory_provider.threading))

    def selected_thread(*args, **kwargs):
        name = kwargs.get("name") or ""
        selected = factory if name.startswith(_SESSION_WORK_PREFIXES) else threading.Thread
        return selected(*args, **kwargs)

    context_threads.Thread = selected_thread
    monkeypatch.setattr(memory_provider, "threading", context_threads)
