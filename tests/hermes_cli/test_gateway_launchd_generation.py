from pathlib import Path
import plistlib

import pytest

from hermes_cli.gateway_launchd_generation import (
    generation_launchd_label,
    render_generation_launchd_plist,
)


def test_generation_labels_use_existing_suffix_convention():
    assert generation_launchd_label("a") == "ai.hermes.gateway-a"
    assert generation_launchd_label("B") == "ai.hermes.gateway-b"
    with pytest.raises(ValueError):
        generation_launchd_label("c")


def test_generation_plist_pins_release_and_starts_standby(tmp_path: Path):
    release = tmp_path / "releases" / "deadbeef"
    interpreter = release / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    raw = render_generation_launchd_plist(
        slot="a", release_sha="deadbeef", release_root=release,
        interpreter=interpreter, hermes_home=tmp_path / "home",
    )
    plist = plistlib.loads(raw.encode())
    assert plist["Label"] == "ai.hermes.gateway-a"
    assert plist["WorkingDirectory"] == str(release)
    assert plist["EnvironmentVariables"]["PYTHONPATH"] == str(release)
    assert plist["ProgramArguments"][-1] == "--standby"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
