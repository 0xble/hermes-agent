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
import json
import shlex

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
    for name in ("run_tests.sh", "run-in-hermes-env", "_activation.sh", "_setup_guard.sh", "setup_guard.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "run-in-hermes-env").chmod(0o755)
    # Admission imports the same runtime as setup, before any provisioning.
    shutil.copy2(ROOT / "hermes_constants.py", root / "hermes_constants.py")
    shutil.copytree(ROOT / "pm", root / "pm", ignore=shutil.ignore_patterns("__pycache__"))
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    python.chmod(0o755)
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
    for name in ("bash", "dirname", "env", "sed", "head", "uname", "mkdir", "id", "cut", "readlink"):
        source = shutil.which(name)
        if source:
            commands.joinpath(name).symlink_to(source)
    for name in ("getent", "dscl"):
        source = shutil.which(name)
        if source:
            commands.joinpath(name).symlink_to(source)
    return root, commands


def _run(
    root: Path,
    commands: Path,
    home: Path,
    extra_env: dict[str, str] | None = None,
    *,
    home_text: str | None = None,
    activation_entry: str | None = None,
    cwd: Path | None = None,
):
    env = {
        "PATH": str(commands),
        "HOME": str(root.parent / "account-home"),
        "HERMES_HOME": home_text if home_text is not None else str(home),
        "HERMES_RUNTIME_DIR": str(root.parent / "runtime"),
        "TMPDIR": str(root.parent / "tmp"),
    }
    Path(env["HOME"]).mkdir(exist_ok=True)
    Path(env["HERMES_RUNTIME_DIR"]).mkdir(exist_ok=True)
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    argv = ["bash", str(root / "scripts" / "run_tests.sh")]
    if activation_entry is not None:
        argv = [
            "bash", "-c", 'source "$1/scripts/_activation.sh"; "$2" "$1"',
            "activation", str(root), activation_entry,
        ]
    return subprocess.run(
        argv,
        cwd=cwd or root,
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
@pytest.mark.parametrize("entry", ["hermes_ensure_env", "hermes_sync"])
@pytest.mark.parametrize("override", [None, "0", "true", "yes"])
def test_direct_activation_refuses_live_home_without_writing(tmp_path, entry, override):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "live-home"
    _mark(home, "gateway-state")
    before = _snapshot(home)
    extra = {} if override is None else {"HERMES_ALLOW_LIVE_HOME_SETUP": override}
    result = _run(root, commands, home, extra_env=extra, activation_entry=entry)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output, output
    assert "SETUP_RAN" not in output, output
    assert _snapshot(home) == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["scratch", "dead-pid", "not-a-pointer"])
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
def test_stale_fallback_still_sets_up_a_non_live_home(tmp_path, kind, entry):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "scratch-home"
    _mark(home, kind)
    result = _run(root, commands, home, activation_entry=entry)
    output = result.stdout + result.stderr
    assert "SETUP_RAN" in output, output
    assert (home / "setup-wrote").read_text(encoding="utf-8") == "setup\n"
    assert "refusing" not in output


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
def test_explicit_opt_in_allows_setup_against_a_live_home(tmp_path, entry):
    root, commands = _checkout(tmp_path)
    home = tmp_path / "live-home"
    _mark(home, "gateway-state")
    result = _run(root, commands, home, extra_env={"HERMES_ALLOW_LIVE_HOME_SETUP": "1"}, activation_entry=entry)
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
def test_symlinked_profile_under_live_root_refuses_and_writes_nothing(tmp_path):
    """A profile symlink keeps the lexical profiles parent, matching Python.

    ``hermes_constants._hermes_root_of`` does not canonicalize before that
    check. ``<live-root>/profiles/dev`` pointing at a quiet directory outside
    the root still resolves to ``<live-root>``. Canonicalizing first would
    treat the quiet target as scratch and let setup write through the symlink.
    """
    root, commands = _checkout(tmp_path)
    live = tmp_path / "live-root"
    quiet = tmp_path / "quiet-profile"
    _mark(live, "gateway-state")
    _mark(quiet, "scratch")
    link = live / "profiles" / "dev"
    link.parent.mkdir(parents=True)
    link.symlink_to(quiet)
    before_live = _snapshot(live)
    before_quiet = _snapshot(quiet)
    result = _run(root, commands, link)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert REBUILD in output
    assert _snapshot(live) == before_live
    assert _snapshot(quiet) == before_quiet
    assert not (quiet / "setup-wrote").exists()
    assert not (live / "setup-wrote").exists()


@pytest.mark.platforms("posix")
def test_diverging_canonical_root_refuses_when_either_interpretation_is_live(tmp_path):
    """If lexical and canonical roots differ, either live root refuses.

    ``quiet-root/profiles`` pointing at ``live-root/profiles`` makes the
    lexical root quiet and the canonical root live. Setup would follow that
    symlink into the live tree, so the guard must refuse and write nothing.
    """
    root, commands = _checkout(tmp_path)
    live = tmp_path / "live-root"
    quiet_root = tmp_path / "quiet-root"
    _mark(live, "gateway-state")
    profile = live / "profiles" / "dev"
    profile.mkdir(parents=True)
    (profile / "keep.txt").write_text("unchanged\n", encoding="utf-8")
    quiet_root.mkdir()
    (quiet_root / "keep.txt").write_text("unchanged\n", encoding="utf-8")
    (quiet_root / "profiles").symlink_to(live / "profiles")
    home = quiet_root / "profiles" / "dev"
    before_live = _snapshot(live)
    before_quiet = _snapshot(quiet_root)
    result = _run(root, commands, home)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert REBUILD in output
    assert _snapshot(live) == before_live
    assert _snapshot(quiet_root) == before_quiet
    assert not (profile / "setup-wrote").exists()
    assert not (quiet_root / "setup-wrote").exists()


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
def test_missing_profile_under_symlinked_profiles_refuses_without_writing(tmp_path):
    """Review round 3: a missing profile under a symlinked profiles dir is live.

    quiet/profiles -> live/profiles, live/gateway_state.json present, and
    HERMES_HOME=quiet/profiles/new-profile does not exist yet. Path.resolve()
    places that home under live. The existing-dir canonicalizer returned quiet
    and let setup run.
    """
    root, commands = _checkout(tmp_path)
    layout = tmp_path / "layout"
    live = layout / "live"
    quiet = layout / "quiet"
    (live / "profiles").mkdir(parents=True)
    quiet.mkdir()
    (quiet / "profiles").symlink_to(live / "profiles")
    (live / "gateway_state.json").write_text("{}\n")
    home = quiet / "profiles" / "new-profile"
    assert not home.exists()
    assert home.resolve() == live / "profiles" / "new-profile"
    before = _snapshot(layout)
    result = _run(root, commands, home)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert _snapshot(layout) == before
    assert not home.exists()
    assert not (live / "profiles" / "new-profile").exists()


@pytest.mark.platforms("posix")
def test_account_default_lookup_failure_refuses_without_writing(tmp_path):
    """A failed lookup is live. The restricted PATH has no id, dscl, getent, or python3."""
    root, commands = _checkout(tmp_path)
    for name in ("id", "getent", "dscl", "cut", "python3"):
        link = commands / name
        if link.is_symlink() or link.exists():
            link.unlink()
    with (root / "hermes_constants.py").open("a") as stream:
        stream.write("\ndef account_default_hermes_home():\n    raise OSError('account lookup failed')\n")
    home = tmp_path / "scratch-home"
    _mark(home, "scratch")
    before = _snapshot(home)
    result = _run(root, commands, home)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert _snapshot(home) == before


RESOLUTION_CASES = (
    "existing_leaf",
    "missing_leaf",
    "missing_multi",
    "symlink_ancestor_absolute",
    "symlink_ancestor_relative",
    "symlinked_profile_dir",
    "dangling_relative",
    "dangling_absolute",
    "dotdot_in_input",
    "dotdot_reenters_symlink",
    "trailing_slash",
    "spaces",
    "relative_missing",
)

GUARD_LAYOUTS = (
    "existing_leaf",
    "missing_leaf",
    "missing_multi",
    "symlink_ancestor_absolute",
    "symlink_ancestor_relative",
    "symlinked_profile_dir",
    "dotdot_under_symlink",
    "trailing_slash_under_symlink",
    "spaces",
    "spaces_symlink",
    "dot_in_symlinked_profile",
    "dot_after_symlinked_profile",
    "slashes_after_symlinked_profile",
    "dangling_relative",
    "dangling_absolute",
    "relative_missing",
)

GUARD_MARKERS = ("lexical", "canonical", "neither")


def _assert_outside_account_default(path: Path) -> None:
    account = (_passwd_account_home() / ".hermes").resolve()
    resolved = path.resolve()
    assert resolved != account and account not in resolved.parents, resolved


def _resolution_input(tmp_path: Path, name: str) -> tuple[str, Path | None]:
    base = tmp_path / "resolve"
    live = base / "live"
    quiet = base / "quiet"
    (live / "profiles" / "dev").mkdir(parents=True)
    quiet.mkdir()
    (quiet / "profiles").symlink_to(live / "profiles")
    (base / "relquiet").mkdir()
    (base / "relquiet" / "profiles").symlink_to(Path("../live/profiles"))
    (base / "link-root" / "profiles").mkdir(parents=True)
    (base / "quiet-profile").mkdir()
    (base / "link-root" / "profiles" / "dev").symlink_to(base / "quiet-profile")
    (base / "dangling").mkdir()
    (base / "dangling" / "gone").symlink_to("missing-target")
    (base / "dangling" / "abs-gone").symlink_to(base / "no-such" / "leaf")
    (base / "space dir" / "sub").mkdir(parents=True)
    (base / "reenter").mkdir()
    (base / "somewhere").mkdir()
    (base / "reenter" / "link").symlink_to(base / "somewhere")
    cases = {
        "existing_leaf": (str(live / "profiles" / "dev"), None),
        "missing_leaf": (str(live / "profiles" / "new-profile"), None),
        "missing_multi": (str(live / "profiles" / "a" / "b"), None),
        "symlink_ancestor_absolute": (str(quiet / "profiles" / "new-profile"), None),
        "symlink_ancestor_relative": (str(base / "relquiet" / "profiles" / "new-profile"), None),
        "symlinked_profile_dir": (str(base / "link-root" / "profiles" / "dev"), None),
        "dangling_relative": (str(base / "dangling" / "gone"), None),
        "dangling_absolute": (str(base / "dangling" / "abs-gone"), None),
        "dotdot_in_input": (str(quiet / "profiles" / "new-profile" / ".." / "other"), None),
        "dotdot_reenters_symlink": (str(base / "reenter" / "missing" / ".." / "link" / "child"), None),
        "trailing_slash": (str(quiet / "profiles" / "dev") + "/", None),
        "spaces": (str(base / "space dir" / "sub" / "no such"), None),
        "relative_missing": ("quiet/profiles/new-profile", base),
    }
    return cases[name]


def _shell_resolve(raw: str, cwd: Path | None, scratch: Path) -> str:
    script = ROOT / "scripts" / "run_tests.sh"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch),
        "HERMES_HOME": str(scratch / "home"),
        "HERMES_RUNTIME_DIR": str(scratch / "runtime"),
    }
    probe = subprocess.run(
        ["bash", "-c", 'source "$1"; hermes_resolve_path "$2"', "probe", str(script), raw],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    return probe.stdout.strip()


def _guard_home(base: Path, name: str) -> str:
    if name == "existing_leaf":
        home = base / "root" / "profiles" / "dev"
        home.mkdir(parents=True)
        return str(home)
    if name == "missing_leaf":
        (base / "root" / "profiles").mkdir(parents=True)
        return str(base / "root" / "profiles" / "new-profile")
    if name == "missing_multi":
        live = base / "live"
        quiet = base / "quiet"
        live.mkdir(parents=True)
        quiet.mkdir()
        (quiet / "sub").symlink_to(live)
        return str(quiet / "sub" / "profiles" / "new-profile")
    if name in {"symlink_ancestor_absolute", "symlink_ancestor_relative", "dotdot_under_symlink", "trailing_slash_under_symlink", "relative_missing"}:
        live = base / "live"
        quiet = base / "quiet"
        (live / "profiles").mkdir(parents=True)
        quiet.mkdir()
        target = live / "profiles" if name == "symlink_ancestor_absolute" else Path("../live/profiles")
        (quiet / "profiles").symlink_to(target)
        if name == "dotdot_under_symlink":
            return str(quiet / "profiles" / ".." / "profiles" / "new-profile")
        home = str(quiet / "profiles" / "new-profile")
        return home + "/" if name == "trailing_slash_under_symlink" else home
    if name in {"symlinked_profile_dir", "dot_in_symlinked_profile", "dot_after_symlinked_profile", "slashes_after_symlinked_profile"}:
        live = base / "live"
        quiet = base / "quiet-profile"
        (live / "profiles").mkdir(parents=True)
        quiet.mkdir()
        (live / "profiles" / "dev").symlink_to(quiet)
        if name == "dot_in_symlinked_profile":
            return str(live / "profiles") + "/./dev"
        home = str(live / "profiles" / "dev")
        if name == "dot_after_symlinked_profile":
            return home + "/."
        return home + "///" if name == "slashes_after_symlinked_profile" else home
    if name in {"dangling_relative", "dangling_absolute"}:
        live = base / "live"
        quiet = base / "quiet"
        (live / "profiles").mkdir(parents=True)
        (quiet / "profiles").mkdir(parents=True)
        target = live / "profiles" / "missing" if name == "dangling_absolute" else Path("../../live/profiles/missing")
        (quiet / "profiles" / "dev").symlink_to(target)
        return str(quiet / "profiles" / "dev")
    if name == "spaces":
        (base / "my root" / "profiles").mkdir(parents=True)
        return str(base / "my root" / "profiles" / "new profile")
    if name == "spaces_symlink":
        live = base / "live dir"
        quiet = base / "quiet dir"
        (live / "profiles").mkdir(parents=True)
        quiet.mkdir()
        (quiet / "profiles").symlink_to(live / "profiles")
        return str(quiet / "profiles" / "new profile")
    raise AssertionError(name)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("case", RESOLUTION_CASES)
def test_resolve_path_matches_python(tmp_path, case):
    """The source-only predicate delegates canonicalization to real pathlib."""
    _assert_outside_account_default(tmp_path)
    raw, cwd = _resolution_input(tmp_path, case)
    expected = str((cwd / raw).resolve() if cwd is not None else Path(raw).resolve())
    assert _shell_resolve(raw, cwd, tmp_path / "scratch") == expected


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("layout", GUARD_LAYOUTS)
@pytest.mark.parametrize("marker", GUARD_MARKERS)
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
def test_guard_matrix_refuses_either_root_and_writes_nothing(tmp_path, layout, marker, entry):
    """A live marker on the lexical root, the canonical root, or neither."""
    _assert_outside_account_default(tmp_path)
    root, commands = _checkout(tmp_path)
    base = tmp_path / "layout"
    home_text = _guard_home(base, layout)
    paths = _runtime_oracle(root, home_text)
    lexical = paths["root"]
    canonical = paths["canonical_root"]
    if marker == "lexical":
        marked = lexical
    elif marker == "canonical":
        marked = canonical
    else:
        marked = None
    if marked is not None:
        assert marked.is_dir(), (layout, marker, marked)
        (marked / "gateway_state.json").write_text("{}\n", encoding="utf-8")
    before = _snapshot(base)
    if layout == "relative_missing":
        home_text = os.path.relpath(home_text, root)
    result = _run(root, commands, Path(home_text), home_text=home_text, activation_entry=entry)
    output = result.stdout + result.stderr
    candidates = [candidate for value in paths.values() for candidate in (value, value.resolve())]
    expected_admission = not any((path / "gateway_state.json").exists() for path in candidates)
    assert ("SETUP_RAN" in output) == expected_admission, output
    if expected_admission:
        assert "SETUP_RAN" in output, output
        assert "refusing" not in output.lower(), output
        assert (result.returncode == 0) == (entry is not None), output
        return
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert _snapshot(base) == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
def test_symlink_loop_resolution_error_refuses_without_writing(tmp_path, entry):
    """A path that cannot be resolved is live, and nothing is written."""
    _assert_outside_account_default(tmp_path)
    root, commands = _checkout(tmp_path)
    base = tmp_path / "layout"
    base.mkdir()
    (base / "a").symlink_to("b")
    (base / "b").symlink_to("a")
    before = _snapshot(base)
    result = _run(root, commands, base / "a", activation_entry=entry)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert "resolution failed" in output.lower(), output
    assert "SETUP_RAN" not in output
    assert _snapshot(base) == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
@pytest.mark.parametrize("failure", ["no_interpreter", "broken_interpreter", "import_error"])
@pytest.mark.parametrize("override", [False, True])
def test_runtime_resolver_failure_is_closed(tmp_path, entry, failure, override):
    root, commands = _checkout(tmp_path)
    if failure == "no_interpreter":
        (root / ".venv" / "bin" / "python").unlink()
    elif failure == "broken_interpreter":
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 1\n")
    else:
        (root / "hermes_constants.py").write_text("raise ImportError('probe failure')\n")
    home = tmp_path / "scratch-home"
    _mark(home, "scratch")
    before = _snapshot(home)
    result = _run(root, commands, home, activation_entry=entry,
                  extra_env={"HERMES_ALLOW_LIVE_HOME_SETUP": "1"} if override else {})
    output = result.stdout + result.stderr
    assert ("SETUP_RAN" in output) == override, output
    assert ("refusing" in output.lower()) != override, output
    if not override:
        assert _snapshot(home) == before


@pytest.mark.platforms("posix")
def test_setup_interpreter_cwd_and_python_environment_match_admission(tmp_path):
    root, commands = _checkout(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    home = root / "quiet"
    _mark(home, "scratch")
    python = root / ".venv" / "bin" / "python"
    setup = root / "setup-hermes.sh"
    setup.write_text(
        'test "$3" = ' + shlex.quote("--bootstrap-python=" + str(python)) + ' || exit 61\n'
        'test "$PWD" = ' + shlex.quote(str(root)) + ' || exit 62\n'
        'test -z "${PYTHONHOME+x}${PYTHONPATH+x}${VIRTUAL_ENV+x}" || exit 63\n'
        'echo SETUP_RAN\n'
    )
    result = _run(root, commands, home, home_text="quiet", cwd=caller,
                  activation_entry="hermes_sync",
                  extra_env={"PYTHONHOME": "poison", "PYTHONPATH": "poison", "VIRTUAL_ENV": "poison"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SETUP_RAN" in result.stdout + result.stderr


def _runtime_oracle(root, raw, extra=None):
    """Ask runtime owners directly, not the admission entrypoint or shell helpers."""
    env = {"HOME": str(root.parent / "account-home"), "HERMES_HOME": raw,
           "HERMES_RUNTIME_DIR": str(root.parent / "runtime"), **(extra or {})}
    probe = subprocess.run(
        [sys.executable, "-B", "-c",
         "import json; from pathlib import Path; "
         "from hermes_constants import get_hermes_home, get_default_hermes_root; "
         "from pm.environments import dependency_home_root, store_root; "
         "home=get_hermes_home(); root=get_default_hermes_root(); "
         "print(json.dumps({key: str(path.absolute()) for key, path in "
         "dict(home=home, root=root, dependency=dependency_home_root(), "
         "canonical_root=get_default_hermes_root(home=home.resolve()), "
         "store=store_root(Path.cwd()).parent).items()}))"],
        cwd=root, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert probe.returncode == 0, probe.stderr
    return {key: Path(value) for key, value in json.loads(probe.stdout).items()}


RUNTIME_LAYOUTS = ("tilde", "variable", "whitespace", "missing_dotdot", "repo_relative",
                   "caller_relative", "symlink_ancestor", "profile", "variable_profile", "tilde_profile")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
@pytest.mark.parametrize("layout", RUNTIME_LAYOUTS)
@pytest.mark.parametrize("marker", ["home", "root", "canonical_root", "neither"])
def test_runtime_oracle_admission_matrix(tmp_path, layout, marker, entry):
    root, commands = _checkout(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    live = root / "live"
    live.mkdir()
    extra = {"LIVE_ROOT": str(live)}
    if layout.startswith("tilde"):
        live = tmp_path / "account-home" / ".hermes"
        live.mkdir(parents=True)
        raw = "~/.hermes" + ("/profiles/dev" if layout.endswith("profile") else "")
    elif layout.startswith("variable"):
        raw = "${LIVE_ROOT}" + ("/profiles/dev" if layout.endswith("profile") else "")
    elif layout == "whitespace":
        raw = " \t" + str(live) + " \t"
    elif layout == "missing_dotdot":
        raw = str(live) + "/missing/.."
    elif layout == "repo_relative":
        raw = "live"
    elif layout == "caller_relative":
        # Quiet from the repo, marked from caller cwd: setup must use the repo.
        (caller / "live").mkdir()
        (caller / "live" / "gateway_state.json").write_text("{}\n")
        raw = "live"
    elif layout == "symlink_ancestor":
        (live / "profiles").mkdir()
        (root / "quiet").mkdir()
        (root / "quiet" / "profiles").symlink_to(live / "profiles")
        raw = str(root / "quiet" / "profiles" / "dev")
    else:
        raw = str(live / "profiles" / "dev")
    paths = _runtime_oracle(root, raw, extra)
    if marker != "neither":
        marked = paths[marker].resolve()
        marked.mkdir(parents=True, exist_ok=True)
        (marked / "gateway_state.json").write_text("{}\n")
    # Independent oracle: every actual home/PM destination and both path forms.
    candidates = [path for value in paths.values() for path in (value, value.resolve())]
    expected_admission = not any((path / "gateway_state.json").exists() for path in candidates)
    for directory in ("account-home", "runtime", "tmp"):
        (tmp_path / directory).mkdir(exist_ok=True)
    (root / "setup-hermes.sh").write_text(
        f'cd {shlex.quote(str(root))} || exit 1\n'
        f'exec {shlex.quote(sys.executable)} -B -c '
        + shlex.quote("from hermes_constants import get_hermes_home; "
                      "from pm.environments import dependency_home_root; "
                      "home=get_hermes_home().resolve(); home.mkdir(parents=True, exist_ok=True); "
                      "(home/'setup-wrote').write_text('setup\\n'); "
                      "root=dependency_home_root().resolve(); root.mkdir(parents=True, exist_ok=True); "
                      "(root/'pm-setup-wrote').write_text('setup\\n'); print('SETUP_RAN')") + "\n"
    )
    before = _snapshot(tmp_path)
    result = _run(root, commands, paths["home"], extra_env=extra, home_text=raw,
                  activation_entry=entry, cwd=caller)
    output = result.stdout + result.stderr
    assert ("SETUP_RAN" in output) == expected_admission, output
    assert ("refusing" not in output.lower()) == expected_admission, output
    if not expected_admission:
        assert _snapshot(tmp_path) == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", [None, "hermes_sync", "hermes_ensure_env"])
@pytest.mark.parametrize("case", ["missing_dotdot", "repo_relative", "tilde", "variable", "whitespace"])
def test_round4_runtime_resolution_repros_refuse(tmp_path, case, entry):
    """Review round 4's literal inputs, with setup's repo cwd (not caller cwd).

    HERMES_HOME=<live>/missing/.. (missing absent)
    HERMES_HOME=live (caller elsewhere; setup cd's to the repo)
    HERMES_HOME=~/.hermes or ${LIVE_ROOT}/profiles/dev
    """
    root, commands = _checkout(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    live = root / "live"
    extra = {}
    if case == "missing_dotdot":
        raw = str(live) + "/missing/.."
    elif case == "repo_relative":
        raw = "live"
    elif case == "tilde":
        live = tmp_path / "account-home" / ".hermes"
        raw = "~/.hermes"
    elif case == "variable":
        raw = "${LIVE_ROOT}/profiles/dev"
        extra["LIVE_ROOT"] = str(live)
    else:
        raw = "  " + str(live) + "  "
    _mark(live, "gateway-state")
    before = _snapshot(live)
    result = _run(root, commands, live, extra_env=extra, home_text=raw,
                  activation_entry=entry, cwd=caller)
    output = result.stdout + result.stderr
    assert "SETUP_RAN" not in output, output
    assert result.returncode != 0, output
    assert "refusing" in output.lower(), output
    assert _snapshot(live) == before
