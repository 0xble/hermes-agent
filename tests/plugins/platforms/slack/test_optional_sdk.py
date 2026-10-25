"""Slack discovery must load before its optional SDK is installed."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("sdk_state", ["unavailable", "mocked"])
def test_adapter_loads_before_optional_sdk_is_complete(sdk_state):
    # A fresh interpreter prevents pytest's SDK imports from hiding the boundary.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import importlib.abc
            import sys
            from unittest.mock import MagicMock, patch

            class NoSlack(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split(".")[0] in {"slack_sdk", "slack_bolt"}:
                        raise ModuleNotFoundError(fullname)

            def mock_sdk():
                sys.modules["aiohttp"] = MagicMock()
                bolt = MagicMock()
                bolt.async_app.AsyncApp = MagicMock
                bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
                sdk = MagicMock()
                sdk.web.async_client.AsyncWebClient = MagicMock
                for name, module in [
                    ("slack_bolt", bolt),
                    ("slack_bolt.async_app", bolt.async_app),
                    ("slack_bolt.adapter", bolt.adapter),
                    ("slack_bolt.adapter.socket_mode", bolt.adapter.socket_mode),
                    ("slack_bolt.adapter.socket_mode.async_handler", bolt.adapter.socket_mode.async_handler),
                    ("slack_sdk", sdk),
                    ("slack_sdk.web", sdk.web),
                    ("slack_sdk.web.async_client", sdk.web.async_client),
                ]:
                    sys.modules[name] = module

            unavailable = sys.argv[1] == "unavailable"
            blocker = NoSlack()
            if unavailable:
                sys.meta_path.insert(0, blocker)
            else:
                mock_sdk()

            with patch("pm.extras.ensure_import") as ensure_import:
                import plugins.platforms.slack.adapter as adapter
                assert adapter.SlackAdapter.__name__ == "SlackAdapter"
                assert adapter.slack_deps_present() == (not unavailable)
                ensure_import.assert_not_called()
                if unavailable:
                    # Make the SDK available after discovery, as lazy setup does.
                    sys.meta_path.remove(blocker)
                    mock_sdk()
                    assert adapter.check_slack_requirements()
                    ensure_import.assert_called_once_with("slack")
                assert adapter.slack_deps_present()
                assert adapter.AsyncApp is MagicMock
                assert adapter.AsyncWebClient is MagicMock
                assert adapter.aiohttp is not None
            print("optional Slack SDK discovery and binding passed")
        """), sdk_state],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "optional Slack SDK discovery and binding passed" in result.stdout
