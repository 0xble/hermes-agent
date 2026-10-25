"""Shell functions and aliases must survive every command of a terminal session.

The login-shell bootstrap captures exports, functions and aliases into the session snapshot, but
the per-command re-dump used to write exports only and then replace the snapshot. Every function
and alias (from the user's profile, ``terminal.shell_init_files``, or an earlier command) was
therefore gone from the second command on, while exported variables persisted.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")


def _run(env, command):
    return (env.execute(command).get("output") or "").strip()


def test_functions_and_aliases_survive_later_commands(tmp_path):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        _run(env, "greet() { echo \"hello $1\"; }; ./tool() { echo wrapped; }; alias hi='echo hi-alias'; export KEEP=1")
        for _ in range(3):
            assert _run(env, "greet there") == "hello there"
            assert _run(env, "./tool") == "wrapped"
            assert _run(env, "hi") == "hi-alias"
            assert _run(env, "echo $KEEP") == "1"
    finally:
        env.cleanup()


def test_init_file_functions_survive_later_commands(tmp_path, monkeypatch):
    from tools.environments import local

    init = tmp_path / "init.sh"
    init.write_text("from_init() { echo from-init; }\n")
    monkeypatch.setattr(local, "_resolve_shell_init_files", lambda: [str(init)])
    env = local.LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        for _ in range(3):
            assert _run(env, "from_init") == "from-init"
    finally:
        env.cleanup()


def test_private_helpers_stay_out_of_the_snapshot(tmp_path):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        _run(env, "_private_helper() { :; }; public_fn() { :; }")
        snapshot = open(env._snapshot_path).read()
        assert "public_fn ()" in snapshot
        assert "_private_helper ()" not in snapshot
    finally:
        env.cleanup()
