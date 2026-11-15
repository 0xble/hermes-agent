"""Source-only browser node refusals through real registration and public dispatch."""
from __future__ import annotations

import importlib
import json
import socket
import subprocess

import pytest

from tools import browser_tool
from tools.registry import registry
from tests.tools.test_computer_use_nodes import endpoint, home  # noqa: F401

# These entrypoints register outside browser_tool's table. Exercise their actual
# registrations too, without calling any handler or replacing the registry.
for _module in ("browser_cdp_tool", "browser_dialog_tool", "browser_use_cli", "browser_vault_tool"):
    importlib.import_module(f"tools.{_module}")

BROWSER_TOOLS = tuple(name for name in registry.get_all_tool_names() if name.startswith("browser_"))


def dispatch(name="browser_navigate", *, session="browser-session", task="raw-task", **args):
    result = registry.dispatch(name, args, session_id=session, task_id=task)
    return json.loads(result) if isinstance(result, str) else result


def assert_refused(out, node):
    assert out.get("code") == "node_operation_unsupported", out
    assert "node" in out and out["node"] == node, out
    assert out.get("reason") == "named_node_browser_not_implemented", out
    assert "No controller browser or Camofox fallback" in out.get("error", ""), out


@pytest.fixture(autouse=True)
def no_other_browser(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("named node fell through to a controller browser")
    monkeypatch.setattr(browser_tool, "browser_navigate", forbidden)
    monkeypatch.setattr(browser_tool, "browser_snapshot", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    from gateway import browser_control_broker
    monkeypatch.setattr(browser_control_broker, "browser_control_enabled", forbidden)


def test_named_navigation_refuses_without_fallback(endpoint):
    a, _, _, calls = endpoint
    args = {"node": "studio", "chrome_profile": "Personal", "url": "https://example.com"}
    with home(a):
        out = dispatch(**args)
    assert_refused(out, "studio")
    assert args == {"node": "studio", "chrome_profile": "Personal", "url": "https://example.com"}
    assert not calls()


@pytest.mark.parametrize("name", BROWSER_TOOLS)
def test_every_registered_browser_refuses_explicit_node(name):
    assert_refused(dispatch(name, node="studio"), "studio")


@pytest.mark.parametrize("node", [None, "", " ", [], {}, 5, "missing"])
def test_bad_node_never_falls_back(endpoint, node):
    a, _, _, calls = endpoint
    with home(a):
        out = dispatch(node=node, chrome_profile="Personal", url="https://example.com")
    assert_refused(out, node)
    assert not calls()


@pytest.mark.parametrize("name", BROWSER_TOOLS)
def test_model_dispatch_preserves_node_refusal(name):
    from model_tools import handle_function_call
    result = handle_function_call(name, {"node": "studio"}, task_id="raw-task",
                                  session_id="browser-session")
    assert_refused(json.loads(str(result)), "studio")


def test_omitted_node_preserves_account_and_raw_task_with_node_default(endpoint, monkeypatch):
    from gateway import browser_control_broker
    a, _, _, calls = endpoint
    forwarded = []

    def navigate(**args):
        forwarded.append(args)
        return json.dumps({"success": True, "backend": "camofox"})

    monkeypatch.setattr(browser_tool, "browser_navigate", navigate)
    monkeypatch.setattr(browser_control_broker, "browser_control_enabled", lambda: False)
    with home(a):  # This profile has nodes.default=studio. Browsers must ignore it.
        out = dispatch(account="personal", url="https://example.com", task="raw-task")
    assert out == {"success": True, "backend": "camofox"}
    assert forwarded == [{"url": "https://example.com", "account": "personal", "task_id": "raw-task"}]
    assert not calls()


def test_non_browser_handler_still_receives_node(monkeypatch):
    from tools.registry import ToolRegistry
    ordinary = ToolRegistry()
    seen = []

    def handler(args, **kw):
        seen.append((args, kw))
        return json.dumps({"success": True})

    ordinary.register(name="example_tool", toolset="example", schema={}, handler=handler)
    assert json.loads(ordinary.dispatch("example_tool", {"node": "studio"}, task_id="raw-task")) == {
        "success": True,
    }
    assert seen == [({"node": "studio"}, {"task_id": "raw-task"})]
