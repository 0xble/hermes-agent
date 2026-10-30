"""Cache path hints must not advertise Docker mounts disabled by the active profile."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import credential_files
from tools.terminal_scope import reset_terminal_scope, set_terminal_scope


@pytest.mark.parametrize("backend,policy,ambient", [
    ("docker", "false", "true"),
    ("docker", "true", "false"),
    ("docker", None, "false"),
    ("ssh", "false", "false"),
    ("modal", "false", "false"),
])
def test_cache_mapping_obeys_active_policy_without_creating_unmounted_dirs(
    monkeypatch, tmp_path, backend, policy, ambient,
):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE", ambient)
    scope = {"TERMINAL_ENV": backend}
    if policy is not None:
        scope["TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE"] = policy
    token = set_terminal_scope(scope)
    host_path = str(home / "cache/images/generated.png")
    container_path = "/root/.hermes/cache/images/generated.png"
    try:
        mapped = credential_files.map_cache_path_to_container(host_path)
        if backend == "docker" and policy == "false":
            assert mapped is None
            assert credential_files.to_agent_visible_cache_path(host_path) == host_path
            assert credential_files.to_agent_visible_cache_path(host_path, fallback_to_host=False) is None
            assert credential_files.from_agent_visible_cache_path(container_path) == container_path
            assert list(home.iterdir()) == []
        else:
            assert mapped == container_path
            base = "~/.hermes" if backend == "ssh" else "/root/.hermes"
            assert credential_files.to_agent_visible_cache_path(host_path, fallback_to_host=False) == (
                f"{base}/cache/images/generated.png"
            )
    finally:
        reset_terminal_scope(token)


def test_optout_callers_omit_unreadable_hints(monkeypatch, tmp_path):
    from tools import browser_tool_snapshot, delegate_tool_results, image_generation_tool, web_tools_truncate

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE", "false")
    monkeypatch.setattr(image_generation_tool, "_active_terminal_env", lambda task_id: None)
    collector = Mock(side_effect=AssertionError("Opt-out must not collect or create mount roots"))
    monkeypatch.setattr(credential_files, "get_cache_directory_mounts", collector)
    body = "\n".join(f"row {i}" for i in range(5000))

    web, truncated = web_tools_truncate._truncate_with_footer(body, "https://example.com/doc", 3000)
    snapshot = browser_tool_snapshot._truncate_snapshot(body, 3000)
    summary, spill = delegate_tool_results._trim_summary_with_footer(body, 3000, 0)
    assert truncated
    assert spill is None
    for text in (web, snapshot, summary):
        assert "row 0" in text
        assert "read_file path=" not in text
        assert "/root/.hermes" not in text
        assert str(home) not in text
    assert "re-run web_extract" in web
    assert "use browser_snapshot" in snapshot
    assert "head+tail above" in summary

    image_path = home / "cache/images/generated.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    raw = json.dumps({"success": True, "image": str(image_path)})
    assert image_generation_tool._postprocess_image_generate_result(raw) == raw
    collector.assert_not_called()
    # Only the actual host artifacts may create dirs, never the mapping helper.
    assert {str(p.relative_to(home)) for p in home.rglob("*") if p.is_dir()} == {
        "cache", "cache/web", "cache/delegation", "cache/images",
    }
    assert all(Path(p).is_file() for p in (image_path, *home.glob("cache/web/*")))
