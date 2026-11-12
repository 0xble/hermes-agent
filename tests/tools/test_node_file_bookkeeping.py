"""A confirmed edit survives later tracking outages (regression for #913)."""
import hashlib
import json
import shlex
import sys

import pytest

from tests.tools.test_named_node_execution import call, peers  # noqa: F401
from tools.environments import ssh
from tools.file_operations import ShellFileOperations


@pytest.fixture
def checksum_peers(peers, monkeypatch):
    """Supply a real target checksum so #913.8 cannot mask bookkeeping failures."""
    roots, *_ = peers
    for root in roots.values():
        binary = root / "home" / "bin" / "sha256sum"
        binary.parent.mkdir()
        binary.write_text(
            f"#!{sys.executable}\n"
            "import hashlib, json, pathlib, sys\n"
            "target = pathlib.Path(sys.argv[1])\n"
            "data = target.read_bytes()\n"
            "digest = hashlib.sha256(data).hexdigest()\n"
            "with pathlib.Path(__file__).with_name('readbacks.jsonl').open('a') as log:\n"
            "    log.write(json.dumps({'path': str(target), 'sha256': digest, "
            "'bytes': len(data)}) + '\\n')\n"
            "print(digest + '  ' + str(target))\n"
        )
        binary.chmod(0o755)

    run_ssh = ssh.SSHEnvironment._run_ssh
    run_bash = ssh.SSHEnvironment._run_bash

    def with_checksum(env, command):
        return f"export PATH={shlex.quote(str(roots[env.host] / 'home' / 'bin'))}:$PATH; " + command

    def checksum_ssh(env, command, timeout):
        return run_ssh(env, with_checksum(env, command), timeout)

    def checksum_bash(env, command, **kwargs):
        return run_bash(env, with_checksum(env, command), **kwargs)

    monkeypatch.setattr(ssh.SSHEnvironment, "_run_ssh", checksum_ssh)
    monkeypatch.setattr(ssh.SSHEnvironment, "_run_bash", checksum_bash)
    return peers

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("mode", ["write_file", "replace", "v4a"])
@pytest.mark.parametrize("failure", ["baseline", "timestamp", "writer"])
def test_committed_result_survives_metadata_outage_without_authorizing_overwrite(checksum_peers, monkeypatch, mode, failure, record_property):
    from tools import file_tools as ft

    roots, path, *_ = checksum_peers
    target = path + "/same.txt"
    assert "content" in call("read_file", {"node": "macstudio", "path": target})
    writes = []
    outages = []
    expected = b"changed\nsecond\n"
    readbacks = roots["macstudio"] / "home" / "bin" / "readbacks.jsonl"
    atomic = ShellFileOperations._atomic_write

    def count_write(self, destination, content):
        writes.append(destination)
        return atomic(self, destination, content)

    def unavailable(*args, **kwargs):
        # This seam must be reached only after the real target commit and
        # production checksum readback, never after an unrelated failed write.
        observed = json.loads(readbacks.read_text().splitlines()[-1])
        assert len(writes) == 1
        assert (roots["macstudio"] / "same.txt").read_bytes() == expected
        assert observed == {"path": str(roots["macstudio"] / "same.txt"),
                            "sha256": hashlib.sha256(expected).hexdigest(), "bytes": len(expected)}
        outages.append({"failure": failure, "mode": mode, "writes": len(writes), "readback": observed})
        raise RuntimeError("fixture post-commit metadata unavailable")

    monkeypatch.setattr(ShellFileOperations, "_atomic_write", count_write)
    with monkeypatch.context() as outage:
        owner, method = {"baseline": (ft, "_mark_full_write_baseline" if mode == "write_file" else "_carry_full_write_baselines"),
                         "timestamp": (ft, "_update_read_timestamp"),
                         "writer": (ft.file_state, "note_write")}[failure]
        outage.setattr(owner, method, unavailable)
        if mode == "write_file":
            result = call("write_file", {"node": "macstudio", "path": target, "content": "changed\nsecond\n"})
        elif mode == "replace":
            result = call("patch", {"node": "macstudio", "path": target, "old_string": "macstudio", "new_string": "changed"})
        else:
            result = call("patch", {"node": "macstudio", "mode": "patch", "patch":
                f"*** Begin Patch\n*** Update File: {target}\n@@\n-macstudio\n+changed\n second\n*** End Patch\n"})
    record_property("post_commit_metadata_outage", outages)
    record_property("edit_result", result)
    assert len(outages) == 1, result
    assert (roots["macstudio"] / "same.txt").read_text() == "changed\nsecond\n"
    assert len(writes) == 1
    assert not result.get("error"), result
    assert result.get("files_modified") and result.get("node") == "macstudio", result
    assert "metadata unavailable" in result.get("bookkeeping_warning", ""), result
    if mode == "write_file":
        assert result.get("verified") is True, result
    refused = call("write_file", {"node": "macstudio", "path": target, "content": "must re-read"})
    assert refused.get("stale_write_blocked"), refused
    assert len(writes) == 1
    refreshed = call("read_file", {"node": "macstudio", "path": target})
    assert "changed" in refreshed.get("content", ""), refreshed
