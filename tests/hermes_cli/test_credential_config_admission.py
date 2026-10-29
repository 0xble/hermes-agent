"""Config admission failures must precede credential-store mutations."""
import json

import pytest

from hermes_cli import config, credential_lifecycle
from hermes_cli.config_errors import ConfigValidationError


@pytest.mark.parametrize("operation", ["rotate", "remove"])
def test_invalid_mirror_candidate_leaves_all_credential_stores_unchanged(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old = "synthetic-old-not-a-secret"
    env = tmp_path / ".env"
    auth = tmp_path / "auth.json"
    yaml = tmp_path / "config.yaml"
    env.write_text(f"SYNTHETIC_API_KEY={old}\n", encoding="utf-8")
    auth.write_text(json.dumps({"credential_pool": {"synthetic": [
        {"id": "fixture", "source": "env:SYNTHETIC_API_KEY", "access_token": old}
    ]}}), encoding="utf-8")
    yaml.write_text(f"timezone: NOT/A_ZONE\nmodel:\n  api_key: {old}\n", encoding="utf-8")
    config.invalidate_env_cache()
    monkeypatch.setenv("SYNTHETIC_API_KEY", old)
    before = {p: p.read_bytes() for p in (env, auth, yaml)}
    with pytest.raises(ConfigValidationError, match="timezone"):
        if operation == "rotate":
            credential_lifecycle.save_provider_env_credential("SYNTHETIC_API_KEY", "synthetic-new-not-a-secret")
        else:
            credential_lifecycle.remove_provider_env_credential("SYNTHETIC_API_KEY")
    assert {p: p.read_bytes() for p in before} == before
    assert config.get_env_value("SYNTHETIC_API_KEY") == old
