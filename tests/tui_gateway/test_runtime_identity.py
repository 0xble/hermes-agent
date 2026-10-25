"""Runtime identity contract for TUI gateway machine protocol fields."""

from hermes_cli.version_info import VersionInfo

import hermes_cli
from tui_gateway import server


def test_session_info_advertises_base_version_and_release_date(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.version_info._cached_version_info",
        VersionInfo("1.2.3", "1.2.3+4.gabcdef0", 4, "abcdef0", "main", "git"),
    )
    monkeypatch.setattr(hermes_cli, "__release_date__", "2026.9.23")

    info = server._session_info(None, {})

    assert info["version"] == "1.2.3"
    assert info["release_date"] == "2026.9.23"
