"""Regression tests for cron prompt/toolset capability diagnostics."""

import pytest

from cron.tool_requirements import job_tool_requirement_issues


def test_cron_doctor_flags_file_only_finding_job(tmp_path, monkeypatch, capsys):
    from cron.jobs import create_job, get_job
    from hermes_cli.cron import cron_doctor

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = create_job(
        prompt="Daily Brief finding: Run `loops finding add --job audit --text result`.",
        name="synthetic-file-only-finding", schedule="every 1h", enabled_toolsets=["file"],
        deliver="local",
    )
    before = get_job(job["id"])
    assert cron_doctor() == 1
    output = capsys.readouterr().out
    assert "synthetic-file-only-finding" in output
    assert "enabled_toolsets" in output and "terminal" in output
    assert get_job(job["id"]) == before


@pytest.mark.parametrize("tools", [["terminal"], ["coding"], ["all"], ["*"]])
def test_terminal_bundles_satisfy_shell_and_relay_cli_steps(tools):
    assert job_tool_requirement_issues({
        "enabled_toolsets": tools,
        "prompt": "Run `loops finding add --text result`; use Pick the Destination.",
    }) == []


@pytest.mark.parametrize("prompt", [
    "Run `custom-check --json`.", "```bash\ncustom-check --json\n```",
    "Use terminal(command='custom-check').",
])
def test_other_explicit_shell_steps_are_flagged(prompt):
    assert job_tool_requirement_issues({"enabled_toolsets": ["file"], "prompt": prompt})


def test_script_only_job_does_not_need_agent_tools():
    assert job_tool_requirement_issues({
        "no_agent": True, "enabled_toolsets": ["file"],
        "prompt": "Run `loops finding add`; use Pick the Destination.",
    }) == []


def test_attached_skill_is_read_without_running_preprocessing(tmp_path, monkeypatch):
    from agent.skill_utils import TIER_LOCAL
    from tools import skills_tool
    skill = tmp_path / "skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: skill\ndeps: [never-install-me]\n---\nRun `custom-check --json`.\n")
    monkeypatch.setattr(skills_tool, "_skill_search_dirs", lambda: ([(TIER_LOCAL, tmp_path)], tmp_path))
    issues = job_tool_requirement_issues({"enabled_toolsets": ["file"], "skills": ["skill"]})
    assert len(issues) == 1 and "terminal" in issues[0]


def test_file_only_job_with_daily_brief_finding_is_flagged():
    issues = job_tool_requirement_issues({
        "enabled_toolsets": ["file"],
        "prompt": "Report the result. Add it with `loops finding add --job NAME`. ",
    })
    assert len(issues) == 1
    assert "finding add" in issues[0]
    assert "terminal" in issues[0]


def test_file_only_job_with_pick_destination_is_flagged():
    issues = job_tool_requirement_issues({
        "enabled_toolsets": ["file"],
        "prompt": "If needed, use the relay skill's Pick the Destination route.",
    })
    assert len(issues) == 1
    assert "Pick the Destination" in issues[0]
    assert "relay" in issues[0]


def test_terminal_and_relay_allowlist_is_healthy():
    assert job_tool_requirement_issues({
        "enabled_toolsets": ["file", "terminal", "relay"],
        "prompt": "Add a finding with `loops finding add`; use Pick the Destination if needed.",
    }) == []


def test_unrestricted_job_is_not_flagged():
    assert job_tool_requirement_issues({
        "prompt": "Add a finding with `loops finding add`; use Pick the Destination if needed.",
    }) == []


def test_attached_skill_text_is_checked():
    issues = job_tool_requirement_issues(
        {"enabled_toolsets": ["file"], "skills": ["relay"]},
        skill_loader=lambda _name: "Use Pick the Destination when no owner is live.",
    )
    assert any("relay" in issue for issue in issues)


_RELAY_MCP_CFG = {"mcp_servers": {"relay": {"command": "relay", "args": ["mcp"]}}}


def test_enabled_relay_mcp_server_satisfies_relay_step_like_the_scheduler():
    from cron.scheduler import _resolve_cron_enabled_toolsets
    job = {"enabled_toolsets": ["file"], "prompt": "If needed, use Pick the Destination."}
    assert "relay" in _resolve_cron_enabled_toolsets(job, _RELAY_MCP_CFG)
    assert job_tool_requirement_issues(job, cfg=_RELAY_MCP_CFG) == []


def test_no_mcp_sentinel_keeps_relay_step_flagged():
    job = {"enabled_toolsets": ["file", "no_mcp"], "prompt": "If needed, use Pick the Destination."}
    issues = job_tool_requirement_issues(job, cfg=_RELAY_MCP_CFG)
    assert len(issues) == 1 and "relay" in issues[0]


def test_explicit_other_mcp_server_does_not_merge_relay():
    cfg = {"mcp_servers": {**_RELAY_MCP_CFG["mcp_servers"], "github": {"command": "gh-mcp"}}}
    job = {"enabled_toolsets": ["file", "github"], "prompt": "If needed, use Pick the Destination."}
    assert job_tool_requirement_issues(job, cfg=cfg)


def test_relay_mcp_server_does_not_satisfy_shell_step():
    job = {"enabled_toolsets": ["file"], "prompt": "Add it with `loops finding add --job x`."}
    issues = job_tool_requirement_issues(job, cfg=_RELAY_MCP_CFG)
    assert len(issues) == 1 and "terminal access" in issues[0]


def test_registered_plugin_skill_is_read_from_its_plugin_path(tmp_path, monkeypatch):
    from cron import tool_requirements
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: ship\n---\nRun `custom-check --json`.\n")

    class _PM:
        def find_plugin_skill(self, name):
            return skill if name == "acme:ship" else None

    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **k: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: _PM())
    assert "custom-check" in tool_requirements._attached_skill_content("acme:ship")
    issues = job_tool_requirement_issues({"enabled_toolsets": ["file"], "skills": ["acme:ship"]}, cfg={})
    assert len(issues) == 1 and "terminal" in issues[0]
