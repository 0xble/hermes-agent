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


def test_disabled_relay_mcp_server_is_not_reported_as_available():
    cfg = {"mcp_servers": {"relay": {"command": "relay", "enabled": False}}}
    job = {"enabled_toolsets": ["file", "relay"], "prompt": "Use Pick the Destination."}
    issues = job_tool_requirement_issues(job, cfg=cfg)
    assert len(issues) == 1 and "relay" in issues[0]


def test_no_mcp_does_not_get_readded_during_capability_check():
    cfg = {"mcp_servers": {"terminal": {"command": "terminal"}}}
    job = {"enabled_toolsets": ["file", "no_mcp"], "prompt": "Add with `loops finding add`."}
    issues = job_tool_requirement_issues(job, cfg=cfg)
    assert len(issues) == 1 and "terminal" in issues[0]


def test_legacy_terminal_alias_satisfies_shell_step():
    job = {"enabled_toolsets": ["terminal_tools"], "prompt": "Add with `loops finding add`."}
    assert job_tool_requirement_issues(job, cfg={}) == []


def test_disabled_bundle_preserves_core_terminal():
    job = {"enabled_toolsets": ["terminal"], "prompt": "Add with `loops finding add`."}
    assert job_tool_requirement_issues(job, cfg={"agent": {"disabled_toolsets": ["coding"]}}) == []
    assert job_tool_requirement_issues(job, cfg={"agent": {"disabled_toolsets": ["hermes-cli"]}}) == []


def test_explicit_canonical_mcp_relay_satisfies_relay_step_with_no_mcp():
    cfg = {"mcp_servers": {"relay": {"command": "relay"}}}
    job = {"enabled_toolsets": ["file", "mcp-relay", "no_mcp"], "prompt": "Use Pick the Destination."}
    assert job_tool_requirement_issues(job, cfg=cfg) == []


def test_explicit_other_mcp_server_does_not_merge_relay():
    cfg = {"mcp_servers": {**_RELAY_MCP_CFG["mcp_servers"], "github": {"command": "gh-mcp"}}}
    job = {"enabled_toolsets": ["file", "github"], "prompt": "If needed, use Pick the Destination."}
    assert job_tool_requirement_issues(job, cfg=cfg)


def test_relay_mcp_server_does_not_satisfy_shell_step():
    job = {"enabled_toolsets": ["file"], "prompt": "Add it with `loops finding add --job x`."}
    issues = job_tool_requirement_issues(job, cfg=_RELAY_MCP_CFG)
    assert len(issues) == 1 and "terminal access" in issues[0]


def test_disabled_terminal_toolset_is_not_reported_as_available():
    job = {"enabled_toolsets": ["terminal"], "prompt": "Add it with `loops finding add --job x`."}
    cfg = {"agent": {"disabled_toolsets": ["terminal"]}}
    assert job_tool_requirement_issues(job, cfg=cfg)


def test_disabled_composite_toolset_is_not_reported_as_available():
    job = {"enabled_toolsets": ["coding"], "prompt": "Add it with `loops finding add --job x`."}
    cfg = {"agent": {"disabled_toolsets": ["terminal"]}}
    assert job_tool_requirement_issues(job, cfg=cfg)


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


def test_cron_doctor_passes_live_config_to_tool_requirements(tmp_path, monkeypatch, capsys):
    from cron.jobs import create_job
    from hermes_cli.cron import cron_doctor
    import hermes_cli.config as config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_job(
        prompt="Add it with `loops finding add --job audit --text result`.",
        name="synthetic-disabled-terminal", schedule="every 1h",
        enabled_toolsets=["terminal"], deliver="local",
    )
    monkeypatch.setattr(config, "read_raw_config_readonly", lambda: {"agent": {"disabled_toolsets": ["terminal"]}})
    assert cron_doctor() == 1
    assert "terminal access" in capsys.readouterr().out


def test_cron_doctor_expands_environment_config(tmp_path, monkeypatch, capsys):
    from cron.jobs import create_job
    from hermes_cli.cron import cron_doctor
    import hermes_cli.config as config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_DISABLED_TS", "terminal")
    create_job(
        prompt="Add with `loops finding add`", name="env-disabled-terminal",
        schedule="every 1h", enabled_toolsets=["terminal"], deliver="local",
    )
    monkeypatch.setattr(config, "read_raw_config_readonly", lambda: {"agent": {"disabled_toolsets": ["${REVIEW_DISABLED_TS}"]}})
    assert cron_doctor() == 1
    assert "terminal access" in capsys.readouterr().out


def test_cron_doctor_handles_null_config_sections(tmp_path, monkeypatch, capsys):
    from cron.jobs import create_job
    from hermes_cli.cron import cron_doctor
    import hermes_cli.config as config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_job(
        prompt="Add with `loops finding add`", name="null-config-sections",
        schedule="every 1h", enabled_toolsets=["file"], deliver="local",
    )
    monkeypatch.setattr(config, "read_raw_config_readonly", lambda: {"cron": None, "agent": None})
    assert cron_doctor() == 1
    assert "terminal access" in capsys.readouterr().out


def test_cron_doctor_config_read_does_not_create_config_backups(tmp_path, monkeypatch, capsys):
    from cron.jobs import create_job
    from hermes_cli.cron import cron_doctor

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("agent:\n  disabled_toolsets: [terminal]\n")
    create_job(
        prompt="Add it with `loops finding add`.", name="active-disabled-terminal",
        schedule="every 1h", enabled_toolsets=["terminal"], deliver="local",
    )
    assert not (tmp_path / "config").exists()
    assert cron_doctor() == 1
    assert "terminal access" in capsys.readouterr().out
    assert not (tmp_path / "config").exists()


def test_attached_non_utf8_skill_does_not_abort_diagnostics(tmp_path, monkeypatch):
    from cron import tool_requirements

    skill = tmp_path / "SKILL.md"
    skill.write_bytes(b"---\nname: bad\n---\n\xff")
    monkeypatch.setattr(tool_requirements, "_plugin_skill_path", lambda _name: skill)
    assert tool_requirements._attached_skill_content("plugin:bad") == ""


def test_capability_diagnostics_do_not_discover_plugins(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "discover_plugins", lambda: (_ for _ in ()).throw(AssertionError("discovery")))
    job = {"enabled_toolsets": ["terminal"], "prompt": "Add with `loops finding add`."}
    assert job_tool_requirement_issues(job, cfg={}) == []


def test_absolute_attached_skill_identifier_is_normalized(tmp_path, monkeypatch):
    from cron import tool_requirements

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill_dir = tmp_path / "skills" / "audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Run `loops finding add --text result`.\n")
    job = {"enabled_toolsets": ["file"], "skills": [str(skill_dir)]}
    issues = tool_requirements.job_tool_requirement_issues(job, cfg={})
    assert len(issues) == 1 and "terminal" in issues[0]


def test_attached_bundle_members_and_instruction_are_scanned(tmp_path, monkeypatch):
    from agent.skill_utils import TIER_LOCAL
    from tools import skills_tool

    skills = tmp_path / "skills"
    (skills / "audit").mkdir(parents=True)
    (skills / "audit" / "SKILL.md").write_text("---\nname: audit\n---\nRun `loops finding add --text x`.\n")
    (skills / "daily").mkdir()
    (skills / "daily" / "SKILL.md").write_text("---\nname: daily\n---\nRead files only.\n")
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    (bundles / "daily.yaml").write_text("name: daily\nskills: [audit]\ninstruction: Use Pick the Destination.\n")
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(bundles))
    monkeypatch.setattr(skills_tool, "_skill_search_dirs", lambda: ([(TIER_LOCAL, skills)], skills))
    issues = job_tool_requirement_issues({"enabled_toolsets": ["file"], "skills": ["daily"]}, cfg={})
    assert len(issues) == 2
    assert "terminal access" in issues[0] and "relay" in issues[1]


def test_relay_mcp_alias_not_registered_by_runtime_is_not_accepted():
    cfg = {"mcp_servers": {"relay": {"command": "relay"}}}
    job = {"enabled_toolsets": ["file", "relay-mcp", "no_mcp"], "prompt": "Use Pick the Destination."}
    assert job_tool_requirement_issues(job, cfg=cfg)


def test_missing_bundle_members_do_not_activate_bundle_instruction(tmp_path, monkeypatch):
    from agent.skill_utils import TIER_LOCAL
    from tools import skills_tool

    skills = tmp_path / "skills"
    skills.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    (bundles / "daily.yaml").write_text("name: daily\nskills: [missing]\ninstruction: Use Pick the Destination.\n")
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(bundles))
    monkeypatch.setattr(skills_tool, "_skill_search_dirs", lambda: ([(TIER_LOCAL, skills)], skills))
    assert job_tool_requirement_issues({"enabled_toolsets": ["file"], "skills": ["daily"]}, cfg={}) == []


def test_project_skill_diagnosis_persists_nothing_under_home(tmp_path, monkeypatch):
    import agent.skill_utils as su

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    skill = repo / ".hermes" / "skills" / "audit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: audit\n---\nRun `loops finding add --text x`.\n")
    (home / "config.yaml").write_text(f"skills:\n  external_dirs: []\n  trusted_project_dirs: ['{repo}']\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(repo)
    su._external_dirs_cache_clear()
    su._PROJECT_QUARANTINE_CACHE.clear()
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    try:
        issues = job_tool_requirement_issues({"enabled_toolsets": ["file"], "skills": ["audit"]}, cfg={})
    finally:
        su._external_dirs_cache_clear()
    assert len(issues) == 1 and "terminal" in issues[0]
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before


def test_execute_code_without_terminal_call_needs_no_shell():
    job = {"enabled_toolsets": ["file", "code_execution"],
           "prompt": "Use execute_code(...) to batch read_file calls."}
    assert job_tool_requirement_issues(job, cfg={}) == []
