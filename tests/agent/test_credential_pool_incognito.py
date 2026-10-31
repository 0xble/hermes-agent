"""A private turn's provider error text must not reach auth.json."""
import json
import secrets

import pytest

from agent.incognito import incognito_scope
from tests.agent.test_credential_pool import _load_two_ok_pool


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("path", ["exhausted", "terminal_refresh"])
def test_private_provider_error_text_stays_out_of_the_auth_store(tmp_path, monkeypatch, private, path):
    pool = _load_two_ok_pool(tmp_path, monkeypatch)
    entry = pool.select()
    canary = "canary-" + secrets.token_hex(12)
    with incognito_scope(private, "private-session"):
        if path == "exhausted":
            pool.mark_exhausted_and_rotate(status_code=429, credential_id=entry.id, error_context={
                "message": f"Rate limited while reading: {canary}", "reason": f"echo {canary}"})
        else:
            pool._mark_dead_refresh_grant(entry, RuntimeError(f"refresh echoed {canary}"))
            pool._persist()
    disk = (tmp_path / "hermes" / "auth.json").read_text()
    saved = next(row for row in json.loads(disk)["credential_pool"]["anthropic"] if row["id"] == entry.id)
    in_ram = next(row for row in pool.entries() if row.id == entry.id)
    if private:
        assert canary not in disk
        assert saved["last_error_message"] is None and in_ram.last_error_message is None
        assert saved["last_status"] in {"exhausted", "dead"}
    else:
        assert canary in saved["last_error_message"]
