import importlib.util
import json
from pathlib import Path


def load_plugin():
    path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("review_candidate_test_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_invalid_candidate_is_not_reviewed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin()
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": "", "head_sha": ""}, task_id="parent"))
    assert result["status"] == "not_reviewed"
    assert result["error_code"] == "invalid_candidate"


def test_child_cannot_review(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin()
    from agent.delegation_context import delegated_child_context
    with delegated_child_context("child"):
        result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": "a", "head_sha": "b"}, task_id="child"))
    assert result["error_code"] == "parent_only"


def test_same_commit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin()
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "file.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": sha, "head_sha": sha}, task_id="parent"))
    assert result["error_code"] == "empty_candidate"

