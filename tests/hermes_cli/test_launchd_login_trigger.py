"""The login trigger registers only absent jobs and obeys deliberate-stop markers."""
import plistlib
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hermes_cli.gateway_guardian import GUARDIAN_LABEL, set_intent
from hermes_cli.gateway_launchd_login import login_bootstrap

pytestmark = pytest.mark.platforms("linux", "macos")


@pytest.mark.parametrize("stopped", [None, "intent", "planned"])
def test_login_trigger_never_execs_gateway_and_is_idempotent(tmp_path, stopped):
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "ai.hermes.gateway.plist"
    names = ("ai.hermes.gateway", GUARDIAN_LABEL)
    for label in names:
        path.with_name(label + ".plist").write_bytes(plistlib.dumps({
            "Label": label, "LimitLoadToSessionType": "Background",
            "EnvironmentVariables": {"HERMES_HOME": str(home)},
        }))
    if stopped == "intent":
        set_intent(home, stopped=True)
    elif stopped == "planned":
        (home / ".gateway-planned-stop.json").write_text(json.dumps({"target_pid": os.getpid(), "written_at": datetime.now(UTC).isoformat()}))
    loaded, calls, logs = set(), [], []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1] in {"print", "bootstrap"}
        if argv[1] == "print":
            label = argv[2].rsplit("/", 1)[-1]
            return SimpleNamespace(returncode=0 if label in loaded and argv[2].startswith("user/") else 113)
        assert argv[2].startswith("user/")
        label = plistlib.loads(path.with_name(argv[3].rsplit("/", 1)[-1]).read_bytes())["Label"]
        assert label not in loaded
        loaded.add(label)
        return SimpleNamespace(returncode=0)

    login_bootstrap(home, path, runner=run, log=logs.append)
    login_bootstrap(home, path, runner=run, log=logs.append)
    if stopped:
        assert not calls
        assert logs == [f"{label}: skipped: stopped intent" for _ in range(2) for label in names]
    else:
        assert loaded == set(names)
        assert logs == [f"{label}: loaded" for label in names] + [f"{label}: already loaded" for label in names]
        assert len([call for call in calls if call[1] == "bootstrap"]) == 2


@pytest.mark.parametrize("marker", ["{}", "not json", "expired", "dead"])
def test_stale_planned_stop_does_not_disable_future_login(tmp_path, marker):
    from datetime import timedelta
    from hermes_cli.gateway_launchd_login import stop_requested
    if marker in {"expired", "dead"}:
        stamp = datetime.now(UTC) - timedelta(seconds=120 if marker == "expired" else 0)
        marker = json.dumps({"target_pid": os.getpid() if marker == "expired" else 99999999,
                             "written_at": stamp.isoformat()})
    (tmp_path / ".gateway-planned-stop.json").write_text(marker)
    assert stop_requested(tmp_path) is False


def test_foreign_gui_copy_prevents_login_bootstrap(tmp_path):
    path = tmp_path / "ai.hermes.gateway.plist"
    path.write_bytes(plistlib.dumps({"Label": path.stem, "LimitLoadToSessionType": "Background",
                                    "EnvironmentVariables": {"HERMES_HOME": str(tmp_path)}}))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0 if argv[2].startswith("gui/") else 113,
                               stdout="", stderr="")
    with pytest.raises(RuntimeError, match="GUI domain"):
        login_bootstrap(tmp_path, path, runner=run)
    assert all(call[1] == "print" for call in calls)


def test_installed_login_script_runs_isolated_without_checkout_imports(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    import hermes_cli.gateway_launchd_login as trigger
    home = tmp_path / "home"
    home.mkdir()
    gateway = tmp_path / "ai.hermes.gateway.plist"
    gateway.write_bytes(plistlib.dumps({"Label": gateway.stem, "LimitLoadToSessionType": "Background",
                                       "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    state = tmp_path / "loaded"
    fake = bin_path / "launchctl"
    fake.write_text(f'#!{sys.executable}\nimport sys\nfrom pathlib import Path\np=Path({str(state)!r})\n'
                    'if sys.argv[1]=="print": sys.exit(0 if p.exists() and sys.argv[2].startswith("user/") else 113)\n'
                    'assert sys.argv[1]=="bootstrap" and sys.argv[2].startswith("user/")\np.touch()\n')
    fake.chmod(0o700)
    result = subprocess.run([sys.executable, "-I", str(Path(trigger.__file__)), "--home", str(home),
                             "--gateway-plist", str(gateway)],
                            cwd=tmp_path, env={**os.environ, "PATH": str(bin_path)}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert state.exists()
    assert result.stdout.count("ai.hermes.gateway: loaded") == 1
