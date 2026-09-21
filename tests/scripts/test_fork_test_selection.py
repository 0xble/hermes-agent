"""Fork smoke selection fails open rather than hiding unclassified coverage."""
import json

import pytest

from scripts.ci.run_fork_tests import MANIFEST, ROOT, select_tests


@pytest.mark.parametrize("changed", [[], ["run_agent.py"], ["tests/conftest.py"], ["pyproject.toml"], [".github/workflows/tests.yml"], ["tests/deleted_test.py"]])
def test_unclassified_or_shared_changes_run_complete_suite(changed):
    assert select_tests(changed, {"unit": ["tests/test_utils_truthy_values.py"]}) is None


def test_changed_test_is_added_to_all_maintained_surfaces():
    surfaces = json.loads(MANIFEST.read_text(encoding="utf-8"))
    units = {path.stem for path in (ROOT / "maintenance").glob("*.md")}
    # Runtime ownership is an activation contract, not a source proof unit.
    assert set(surfaces) == units - {"runtime-ownership"}
    assert all(tests for tests in surfaces.values())
    assert all((ROOT / path).is_file() for tests in surfaces.values() for path in tests)
    changed = "tests/test_utils_truthy_values.py"
    selected = select_tests([changed, "MAINTENANCE.md"], surfaces)
    assert set(selected) == {changed} | {path for tests in surfaces.values() for path in tests}


@pytest.mark.parametrize("event_kind", ["pull_request", "push", "manual_smoke", "missing_context"])
def test_workflow_diff_respects_branch_history(tmp_path, monkeypatch, event_kind):
    import subprocess
    from scripts.ci import run_fork_tests

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, text=True, capture_output=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "CI fixture")
    # Test-only branch diverges before main gets an unrelated core change.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_maintained.py").write_text("", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    common = git("rev-parse", "HEAD")
    git("switch", "-c", "feature")
    (tmp_path / "tests/test_added.py").write_text("", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "test change")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    (tmp_path / "run_agent.py").write_text("", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "unrelated main change")
    base = git("rev-parse", "HEAD")
    git("switch", "feature")
    manifest = tmp_path / "surfaces.json"
    manifest.write_text(json.dumps({"unit": ["tests/test_maintained.py"]}), encoding="utf-8")
    event = (
        {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
        if event_kind == "pull_request" else {"before": common}
    )
    if event_kind == "manual_smoke":
        event = {"inputs": {"full_python": "false"}}
    elif event_kind == "missing_context":
        event = {}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setattr(run_fork_tests, "ROOT", tmp_path)
    monkeypatch.setattr(run_fork_tests, "MANIFEST", manifest)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch" if event_kind == "manual_smoke" else event_kind)
    monkeypatch.setenv("GITHUB_SHA", base)
    commands = []
    monkeypatch.setattr(run_fork_tests.subprocess, "call", lambda command, **kw: commands.append(command) or 0)

    assert run_fork_tests.main() == 0
    expected = ["tests/test_added.py", "tests/test_maintained.py"] if event_kind == "pull_request" else []
    if event_kind == "manual_smoke":
        expected = ["tests/test_maintained.py"]
    assert commands == [["bash", "scripts/run_tests.sh", *expected]]
