import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import subagent_worktree as sw
from tools import delegate_tool_child_run as cr
from tools.delegate_tool_config import _get_worktree_isolation, _get_worktree_repo_root


def git(args, cwd, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, text=True,
                          capture_output=True, check=True)


def repo_at(root):
    root.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], root)
    git(["config", "user.email", "t@t"], root)
    git(["config", "user.name", "T"], root)
    (root / "tracked").write_text("x\n")
    git(["add", "tracked"], root)
    git(["commit", "-qm", "seed"], root)
    return root


class WorktreeIsolationModeTests(unittest.TestCase):
    def test_config_modes_and_invalid_string(self):
        with mock.patch("tools.delegate_tool._load_config", return_value={"worktree_isolation": "required"}):
            self.assertEqual(_get_worktree_isolation(), "required")
        with mock.patch("tools.delegate_tool._load_config", return_value={"worktree_isolation": "bogus"}):
            with self.assertRaises(ValueError):
                _get_worktree_isolation()

    def test_required_nonrepo_is_visible_and_does_not_run_child(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("tools.delegate_tool._load_config", return_value={"worktree_isolation": "required"}), \
                 mock.patch.object(sw, "local_backend_active", return_value=True), \
                 mock.patch("tools.terminal_tool.get_session_cwd", return_value=td), \
                 mock.patch("tools.delegate_tool._resolve_workspace_hint", return_value=td):
                with self.assertRaises(cr.WorktreeIsolationRequiredError):
                    cr._create_isolated_worktree(object(), "parent", "child")

    def test_home_cwd_does_not_suppress_workspace_hint(self):
        with tempfile.TemporaryDirectory() as td:
            repo = repo_at(Path(td) / "repo")
            with mock.patch("tools.delegate_tool._load_config", return_value={"worktree_isolation": True}), \
                 mock.patch.object(sw, "local_backend_active", return_value=True), \
                 mock.patch("tools.terminal_tool.get_session_cwd", return_value=os.path.expanduser("~")), \
                 mock.patch("tools.delegate_tool._resolve_workspace_hint", return_value=str(repo)):
                receipt = {}
                info = cr._create_isolated_worktree(object(), "parent", "hint", receipt)
                self.assertEqual(receipt["state"], "engaged")
                self.assertEqual(Path(receipt["repo_root"]).resolve(), repo.resolve())
                self.assertIsNotNone(info)
                sw.finalize_subagent_worktree(info)

    def test_git_routing_environment_cannot_redirect_probe(self):
        with tempfile.TemporaryDirectory() as td:
            repo = repo_at(Path(td) / "repo")
            other = repo_at(Path(td) / "other")
            env = os.environ.copy()
            env.update(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other))
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(Path(sw.resolve_repo_root(str(repo))).resolve(), repo.resolve())

    def test_info_exclude_is_used_not_gitignore(self):
        with tempfile.TemporaryDirectory() as td:
            repo = repo_at(Path(td) / "repo")
            info = sw.create_subagent_worktree(str(repo), "exclude")
            self.assertIsNotNone(info)
            self.assertFalse((repo / ".gitignore").exists())
            exclude = Path(git(["rev-parse", "--git-path", "info/exclude"], repo).stdout.strip())
            if not exclude.is_absolute():
                exclude = repo / exclude
            self.assertIn(".worktrees/", exclude.read_text().splitlines())
            sw.finalize_subagent_worktree(info)


if __name__ == "__main__":
    unittest.main()
