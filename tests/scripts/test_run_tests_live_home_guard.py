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
    # id/dscl/getent are the account-default lookup. python3 is intentionally
    # absent: the lookup must not fall back to the macOS Command Line Tools stub.
    for name in ("bash", "dirname", "env", "sed", "head", "uname", "mkdir", "id", "cut"):
        source = shutil.which(name)
        if source:
            commands.joinpath(name).symlink_to(source)
    for name in ("getent", "dscl"):
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
        (home / "gateway.pid").write_text(f'{{"pid": {proc.pid}}}\n', encoding="utf-8")
        home.joinpath(".sleep-pid").write_text(str(proc.pid), encoding="utf-8")
    elif kind == "bare-pid":
        proc = subprocess.Popen(["sleep", "60"])
        (home / "gateway.b.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        home.joinpath(".sleep-pid").write_text(str(proc.pid), encoding="utf-8")
    elif kind == "dead-pid":
        proc = subprocess.Popen(["sleep", "60"])
        proc.kill()
        proc.wait(timeout=5)
        (home / "gateway.pid").write_text(f'{{"pid": {proc.pid}}}\n', encoding="utf-8")
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


def _source(script: Path, body: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", body, "probe", str(script), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )


@pytest.mark.platforms("posix")
def test_profile_under_live_custom_root_refuses_and_writes_nothing(tmp_path):
    """PM resolves <root>/profiles/<name> to <root> and writes <root>/tools."""
    root, commands = _checkout(tmp_path)
    custom = tmp_path / "custom-root"
    _mark(custom, "gateway-state")
    profile = custom / "profiles" / "dev"
    _mark(profile, "scratch")
    before_root = _snapshot(custom)
    before_profile = _snapshot(profile)
    result = _run(root, commands, profile)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert REBUILD in output
    assert _snapshot(custom) == before_root
    assert _snapshot(profile) == before_profile


@pytest.mark.platforms("posix")
def test_profile_under_quiet_custom_root_still_sets_up(tmp_path):
    root, commands = _checkout(tmp_path)
    custom = tmp_path / "quiet-root"
    _mark(custom, "scratch")
    profile = custom / "profiles" / "dev"
    _mark(profile, "scratch")
    result = _run(root, commands, profile)
    output = result.stdout + result.stderr
    assert "SETUP_RAN" in output, output
    assert (profile / "setup-wrote").read_text(encoding="utf-8") == "setup\n"
    assert not (custom / "setup-wrote").exists()


@pytest.mark.platforms("posix")
def test_descendant_of_account_default_is_live_without_markers(tmp_path):
    """A profile under the account-default root is live even with no markers of its own."""
    script = ROOT / "scripts" / "run_tests.sh"
    account = tmp_path / "account-default"
    profile = account / "profiles" / "dev"
    sibling = tmp_path / "account-default-extra" / "profiles" / "dev"
    profile.mkdir(parents=True)
    sibling.mkdir(parents=True)
    probe = _source(
        script,
        "source \"$1\"\n"
        "if hermes_runtime_home_is_live \"$2\" \"$4\"; then echo PROFILE_LIVE; else echo PROFILE_SCRATCH; fi\n"
        "if hermes_runtime_home_is_live \"$3\" \"$4\"; then echo SIBLING_LIVE; else echo SIBLING_SCRATCH; fi\n",
        str(profile),
        str(sibling),
        str(account),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "pinned-home")},
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.splitlines() == ["PROFILE_LIVE", "SIBLING_SCRATCH"]
    assert not (tmp_path / "pinned-home").exists()


@pytest.mark.platforms("posix")
def test_gateway_pid_eperm_counts_as_alive_and_esrch_does_not(tmp_path):
    script = ROOT / "scripts" / "run_tests.sh"
    home = tmp_path / "home"
    home.mkdir()
    (home / "gateway.pid").write_text("4242\n", encoding="utf-8")
    denied = _source(
        script,
        "set -euo pipefail\n"
        "source \"$1\"\n"
        "kill() { printf '%s\\n' \"kill: ($2) - Operation not permitted\" >&2; return 1; }\n"
        "if hermes_gateway_pid_running \"$2\"; then echo ALIVE; else echo DEAD; fi\n",
        str(home),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "pinned-home")},
    )
    missing = _source(
        script,
        "set -euo pipefail\n"
        "source \"$1\"\n"
        "kill() { printf '%s\\n' \"kill: ($2) - No such process\" >&2; return 1; }\n"
        "if hermes_gateway_pid_running \"$2\"; then echo ALIVE; else echo DEAD; fi\n",
        str(home),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "pinned-home")},
    )
    assert denied.returncode == 0, denied.stdout + denied.stderr
    assert denied.stdout.strip() == "ALIVE", denied.stdout + denied.stderr
    assert missing.returncode == 0, missing.stdout + missing.stderr
    assert missing.stdout.strip() == "DEAD", missing.stdout + missing.stderr


def _passwd_account_home() -> Path:
    """The password-database home, not the scripts-suite installer-home patch.

    tests/scripts/conftest.py patches in-process pwd.getpwuid to a disposable
    installer home. The sourced shell cannot see that patch, and must not: the
    production lookup reads the password database. A fresh interpreter does.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent-home-pin"},
    )
    assert probe.returncode == 0, probe.stderr
    return Path(probe.stdout.strip())


@pytest.mark.platforms("posix")
def test_account_default_home_matches_passwd_lookup_and_suffix(tmp_path):
    """The printed path comes from the password database, never $HOME, and is not created."""
    script = ROOT / "scripts" / "run_tests.sh"
    account = _passwd_account_home()
    pinned = tmp_path / "pinned-home"
    env = {"PATH": os.environ["PATH"], "HOME": str(pinned)}
    plain = _source(script, "source \"$1\"; hermes_account_default_home", env=env)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert plain.stdout.strip() == str(account / ".hermes")
    suffixed = _source(
        script,
        "source \"$1\"; hermes_account_default_home",
        env={**env, "HERMES_DATA_DIR_SUFFIX": "-probe"},
    )
    assert suffixed.returncode == 0, suffixed.stdout + suffixed.stderr
    assert suffixed.stdout.strip() == str(account / ".hermes-probe")
    assert not pinned.exists()
    assert not (account / ".hermes-probe").exists()


@pytest.mark.platforms("posix")
def test_account_default_tilde_fallback_ignores_pinned_home(tmp_path):
    """Without getent or dscl, ~user still comes from the password database."""
    script = ROOT / "scripts" / "run_tests.sh"
    account = _passwd_account_home()
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("id", "uname", "bash"):
        source = shutil.which(name)
        assert source
        (commands / name).symlink_to(source)
    pinned = tmp_path / "pinned-home"
    probe = _source(
        script,
        "source \"$1\"; hermes_account_default_home",
        env={"PATH": str(commands), "HOME": str(pinned)},
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip() == str(account / ".hermes")
    assert not pinned.exists()


@pytest.mark.platforms("posix")
def test_account_default_lookup_does_not_invoke_usr_bin_python3():
    script = ROOT / "scripts" / "run_tests.sh"
    probe = subprocess.run(
        ["bash", "-x", "-c", "source \"$1\"; hermes_account_default_home >/dev/null", "probe", str(script)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "/usr/bin/python3" not in probe.stderr


@pytest.mark.platforms("posix")
def test_account_default_lookup_failure_refuses_without_writing(tmp_path):
    """A failed lookup is live. The restricted PATH has no id, dscl, getent, or python3."""
    root, commands = _checkout(tmp_path)
    for name in ("id", "getent", "dscl", "cut", "python3"):
        link = commands / name
        if link.is_symlink() or link.exists():
            link.unlink()
    home = tmp_path / "scratch-home"
    _mark(home, "scratch")
    before = _snapshot(home)
    result = _run(root, commands, home)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert _snapshot(home) == before
