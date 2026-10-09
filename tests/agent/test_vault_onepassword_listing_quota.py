"""1Password request quota: how many ``op`` processes one vault list or fill spends.

The CLI's per-account daily request quota was being drained by repeated
browser_vault_list calls and by fills that listed the account twice (get_meta's
origin authorization, then _locate's vault selector). These tests count real
subprocess invocations of an offline fake ``op``.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_backends import onepassword
from agent.vault_backends.base import backend_for_handle

_FAKE_OP = r'''
import json
import os
import sys
import time
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
account = {"dummy-personal-token": "personal", "dummy-business-token": "business"}.get(token)
with (root / "audit.jsonl").open("a") as stream:
    stream.write(json.dumps({"argv": args, "account": account}) + "\n")
if (root / "fail").exists() or account is None:
    print("synthetic failure", file=sys.stderr)
    sys.exit(1)
delay = root / "delay"
if delay.exists():
    time.sleep(float(delay.read_text()))
item = {"personal": ("item-p", "vault-p", "https://personal.example/login"),
        "business": ("item-b", "vault-b", "https://business.example/login")}[account]
if account == "personal" and (root / "new-item").exists():
    item = ("item-new", "vault-new", "https://new.personal.example/login")
if account == "personal" and (root / "fresh-url").exists():
    item = ("item-p", "vault-p", "https://fresh.personal.example/login")
if args == ["item", "list", "--categories", "Login,Credit Card", "--format", "json"]:
    print(json.dumps([{"id": item[0], "title": account, "category": "LOGIN", "vault": {"id": item[1]},
                       "urls": [{"href": item[2]}], "additional_information": account + "@example.com"}]))
elif args == ["item", "get", item[0], "--format", "json"]:
    print(json.dumps({"id": item[0], "title": account, "category": "LOGIN", "state": "ACTIVE",
                      "vault": {"id": item[1]}, "urls": [{"href": item[2]}],
                      "additional_information": account + "@example.com",
                      "fields": [{"id": "password", "value": "must-not-be-retained"}]}))
elif args == ["item", "get", item[0], "--vault", item[1], "--fields", "label=password", "--reveal"]:
    print("dummy-" + account + "-password")
else:
    print("unknown item", file=sys.stderr)
    sys.exit(2)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("OP_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "dummy-personal-token")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "dummy-business-token")
    op = tmp_path / "op"
    op.write_text(f"#!{sys.executable}\n" + _FAKE_OP)
    op.chmod(0o700)
    home.joinpath("config.yaml").write_text(
        "vault:\n  onepassword:\n"
        f"    binary_path: {op}\n"
        "    accounts:\n"
        "      - alias: business\n"
        "        account: business.example.com\n"
        "        service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN_BUSINESS\n")
    audit = tmp_path / "audit.jsonl"

    def calls(kind=None):
        rows = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        return [r for r in rows if kind is None or r["argv"][:2] == ["item", kind]]
    return op, calls


@pytest.fixture
def clock(monkeypatch):
    """Monotonic time the listing caches read, advanced by hand."""
    now = [1000.0]
    monkeypatch.setattr(onepassword.time, "monotonic", lambda: now[0])
    return now


def _fill(handle, origin):
    from tools import browser_vault_tool
    controls = [
        {"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "user", "type": "text"},
        {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
    ]
    with patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
            patch.object(browser_vault_tool, "_current_page_origin", return_value=origin), \
            patch.object(browser_vault_tool, "_eval_js",
                         return_value={"success": True, "result": json.dumps(controls)}), \
            patch.object(browser_vault_tool, "_eval_js_secret",
                         return_value={"success": True, "result": json.dumps({"filled": 1})}):
        return json.loads(browser_vault_tool.browser_vault_fill(handle, task_id="quota"))


def test_one_vault_list_over_two_accounts_lists_each_once_then_reuses(env, clock):
    from tools.browser_vault_tool import browser_vault_list
    _op, calls = env
    listed = json.loads(browser_vault_list())
    assert {i["handle"] for i in listed.get("items", [])} >= {"op:item-p", "op@business:item-b"}, listed
    assert sorted(c["account"] for c in calls("list")) == ["business", "personal"]
    clock[0] += 5 * 60  # (a) five minutes later, still inside the display TTL
    browser_vault_list()
    assert len(calls()) == 2


def test_one_fill_reads_fresh_item_metadata_without_listing(env):
    _op, calls = env
    out = _fill("op:item-p", "https://personal.example")
    assert out["success"] is True and "dummy-personal-password" not in json.dumps(out)
    assert [c["argv"][:2] for c in calls()] == [["item", "get"], ["item", "get"]]
    assert calls()[0]["argv"] == ["item", "get", "item-p", "--format", "json"]


def test_stale_display_url_cannot_authorize_fill(env):
    from tools.browser_vault_tool import browser_vault_list
    op, calls = env
    backend = backend_for_handle("op:item-p")
    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        browser_vault_list()
        op.with_name("fresh-url").write_text("")
        assert backend.get_meta("op:item-p").origin == "https://fresh.personal.example"
    assert [row["argv"][:2] for row in calls()] == [["item", "list"], ["item", "get"]]


def test_origin_filtered_cache_miss_refetches_once_and_finds_new_item(env):
    from tools.browser_vault_tool import browser_vault_list
    op, calls = env
    backend = backend_for_handle("op:item-p")
    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        assert json.loads(browser_vault_list())["items"][0]["handle"] == "op:item-p"
        op.with_name("new-item").write_text("")
        listed = json.loads(browser_vault_list(origin="https://new.personal.example"))
    assert [item["handle"] for item in listed["items"]] == ["op:item-new"]
    assert len(calls("list")) == 2


def test_origin_filtered_cache_hit_makes_no_extra_call(env):
    from tools.browser_vault_tool import browser_vault_list
    _op, calls = env
    backend = backend_for_handle("op:item-p")
    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        browser_vault_list()
        listed = json.loads(browser_vault_list(origin="https://personal.example"))
    assert [item["handle"] for item in listed["items"]] == ["op:item-p"]
    assert len(calls("list")) == 1


def test_unfiltered_list_makes_no_extra_call(env):
    from tools.browser_vault_tool import browser_vault_list
    _op, calls = env
    backend = backend_for_handle("op:item-p")
    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        browser_vault_list()
        browser_vault_list()
    assert len(calls("list")) == 1


def test_concurrent_display_listings_share_one_op_call(env):
    op, calls = env
    op.with_name("delay").write_text("0.5")
    backend = backend_for_handle("op:item-p")
    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append([m.id for m in backend.list_items()])
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [["op:item-p"]] * 4
    assert len(calls("list")) == 1  # (b)


def test_concurrent_item_metadata_reads_share_one_op_call(env):
    op, calls = env
    op.with_name("delay").write_text("0.5")
    backend = backend_for_handle("op:item-p")
    barrier = threading.Barrier(3)
    results = []

    def worker():
        barrier.wait()
        results.append(backend.get_meta("op:item-p").origin)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == ["https://personal.example"] * 3
    assert len(calls("list")) == 0
    assert len([row for row in calls() if row["argv"][:2] == ["item", "get"]]) == 1


def test_fill_consumes_authorized_metadata_for_one_secret_read(env):
    _op, calls = env
    backend = backend_for_handle("op:item-p")
    backend.list_items()  # A display listing never authorizes a fill.
    assert backend.get_meta("op:item-p") is not None
    backend.resolve_password("op:item-p")
    assert len(calls("list")) == 1
    assert [row["argv"][:2] for row in calls() if row["argv"][:2] == ["item", "get"]] == [
        ["item", "get"], ["item", "get"]
    ]
    # A later fill gets fresh metadata again rather than reusing a prior authorization.
    backend.resolve_password("op:item-p")
    assert len([row for row in calls() if row["argv"][:2] == ["item", "get"]]) == 4


def test_failed_listing_is_never_cached(env, clock):
    op, calls = env
    backend = backend_for_handle("op:item-p")
    op.with_name("fail").write_text("")
    for call in (backend.list_items, lambda: backend.get_meta("op:item-p")):
        with pytest.raises(RuntimeError):
            call()
    op.with_name("fail").unlink()
    assert [m.id for m in backend.list_items()] == ["op:item-p"]  # (e)
    assert backend.get_meta("op:item-p") is not None
    assert len(calls("list")) == 2


def test_accounts_and_credentials_never_share_listings(env, monkeypatch):
    _op, calls = env
    personal, business = backend_for_handle("op:item-p"), backend_for_handle("op@business:item-b")
    assert [m.id for m in personal.list_items()] == ["op:item-p"]
    assert [m.id for m in business.list_items()] == ["op@business:item-b"]  # (f) other account
    assert personal.get_meta("op:item-p") and business.get_meta("op@business:item-b")
    assert len(calls("list")) == 2
    # A rotated token is a new identity even for the same backend and account.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "dummy-personal-token")
    rotated = backend_for_handle("op@business:item-b")
    assert [m.id for m in rotated.list_items()] == ["op@business:item-p"]
    assert rotated.get_meta("op@business:item-p") is not None
    assert len(calls("list")) == 3


def test_invalidation_forgets_listings_and_drops_a_fetch_in_flight(env):
    op, calls = env
    backend = backend_for_handle("op:item-p")
    backend.list_items()
    onepassword.invalidate_listing_cache()
    backend.list_items()
    assert len(calls("list")) == 2
    op.with_name("delay").write_text("0.5")
    onepassword.invalidate_listing_cache()
    worker = threading.Thread(target=backend.list_items)
    worker.start()
    deadline = time.monotonic() + 5
    while len(calls("list")) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    onepassword.invalidate_listing_cache()  # an item changed while that listing was being fetched
    worker.join(10)
    backend.list_items()
    assert len(calls("list")) == 4
