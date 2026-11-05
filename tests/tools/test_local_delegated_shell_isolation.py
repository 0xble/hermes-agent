"""Delegations share a host filesystem, never a local shell's export snapshot.

Exercise the same alias registration used by _ChildRun.seed_workspace, including
lifecycle/review children, without a model call or any live Hermes home.
"""

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import terminal_tool as tt
from tools.terminal_tool_lifecycle import cleanup_vm, get_active_env


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("surface", ["gateway", "cli", "cron"])
def test_child_exports_cannot_reach_parent_or_siblings(monkeypatch, tmp_path, surface):
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("TERMINAL_LOCAL_PERSISTENT", "true")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SHELL_OWNER", raising=False)
    tokens = set_session_vars(session_key="shell-parent" if surface == "gateway" else "")
    parent = "parent-task"
    if surface == "cron":
        tt.register_run_scoped_task(parent)
    from tools.delegate_tool_child_run import _ChildRun

    monkeypatch.setattr("tools.delegate_tool_child_run._create_isolated_worktree", lambda *a: None)
    seeded = []
    for index, owner in enumerate((parent, parent, None)):
        if owner is None:
            owner = seeded[0].child_task_id
        run_state = _ChildRun(SimpleNamespace(), SimpleNamespace(_current_task_id=owner),
                              index, "review candidate", None, None)
        run_state.seed_workspace()
        seeded.append(run_state)
    children = tuple(state.child_task_id for state in seeded)

    def run(task, command):
        result = json.loads(tt.terminal_tool(command, task_id=task, force=True))
        assert result["exit_code"] == 0, result
        return result["output"].strip()

    probe = 'printf "%s|%s" "${HERMES_SHELL_OWNER:-unset}" "$HERMES_HOME"'
    parent_home = str(tmp_path / "parent-home")
    child_home = str(tmp_path / "review-home")
    try:
        run(parent, f"export HERMES_SHELL_OWNER=parent HERMES_HOME={shlex.quote(parent_home)}")
        child_initial = run(children[0], 'printf "%s" "${HERMES_SHELL_OWNER:-unset}"')
        run(children[0], f"export HERMES_SHELL_OWNER=reviewer HERMES_HOME={shlex.quote(child_home)}")
        assert run(children[0], probe) == f"reviewer|{child_home}"
        assert run(parent, probe) == f"parent|{parent_home}"
        assert child_initial == "unset"
        for child in children[1:]:
            assert run(child, 'printf "%s" "${HERMES_SHELL_OWNER:-unset}"') == "unset"
        environments = [get_active_env(task) for task in (parent, *children)]
        assert all(environments)
        snapshots = [env._snapshot_path for env in environments]
        assert len(set(snapshots)) == 4
        # Existing idle cleanup owns snapshots; do not close a child environment
        # early when a timed-out worker/adopted shell might still be using it.
        tt._last_activity[tt._resolve_container_task_id(children[0])] = 0
        tt._cleanup_inactive_envs(lifetime_seconds=60)
        assert not Path(snapshots[1]).exists()
        assert run(parent, probe) == f"parent|{parent_home}"
    finally:
        for task in (*children, parent):
            cleanup_vm(tt._resolve_container_task_id(task))
        for child in children:
            tt.clear_task_env_overrides(child)
        tt.clear_run_scoped_task(parent)
        clear_session_vars(tokens)


@pytest.mark.platforms("linux", "macos")
def test_routed_profile_children_keep_their_own_shells(monkeypatch, tmp_path):
    from tools.delegate_tool_routes import delegated_profile_scope

    home = tmp_path / "routed-profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda _h: None)
    with delegated_profile_scope(home):
        test_child_exports_cannot_reach_parent_or_siblings(monkeypatch, tmp_path, "gateway")
