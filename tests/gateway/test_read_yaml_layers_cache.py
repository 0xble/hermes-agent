"""The gateway and multiplex cron share a raw, signature-cached config read."""

import os
from unittest.mock import patch

import pytest

import utils
from gateway.config_loader import read_yaml_layers


def test_gateway_yaml_cache_reuses_parse_not_expanded_values(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("model: ${GATEWAY_TEST_MODEL}\ntags: !!set {one: null}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setenv("GATEWAY_TEST_MODEL", "first")
    # Stay outside the brief racy-content window throughout this test.
    os.utime(path, (1, 1))
    with patch.object(utils, "fast_safe_load", wraps=utils.fast_safe_load) as parse:
        first = read_yaml_layers(tmp_path)
        assert first["model"] == "first"
        first["tags"].add("caller-only")
        monkeypatch.setenv("GATEWAY_TEST_MODEL", "second")
        assert read_yaml_layers(tmp_path) == {"model": "second", "tags": {"one"}}
        assert parse.call_count == 1

        replacement = tmp_path / "replacement.yaml"
        replacement.write_text("\ufeffmodel: changed\n", encoding="utf-8")
        replacement.replace(path)
        assert read_yaml_layers(tmp_path) == {"model": "changed"}
        assert parse.call_count == 2


def test_gateway_yaml_cache_does_not_mask_read_error(tmp_path):
    from hermes_cli import config_backend

    path = tmp_path / "config.yaml"
    path.write_text("model: original\n", encoding="utf-8")
    assert read_yaml_layers(tmp_path) == {"model": "original"}

    with patch.object(config_backend, "open", create=True, side_effect=PermissionError):
        with pytest.raises(PermissionError):
            read_yaml_layers(tmp_path)

    assert read_yaml_layers(tmp_path) == {"model": "original"}
