"""Incognito content never reaches undeclared plugin callbacks."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent.incognito import incognito_scope, register_session
from hermes_cli.middleware import run_tool_execution_middleware
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugins_manifest import parse_manifest_file


CANARY = "PRIVATE_PLUGIN_CANARY_f7a239c8d15b"


class IncognitoPluginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="incognito-plugins-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.manager = PluginManager()
        self.addCleanup(self.manager.unload)
        self.unsafe = PluginContext(PluginManifest(name="unsafe"), self.manager)
        self.safe = PluginContext(PluginManifest(name="safe", incognito_safe=True), self.manager)
        self.seen = []
        self.sid = "private-plugin-session"
        self.addCleanup(register_session, self.sid, False)

    def test_sync_async_hooks_and_request_middleware(self):
        for ctx in (self.unsafe, self.safe):
            def callback(_name=ctx.plugin_id, **kwargs):
                self.seen.append((_name, kwargs["content"]))
                return _name
            ctx.register_hook("pre_tool_call", callback)
            ctx.register_middleware("tool_request", callback)
        with incognito_scope():
            self.assertEqual(self.manager.invoke_hook("pre_tool_call", content=CANARY), ["safe"])
            self.assertEqual(asyncio.run(self.manager.ainvoke_hook("pre_tool_call", content=CANARY)), ["safe"])
            self.assertEqual(self.manager.invoke_middleware("tool_request", content=CANARY), ["safe"])
        self.assertEqual(self.seen, [("safe", CANARY)] * 3)
        self.seen.clear()
        self.assertEqual(self.manager.invoke_hook("pre_tool_call", content="normal"), ["unsafe", "safe"])
        self.assertEqual([name for name, _ in self.seen], ["unsafe", "safe"])

    def test_plugin_tools_require_the_same_privacy_opt_in_as_hooks(self):
        from tools.registry import registry

        for ctx in (self.unsafe, self.safe):
            name = "incognito_probe_" + ctx.plugin_id
            callback = mock.Mock(return_value='{"ok": true}')
            handle = ctx.register_tool(
                name=name, toolset="incognito-probe",
                schema={"name": name, "parameters": {"type": "object"}},
                handler=callback,
            )
            assert handle is not None
            self.addCleanup(handle.dispose)
            with incognito_scope():
                result = registry.dispatch(name, {"content": CANARY})
            if ctx is self.safe:
                self.assertEqual(result, '{"ok": true}')
                callback.assert_called_once()
            else:
                assert isinstance(result, str)
                self.assertIn("incognito", result.lower())
                callback.assert_not_called()
            callback.reset_mock()
            self.assertEqual(registry.dispatch(name, {"content": "normal"}), '{"ok": true}')
            callback.assert_called_once()

    def test_registry_blocks_delayed_hooks_without_scope(self):
        callback = mock.Mock()
        self.unsafe.register_hook("on_session_finalize", callback)
        register_session(self.sid)
        self.assertEqual(self.manager.invoke_hook("on_session_finalize", session_id=self.sid, content=CANARY), [])
        callback.assert_not_called()

    def test_execution_middleware_bypass_still_calls_terminal_once(self):
        for ctx in (self.unsafe, self.safe):
            def callback(next_call, _name=ctx.plugin_id, **kwargs):
                self.seen.append((_name, kwargs["args"]["content"]))
                return next_call()
            ctx.register_middleware("tool_execution", callback)
        terminal = mock.Mock(return_value="tool result")
        register_session(self.sid)
        with mock.patch("hermes_cli.plugins._delivery_manager", return_value=self.manager):
            result = run_tool_execution_middleware("terminal", {"content": CANARY}, terminal, session_id=self.sid)
        self.assertEqual(result, "tool result")
        terminal.assert_called_once_with({"content": CANARY})
        self.assertEqual(self.seen, [("safe", CANARY)])

    def test_event_and_prompt_content_require_manifest_opt_in(self):
        for ctx in (self.unsafe, self.safe):
            def callback(_name=ctx.plugin_id, **kwargs):
                self.seen.append((_name, kwargs["content"]))
            ctx.subscribe("hermes:content", callback)
            def section(info, _name=ctx.plugin_id):
                self.seen.append((_name, info["content"]))
                return "section"
            ctx.register_system_prompt_section(ctx.plugin_id, section)
        with incognito_scope():
            self.assertEqual(self.manager._dispatch_event("hermes:content", {"content": CANARY}), 1)
            self.assertTrue(self.manager._wait_for_event_dispatch())
            sections = self.manager.render_system_prompt_sections({"content": CANARY})
        self.assertEqual([section.plugin for section in sections], ["safe"])
        self.assertEqual(self.seen, [("safe", CANARY)] * 2)

    def test_queued_event_rechecks_registry_before_delivering(self):
        started, release = threading.Event(), threading.Event()
        def hold(**kwargs):
            started.set()
            release.wait(5)
        self.unsafe.subscribe("hermes:hold", hold)
        callback = mock.Mock()
        self.unsafe.subscribe("hermes:queued", callback)
        self.manager._dispatch_event("hermes:hold", {})
        self.assertTrue(started.wait(5))
        try:
            self.manager._dispatch_event("hermes:queued", {"session_id": self.sid, "content": CANARY})
            register_session(self.sid)
        finally:
            release.set()
        self.assertTrue(self.manager._wait_for_event_dispatch())
        callback.assert_not_called()

    def test_failed_privacy_lookup_never_dispatches_or_logs_payload(self):
        callback = mock.Mock()
        self.unsafe.register_hook("on_session_finalize", callback)
        with mock.patch("agent.incognito.is_incognito", side_effect=RuntimeError(CANARY)):
            with self.assertLogs("hermes_cli.plugins", level="WARNING") as logs:
                self.assertEqual(self.manager.invoke_hook("on_session_finalize", content=CANARY), [])
        callback.assert_not_called()
        self.assertNotIn(CANARY, "\n".join(logs.output))

    def test_only_literal_yaml_true_allows_incognito(self):
        for value, expected in [("true", True), ("false", False), ('"true"', False), ("1", False)]:
            with self.subTest(value=value):
                plugin = self.home / "manifest"
                plugin.mkdir(exist_ok=True)
                (plugin / "plugin.yaml").write_text("name: manifest\nincognito_safe: " + value + "\n")
                manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
                self.assertIsNotNone(manifest)
                self.assertEqual(manifest.incognito_safe, expected)

    def test_disposal_clears_privacy_ownership(self):
        callback = lambda **kwargs: None
        handle = self.unsafe.register_hook("on_session_finalize", callback)
        self.assertIn(id(callback), self.manager._incognito_callback_manifests)
        handle.dispose()
        self.assertNotIn(id(callback), self.manager._incognito_callback_manifests)


if __name__ == "__main__":
    unittest.main()
