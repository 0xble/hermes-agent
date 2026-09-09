from unittest.mock import MagicMock, patch




def test_format_banner_version_label_on_upstream_main():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={"upstream": "b2f477a3", "local": "b2f477a3", "ahead": 0},
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· upstream b2f477a3")
    assert "local" not in value


def test_get_git_banner_state_reads_origin_and_head(tmp_path):
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="3\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {"upstream": "b2f477a3", "local": "af8aad31", "ahead": 3}


# The four `_check_via_local_git` SSH-fast-path tests that lived here were removed with the
# routing they asserted. `CLI updater: use maintained fork only (#133)` repointed the updater at
# git@github.com:0xble/hermes-agent.git and deleted the fast-path branch from
# `_check_via_local_git`: that branch keyed on `_is_official_ssh_remote` (the NousResearch remote),
# which can never match once the updater targets the fork. The prompt-stealing hazard the fast path
# existed for (#104591 — a host-key prompt opening /dev/tty and taking the CLI's keystrokes) is now
# handled instead by forcing `GIT_SSH_COMMAND=ssh -o BatchMode=yes` in the same function.
#
# Note for follow-up: `_is_official_ssh_remote` has no remaining non-test call sites and is dead
# code. `_tips_behind` and `_upstream_main_sha` are still used and keep their coverage elsewhere.
