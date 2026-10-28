"""Metadata for named-node file guards, through the existing file backend.

Transport failures are not absence. Nothing in this module probes a target
path on the controller, including realpath, hashes and negative-cache checks.
"""
from __future__ import annotations

import json
import posixpath

from tools.node_execution import current_execution


def file_ops():
    from tools.file_tools import _get_file_ops
    execution = current_execution()
    if execution is None:
        raise RuntimeError("Remote file state requires a bound node")
    return _get_file_ops(execution.task_id)


def remote_home() -> str:
    env = file_ops().env
    if not getattr(env, "_remote_home_detected", False):
        raise RuntimeError("Selected node's home is unavailable, refusing controller path expansion")
    return env._remote_home


def remote_path(path: str, *, entry: bool = False) -> str:
    ops = file_ops()
    text = ops._expand_path(str(path))
    if text.startswith("~"):
        raise RuntimeError("Cannot expand selected-node path")
    snippet = (
        "import os,json; p=" + repr(text) + "; "
        + ("p=os.path.join(os.path.realpath(os.path.dirname(p) or '.'),os.path.basename(p)); "
           if entry else "p=os.path.realpath(p); ")
        + "print(json.dumps(p))")
    result = ops._exec_python_snippet(snippet)
    if result.exit_code:
        raise RuntimeError(f"Selected-node path resolution failed: {result.stdout}")
    return json.loads(result.stdout)


def remote_state(path: str, *, digest: bool = False) -> dict:
    snippet = '''import hashlib,json,os,stat
p=PATH
try:
    st=os.stat(p)
except FileNotFoundError:
    print(json.dumps({"exists":False}))
else:
    fields=lambda s:[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
    result={"exists":True,"mode":st.st_mode,"metadata":fields(st)}
    if DIGEST and stat.S_ISREG(st.st_mode):
        fd=os.open(p,os.O_RDONLY|getattr(os,"O_NONBLOCK",0))
        with os.fdopen(fd,"rb") as stream:
            before=os.fstat(stream.fileno())
            if stat.S_ISREG(before.st_mode):
                h=hashlib.sha256()
                for block in iter(lambda:stream.read(1048576),b""):h.update(block)
                if fields(before)==fields(os.stat(p)):
                    result.update(metadata=fields(before),sha256=h.hexdigest())
    print(json.dumps(result))
'''.replace("p=PATH\n", "p=" + repr(str(path)) + "\n").replace("if DIGEST and", f"if {digest!r} and")
    result = file_ops()._exec_python_snippet(snippet)
    if result.exit_code:
        raise RuntimeError(f"Selected-node metadata unavailable: {result.stdout}")
    return json.loads(result.stdout)


def metadata(path: str) -> tuple | None:
    state = remote_state(path)
    return tuple(state["metadata"]) if state.get("exists") else None


def version(path: str) -> tuple | None:
    state = remote_state(path, digest=True)
    if "sha256" not in state:
        return None
    return (*state["metadata"], bytes.fromhex(state["sha256"]))


def mtime(path: str) -> float | None:
    fields = metadata(path)
    return fields[3] / 1_000_000_000 if fields else None


def policy_realpath(path: str) -> str:
    """Policy candidates are already in target coordinates, not controller symlinks."""
    return posixpath.normpath(str(path))
