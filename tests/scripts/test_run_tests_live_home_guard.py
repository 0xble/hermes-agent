"""The stale-env fallback must not run setup against a live runtime home.

A missing checkout venv used to re-exec ``run-in-hermes-env``, which runs
``setup-hermes.sh`` with the inherited ``HERMES_HOME``. That writes tool,
dependency, and test-env state into a live runtime home. The fallback must
refuse, write nothing, and tell the caller how to rebuild the checkout venv
or point ``HERMES_HOME`` at a scratch directory.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
REBUILD = "python -m pm.build_env --source . --out .venv --group dev --group test"


def _snapshot(home: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not home.exists():
        return found
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home).as_posix()
        if path.is_symlink():
            found[rel] = f"symlink:{os.readlink(path)}"
        elif path.is_dir():
            found[rel] = "dir"
        else:
            found[rel] = path.read_bytes().hex()
    return found


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("run_tests.sh", "run-in-hermes-env", "_activation.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "run-in-hermes-env").chmod(0o755)
    setup = root / "setup-hermes.sh"
    setup.write_text(
        "#!/bin/bash\n"
        "mkdir -p \"${HERMES_HOME:?}\"\n"
        "printf 'setup\\n' > \"$HERMES_HOME/setup-wrote\"\n"
        "printf 'SETUP_RAN\\n' >&2\n"
        "exit 0\n",
        encoding="utf-8",
    )
    setup.chmod(0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("bash", "dirname", "env", "sed", "head", "uname", "mkdir"):
        source = shutil.which(name)
        if source:
            commands.joinpath(name).symlink_to(source)
    return root, commands


def _run(root: Path, commands: Path, home: Path, *, extra_env: dict[str, str] | None = None):
    env = {
        "PATH": str(commands),
        "HOME": str(root.parent / "account-home"),
        "HERMES_HOME": str(home),
        "HERMES_RUNTIME_DIR": str(root.parent / "runtime"),
        "TMPDIR": str(root.parent / "tmp"),
    }
    Path(env["HOME"]).mkdir(exist_ok=True)
    Path(env["HERMES_RUNTIME_DIR"]).mkdir(exist_ok=True)
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(root / "scripts" / "run_tests.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mark(home: Path, kind: str) -> None:
    home.mkdir(parents=True)
    (home / "keep.txt").write_text("unchanged\n", encoding="utf-8")
    if kind == "release-pointer":
        target = home / "releases" / "abc"
        target.mkdir(parents=True)
        home.joinpath("current").symlink_to(target, target_is_directory=True)
    elif kind == "dangling-pointer":
        home.joinpath("current").symlink_to(home / "missing-release")
    elif kind == "gateway-state":
        (home / "gateway_state.json").write_text("{}\n", encoding="utf-8")
    elif kind == "running-pid":
        proc = subprocess.Popen(["sleep", "60"])
        (home / "gateway.pid").write_text('{"pid": %d}\n' % proc.pid, encoding="utf-8")
        home.joinpath(".sleep-pid").write_text(str(proc.pid), encoding="utf-8")
    elif kind == "bare-pid":
        proc = subprocess.Popen(["sleep", "60"])
        (home / "gateway.b.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        home.joinpath(".sleep-pid").write_text(str(proc.pid), encoding="utf-8")
    elif kind == "dead-pid":
        proc = subprocess.Popen(["sleep", "60"])
        proc.kill()
        proc.wait(timeout=5)
        (home / "gateway.pid").write_text('{"pid": %d}\n' % proc.pid, encoding="utf-8")
    elif kind == "not-a-pointer":
        (home / "current").write_text("not a release pointer\n", encoding="utf-8")
    elif kind == "scratch":
        return
    else:
        raise AssertionError(kind)


def _stop_sleep(home: Path) -> None:
    pid_file = home / ".sleep-pid"
    if not pid_file.is_file():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    subprocess.run(["kill", str(pid)], check=False, capture_output=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["release-pointer", "dangling-pointer", "gateway-state", "running-pid", "bare-pid"])
def test_stale_fallback_refuses_live_home_and_writes_nothing(tmp_path, kind):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "live-home"
    _mark(home, kind)
    before = _snapshot(home)
    try:
        result = _run(root, commands, home)
    finally:
        _stop_sleep(home)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output
    assert REBUILD in output
    assert "scratch" in output
    assert "HERMES_ALLOW_LIVE_HOME_SETUP" in output
    assert "re-running under run-in-hermes-env" not in output
    assert "SETUP_RAN" not in output
    assert not (home / "setup-wrote").exists()
    assert _snapshot(home) == before
    assert (home / "keep.txt").read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("activated", ["", "stale-checkout"])
def test_refusal_advice_selects_rebuilt_interpreter_without_setup(tmp_path, activated):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "live-home"
    _mark(home, "gateway-state")
    before = _snapshot(home)
    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  -c) [ \"$2\" = 'import pytest' ] ;;\n"
        "  -m) exit 0 ;;\n"
        "  *) printf 'TEST_RUNNER_ACCEPTED\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    result = _run(root, commands, home, extra_env={"__HERMES_ACTIVATED": activated})
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    advice = [line.strip() for line in output.splitlines() if "HERMES_PYTHON=" in line]
    assert len(advice) == 1, output
    # Execute the emitted command verbatim in the caller's same stale shell.
    (root / "advice.sh").write_text(advice[0] + "\n", encoding="utf-8")
    env = {
        "PATH": str(commands), "HOME": str(tmp_path / "account-home"),
        "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(tmp_path / "runtime"),
        "__HERMES_ACTIVATED": activated,
    }
    followed = subprocess.run(
        ["bash", str(root / "advice.sh")], cwd=root, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    followed_output = followed.stdout + followed.stderr
    assert followed.returncode == 0, followed_output
    assert "TEST_RUNNER_ACCEPTED" in followed_output
    assert "SETUP_RAN" not in followed_output
    assert _snapshot(home) == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["scratch", "dead-pid", "not-a-pointer"])
def test_stale_fallback_still_sets_up_a_non_live_home(tmp_path, kind):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "scratch-home"
    _mark(home, kind)
    result = _run(root, commands, home)
    output = result.stdout + result.stderr
    assert "SETUP_RAN" in output, output
    assert (home / "setup-wrote").read_text(encoding="utf-8") == "setup\n"
    assert "refusing" not in output


@pytest.mark.platforms("posix")
def test_explicit_opt_in_allows_setup_against_a_live_home(tmp_path):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "live-home"
    _mark(home, "gateway-state")
    result = _run(root, commands, home, extra_env={"HERMES_ALLOW_LIVE_HOME_SETUP": "1"})
    output = result.stdout + result.stderr
    assert "SETUP_RAN" in output, output
    assert (home / "setup-wrote").read_text(encoding="utf-8") == "setup\n"


@pytest.mark.platforms("posix")
def test_account_default_match_is_live_without_touching_that_home(tmp_path):
    """Equality is a live signal even when the directory has no runtime files.

    The second argument is the already-resolved account default, so this does
    not read or write the OS account's real Hermes home.
    """
    script = ROOT / "scripts" / "run_tests.sh"
    text = script.read_text(encoding="utf-8")
    assert "hermes_runtime_home_is_live" in text
    assert 'BASH_SOURCE[0]' in text and '"$0"' in text
    home = tmp_path / "account" / ".hermes"
    other = tmp_path / "scratch" / ".hermes"
    home.mkdir(parents=True)
    other.mkdir(parents=True)
    probe = subprocess.run(
        [
            "bash", "-c",
            'source "$1"\n'
            'if hermes_runtime_home_is_live "$2" "$2"; then echo MATCH; else echo MISS; fi\n'
            'if hermes_runtime_home_is_live "$3" "$2"; then echo OTHER_LIVE; else echo OTHER_SCRATCH; fi\n',
            "probe", str(script), str(home), str(other),
        ],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.splitlines() == ["MATCH", "OTHER_SCRATCH"]
