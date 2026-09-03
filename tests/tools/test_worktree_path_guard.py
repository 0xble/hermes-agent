import json
import shlex
import subprocess

from tools.terminal_tool import cleanup_all_environments, terminal_tool
from tools.worktree_path_guard import nonstandard_worktree_add_warning


def test_warns_for_absolute_external_destination():
    warning = nonstandard_worktree_add_warning(
        "git worktree add /private/tmp/review -b review origin/main",
        "/repo",
    )

    assert warning is not None
    assert "/private/tmp/review" in warning
    assert "<repo>/.worktrees/<name>" in warning


def test_warns_for_sibling_destination():
    warning = nonstandard_worktree_add_warning(
        "git worktree add ../repo-feature feature/review",
        "/src/repo",
    )

    assert warning is not None


def test_allows_repository_dot_worktrees_destination():
    assert nonstandard_worktree_add_warning(
        "git worktree add .worktrees/review -b review origin/main",
        "/repo",
    ) is None


def test_warns_when_dot_worktrees_path_traverses_back_out():
    assert nonstandard_worktree_add_warning(
        "git worktree add .worktrees/../external HEAD",
        "/repo",
    ) is not None


def test_allows_nested_dot_worktrees_destination_with_git_dash_c():
    assert nonstandard_worktree_add_warning(
        "git -C /repo worktree add --detach .worktrees/review origin/main",
        "/tmp",
    ) is None


def test_warns_for_git_dash_c_external_destination():
    warning = nonstandard_worktree_add_warning(
        "git -C /repo worktree add ../review origin/main",
        "/tmp",
    )

    assert warning is not None


def test_handles_options_that_consume_values():
    warning = nonstandard_worktree_add_warning(
        "git worktree add --lock --reason 'manual review' -B review /tmp/review HEAD",
        "/repo",
    )

    assert warning is not None
    assert "/tmp/review" in warning


def test_warns_for_dynamic_destination_that_is_not_explicitly_standard():
    warning = nonstandard_worktree_add_warning(
        "git worktree add \"$TMPDIR/review\" review",
        "/repo",
    )

    assert warning is not None


def test_does_not_warn_for_worktree_remove_or_move():
    assert nonstandard_worktree_add_warning(
        "git worktree remove /tmp/review && git worktree move old new",
        "/repo",
    ) is None


def test_does_not_treat_documentation_or_heredoc_body_as_execution():
    assert nonstandard_worktree_add_warning(
        "printf '%s\\n' 'git worktree add /tmp/review'",
        "/repo",
    ) is None
    assert nonstandard_worktree_add_warning(
        "python3 - <<'PY'\nprint('git worktree add /tmp/review')\nPY",
        "/repo",
    ) is None


def test_finds_worktree_add_after_shell_separator():
    warning = nonstandard_worktree_add_warning(
        "printf ready && git worktree add /tmp/review review",
        "/repo",
    )

    assert warning is not None


def test_finds_worktree_add_after_newline():
    warning = nonstandard_worktree_add_warning(
        "printf ready\ngit worktree add /tmp/review review",
        "/repo",
    )

    assert warning is not None


def test_finds_worktree_add_after_compound_separator_runs():
    for command in (
        "printf ready\n\ngit worktree add /tmp/review review",
        "printf ready;\ngit worktree add /tmp/review review",
        "printf ready &&\ngit worktree add /tmp/review review",
    ):
        assert nonstandard_worktree_add_warning(command, "/repo") is not None


def test_finds_worktree_add_through_env_and_sudo_wrappers():
    assert nonstandard_worktree_add_warning(
        "env FOO=bar git worktree add /tmp/review review",
        "/repo",
    ) is not None
    assert nonstandard_worktree_add_warning(
        "sudo -n git worktree add /tmp/review review",
        "/repo",
    ) is not None


def test_terminal_result_surfaces_warning_after_successful_add(tmp_path):
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Hermes Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "hermes-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    try:
        result = json.loads(
            terminal_tool(
                command=(
                    "git worktree add -b test/external "
                    f"{shlex.quote(str(external))} HEAD"
                ),
                task_id="test-worktree-path-warning",
                workdir=str(repo),
            )
        )
        assert result["exit_code"] == 0
        assert "warning" in result
        assert str(external) in result["warning"]
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(external)],
            cwd=repo,
            check=False,
        )
        cleanup_all_environments()
