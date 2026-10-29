"""Portable selected-node write readback, without GNU tools (regression for #913)."""
from tests.tools.test_named_node_execution import call, peers  # noqa: F401
from tools.file_operations import ShellFileOperations, ExecuteResult

import pytest

pytestmark = pytest.mark.platforms("posix")


def test_write_and_patch_verify_without_gnu_sha256sum(peers, monkeypatch):
    roots, path, *_ = peers
    original = ShellFileOperations._exec

    def without_gnu(self, command, *args, **kwargs):
        if "sha256sum" in command:
            return ExecuteResult(stdout="sha256sum: command not found", exit_code=127)
        return original(self, command, *args, **kwargs)

    monkeypatch.setattr(ShellFileOperations, "_exec", without_gnu)
    result = call("write_file", {"node": "macstudio", "path": path + "/portable.txt", "content": "old"})
    assert result.get("verified") is True, result
    patched = call("patch", {"node": "macstudio", "path": path + "/portable.txt", "old_string": "old", "new_string": "new"})
    assert not patched.get("error"), patched
    assert (roots["macstudio"] / "portable.txt").read_text() == "new"


def test_portable_readback_rejects_mismatch_and_missing_file(peers, monkeypatch):
    roots, path, *_ = peers
    original = ShellFileOperations._atomic_write
    fault = ["mismatch"]

    def interfere(self, target, content):
        result = original(self, target, content)
        physical = roots["macstudio"] / target.rsplit("/", 1)[-1]
        if fault[0] == "mismatch":
            physical.write_text("different bytes")
        else:
            physical.unlink()
        return result

    monkeypatch.setattr(ShellFileOperations, "_atomic_write", interfere)
    mismatch = call("write_file", {"node": "macstudio", "path": path + "/mismatch.txt", "content": "intended"})
    assert "hash differs" in mismatch.get("error", ""), mismatch
    fault[0] = "missing"
    missing = call("write_file", {"node": "macstudio", "path": path + "/missing-readback.txt", "content": "intended"})
    assert "unverified" in missing.get("error", "").lower(), missing
    assert missing.get("verified") is not True
