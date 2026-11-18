"""The macOS gate must reject unsuitable scratch before doing any setup."""
from contextlib import nullcontext
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from scripts.ci import portable as ci


@pytest.mark.platforms("macos")
def test_case_insensitive_scratch_stops_gate_before_setup_or_lanes(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setattr(ci, 'STATE', tmp_path)
    monkeypatch.setattr(ci, 'checkout_lock', nullcontext)
    monkeypatch.setattr(ci, 'source_unchanged', nullcontext)
    monkeypatch.setattr(ci, 'assert_exact_checkout', Mock())
    environment = Mock(return_value={})
    setup = Mock()
    lanes = Mock(return_value=True)
    monkeypatch.setattr(ci, 'environment', environment)
    monkeypatch.setattr(ci, 'resolve_local_toolchain', Mock(return_value={}))
    monkeypatch.setattr(ci, 'setup', setup)
    monkeypatch.setattr(ci, 'aggregate', lanes)
    monkeypatch.setattr(sys, 'argv', ['bin/ci', 'gate', 'a' * 40])
    # Model a case-folding filesystem at its lookup boundary, irrespective of
    # whether this regression itself runs on a case-sensitive APFS image.
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: exists(path.with_name('a'))
                        if path.name == 'A' else exists(path))

    with pytest.raises(RuntimeError, match='run with TMPDIR on a case-sensitive volume') as failure:
        ci.main()
    assert 'scripts/ci/case-sensitive-tmpdir.sh' in str(failure.value)
    environment.assert_not_called()
    setup.assert_not_called()
    lanes.assert_not_called()


@pytest.mark.platforms("macos")
def test_case_sensitive_scratch_reaches_setup_and_preserves_child_volume(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setattr(ci, 'STATE', tmp_path)
    monkeypatch.setattr(ci, 'checkout_lock', nullcontext)
    monkeypatch.setattr(ci, 'source_unchanged', nullcontext)
    # Model distinct case-only names without pretending to be another OS.
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: False if path.name == 'A' else exists(path))
    seen = []

    def environment(home):
        seen.append(home)
        assert home.parent == tmp_path.resolve()
        assert not (home / 'a').exists()
        assert not (home / 'A').exists()
        return {'TMPDIR': str(home / 'tmp')}

    monkeypatch.setattr(ci, 'environment', environment)
    monkeypatch.setattr(ci, 'resolve_local_toolchain', lambda env: env)
    setup = Mock()
    monkeypatch.setattr(ci, 'setup', setup)
    monkeypatch.setattr(sys, 'argv', ['bin/ci', 'setup'])

    assert ci.main() == 0
    setup.assert_called_once_with({'TMPDIR': str(seen[0] / 'tmp')})
    assert not seen[0].exists()
