"""Tests for local machine-wide throttling of heavy terminal commands."""

from __future__ import annotations

from tools.terminal_tool_heavy_slot import classify_heavy_command, wrap_heavy_command


def test_classifies_supported_test_and_ci_commands():
    cases = {
        "pytest -q tests": "pytest",
        "python -m pytest tests": "pytest",
        "cd repo && uv run pytest -q": "pytest",
        "vitest run": "vitest",
        "pnpm test": "test",
        "turbo run test --filter=app": "test",
        "CI_EXPECTED_SHA=abc ./bin/ci gate deadbeef": "ci-gate",
        "~/.hermes/plugins/git-guard/bin/ci-gate deadbeef": "ci-gate",
    }
    for command, expected in cases.items():
        assert classify_heavy_command(command) == expected


def test_does_not_classify_unrelated_commands():
    assert classify_heavy_command("git status") is None
    assert classify_heavy_command("printf '%s' pytest") is None
    assert classify_heavy_command("./bin/ci ready") is None
    assert classify_heavy_command("pnpm build") is None


def test_wraps_only_local_commands_and_preserves_nested_slot(monkeypatch):
    monkeypatch.setattr(
        "tools.terminal_tool_heavy_slot._heavy_slot_executable",
        lambda _environment: "/Users/brianle/.local/bin/heavy-slot",
    )
    wrapped = wrap_heavy_command("pytest -q tests", env_type="local")
    assert "heavy-slot" in wrapped
    assert "--label 'hermes-terminal pytest'" in wrapped
    assert "bash -c" in wrapped

    assert wrap_heavy_command("pytest -q tests", env_type="docker") == "pytest -q tests"
    assert wrap_heavy_command(
        "pytest -q tests", env_type="local", environment={"HEAVY_SLOT_HELD": "1"}
    ) == "pytest -q tests"
