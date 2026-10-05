"""Continue immutable promotion only inside the exact staged release interpreter."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
from hermes_cli.update_handoff import post_swap_child_env, write_handoff


def detach_update_receipt() -> dict | None:
    from copy import deepcopy
    from hermes_cli import update_receipt
    current = update_receipt._current.get()
    if current is None:
        return None
    data = deepcopy(current.data)
    update_receipt._current.reset(current.current_token)
    return data


def continue_update_in_fresh_interpreter(payload: dict, *, argv_tail: list[str]) -> int | None:
    from hermes_cli.immutable_releases import _release_python
    release = Path(payload["release"]).resolve(strict=True)
    if payload.get("swap") != "immutable" or release.name != payload["candidate_sha"]:
        raise RuntimeError("immutable handoff identity mismatch")
    python = _release_python(release)
    if not python.is_file():
        raise RuntimeError(f"staged release interpreter missing: {python}")
    handoff = write_handoff(payload)
    env = post_swap_child_env()
    env.pop("PYTHONHOME", None)
    env.pop("HERMES_INSTALL_ROOT", None)
    env["PYTHONPATH"] = str(release)
    env["VIRTUAL_ENV"] = str(release / ".venv")
    command = [str(python), "-m", "hermes_cli.main", "update", *argv_tail,
               "--post-swap", str(handoff)]
    try:
        process = subprocess.Popen(command, cwd=release, env=env)
    except OSError:
        return None
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue  # The child owns transaction cleanup before the parent reports its exit.
