"""Shared setup for tests that seed a plugin registry instead of scanning disk."""


def mark_plugin_discovery_complete(manager) -> None:
    """Publish both lifecycle flags for a fully seeded test registry."""
    manager._discovered = manager._discovery_complete = True


def mark_plugin_discovery_started(manager) -> None:
    """Model discovery's published re-entrancy flag before loading completes."""
    manager._discovered = True
    manager._discovery_complete = False


def mock_completed_plugin_discovery(monkeypatch) -> None:
    """Use a complete, empty registry when a unit fixture supplies tool definitions."""
    from hermes_cli import plugins

    manager = plugins.PluginManager()
    mark_plugin_discovery_complete(manager)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **k: None)
