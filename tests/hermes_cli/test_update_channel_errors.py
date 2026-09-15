"""Every CLI resolver boundary refuses damaged config before update effects."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import config, main, update_cmd, update_cmd_zip
from hermes_cli.main_install_repair import _resolve_update_branch


@pytest.mark.parametrize('surface', ['check', 'update', 'impl', 'zip'])
@pytest.mark.parametrize('bad', [None, [], {'updates': None}, {'updates': []},
    {'updates': {'channel': 'typo'}}, {'updates': {'channel': []}},
    {'updates': {'channel': None}}, OSError('unreadable'), ValueError('bad YAML')])
def test_bad_channel_refuses_before_effects(monkeypatch, capsys, surface, bad):
    def load(path):
        if isinstance(bad, Exception):
            raise bad
        return bad
    monkeypatch.setattr(config, 'read_config_mapping_strict', load)
    monkeypatch.setattr(config, 'is_managed', lambda: False)
    effects = Mock(side_effect=AssertionError('must not perform update effects'))
    monkeypatch.setattr(update_cmd, '_resolve_update_options', effects)
    monkeypatch.setattr(update_cmd, '_begin_update_receipt_and_plan', effects)
    monkeypatch.setattr(main, '_capture_active_tool_dependencies', effects)
    monkeypatch.setattr(update_cmd_zip, '_download_and_swap_zip', effects)
    monkeypatch.setattr(main, '_install_hangup_protection', effects)
    args = SimpleNamespace(branch=None, revision=None, check=surface == 'check')
    calls = {'check': main.cmd_update, 'update': main.cmd_update,
             'impl': lambda a: update_cmd._cmd_update_impl(a, False),
             'zip': update_cmd_zip._update_via_zip}
    with pytest.raises(SystemExit) as error:
        calls[surface](args)
    assert error.value.code == 1
    effects.assert_not_called()
    output = capsys.readouterr()
    assert 'Cannot resolve update channel' in output.out
    assert 'Traceback' not in output.out + output.err


@pytest.mark.parametrize('original', [
    'updates:\n  channel: stable\n  broken: [\n',
    'updates: []\n', 'updates: null\n', 'updates:\n  channel: typo\n',
    'updates:\n  channel: []\n', '- nonmapping-root\n'])
def test_real_malformed_yaml_cannot_fallback_to_main(tmp_path, monkeypatch, capsys, original):
    path = tmp_path / 'config.yaml'
    path.write_text(original, encoding='utf-8')
    monkeypatch.setattr(config, 'get_config_path', lambda: path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit) as error:
        _resolve_update_branch(SimpleNamespace(branch=None))
    assert error.value.code == 1
    assert path.read_text(encoding='utf-8') == original
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    output = capsys.readouterr()
    assert 'Cannot resolve update channel' in output.out
    assert 'Traceback' not in output.out + output.err


@pytest.mark.parametrize('cfg,expected', [({}, 'main'), ({'updates': {}}, 'main'),
    ({'updates': {'channel': 'main'}}, 'main'), ({'updates': {'channel': 'stable'}}, 'stable')])
def test_valid_channels_and_explicit_precedence(monkeypatch, cfg, expected):
    monkeypatch.setattr(config, 'read_config_mapping_strict', lambda path: cfg)
    assert _resolve_update_branch(SimpleNamespace(branch=None)) == expected
    assert _resolve_update_branch(SimpleNamespace(branch='feature')) == 'feature'


@pytest.mark.parametrize("managed", ["updates: []", "updates: null", "updates: {channel: typo}", "updates: [SECRET_SENTINEL"])
def test_managed_damage_is_actionable_without_source_leaks(tmp_path, monkeypatch, capsys, managed):
    from hermes_cli import managed_scope
    user = tmp_path / "user.yaml"
    user.write_text("updates: {channel: stable}", encoding="utf-8")
    directory = tmp_path / "managed"
    directory.mkdir()
    (directory / "config.yaml").write_text(managed, encoding="utf-8")
    monkeypatch.setattr(config, "get_config_path", lambda: user)
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: directory)
    with pytest.raises(SystemExit):
        _resolve_update_branch(SimpleNamespace(branch=None))
    text = capsys.readouterr().out
    assert "managed config.yaml" in text
    assert "SECRET_SENTINEL" not in text
    assert "updates" in text or "YAML" in text


@pytest.mark.parametrize("managed_channel, expected", [("main", "main"), ("stable", "stable")])
def test_broken_unrelated_preset_does_not_gate_channel(tmp_path, monkeypatch, managed_channel, expected):
    from hermes_cli import managed_scope
    user = tmp_path / "user.yaml"
    user.write_text("model: {preset: nonexistent}\nupdates: {channel: '${TEST_CHANNEL}'}", encoding="utf-8")
    directory = tmp_path / "managed"
    directory.mkdir()
    (directory / "config.yaml").write_text("model: {preset: also_missing}\nupdates: {channel: " + managed_channel + "}", encoding="utf-8")
    monkeypatch.setenv("TEST_CHANNEL", "stable")
    monkeypatch.setattr(config, "get_config_path", lambda: user)
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: directory)
    assert _resolve_update_branch(SimpleNamespace(branch=None)) == expected
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: None)
    assert _resolve_update_branch(SimpleNamespace(branch=None)) == "stable"


@pytest.mark.parametrize("code", ["docker", "apt"])
def test_image_package_refusal_precedes_invalid_channel(monkeypatch, code):
    from hermes_cli import update_contract
    refusal = update_contract.UpdateRefusal(code, "external update required", "external-command")
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda root: refusal)
    monkeypatch.setattr(update_contract, "record_refusal_receipt", lambda r: None)
    def forbidden(*args):
        raise AssertionError("channel must not be read before install admission")
    monkeypatch.setattr(config, "read_config_mapping_strict", forbidden)
    with pytest.raises(SystemExit) as exc:
        main.cmd_update(SimpleNamespace(branch=None, revision=None, check=True))
    assert exc.value.code == 2


@pytest.mark.parametrize("parsed", [True, False])
def test_revision_check_refuses_before_channel_or_update_effects(monkeypatch, capsys, parsed):
    import argparse
    from hermes_cli.subcommands.update import build_update_parser
    if parsed:
        parser = argparse.ArgumentParser()
        build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
        args = parser.parse_args(["update", "--revision", "a" * 40, "--check"])
    else:
        args = SimpleNamespace(revision="a" * 40, check=True)
    effects = Mock(side_effect=AssertionError("must refuse before update effects"))
    monkeypatch.setattr(main, "_resolve_update_branch", effects)
    monkeypatch.setattr(main, "_install_hangup_protection", effects)
    monkeypatch.setattr(update_cmd, "_cmd_update_check", effects)
    monkeypatch.setattr(config, "is_managed", lambda: False)
    from hermes_cli import update_contract
    monkeypatch.setattr(update_contract, "evaluate_update_admission", effects)
    with pytest.raises(SystemExit) as error:
        main.cmd_update(args)
    assert error.value.code == 2
    assert "--revision cannot be combined with --check" in capsys.readouterr().out
    effects.assert_not_called()
