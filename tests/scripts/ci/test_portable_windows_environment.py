"""The hermetic native Windows environment distinguishes missing and empty module paths."""

import pytest

from scripts.ci import portable


pytestmark = pytest.mark.platforms("windows")


@pytest.mark.parametrize("module_path", [None, "", r"C:\Runner Modules;C:\Second Module Directory"])
def test_native_windows_environment_preserves_or_defaults_module_path(tmp_path, monkeypatch, module_path):
    if module_path is None:
        monkeypatch.delenv("PSModulePath", raising=False)
    else:
        monkeypatch.setenv("PSModulePath", module_path)

    env = portable.environment(tmp_path / "isolated")

    assert env["PSModulePath"] == (module_path if module_path is not None else "")
