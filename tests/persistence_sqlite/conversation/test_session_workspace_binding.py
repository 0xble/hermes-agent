<<<<<<< HEAD:tests/hermes_state/test_session_workspace_binding.py
"""Session <-> workspace grouping key (hermes_state_sessions_workspace.workspace_key).
||||||| dff103e05878:tests/hermes_state/test_session_workspace_binding.py
"""Session <-> workspace grouping key (hermes_state_sessions.workspace_key).
=======
"""Session <-> workspace grouping key (persistence.conversation.sessions.workspace_key).
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca:tests/persistence_sqlite/conversation/test_session_workspace_binding.py

The key is what `hermes sessions list --workspace` groups/filters on. It is a
coarse workspace identity derived from fields already recorded on sessions
(git_repo_root, cwd): no git shelling, no new columns. Branch is deliberately
NOT part of the key.
"""

<<<<<<< HEAD:tests/hermes_state/test_session_workspace_binding.py
import hermes_state_sessions
from hermes_state_sessions_workspace import workspace_key
||||||| dff103e05878:tests/hermes_state/test_session_workspace_binding.py
import hermes_state_sessions
from hermes_state_sessions import workspace_key
=======
from persistence.conversation.sessions import workspace_key
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca:tests/persistence_sqlite/conversation/test_session_workspace_binding.py


def test_repo_root_is_the_key_when_known():
    row = {"git_repo_root": "/www/app", "cwd": "/www/app/src", "git_branch": "feat"}
    assert workspace_key(row) == "/www/app"


def test_falls_back_to_cwd_for_non_git_sessions():
    assert workspace_key({"cwd": "/work/notes"}) == "/work/notes"
    assert workspace_key({"git_repo_root": "", "cwd": "/work/notes"}) == "/work/notes"


def test_none_when_unbound():
    assert workspace_key({}) is None
    assert workspace_key({"cwd": "", "git_repo_root": ""}) is None
    assert workspace_key({"cwd": "   "}) is None


def test_branch_does_not_affect_the_key():
    # Two sessions on the same repo, different branches, group together.
    a = {"git_repo_root": "/www/app", "git_branch": "main"}
    b = {"git_repo_root": "/www/app", "git_branch": "feature-x"}
    assert workspace_key(a) == workspace_key(b) == "/www/app"


def test_repo_root_wins_over_a_differing_cwd():
    # A worktree/subdir session still groups under its repo root, not its cwd.
    row = {"git_repo_root": "/www/app", "cwd": "/www/app/.worktrees/x"}
    assert workspace_key(row) == "/www/app"
