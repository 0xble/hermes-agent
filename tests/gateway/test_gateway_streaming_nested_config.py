"""Regression test for #25676 — nested gateway.streaming config must be loaded."""
import pytest
import yaml



@pytest.fixture
def load_with_yaml_dict(tmp_path, monkeypatch):
    """Exercise the effective-config loader against a real isolated config file."""
    from gateway.config import load_gateway_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)
    def load(yaml_dict):
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(yaml_dict), encoding="utf-8")
        return load_gateway_config()
    return load


class TestStreamingConfigNested:
    def test_top_level_streaming(self, load_with_yaml_dict):
        cfg = load_with_yaml_dict({"streaming": {"enabled": True, "transport": "draft"}})
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "draft"


    def test_top_level_takes_precedence(self, load_with_yaml_dict):
        cfg = load_with_yaml_dict({
            "streaming": {"enabled": True, "transport": "edit"},
            "gateway": {"streaming": {"enabled": False, "transport": "draft"}},
        })
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "edit"

    def test_nested_streaming_when_top_level_absent(self, load_with_yaml_dict):
        cfg = load_with_yaml_dict({"gateway": {"streaming": {"enabled": True, "transport": "draft"}}})
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "draft"

    def test_empty_top_level_does_not_enable_nested_streaming(self, load_with_yaml_dict):
        cfg = load_with_yaml_dict({"streaming": {}, "gateway": {"streaming": {"enabled": True}}})
        assert cfg.streaming.enabled is False


class TestStreamingModeAlias:
    """``streaming: {mode: ...}`` is an alias that also implies ``enabled``.

    Regression for a live config footgun: ``streaming: {mode: auto}`` was
    silently ignored (mode was never read), so streaming stayed disabled and
    the whole reply buffered before the first Telegram send.
    """

    def test_mode_auto_enables_streaming(self):
        from gateway.config import StreamingConfig

        sc = StreamingConfig.from_dict({"mode": "auto"})
        assert sc.enabled is True
        assert sc.transport == "auto"

    def test_mode_edit_enables_streaming(self):
        from gateway.config import StreamingConfig

        sc = StreamingConfig.from_dict({"mode": "edit"})
        assert sc.enabled is True
        assert sc.transport == "edit"



    def test_explicit_enabled_overrides_mode(self):
        from gateway.config import StreamingConfig

        sc = StreamingConfig.from_dict({"mode": "auto", "enabled": False})
        assert sc.enabled is False
        # transport still resolves from mode
        assert sc.transport == "auto"


    def test_empty_block_stays_disabled(self):
        from gateway.config import StreamingConfig

        sc = StreamingConfig.from_dict({})
        assert sc.enabled is False



class TestStreamingYamlBooleanQuirk:
    """YAML 1.1 parses bare ``off``/``on`` as booleans; ``mode``/``transport``
    must normalize those back to canonical string tokens.

    Regression for the review on PR #62873: bare ``mode: off`` arrived as
    Python ``False`` and stringified to ``"false"``, which is not ``"off"``,
    so streaming was enabled instead of honoring the advertised disable.
    """

    def test_mode_bare_off_boolean_disables(self):
        from gateway.config import StreamingConfig

        # yaml.safe_load("off") -> False
        sc = StreamingConfig.from_dict({"mode": False})
        assert sc.enabled is False
        assert sc.transport == "off"

    def test_mode_bare_on_boolean_enables(self):
        from gateway.config import StreamingConfig

        # yaml.safe_load("on") -> True
        sc = StreamingConfig.from_dict({"mode": True})
        assert sc.enabled is True
        assert sc.transport == "auto"


    def test_loader_normalizes_bare_yaml_off(self, load_with_yaml_dict):
        """End-to-end through load_gateway_config(): unquoted ``mode: off``
        (a YAML boolean) must keep streaming disabled."""
        cfg = load_with_yaml_dict({"streaming": {"mode": False}})
        assert cfg.streaming.enabled is False
        assert cfg.streaming.transport == "off"

