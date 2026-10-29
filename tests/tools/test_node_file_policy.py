"""Selected-node policy anchors must follow target symlinks (regression for #913)."""
import json

import pytest

from tests.tools.test_named_node_execution import call, peers  # noqa: F401

pytestmark = pytest.mark.platforms("posix")


def test_target_credential_symlink_is_denied_but_config_needs_approval(peers):
    roots, path, *_ = peers
    home = roots["macstudio"] / "home"
    keys = roots["macstudio"] / "keys"
    keys.mkdir()
    (home / ".ssh").symlink_to(keys, target_is_directory=True)
    target = keys / "id_ed25519"
    result = call("write_file", {"node": "macstudio", "path": path + "/keys/id_ed25519", "content": "refused"})
    assert "denied" in result.get("error", "").lower(), result
    assert not target.exists()
    from agent.file_safety import is_write_approval_required
    from tools.node_execution import node_handler

    @node_handler
    def approval(args, **kwargs):
        return json.dumps({"approval": is_write_approval_required(path + "/keys/config")})

    result = json.loads(approval({"node": "macstudio"}, task_id="raw-owner", session_id="chat"))
    assert result.get("approval"), result


def test_system_policy_anchors_resolve_on_the_selected_target(peers):
    from agent.file_safety import get_write_denied_error
    from tools.node_execution import node_handler

    @node_handler
    def classify(args, **kwargs):
        return json.dumps({"denials": [get_write_denied_error(path) for path in
            ("/etc/sudoers", "/etc/passwd", "/etc/shadow", "/etc/sudoers.d/fixture", "/etc/systemd/fixture")]})

    result = json.loads(classify({"node": "macstudio"}, task_id="raw-owner", session_id="chat"))
    assert all(result["denials"]), result


def test_unavailable_policy_anchor_refuses_without_writing(peers, monkeypatch):
    from tools import file_backend_state

    roots, path, *_ = peers
    remote_path = file_backend_state.remote_path

    def unavailable(anchor, **kwargs):
        if str(anchor).endswith("/.ssh/config"):
            raise RuntimeError("fixture selected-node policy metadata unavailable")
        return remote_path(anchor, **kwargs)

    monkeypatch.setattr(file_backend_state, "remote_path", unavailable)
    result = call("write_file", {"node": "macstudio", "path": path + "/untouched.txt", "content": "refused"})
    assert "policy metadata unavailable" in result.get("error", ""), result
    assert not (roots["macstudio"] / "untouched.txt").exists()
