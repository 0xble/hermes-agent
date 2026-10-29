"""Broken unrelated YAML must not block env-only credential changes."""
import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli import config, credential_lifecycle


@pytest.fixture
def credential_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old = "synthetic-old-not-a-secret"
    monkeypatch.setenv("SYNTHETIC_API_KEY", old)
    (tmp_path / ".env").write_text(f"SYNTHETIC_API_KEY={old}\n", encoding="utf-8")
    (tmp_path / "auth.json").write_text(json.dumps({"credential_pool": {"synthetic": [
        {"id": "fixture", "source": "env:SYNTHETIC_API_KEY", "access_token": old},
    ]}}), encoding="utf-8")
    config.invalidate_env_cache()
    return tmp_path, old


@pytest.mark.parametrize("operation", ["rotate", "remove"])
@pytest.mark.parametrize("document,blocked", [
    ("model: [broken\n", False),
    ("timezone: NOT/A_ZONE\n", False),
    ("model: [broken\n# possible mirror: {old}\n", True),
])
def test_dashboard_credential_change_only_refuses_a_possible_broken_mirror(
    credential_home, operation, document, blocked,
):
    from hermes_cli.web_server import _SESSION_TOKEN, app

    home, old = credential_home
    yaml = home / "config.yaml"
    yaml.write_text(document.format(old=old), encoding="utf-8")
    before = {p: p.read_bytes() for p in (yaml, home / ".env", home / "auth.json")}
    headers = {"X-Hermes-Session-Token": _SESSION_TOKEN}
    client = TestClient(app)
    if operation == "rotate":
        response = client.put("/api/env", json={
            "key": "SYNTHETIC_API_KEY", "value": "synthetic-new-not-a-secret",
        }, headers=headers)
    else:
        response = client.request("DELETE", "/api/env", json={
            "key": "SYNTHETIC_API_KEY",
        }, headers=headers)
    assert response.status_code == (400 if blocked else 200), response.text
    assert yaml.read_bytes() == before[yaml]
    if blocked:
        assert "hermes config edit" in response.json()["detail"]
        assert old not in response.text
        assert {p: p.read_bytes() for p in before} == before
        assert config.get_env_value("SYNTHETIC_API_KEY") == old
    elif operation == "rotate":
        assert config.load_env()["SYNTHETIC_API_KEY"] == "synthetic-new-not-a-secret"
    else:
        assert "SYNTHETIC_API_KEY" not in config.load_env()
        assert "synthetic" not in json.loads((home / "auth.json").read_text())["credential_pool"]


@pytest.mark.parametrize("operation", ["rotate", "remove"])
def test_broken_possible_mirror_refusal_is_a_value_error(credential_home, operation):
    home, old = credential_home
    (home / "config.yaml").write_text(f"model: [broken\n# {old}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hermes config edit"):
        if operation == "rotate":
            credential_lifecycle.save_provider_env_credential("SYNTHETIC_API_KEY", "synthetic-new")
        else:
            credential_lifecycle.remove_provider_env_credential("SYNTHETIC_API_KEY")
