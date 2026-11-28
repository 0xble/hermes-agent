"""Local receipts store their frozen config once per distinct config, and readers see it inline."""

import json

import pytest

from hermes_state import SessionDB
from hermes_state_local import CONFIG_PREFIX, POLICY_PREFIX, decode_receipt, encode_receipt, local_receipt
from hermes_state_runtime import RuntimeStoreError, _json

CONFIG = json.dumps({"model": {"default": "m"}, "mcp_servers": {"big": {"command": "x" * 4000}}}, sort_keys=True)


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _receipt(session_id, config=CONFIG):
    return {"session_id": session_id, "route": f"route-{session_id}", "entry": {"session_id": session_id},
            "policy": {"model": "m", "request_json": "{}", "config_json": config}}


def _store(db, key, value):
    db._conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, value))
    db._conn.commit()


def _stored(db, session_id):
    return db._conn.execute("SELECT value FROM state_meta WHERE key=?", (POLICY_PREFIX + session_id,)).fetchone()[0]


def test_identical_configs_are_stored_once_and_read_back_inline(db):
    for sid in ("local-a", "local-b"):
        _store(db, POLICY_PREFIX + sid, encode_receipt(db._conn, _receipt(sid)))

    shared = db._conn.execute("SELECT COUNT(*) FROM state_meta WHERE key GLOB ?", (CONFIG_PREFIX + "*",)).fetchone()[0]
    assert shared == 1
    assert "config_json" not in json.loads(_stored(db, "local-a"))["policy"]
    assert len(_stored(db, "local-a")) < len(CONFIG)
    assert local_receipt(db, "local-a") == _receipt("local-a")
    assert local_receipt(db, "local-b") == _receipt("local-b")


def test_receipt_written_before_the_shared_form_reads_unchanged(db):
    _store(db, POLICY_PREFIX + "local-old", _json(_receipt("local-old")))

    assert local_receipt(db, "local-old") == _receipt("local-old")


def test_a_ref_whose_config_row_is_missing_refuses(db):
    _store(db, POLICY_PREFIX + "local-a", encode_receipt(db._conn, _receipt("local-a")))
    db._conn.execute("DELETE FROM state_meta WHERE key GLOB ?", (CONFIG_PREFIX + "*",))
    db._conn.commit()

    with pytest.raises(RuntimeStoreError) as raised:
        local_receipt(db, "local-a")
    assert raised.value.reason == "storage_unavailable"


def test_raw_read_modify_write_keeps_the_ref(db):
    """Writers that edit a receipt without decoding it (lineage, rekey) store the ref as it was."""
    stored = encode_receipt(db._conn, _receipt("local-a"))
    raw = json.loads(stored)
    raw["lineage"] = ["local-a", "child"]

    rewritten = encode_receipt(db._conn, raw)

    assert json.loads(rewritten)["policy"] == json.loads(stored)["policy"]
    assert decode_receipt(db._conn, rewritten)["policy"]["config_json"] == CONFIG


def test_a_changed_config_gets_its_own_row(db):
    changed = json.dumps({"model": {"default": "other"}}, sort_keys=True)
    _store(db, POLICY_PREFIX + "local-a", encode_receipt(db._conn, _receipt("local-a")))
    _store(db, POLICY_PREFIX + "local-b", encode_receipt(db._conn, _receipt("local-b", changed)))

    assert local_receipt(db, "local-a")["policy"]["config_json"] == CONFIG
    assert local_receipt(db, "local-b")["policy"]["config_json"] == changed
