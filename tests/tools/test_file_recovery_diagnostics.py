"""Recovery contracts at the real file-tool dispatch boundary."""

import json

import pytest

from tools import file_tools
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.registry import registry


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    env = LocalEnvironment(cwd=str(tmp_path))
    ops = ShellFileOperations(env, cwd=str(tmp_path))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    monkeypatch.setattr(file_tools, "_resolve_path_for_task", lambda path, task_id: tmp_path / path)

    def call(name, **args):
        return json.loads(registry.dispatch(name, args, task_id=str(tmp_path)))

    call.ops = ops
    return call


@pytest.mark.parametrize("native", ["0", "1"])
def test_invalid_regex_retries_literal_once_without_broadening_search(dispatch, tmp_path, monkeypatch, native):
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    if not dispatch.ops._has_command("rg"):
        pytest.skip("requires ripgrep")
    (tmp_path / "data.txt").write_text("call(\nordinary\ncall( again\n")
    (tmp_path / "ignored.py").write_text("call(\n")
    calls = []
    run = dispatch.ops._run_search_pipeline

    def recording(parts, *args, **kwargs):
        calls.append(parts)
        return run(parts, *args, **kwargs)

    monkeypatch.setattr(dispatch.ops, "_run_search_pipeline", recording)
    result = dispatch("search_files", pattern="call(", path=".", file_glob="*.txt", limit=10)
    assert result.get("error") is None
    assert [item["line"] for item in result["matches"]] == [1, 3]
    assert "fixed-string" in result["warning"]
    assert len(calls) == 2 and "--fixed-strings" in calls[1]
    assert "--glob" in calls[0] and "--glob" in calls[1]
    # A valid regex remains a regex; no retry and no recovery warning.
    calls.clear()
    valid = dispatch("search_files", pattern="call.*", path=".", file_glob="*.txt", limit=10)
    assert valid["total_count"] == 2 and len(calls) == 1
    assert "warning" not in valid
    # Count and files-only modes must also disclose the interpretation change.
    for mode in ("count", "files_only"):
        recovered = dispatch("search_files", pattern="call(", path=".", file_glob="*.txt", output_mode=mode)
        assert recovered["total_count"] == (2 if mode == "count" else 1)
        assert "fixed-string" in recovered["warning"]
    from tools.file_operations import SearchResult

    calls.clear()

    def denied(*args, **kwargs):
        calls.append(args)
        return SearchResult(error="Search failed: regex parse error.txt: Permission denied")

    monkeypatch.setattr(dispatch.ops, "_run_search_pipeline", denied)
    denied_result = dispatch("search_files", pattern="call(", path=".")
    assert "Permission denied" in denied_result["error"] and len(calls) == 1


def test_literal_retry_warning_survives_protected_folder_and_multiline_notes(dispatch, tmp_path, monkeypatch):
    if not dispatch.ops._has_command("rg"):
        pytest.skip("requires ripgrep")
    (tmp_path / "data.txt").write_text("call(\n")
    monkeypatch.setattr(dispatch.ops, "_macos_search_exclusions", lambda path: ["Desktop"])
    broad = dispatch("search_files", pattern="call(", path=".")
    assert "fixed-string" in broad["warning"] and "Skipped macOS protected folders" in broad["warning"]
    assert broad["warning"].index("fixed-string") < broad["warning"].index("Skipped macOS")
    # A malformed multiline pattern keeps the -U note and appends the retry note.
    monkeypatch.setattr(dispatch.ops, "_macos_search_exclusions", lambda path: [])
    multi = dispatch("search_files", pattern="call(\\nx", path=".")
    assert multi.get("error") is None, multi
    assert multi["warning"].index("multiline") < multi["warning"].index("fixed-string"), multi


def test_missing_paths_report_resolution_and_stable_suggestions(dispatch, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AGENTS.md").write_text("guide\n")
    missing = dispatch("read_file", path="docs/AGENT.md")
    assert missing["not_found"] is True
    assert missing["resolved_path"] == str(tmp_path / "docs" / "AGENT.md")
    assert missing["similar_files"] == [str(tmp_path / "docs" / "AGENTS.md")]
    assert "Do not retry the same path" in missing["hint"]
    empty = dispatch("read_file", path="docs/unrelated.zzz")
    assert empty["similar_files"] == []
    search = dispatch("search_files", pattern="guide", path="docs/absent/deeper")
    assert "Path not found" in search["error"]
    assert search["resolved_cwd"] == str(tmp_path)
    assert search["nearest_existing_parent"] == str(tmp_path / "docs")
    assert search["resolved_path"] == str(tmp_path / "docs" / "absent" / "deeper")


def test_ambiguous_patch_reports_every_location_and_never_writes(dispatch, tmp_path):
    path = tmp_path / "data.txt"
    content = "anchor\n" * 7
    path.write_text(content)
    result = dispatch("patch", path="data.txt", old_string="anchor", new_string="changed")
    assert result["success"] is False
    assert result["match_count"] == 7
    assert result["match_lines"] == list(range(1, 8))
    assert "widen" in result["error"].lower() and "replace_all" in result["error"]
    assert path.read_text() == content
    # Fuzzy (non-exact) matches must describe the actual matched locations too.
    spaced = "context\n  anchor\nother\n  anchor\n"
    path.write_text(spaced)
    fuzzy = dispatch("patch", path="data.txt", old_string="\tanchor", new_string="changed")
    assert fuzzy.get("match_count") == 2 and fuzzy.get("match_lines") == [2, 4], fuzzy
    assert path.read_text() == spaced
    many = "anchor\n" * 25
    path.write_text(many)
    capped = dispatch("patch", path="data.txt", old_string="anchor", new_string="changed")
    assert capped["match_count"] == 25
    assert capped["match_lines"] == list(range(1, 21)) and capped["match_lines_truncated"] is True
    assert "match_lines_truncated" not in result
    path.write_text(content)
    applied = dispatch("patch", path="data.txt", old_string="anchor", new_string="changed", replace_all=True)
    assert applied["success"] is True
    assert path.read_text() == "changed\n" * 7


def test_missing_patch_path_names_the_parameter_before_access(dispatch, monkeypatch):
    def no_access(*args, **kwargs):
        raise AssertionError("invalid request must not acquire a file backend")

    monkeypatch.setattr(file_tools, "_get_file_ops", no_access)
    result = dispatch("patch", old_string="before", new_string="after")
    assert result["missing_parameter"] == "path"
    assert "path" in result["error"] and "parameter" in result["error"]
