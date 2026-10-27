"""Recovery must leave the running test checkout entirely unprobed."""

from pathlib import Path

import pytest

from hermes_cli import _early_recovery as recovery
from hermes_cli import _early_recovery_zip as zip_recovery


@pytest.mark.parametrize("suite_checkout", [True, False])
def test_pull_recovery_probes_only_disposable_checkouts(tmp_path, monkeypatch, suite_checkout):
    root = recovery._project_root() if suite_checkout else tmp_path
    probes = []

    def marker(candidate):
        probes.append(candidate)
        return tmp_path / "absent-marker"

    monkeypatch.setattr(recovery, "interrupted_pull_marker", marker)
    assert recovery.restore_interrupted_pull(root) is False
    assert probes == ([] if suite_checkout else [root])


@pytest.mark.parametrize("suite_checkout", [True, False])
def test_zip_recovery_probes_only_disposable_checkouts(tmp_path, monkeypatch, suite_checkout):
    root = recovery._project_root() if suite_checkout else tmp_path
    journal = root / recovery.ZIP_SWAP_JOURNAL
    probes = []
    original = Path.is_file

    def is_file(path):
        if path == journal:
            probes.append(path)
            return False
        return original(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert zip_recovery.restore_interrupted_zip_swap(root) is False
    assert probes == ([] if suite_checkout else [journal])
