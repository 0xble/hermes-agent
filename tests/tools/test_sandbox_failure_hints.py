"""Tests for execute_code sandbox failure hints."""

import json


from tools.code_execution_tool import _sandbox_failure_hint, execute_code


class TestSandboxFailureHint:
    def test_unavailable_tool_import_lists_available(self):
        err = ("Traceback (most recent call last):\n  File \"script.py\", line 1\n"
               "ImportError: cannot import name 'browser_navigate' from 'hermes_tools'")
        h = _sandbox_failure_hint(err, enabled_tools={"terminal", "read_file"})
        assert "browser_navigate" in h
        assert "read_file" in h and "terminal" in h




    def test_unknown_failure_no_hint(self):
        assert _sandbox_failure_hint("ZeroDivisionError: division by zero") is None

    def test_empty_stderr_no_hint(self):
        assert _sandbox_failure_hint("") is None

    def test_json_decode_failure_explains_structured_tool_results(self):
        hint = _sandbox_failure_hint(
            "json.decoder.JSONDecodeError: Extra data: line 1 column 2 (char 1)"
        )
        assert hint is not None
        assert "structured dict" in hint
        assert "json.loads() the entire tool output" in hint


class TestLiveSandboxHint:
    def test_bad_import_produces_hint_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = json.loads(execute_code(
            "from hermes_tools import totally_fake_tool\nprint('unreachable')",
            task_id="t-sbhint",
        ))
        assert r["status"] == "error"
        assert "hint" in r
        assert "totally_fake_tool" in r["hint"]


    def test_successful_script_has_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = json.loads(execute_code("print('fine')", task_id="t-sbhint"))
        assert r["status"] == "success"
        assert "hint" not in r
