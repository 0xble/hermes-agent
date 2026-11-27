"""A named-node write resolves its policy anchors in batches, not one target round trip each."""
import pytest

pytest_plugins = ("tests.tools.test_named_node_execution",)
from tests.tools.test_named_node_execution import call, peers as _peers  # noqa: F401
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("posix")


def test_patch_on_a_named_node_batches_credential_anchor_resolution(peers, monkeypatch):
    roots, path, *_ = peers
    snippets = []
    original = ShellFileOperations._exec_python_snippet

    def counted(self, snippet, py=None):
        snippets.append(snippet)
        return original(self, snippet, py)

    monkeypatch.setattr(ShellFileOperations, "_exec_python_snippet", counted)
    result = call("patch", {"node": "macstudio", "path": path + "/same.txt",
                            "old_string": "macstudio", "new_string": "first"}, task="batch", session="batch")
    assert not result.get("error"), result
    assert (roots["macstudio"] / "same.txt").read_text() == "first\nsecond\n"
    batched = [s for s in snippets if "[os.path.realpath(p) for p in" in s]
    singles = [s for s in snippets if "p=os.path.realpath(p)" in s]
    assert batched, "credential anchors were not batched"
    anchors = sum(s.count("', '") + 1 for s in batched)
    # Before batching every anchor cost its own round trip (68 resolutions for one patch). The
    # per-path resolutions left are the target and its spellings and must not scale with anchors.
    assert anchors >= 40, anchors
    assert len(singles) * 3 < anchors, f"{len(singles)} single resolutions beside {anchors} batched anchors"
    # Fail-closed property: every anchor the guards compare against was resolved on the node.
    assert any(str(roots["macstudio"] / "home" / ".ssh" / "id_rsa") in s for s in batched)
