"""Script/installer tests own a machine home in addition to HERMES_HOME."""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_installer_machine_home(tmp_path_factory, monkeypatch):
    # Separate from tmp_path: tests that inspect their fixture directory must not see
    # the harness's home. Shell/native children inherit the same destination.
    home = tmp_path_factory.mktemp("installer-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    if os.name == "nt":
        for name, directory in (("LOCALAPPDATA", "Local"), ("APPDATA", "Roaming")):
            target = home / "AppData" / directory
            target.mkdir(parents=True)
            monkeypatch.setenv(name, str(target))
    else:
        # launchd deliberately consults pwd before HOME so profile pinning cannot move
        # a real user's service. Keep that production resolver intact inside the fixture.
        import pwd

        getpwuid = pwd.getpwuid
        uid = os.getuid()  # windows-footgun: ok — POSIX-only branch

        def isolated_account(account_uid):
            record = getpwuid(account_uid)
            if account_uid == uid:
                record = pwd.struct_passwd((*record[:5], str(home), record[6]))
            return record

        monkeypatch.setattr(pwd, "getpwuid", isolated_account)
