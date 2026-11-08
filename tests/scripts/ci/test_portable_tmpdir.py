"""An explicitly requested temporary volume must never silently fall back."""
from contextlib import nullcontext
import sys
import tempfile
from unittest.mock import Mock

import pytest

from scripts.ci import portable as ci


@pytest.mark.parametrize('invalid', ['missing', 'file', 'unwritable'])
def test_invalid_tmpdir_stops_gate_before_setup_or_lanes(tmp_path, monkeypatch, invalid):
    requested = tmp_path / invalid
    if invalid == 'file':
        requested.touch()
    elif invalid == 'unwritable':
        requested.mkdir()
        # Exercise a real creation failure without depending on host ACLs or root.
        monkeypatch.setattr(tempfile, 'TemporaryFile', Mock(side_effect=PermissionError('denied')))
    monkeypatch.setenv('TMPDIR', str(requested))
    monkeypatch.setattr(ci, 'STATE', tmp_path)
    monkeypatch.setattr(ci, 'checkout_lock', nullcontext)
    monkeypatch.setattr(ci, 'source_unchanged', nullcontext)
    monkeypatch.setattr(ci, 'assert_exact_checkout', Mock())
    monkeypatch.setattr(ci, 'environment', Mock(return_value={}))
    monkeypatch.setattr(ci, 'resolve_local_toolchain', Mock(return_value={}))
    setup = Mock()
    lanes = Mock(return_value=True)
    monkeypatch.setattr(ci, 'setup', setup)
    monkeypatch.setattr(ci, 'aggregate', lanes)
    monkeypatch.setattr(sys, 'argv', ['bin/ci', 'gate', 'a' * 40])

    with pytest.raises(RuntimeError, match='Requested TMPDIR') as failure:
        ci.main()
    assert str(requested) in str(failure.value)
    setup.assert_not_called()
    lanes.assert_not_called()


@pytest.mark.parametrize('setting', ['existing', 'unset', 'empty'])
def test_temporary_parent_honors_request_or_stdlib_default(tmp_path, monkeypatch, setting):
    requested = tmp_path / 'requested'
    requested.mkdir()
    default = tmp_path / 'default'
    default.mkdir()
    gettempdir = Mock(return_value=str(default))
    monkeypatch.setattr(tempfile, 'gettempdir', gettempdir)
    if setting == 'unset':
        monkeypatch.delenv('TMPDIR', raising=False)
    else:
        monkeypatch.setenv('TMPDIR', str(requested) if setting == 'existing' else '')

    with ci.external_temporary_directory('hermes-ci-home-') as home:
        assert home.is_dir()
        assert home.parent == (requested if setting == 'existing' else default).resolve()
    assert not home.exists()
    assert gettempdir.call_count == (0 if setting == 'existing' else 1)
