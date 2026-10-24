"""Camofox tab reuse across agent turns (fork patch ``camofox-tab-reuse``).

End-of-turn cleanup used to drop a managed task's tab binding, so every turn opened a new tab,
including right after ``browser_handoff`` where the user had just logged into the visible tab.
These run through the real browser tool dispatch, the real end-of-turn cleanup and the real
compression handoff against a stub Camofox HTTP server, with a temporary ``HERMES_HOME``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

SHARED = "__shared_identity__"


class _Camofox:
    """Minimal Camofox REST stub: tab registry per userId, newest-last listing."""

    def __init__(self):
        self.tabs: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.created = 0
        self.gone: set[str] = set()
        self.open_tab_ids: list[str] = []
        self.identities_status = 200
        self.identity_barrier = None
        self.identities = [{"alias": alias, "userId": "hermes_camofox_" + hashlib.sha256(alias.encode()).hexdigest()[:24]}
                           for alias in ("brianle", "lpg", "meridian", "mgf")]
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                stub.calls.append(("GET", parsed.path, query))
                if parsed.path == "/browser/identities":
                    barrier = stub.identity_barrier
                    stub.identity_barrier = None
                    if barrier is not None:
                        barrier[0].set()
                        assert barrier[1].wait(15), "registry response was not released"
                    return self._send({"identities": stub.identities}, stub.identities_status)
                if parsed.path == "/tabs":
                    return self._send({"running": True, "tabs": stub.tabs.get(query.get("userId"), [])})
                tab_id = parsed.path.split("/")[2] if parsed.path.startswith("/tabs/") else ""
                if tab_id in stub.gone:
                    return self._send({"code": "tab_not_found"}, 404)
                if parsed.path.endswith("/snapshot"):
                    return self._send({"snapshot": f"- heading {tab_id} [e1]", "refsCount": 1})
                return self._send({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                parsed = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                stub.calls.append(("POST", parsed.path, body))
                if parsed.path == "/tabs":
                    stub.created += 1
                    tab = {"tabId": f"created-{stub.created}", "url": "about:blank", "listItemId": body["listItemId"]}
                    stub.tabs.setdefault(body["userId"], []).append(tab)
                    return self._send({"tabId": tab["tabId"], "url": tab["url"]})
                if parsed.path.startswith("/browser/identities/") and parsed.path.endswith("/open"):
                    user_id = parsed.path.split("/")[3]
                    tab_id = stub.open_tab_ids.pop(0) if stub.open_tab_ids else "visible-tab"
                    tab = {"tabId": tab_id, "url": "https://login.example.test/", "listItemId": SHARED}
                    stub.tabs.setdefault(user_id, []).append(tab)
                    return self._send({"ok": True, "focused": True, "restarted": True, **tab})
                if parsed.path.startswith("/browser/identities/") and parsed.path.endswith("/release"):
                    return self._send({"ok": True, "released": True})
                tab_id = parsed.path.split("/")[2] if parsed.path.startswith("/tabs/") else ""
                if tab_id in stub.gone:
                    return self._send({"code": "tab_not_found"}, 404)
                if parsed.path.endswith("/navigate"):
                    for tabs in stub.tabs.values():
                        for tab in tabs:
                            if tab["tabId"] == tab_id:
                                tab["url"] = body["url"]
                    return self._send({"url": body["url"], "title": "Example"})
                if parsed.path.endswith(("/click", "/type", "/press")):
                    return self._send({"ok": True})
                return self._send({"error": "not found"}, 404)

            def do_DELETE(self):  # noqa: N802
                parsed = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                stub.calls.append(("DELETE", parsed.path, query))
                if parsed.path.startswith("/tabs/"):
                    tab_id = parsed.path.split("/")[2]
                    stub.tabs[query.get("userId")] = [
                        t for t in stub.tabs.get(query.get("userId"), []) if t["tabId"] != tab_id]
                elif parsed.path.startswith("/sessions/"):
                    stub.tabs.pop(parsed.path.split("/")[2], None)
                return self._send({"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def seed(self, user_id, *tabs):
        self.tabs.setdefault(user_id, []).extend(dict(tab) for tab in tabs)

    def tab_posts(self):
        return [path for method, path, _ in self.calls if method == "POST" and path == "/tabs"]

    def acted_on(self):
        return [path.split("/")[2] for method, path, _ in self.calls
                if path.startswith("/tabs/") and method == "POST"]


@pytest.fixture
def camofox(tmp_path, monkeypatch):
    """Managed-persistence Camofox profile in a temporary HERMES_HOME, real modules."""
    from tools import browser_camofox as cf, browser_tool as bt, browser_tool_eval_policy as policy

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "browser:\n  cloud_provider: camofox\n  camofox:\n    managed_persistence: true\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools import browser_camofox_leases as leases
    monkeypatch.setattr(leases, "_lease_root", lambda: home / "test-tab-leases")
    server = _Camofox()
    server.thread.start()
    monkeypatch.setenv("CAMOFOX_URL", server.url)
    monkeypatch.setattr(cf, "get_vnc_url", lambda: None)
    monkeypatch.setattr(policy, "_eval_ssrf_guard_active", lambda task_id: False)
    monkeypatch.setattr(bt, "_url_policy_error", lambda url, auto_local=False: None)

    def reset():
        for server_url, user_id, tab_id in list(leases._handles):
            leases.release(server_url, user_id, tab_id)
        with cf._sessions_lock:
            cf._sessions.clear()
            getattr(cf, "_stale_tab_ids", {}).clear()
            getattr(cf, "_owned_tabs", {}).clear()
            cf._closing_tabs.clear()
        cf._protected_tab_ids.clear()
        cf._protected_tab_owners.clear()

    reset()
    yield SimpleNamespace(server=server, cf=cf, bt=bt, home=home)
    reset()
    server.server.shutdown()
    server.server.server_close()


def _dispatch(name, args, task_id):
    from tools.registry import registry
    raw = registry.dispatch(name, args, task_id=task_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_named_accounts_reuse_existing_profiles_across_hosts(camofox, tmp_path, monkeypatch, request):
    """The server registry, not the client's absolute home, selects each persistent profile."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    c = camofox
    aliases = ("brianle", "lpg", "meridian", "mgf")
    mac_home = tmp_path / "mac" / ".hermes"
    canonical = {alias: "hermes_camofox_" + hashlib.sha256(
        f"camofox-account:{mac_home / 'browser_auth' / 'camofox'}:{alias}".encode()
    ).hexdigest()[:24] for alias in aliases}
    c.server.identities = [{"alias": alias, "userId": user_id} for alias, user_id in canonical.items()]
    for alias, user_id in canonical.items():
        c.server.seed(user_id, {"tabId": f"existing-{alias}", "url": "https://example.com",
                                "listItemId": SHARED})
    for host, home in (("mac", mac_home), ("linux", tmp_path / "linux" / "home" / "hermes" / ".hermes")):
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "browser:\n  cloud_provider: camofox\n  camofox:\n    accounts: [brianle, lpg, meridian, mgf]\n",
            encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        token = set_hermes_home_override(home)
        request.addfinalizer(lambda token=token: reset_hermes_home_override(token))
        for alias in aliases:
            task = f"{host}-{alias}"
            result = _dispatch("browser_navigate", {"url": "https://example.com", "account": alias}, task)
            assert result["success"], result
            assert c.cf._sessions[task]["user_id"] == canonical[alias]
            assert c.cf._sessions[task]["tab_id"] == f"existing-{alias}"
            assert canonical[alias] not in json.dumps(result)
            c.cf.release_task_bindings([task])
    assert not c.server.tab_posts(), "Selecting an existing account must not create a replacement profile/tab"


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("metadata,status", [
    ([], 200),
    (None, 200),
    ({}, 200),
    ([{"alias": "brianle", "userId": "ephemeral"}], 200),
    ([{"alias": "brianle", "userId": None}], 200),
    ([{"alias": "brianle", "userId": "hermes_camofox_" + "a" * 24}] * 2, 200),
    ([{"alias": "brianle", "userId": "hermes_camofox_" + "b" * 24}], 503),
])
def test_named_accounts_fail_closed_without_persistent_mapping(camofox, metadata, status, bound):
    c = camofox
    tasks = ("unmapped-browser_navigate", "unmapped-browser_handoff")
    if bound:
        for task in tasks:
            assert _dispatch("browser_navigate", {"url": "https://example.com", "account": "brianle"}, task)["success"]
        c.server.calls.clear()
    c.server.identities = metadata
    c.server.identities_status = status
    for tool, args in (("browser_navigate", {"url": "https://example.com", "account": "brianle"}),
                       ("browser_handoff", {"account": "brianle"})):
        result = _dispatch(tool, args, f"unmapped-{tool}")
        assert result["success"] is False
        assert "persistent" in result["error"].lower()
        assert "brianle" in result["error"]
    if bound:
        result = _dispatch("browser_snapshot", {}, tasks[0])
        assert result["success"] is False
        assert "persistent" in result["error"].lower()
    assert c.server.calls
    assert all(method == "GET" and path == "/browser/identities" for method, path, _ in c.server.calls)
    assert bool(c.cf._sessions) == bound


def _end_turn(task_id, *, headed=False):
    """The real per-turn cleanup every agent turn runs (headless mode)."""
    from agent.chat_completion_helpers import cleanup_task_resources
    with patch("tools.browser_tool_cloud._is_headed_mode", return_value=headed), \
         patch("agent.chat_completion_helpers.is_persistent_env", return_value=False), \
         patch("run_agent.cleanup_vm"):
        cleanup_task_resources(SimpleNamespace(verbose_logging=False), task_id)


def _user_id(cf, task_id):
    return cf._sessions[task_id]["user_id"]


@pytest.mark.parametrize("headed", [False, True])
def test_owned_tab_closes_at_end_of_turn(camofox, headed):
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/a"}, "chat")["success"]
    first_tab = camofox.cf._sessions["chat"]["tab_id"]
    _end_turn("chat", headed=headed)

    # Disposable owned tabs do not survive the owning turn.
    assert not _dispatch("browser_snapshot", {}, "chat")["success"]
    assert camofox.server.tab_posts() == ["/tabs"]
    assert all(tab["tabId"] != first_tab for tabs in camofox.server.tabs.values() for tab in tabs)
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/b"}, "chat")["success"]
    assert camofox.server.tab_posts() == ["/tabs", "/tabs"]

@pytest.mark.parametrize("managed_persistence", [False, True])
def test_handoff_tab_persists_across_turns(camofox, managed_persistence):
    (camofox.home / "config.yaml").write_text(
        "browser:\n  cloud_provider: camofox\n  camofox:\n"
        f"    managed_persistence: {str(managed_persistence).lower()}\n", encoding="utf-8")
    handoff = _dispatch("browser_handoff", {"account": "brianle"}, "chat")
    assert handoff["success"] and handoff["tabId"] == "visible-tab"
    user_id = _user_id(camofox.cf, "chat")
    _end_turn("chat")  # the user logs in on the visible tab between turns

    assert _dispatch("browser_snapshot", {}, "chat")["success"]
    result = _dispatch("browser_navigate", {"url": "https://login.example.test/account"}, "chat")
    assert result["success"] and result["account"] == "brianle"
    assert _user_id(camofox.cf, "chat") == user_id
    assert camofox.cf._sessions["chat"]["tab_id"] == handoff["tabId"]
    assert camofox.server.tab_posts() == []
    assert camofox.server.acted_on() == ["visible-tab"]
    assert not any(method == "DELETE" for method, _, _ in camofox.server.calls)


def test_handoff_sends_prior_binding_and_keeps_server_returned_own_tab(camofox):
    cf = camofox.cf
    session = cf._get_session("chat", "brianle")
    session["tab_id"] = "own-tab"
    camofox.server.open_tab_ids.append("own-tab")

    result = _dispatch("browser_handoff", {"account": "brianle"}, "chat")

    assert result["success"] and result["tabId"] == "own-tab"
    assert "replacedTab" not in result
    assert session["tab_id"] == "own-tab"
    open_calls = [payload for method, path, payload in camofox.server.calls
                  if method == "POST" and path.endswith("/open")]
    assert open_calls == [{"tabId": "own-tab"}]


def test_handoff_without_prior_binding_sends_no_tab_id(camofox):
    result = _dispatch("browser_handoff", {"account": "brianle"}, "new-task")

    assert result["success"] and result["tabId"] == "visible-tab"
    assert camofox.cf._sessions["new-task"]["tab_id"] == "visible-tab"
    open_calls = [payload for method, path, payload in camofox.server.calls
                  if method == "POST" and path.endswith("/open")]
    assert open_calls == [{}]


def test_handoff_never_steals_another_live_tasks_tab(camofox):
    cf = camofox.cf
    owner = cf._get_session("owner", "brianle")
    owner["tab_id"] = "foreign-tab"
    caller = cf._get_session("caller", "brianle")
    caller["tab_id"] = "caller-tab"
    camofox.server.open_tab_ids.append("foreign-tab")

    result = _dispatch("browser_handoff", {"account": "brianle"}, "caller")

    assert result["success"] and result["modelDetached"] is True
    assert "replacedTab" not in result
    assert "another task's tab" in result["note"]
    assert caller["tab_id"] == "caller-tab"
    assert owner["tab_id"] == "foreign-tab"


def test_handoff_restarted_flag_does_not_rebind_or_detach_tasks(camofox):
    cf = camofox.cf
    owner = cf._get_session("owner", "brianle")
    owner["tab_id"] = "own-tab"
    camofox.server.open_tab_ids.append("own-tab")

    result = _dispatch("browser_handoff", {"account": "brianle"}, "owner")

    assert result["success"] and result["restarted"] is True
    assert "replacedTab" not in result
    assert owner["tab_id"] == "own-tab"


def test_release_keeps_task_tab_binding(camofox):
    cf = camofox.cf
    session = cf._get_session("release", "brianle")
    session["tab_id"] = "own-tab"

    result = _dispatch("browser_handoff", {"account": "brianle", "release": True}, "release")

    assert result == {"success": True, "account": "brianle", "released": True}
    assert session["tab_id"] == "own-tab"


def test_new_task_adopts_same_origin_tab_before_creating(camofox):
    cf = camofox.cf
    seed_session = cf._get_session("seed")
    user_id = seed_session["user_id"]
    cf._drop_session("seed")
    camofox.server.seed(user_id,
                        {"tabId": "same-origin", "url": "https://shop.example.test/old", "listItemId": "task_other"},
                        {"tabId": "shared", "url": "https://elsewhere.example.test/", "listItemId": SHARED},
                        {"tabId": "unrelated", "url": "https://elsewhere.example.test/", "listItemId": "task_other"})

    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/new"}, "fresh")["success"]
    assert camofox.server.tab_posts() == []
    assert cf._sessions["fresh"]["tab_id"] == "same-origin"


def test_without_origin_match_the_newest_shared_identity_tab_wins(camofox):
    cf = camofox.cf
    user_id = cf._get_session("seed")["user_id"]
    cf._drop_session("seed")
    camofox.server.seed(user_id,
                        {"tabId": "shared-old", "url": "https://a.example.test/", "listItemId": SHARED},
                        {"tabId": "shared-new", "url": "https://b.example.test/", "listItemId": SHARED},
                        {"tabId": "other-task", "url": "https://c.example.test/", "listItemId": "task_other"})
    assert _dispatch("browser_navigate", {"url": "https://d.example.test/"}, "fresh")["success"]
    assert camofox.server.tab_posts() == []
    assert cf._sessions["fresh"]["tab_id"] == "shared-new"


def test_adoption_refuses_live_quarantined_and_stale_tabs(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/"}, "owner")["success"]
    owned = cf._sessions["owner"]["tab_id"]
    user_id = _user_id(cf, "owner")
    camofox.server.seed(user_id,
                        {"tabId": "protected", "url": "https://shop.example.test/p", "listItemId": SHARED},
                        {"tabId": "dead", "url": "https://shop.example.test/d", "listItemId": SHARED})
    cf._quarantine_protected_tab("protected")
    cf._protected_tab_ids.clear()  # simulate a restart: only the on-disk quarantine remains
    with cf._sessions_lock:
        cf._stale_tab_ids[user_id] = {"dead"}  # a 404 already reported this tab destroyed

    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/x"}, "other")["success"]
    adopted = cf._sessions["other"]["tab_id"]
    assert adopted not in {owned, "protected", "dead"}
    assert camofox.server.tab_posts() == ["/tabs", "/tabs"]


def test_adoption_never_takes_another_identitys_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/", "account": "lpg"}, "lpg-task")["success"]
    lpg_tab = cf._sessions["lpg-task"]["tab_id"]
    _end_turn("lpg-task")
    cf._drop_session("lpg-task")  # gateway restart: binding gone, tab remains on the server
    camofox.server.calls.clear()
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/", "account": "brianle"}, "b")["success"]
    assert cf._sessions["b"]["tab_id"] != lpg_tab
    for method, path, payload in camofox.server.calls:
        if method == "GET" and path == "/tabs":
            assert payload["userId"] == _user_id(cf, "b")


def test_ephemeral_sessions_still_close_at_end_of_turn(camofox):
    (camofox.home / "config.yaml").write_text("browser:\n  cloud_provider: camofox\n", encoding="utf-8")
    deleted = []
    original = requests.delete
    with patch("tools.browser_camofox.requests.delete",
               side_effect=lambda url, **kw: deleted.append(url) or original(url, **kw)):
        assert _dispatch("browser_navigate", {"url": "https://shop.example.test/"}, "eph")["success"]
        user_id = _user_id(camofox.cf, "eph")
        _end_turn("eph")
    assert "eph" not in camofox.cf._sessions
    assert deleted == [f"{camofox.server.url}/tabs/created-1", f"{camofox.server.url}/sessions/{user_id}"]
    assert user_id not in camofox.server.tabs


def test_managed_sessions_do_not_delete_the_server_session(camofox):
    deleted = []
    original = requests.delete
    with patch("tools.browser_camofox.requests.delete",
               side_effect=lambda url, **kw: deleted.append(url) or original(url, **kw)):
        assert _dispatch("browser_navigate", {"url": "https://shop.example.test/"}, "managed")["success"]
        assert json.loads(camofox.cf.camofox_close("managed"))["closed"]
    assert len(deleted) == 1 and "/tabs/created-1" in deleted[0]
    assert not any("/sessions/" in path for path in deleted)


def test_stale_carried_tab_recovers_through_navigation_only(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    _end_turn("chat")
    camofox.server.gone.add(tab)  # the server reaped the idle tab between turns

    snapshot = _dispatch("browser_snapshot", {}, "chat")
    assert not snapshot["success"] and "browser_navigate" in snapshot["error"]
    # A page action never continues on a replacement tab, even an adoptable one.
    assert not _dispatch("browser_snapshot", {}, "chat")["success"]
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/"}, "chat")["success"]
    assert cf._sessions["chat"]["tab_id"] not in {tab, None}


def test_carried_binding_may_switch_account_next_turn(camofox):
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "chat")["success"]
    second = _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "lpg"}, "chat")
    assert not second["success"] and "already bound" in second["error"]
    _end_turn("chat")
    third = _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "lpg"}, "chat")
    assert third["success"] and third["account"] == "lpg"


def test_compression_carries_the_binding_to_the_continuation_session(camofox):
    from agent.conversation_compression import _carry_session_state_to_child

    handoff = _dispatch("browser_handoff", {"account": "brianle"}, "session-1")
    assert handoff["success"]
    agent = SimpleNamespace(session_id="session-2", _session_db=None)
    _carry_session_state_to_child(agent, "session-1", None)
    # The rest of the current turn still runs under the old id.
    assert _dispatch("browser_snapshot", {}, "session-1")["success"]
    _end_turn("session-1")
    assert "session-1" not in camofox.cf._sessions

    result = _dispatch("browser_navigate", {"url": "https://login.example.test/next"}, "session-2")
    assert result["success"] and result["account"] == "brianle"
    assert camofox.server.tab_posts() == []
    assert set(camofox.server.acted_on()) == {"visible-tab"}


def test_agent_close_releases_its_bindings(camofox):
    from agent.client_lifecycle import ClientLifecycleMixin

    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "turn-task")["success"]
    _end_turn("turn-task")
    assert "turn-task" in camofox.cf._sessions
    agent = SimpleNamespace(_process_owner_task_ids={"turn-task"})
    with patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(agent, "session-id")
    assert "turn-task" not in camofox.cf._sessions


def test_protected_binding_is_kept_by_its_owner_across_turns(camofox):
    """Vault protection is retained with the binding, so the owning task keeps its tab."""
    from agent.redact import clear_vault_date_components, register_vault_date_component
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    cf.quarantine_current_protected_tab("chat")
    register_vault_date_component("bday-year", "1990", tab="chat", origin="https://a.example.test")
    try:
        _end_turn("chat")
        assert cf._sessions["chat"]["tab_id"] == tab
        assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "other")["success"]
        assert cf._sessions["other"]["tab_id"] != tab
    finally:
        clear_vault_date_components("chat")


def test_agent_close_releases_unused_continuations_only(camofox):
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.conversation_compression import _carry_session_state_to_child

    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "s1")["success"]
    _carry_session_state_to_child(SimpleNamespace(session_id="s2", _session_db=None), "s1", None)
    _carry_session_state_to_child(SimpleNamespace(session_id="s3", _session_db=None), "s1", None)
    _end_turn("s1")
    assert _dispatch("browser_snapshot", {}, "s3")["success"]  # a newer agent's turn uses s3
    _end_turn("s3")  # its own turn end marks it carried again; it still belongs to that agent
    with patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(SimpleNamespace(_process_owner_task_ids={"s1"}), "s1")
    assert "s2" not in cf._sessions  # never used: released with its origin
    assert "s3" in cf._sessions  # in use by another agent: kept


def test_agent_close_deletes_an_ephemeral_owned_tab_left_open(camofox):
    """A cut turn must close its exact tab, not a whole identity that could hold user tabs."""
    from agent.client_lifecycle import ClientLifecycleMixin

    (camofox.home / "config.yaml").write_text("browser:\n  cloud_provider: camofox\n", encoding="utf-8")
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "cut-turn")["success"]
    tab = camofox.cf._sessions["cut-turn"]["tab_id"]
    deleted = []
    with patch("tools.browser_camofox._delete", side_effect=lambda path, *a, **k: deleted.append(path) or {}), \
         patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(SimpleNamespace(_process_owner_task_ids={"cut-turn"}), "other")
    assert "cut-turn" not in camofox.cf._sessions
    assert deleted == [f"/tabs/{tab}"]


@pytest.mark.parametrize("status,code", [(410, "page_crashed"), (404, "browser_restarted")])
def test_stale_error_keeps_durable_quarantine_across_restart(camofox, status, code):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    session = cf._sessions["chat"]
    tab = session["tab_id"]
    cf.quarantine_current_protected_tab("chat")
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"code": code}).encode()
    assert cf._clear_stale_tab(session, requests.HTTPError("stale", response=response))
    assert tab in cf._protected_tabs_on_disk()

    # Model a fresh process: only disk quarantine survives, while the server still lists it.
    cf._sessions.clear()
    cf._stale_tab_ids.clear()
    cf._protected_tab_ids.clear()
    cf._protected_tab_owners.clear()
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "fresh")["success"]
    assert cf._sessions["fresh"]["tab_id"] != tab
    assert tab in cf._protected_tabs_on_disk()


def test_stale_tab_record_is_pruned_once_the_server_forgets_the_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    user_id = _user_id(cf, "chat")
    cf.quarantine_current_protected_tab("chat")
    camofox.server.gone.add(tab)
    assert not _dispatch("browser_snapshot", {}, "chat")["success"]
    assert cf._stale_tab_ids[user_id] == {tab}
    assert tab in cf._protected_tabs_on_disk()
    camofox.server.tabs[user_id] = [t for t in camofox.server.tabs[user_id] if t["tabId"] != tab]
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    assert user_id not in cf._stale_tab_ids
    assert tab not in cf._protected_tabs_on_disk()
    assert tab not in cf._protected_tab_ids


def test_turns_without_a_task_id_keep_the_tab(camofox):
    """CLI one-shot and direct AIAgent callers omit task_id, so each turn gets a new UUID."""
    from agent.turn_context import _bind_turn_identity

    agent = SimpleNamespace(session_id="s", _relay_pending_turn_id=None)
    with patch("agent.agent_runtime_helpers.note_turn_start"):
        first, _ = _bind_turn_identity(agent, None, None, None, None, None)
        assert _dispatch("browser_handoff", {"account": "brianle"}, first)["success"]
        _end_turn(first)
        second, _ = _bind_turn_identity(agent, None, None, None, None, None)
    assert second != first and first not in camofox.cf._sessions
    snapshot = _dispatch("browser_snapshot", {}, second)
    assert snapshot["success"] and "visible-tab" in snapshot["snapshot"]
    assert _dispatch("browser_navigate", {"url": "https://login.example.test/next"}, second)["success"]
    assert camofox.server.tab_posts() == []
    assert set(camofox.server.acted_on()) == {"visible-tab"}


def test_handoff_reserves_the_shared_tab_from_concurrent_adoption(camofox):
    """While /open is in flight another task must not adopt the shared tab it returns."""
    cf = camofox.cf
    real_post = cf._post
    adopted = {}

    def post(path, body, *args, **kwargs):
        result = real_post(path, body, *args, **kwargs)
        if path.endswith("/open"):  # the tab exists on the server, the handoff has not bound it yet
            adopted["result"] = _dispatch("browser_navigate", {"url": "https://login.example.test/x",
                                                               "account": "brianle"}, "racer")
        return result

    with patch.object(cf, "_post", side_effect=post):
        assert _dispatch("browser_handoff", {"account": "brianle"}, "owner")["success"]
    assert adopted["result"]["success"]
    assert cf._sessions["owner"]["tab_id"] == "visible-tab"
    assert cf._sessions["racer"]["tab_id"] != "visible-tab"


def test_handoff_keeps_another_task_bound_to_the_visible_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_handoff", {"account": "brianle"}, "first")["success"]
    _end_turn("first")
    second = _dispatch("browser_handoff", {"account": "brianle"}, "second")
    assert second["success"] and second["modelDetached"] is True
    assert cf._sessions["second"]["tab_id"] is None
    assert cf._sessions["first"]["tab_id"] == "visible-tab"
    assert not _dispatch("browser_snapshot", {}, "second")["success"]  # no prior binding to continue


def test_a_protected_binding_does_not_move_to_a_continuation(camofox):
    from agent.redact import clear_vault_date_components, register_vault_date_component
    from agent.turn_context import _bind_turn_identity

    cf = camofox.cf
    agent = SimpleNamespace(session_id="s", _relay_pending_turn_id=None)
    with patch("agent.agent_runtime_helpers.note_turn_start"):
        first, _ = _bind_turn_identity(agent, None, None, None, None, None)
        assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, first)["success"]
        tab = cf._sessions[first]["tab_id"]
        cf.quarantine_current_protected_tab(first)
        register_vault_date_component("bday-year", "1990", tab=first, origin="https://a.example.test")
        try:
            _end_turn(first)
            second, _ = _bind_turn_identity(agent, None, None, None, None, None)
        finally:
            clear_vault_date_components(first)
    assert cf._sessions[second]["tab_id"] is None
    assert not _dispatch("browser_snapshot", {}, second)["success"]
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, second)["success"]
    assert cf._sessions[second]["tab_id"] not in {tab, None}


def _close_agent(task_ids, **attrs):
    """The real agent-close resource release for an agent that ran ``task_ids``."""
    from agent.client_lifecycle import ClientLifecycleMixin

    agent = SimpleNamespace(_process_owner_task_ids=set(task_ids), **attrs)
    with patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(agent, "session-id")


def _tab_deletes(server):
    return [(path, query) for method, path, query in server.calls if method == "DELETE"]


@pytest.mark.parametrize("attrs", [{"_delegate_depth": 1}, {"platform": "cron"}])
def test_finished_subagent_or_cron_run_closes_the_tab_it_created(camofox, attrs):
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child-task")["success"]
    tab, user_id = camofox.cf._sessions["child-task"]["tab_id"], _user_id(camofox.cf, "child-task")
    _end_turn("child-task")
    _close_agent({"child-task"}, **attrs)
    assert "child-task" not in camofox.cf._sessions
    assert _tab_deletes(camofox.server) == [(f"/tabs/{tab}", {"userId": user_id})]
    assert all(t["tabId"] != tab for t in camofox.server.tabs[user_id])


def test_one_shot_run_closes_the_tab_it_created(camofox, monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "oneshot-task")["success"]
    tab = camofox.cf._sessions["oneshot-task"]["tab_id"]
    _close_agent({"oneshot-task"}, platform="cli")
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


def test_top_level_session_close_has_already_reaped_its_disposable_tab(camofox, monkeypatch):
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab, user_id = camofox.cf._sessions["chat"]["tab_id"], _user_id(camofox.cf, "chat")
    _end_turn("chat")
    _close_agent({"chat"}, platform="telegram", _delegate_depth=0)
    assert "chat" not in camofox.cf._sessions
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]
    assert not any(t["tabId"] == tab for t in camofox.server.tabs[user_id])

def test_subagent_release_refuses_adoption_before_tab_delete(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child")["success"]
    tab = cf._sessions["child"]["tab_id"]

    real_delete = cf._delete
    def delete_with_racing_adoption(path, *args, **kwargs):
        if path == f"/tabs/{tab}":
            raced = _dispatch("browser_navigate", {"url": "https://a.example.test/race"}, "sibling")
            assert raced["success"]
            assert cf._sessions["sibling"]["tab_id"] != tab
        return real_delete(path, *args, **kwargs)

    with patch.object(cf, "_delete", side_effect=delete_with_racing_adoption):
        _close_agent({"child"}, _delegate_depth=1)
    assert cf._sessions["sibling"]["tab_id"] != tab
    assert _tab_deletes(camofox.server) == [(f"/tabs/{tab}", {"userId": _user_id(cf, "sibling")})]


def test_subagent_close_never_deletes_an_adopted_tab(camofox):
    cf = camofox.cf
    user_id = cf._get_session("seed")["user_id"]
    cf._drop_session("seed")
    camofox.server.seed(user_id, {"tabId": "left-open", "url": "https://a.example.test/", "listItemId": "task_x"})
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child")["success"]
    assert cf._sessions["child"]["tab_id"] == "left-open"
    _close_agent({"child"}, _delegate_depth=1)
    assert _tab_deletes(camofox.server) == []


def test_subagent_close_never_deletes_a_handed_off_shared_tab(camofox):
    # The task first opens its own disposable tab, then a handoff rebinds it to the shared visible tab.
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "child")["success"]
    owned = camofox.cf._sessions["child"]["tab_id"]
    assert _dispatch("browser_handoff", {"account": "brianle"}, "child")["success"]
    assert camofox.cf._sessions["child"]["tab_id"] == "visible-tab"
    _close_agent({"child"}, _delegate_depth=1)
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{owned}"]
    assert any(t["tabId"] == "visible-tab" for tabs in camofox.server.tabs.values() for t in tabs)

def test_subagent_close_never_deletes_a_tab_another_task_is_bound_to(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child")["success"]
    tab = cf._sessions["child"]["tab_id"]
    with cf._sessions_lock:
        cf._sessions["sibling"] = {**cf._sessions["child"], "task_id": "sibling", "created_by_task": None}
    _close_agent({"child"}, _delegate_depth=1)
    assert cf._sessions["sibling"]["tab_id"] == tab
    assert _tab_deletes(camofox.server) == []


def test_subagent_close_deletes_its_protected_owned_tab(camofox):
    from agent.redact import clear_vault_date_components, register_vault_date_component
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child")["success"]
    tab = cf._sessions["child"]["tab_id"]
    cf.quarantine_current_protected_tab("child")
    register_vault_date_component("bday-year", "1990", tab="child", origin="https://a.example.test")
    try:
        _close_agent({"child"}, _delegate_depth=1)
    finally:
        clear_vault_date_components("child")
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]
    assert tab not in cf._protected_tab_ids
    assert tab not in cf._protected_tabs_on_disk()
    assert not any(key[3] == tab for key in cf._owned_tabs)


def test_ephemeral_close_deletes_protected_owned_tab_and_releases_lease(camofox, monkeypatch):
    from agent.redact import register_vault_date_component
    from tools import browser_camofox_leases as leases
    cf = camofox.cf
    monkeypatch.setattr(cf, "_get_camofox_config", lambda: {})
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "ephemeral")["success"]
    session = cf._sessions["ephemeral"]
    assert session["managed"] is False
    tab, user_id = session["tab_id"], session["user_id"]
    key = (camofox.server.url, user_id, tab)
    cf.quarantine_current_protected_tab("ephemeral")
    register_vault_date_component("bday-year", "1990", tab="ephemeral", origin="https://a.example.test")
    assert key in leases._handles

    assert json.loads(cf.camofox_close("ephemeral"))["closed"]

    assert _tab_deletes(camofox.server) == [
        (f"/tabs/{tab}", {"userId": user_id}),
        (f"/sessions/{user_id}", {}),
    ]
    assert user_id not in camofox.server.tabs
    assert "ephemeral" not in cf._sessions
    assert tab not in cf._protected_tab_ids and tab not in cf._protected_tabs_on_disk()
    assert tab not in cf._protected_tab_owners
    assert not any(owned[3] == tab for owned in cf._owned_tabs)
    assert key not in leases._handles and key not in leases._local_claims
    assert not leases._lease_path(key).exists()


@pytest.mark.parametrize("exclusion", ["live-protection", "handoff", "shared-tab", "unreadable", "tab-delete-failure"])
def test_ephemeral_close_does_not_bypass_tab_retention(camofox, monkeypatch, exclusion):
    cf = camofox.cf
    monkeypatch.setattr(cf, "_get_camofox_config", lambda: {})
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "ephemeral")["success"]
    session = cf._sessions["ephemeral"]
    tab, user_id = session["tab_id"], session["user_id"]
    if exclusion == "live-protection":
        cf.quarantine_current_protected_tab("ephemeral")
        cf._sessions["holder"] = {**session, "task_id": "holder"}
        cf._protected_tab_owners[tab] = "holder"
    elif exclusion == "handoff":
        cf._handoffs_in_flight[user_id] = 1
        monkeypatch.setattr(cf, "_handoffs_in_flight", cf._handoffs_in_flight.copy())
    elif exclusion == "shared-tab":
        cf._owned_tabs[cf._owned_tab_key("ephemeral", tab)]["session_key"] = SHARED
    elif exclusion == "unreadable":
        monkeypatch.setattr(cf, "_protected_tabs_on_disk", lambda: None)
    else:
        original_delete = cf._delete

        def fail_tab_delete(path, **kwargs):
            if path.startswith("/tabs/"):
                raise requests.ConnectionError("fake tab close failure")
            return original_delete(path, **kwargs)

        monkeypatch.setattr(cf, "_delete", fail_tab_delete)
    try:
        assert json.loads(cf.camofox_close("ephemeral"))["closed"]
        assert _tab_deletes(camofox.server) == []
        assert any(t["tabId"] == tab for t in camofox.server.tabs[user_id])
    finally:
        cf._handoffs_in_flight.pop(user_id, None)


def test_agent_close_does_not_delete_protected_adopted_tab(camofox):
    cf = camofox.cf
    user_id = cf._get_session("owner")["user_id"]
    camofox.server.seed(user_id, {"tabId": "user-tab", "url": "https://a.example.test/", "listItemId": SHARED})
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "owner")["success"]
    assert cf._sessions["owner"]["tab_id"] == "user-tab"
    cf.quarantine_current_protected_tab("owner")
    cf.release_task_bindings({"owner"}, close_created_tabs=True)
    assert _tab_deletes(camofox.server) == []
    assert "user-tab" in cf._protected_tabs_on_disk()


@pytest.mark.parametrize("protected", [False, True])
def test_agent_abandons_failed_delete_records_and_leases(camofox, protected):
    from tools import browser_camofox_leases as leases
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "owner")["success"]
    tab, user_id = cf._sessions["owner"]["tab_id"], _user_id(cf, "owner")
    if protected:
        cf.quarantine_current_protected_tab("owner")
    with patch.object(cf, "_delete", side_effect=requests.ConnectionError("fake server down")):
        cf.release_task_bindings({"owner"}, close_created_tabs=True)
    assert not any(key[3] == tab for key in cf._owned_tabs)
    assert (camofox.server.url, user_id, tab) not in leases._handles
    assert any(t["tabId"] == tab for t in camofox.server.tabs[user_id])
    if protected:
        assert tab in cf._protected_tabs_on_disk()  # abandonment must not permit reads


def test_stale_owned_tab_releases_record_and_lease_but_retains_quarantine(camofox):
    from tools import browser_camofox_leases as leases
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "owner")["success"]
    tab, user_id = cf._sessions["owner"]["tab_id"], _user_id(cf, "owner")
    cf.quarantine_current_protected_tab("owner")
    camofox.server.gone.add(tab)
    assert not _dispatch("browser_snapshot", {}, "owner")["success"]
    assert not any(key[3] == tab for key in cf._owned_tabs)
    assert tab in cf._protected_tabs_on_disk()
    assert tab not in cf._protected_tab_owners
    assert (camofox.server.url, user_id, tab) not in leases._handles


def test_subagent_tab_close_failure_is_swallowed(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "child")["success"]
    tab = cf._sessions["child"]["tab_id"]
    with patch.object(cf, "_delete", side_effect=requests.ConnectionError("camofox down")) as delete:
        cf.release_task_bindings({"child"}, close_created_tabs=True)  # must not raise
    assert delete.call_args.args == (f"/tabs/{tab}",)
    assert "child" not in cf._sessions


@pytest.mark.parametrize("replacement", ["none", "stale", "identity", "handoff", "compression", "move"])
def test_owning_turn_closes_every_created_disposable_tab(camofox, replacement):
    """Creation, not the current binding or identity, determines cleanup ownership."""
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "owner")["success"]
    first = cf._sessions["owner"]["tab_id"]
    user_id = _user_id(cf, "owner")
    expected = [(f"/tabs/{first}", {"userId": user_id})]
    ending = "owner"
    if replacement == "stale":
        cf._sessions["owner"]["tab_id"] = None
        cf._stale_tab_ids.setdefault(user_id, set()).add(first)
        assert _dispatch("browser_navigate", {"url": "https://b.example.test/"}, "owner")["success"]
        expected.append((f"/tabs/{cf._sessions['owner']['tab_id']}", {"userId": user_id}))
    elif replacement == "identity":
        cf._sessions["owner"]["carried"] = True
        assert _dispatch("browser_navigate", {"url": "https://b.example.test/", "account": "lpg"}, "owner")["success"]
        expected.append((f"/tabs/{cf._sessions['owner']['tab_id']}", {"userId": _user_id(cf, "owner")}))
    elif replacement == "handoff":
        assert _dispatch("browser_handoff", {"account": "brianle"}, "owner")["success"]
    elif replacement in {"compression", "move"}:
        cf.carry_task_binding("owner", "continuation", move=replacement == "move")
        if replacement == "move":
            ending = "continuation"
    _end_turn(ending)
    if replacement == "compression":
        assert _tab_deletes(camofox.server) == []
        _end_turn("continuation")
    assert sorted(_tab_deletes(camofox.server)) == sorted(expected)
    assert all(tab["tabId"] != first for tabs in camofox.server.tabs.values() for tab in tabs)
    if replacement == "handoff":
        assert _dispatch("browser_snapshot", {}, "owner")["success"]
    else:
        assert not _dispatch("browser_snapshot", {}, "continuation" if replacement in {"compression", "move"} else "owner")["success"]


@pytest.mark.parametrize("exclusion", ["adopted", "keep", "other-task", "protected", "unreadable", "failure"])
def test_turn_cleanup_preserves_non_disposable_tabs_and_retry_ownership(camofox, exclusion):
    cf = camofox.cf
    if exclusion == "adopted":
        user_id = cf._get_session("owner", "brianle")["user_id"]
        camofox.server.seed(user_id, {"tabId": "user-tab", "url": "https://a.example.test/", "listItemId": SHARED})
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "owner")["success"]
    tab = cf._sessions["owner"]["tab_id"]
    user_id = _user_id(cf, "owner")
    if exclusion == "keep":
        assert cf.keep_task_tabs("owner") == 1
    elif exclusion == "other-task":
        cf._sessions["sibling"] = {**cf._sessions["owner"], "task_id": "sibling"}
    elif exclusion == "protected":
        cf.quarantine_current_protected_tab("owner")
    if exclusion == "unreadable":
        with patch.object(cf, "_protected_tabs_on_disk", return_value=None):
            _end_turn("owner")
    elif exclusion == "failure":
        with patch.object(cf, "_delete", side_effect=requests.ConnectionError("fake server down")):
            _end_turn("owner")
        assert cf._sessions["owner"]["tab_id"] == tab
    else:
        _end_turn("owner")
    assert _tab_deletes(camofox.server) == []
    assert any(t["tabId"] == tab for t in camofox.server.tabs[user_id])
    if exclusion == "keep":
        assert cf.keep_task_tabs("owner", keep=False) == 1
    elif exclusion == "other-task":
        cf._drop_session("sibling")
    if exclusion in {"keep", "other-task", "unreadable", "failure"}:
        _end_turn("owner")
        assert _tab_deletes(camofox.server) == [(f"/tabs/{tab}", {"userId": user_id})]


def test_top_level_agent_close_cleans_a_cut_turn(camofox):
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab = camofox.cf._sessions["chat"]["tab_id"]
    _close_agent({"chat"}, _delegate_depth=0, platform="telegram")
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


def test_agent_close_cleans_unused_compression_continuation(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    cf.carry_task_binding("chat", "continuation")
    _close_agent({"chat"}, _delegate_depth=0, platform="telegram")
    assert "continuation" not in cf._sessions
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


def test_handoff_in_flight_preserves_owned_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    user_id = _user_id(cf, "chat")
    cf._handoffs_in_flight[user_id] = 1
    try:
        _end_turn("chat")
        assert _tab_deletes(camofox.server) == []
    finally:
        cf._handoffs_in_flight.pop(user_id)
    _end_turn("chat")
    assert len(_tab_deletes(camofox.server)) == 1


def test_server_configuration_change_never_deletes_on_the_wrong_server(camofox, monkeypatch):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    monkeypatch.setenv("CAMOFOX_URL", "http://other.invalid")
    _end_turn("chat")
    assert _tab_deletes(camofox.server) == []
    assert len(cf._owned_tabs) == 1
    monkeypatch.setenv("CAMOFOX_URL", camofox.server.url)
    _end_turn("chat")
    assert len(_tab_deletes(camofox.server)) == 1


@pytest.mark.parametrize("exclusion", ["keep", "user-tab"])
def test_ephemeral_cleanup_preserves_kept_and_user_tabs(camofox, exclusion):
    (camofox.home / "config.yaml").write_text("browser:\n  cloud_provider: camofox\n", encoding="utf-8")
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    user_id = _user_id(cf, "chat")
    if exclusion == "keep":
        cf.keep_task_tabs("chat")
    else:
        camofox.server.seed(user_id, {"tabId": "user-created", "url": "https://b.example.test/", "listItemId": SHARED})
    _end_turn("chat")
    if exclusion == "keep":
        assert _tab_deletes(camofox.server) == []
    else:
        assert [t["tabId"] for t in camofox.server.tabs[user_id]] == ["user-created"]
    assert not any("/sessions/" in path for _method, path, _payload in camofox.server.calls)


def test_explicit_handoff_keeps_an_owned_tab_atomically(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    real_post = cf._post
    def open_owned(path, body, **kwargs):
        if path.endswith("/open"):
            return {"ok": True, "tabId": tab}
        return real_post(path, body, **kwargs)
    with patch.object(cf, "_post", side_effect=open_owned):
        assert _dispatch("browser_handoff", {"account": "brianle"}, "chat")["success"]
    _end_turn("chat")
    _close_agent({"chat"}, _delegate_depth=0, platform="telegram")
    assert _tab_deletes(camofox.server) == []


def test_other_live_process_blocks_adoption_and_owned_cleanup(camofox):
    import subprocess
    import sys
    from tools import browser_camofox_leases as leases
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "owner")["success"]
    tab = cf._sessions["owner"]["tab_id"]
    user_id = _user_id(cf, "owner")
    leases.release(camofox.server.url, user_id, tab)
    code = "from pathlib import Path; import sys; from tools import browser_camofox_leases as l; l._lease_root=lambda:Path(sys.argv[1]); print(l.claim(*sys.argv[2:]), flush=True); sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-u", "-c", code, str(leases._lease_root()), camofox.server.url, user_id, tab],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "True"
        _end_turn("owner")
        assert _tab_deletes(camofox.server) == []
        cf._drop_session("owner")
        assert _dispatch("browser_navigate", {"url": "https://a.example.test/next"}, "sibling")["success"]
        assert cf._sessions["sibling"]["tab_id"] != tab
    finally:
        child.communicate("release\n", timeout=15)
    cf.release_task_bindings({"owner"}, close_created_tabs=True)
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


def test_confirmed_release_removes_keep_but_preserves_binding_until_cleanup(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    cf.keep_task_tabs("chat")
    assert _dispatch("browser_handoff", {"account": "brianle", "release": True}, "chat")["success"]
    assert cf._sessions["chat"]["tab_id"] == tab
    _end_turn("chat")
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


def test_handoff_cannot_rebind_when_cross_process_ownership_is_uncertain(camofox):
    cf = camofox.cf
    session = cf._get_session("chat", "brianle")
    session["tab_id"] = "own-bound-tab"
    with patch("tools.browser_camofox_leases.claim", return_value=False):
        result = _dispatch("browser_handoff", {"account": "brianle"}, "chat")
    assert result["success"] and result["modelDetached"]
    assert "ownership could not be confirmed" in result["note"]
    assert "replacedTab" not in result
    assert session["tab_id"] == "own-bound-tab"


def test_handoff_retains_an_owned_tab_without_stealing_its_live_binding(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "owner")["success"]
    tab = cf._sessions["owner"]["tab_id"]
    camofox.server.open_tab_ids.append(tab)
    result = _dispatch("browser_handoff", {"account": "brianle"}, "caller")
    assert result["success"] and result["modelDetached"]
    assert cf._sessions["owner"]["tab_id"] == tab
    assert cf._sessions["caller"]["tab_id"] is None
    _end_turn("owner")
    _close_agent({"owner"}, _delegate_depth=0)
    assert _tab_deletes(camofox.server) == []


def test_temporary_agent_close_does_not_clean_another_agents_live_browser(camofox):
    from agent.client_lifecycle import ClientLifecycleMixin
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "live-owner")["success"]
    tab = cf._sessions["live-owner"]["tab_id"]
    # Do not mock cleanup_browser: generic close-time cleanup shares the session ID too.
    temporary = ClientLifecycleMixin()
    setattr(temporary, "_process_owner_task_ids", set())
    with patch("run_agent.cleanup_vm"):
        temporary._close_task_resources("live-owner")
    assert cf._sessions["live-owner"]["tab_id"] == tab
    assert _tab_deletes(camofox.server) == []


def test_agent_close_cleans_actual_tool_task_not_shared_session_id(camofox):
    from agent.client_lifecycle import ClientLifecycleMixin
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "tool-owner")["success"]
    tab = cf._sessions["tool-owner"]["tab_id"]
    owner = ClientLifecycleMixin()
    setattr(owner, "_process_owner_task_ids", {"tool-owner"})
    with patch("run_agent.cleanup_vm"):
        owner._close_task_resources("different-session-id")
    assert [path for path, _ in _tab_deletes(camofox.server)] == [f"/tabs/{tab}"]


@pytest.mark.parametrize("bound", [False, True])
def test_slow_identity_registry_does_not_block_existing_task_or_cleanup(camofox, bound):
    from agent.memory_provider import spawn_context_thread

    cf = camofox.cf
    for task in ("sibling", "cleanup"):
        assert _dispatch("browser_navigate", {"url": "https://example.test/"}, task)["success"]
    if bound:
        assert _dispatch("browser_navigate", {"url": "https://example.test/", "account": "brianle"}, "slow")["success"]
    started, finish = threading.Event(), threading.Event()
    sibling_done, cleanup_done = threading.Event(), threading.Event()
    camofox.server.identity_barrier = (started, finish)
    results = {}

    def named_tool():
        results["named"] = _dispatch("browser_navigate", {"url": "https://slow.test/", "account": "brianle"}, "slow")

    def sibling_tool():
        results["sibling"] = _dispatch("browser_snapshot", {}, "sibling")
        sibling_done.set()

    def cleanup():
        cf.release_task_bindings({"cleanup"}, close_created_tabs=True)
        cleanup_done.set()

    named = spawn_context_thread(named_tool, name="slow-registry")
    sibling = spawn_context_thread(sibling_tool, name="sibling-browser")
    closer = spawn_context_thread(cleanup, name="browser-cleanup")
    named.start()
    try:
        assert started.wait(2)
        sibling.start()
        closer.start()
        # Both must finish before the registry responds, not merely after its timeout.
        sibling_ready = sibling_done.wait(2)
        cleanup_ready = cleanup_done.wait(2)
        assert sibling_ready, "existing task blocked on the identity registry"
        assert cleanup_ready, "cleanup blocked on the identity registry"
        assert results["sibling"]["success"]
        assert "cleanup" not in cf._sessions
    finally:
        finish.set()
        for thread in (named, sibling, closer):
            if thread.ident is not None:
                thread.join(5)
    assert not any(thread.is_alive() for thread in (named, sibling, closer))
    assert results["named"]["success"]


@pytest.mark.parametrize("change", ["initial-bind", "replacement", "drop", "account", "user-id"])
def test_identity_lookup_refuses_a_concurrently_changed_binding(camofox, change):
    from agent.memory_provider import spawn_context_thread

    cf = camofox.cf
    old = None
    if change != "initial-bind":
        assert _dispatch("browser_navigate", {"url": "https://old.test/", "account": "brianle"}, "target")["success"]
        old = cf._sessions["target"]
        cf.quarantine_current_protected_tab("target")
    started, finish, changed = threading.Event(), threading.Event(), threading.Event()
    camofox.server.identity_barrier = (started, finish)
    results = []

    def named_tool():
        results.append(_dispatch("browser_navigate", {"url": "https://stale.test/", "account": "brianle"}, "target"))

    def change_binding():
        if change in {"replacement", "drop"}:
            cf.release_task_bindings({"target"})
        if change in {"initial-bind", "replacement"}:
            cf._get_session("target", "lpg" if change == "initial-bind" else "brianle")
        elif change in {"account", "user-id"}:
            assert old is not None
            with cf._sessions_lock:
                field, value = (("account", "lpg") if change == "account"
                                else ("user_id", "hermes_camofox_" + "f" * 24))
                old[field] = value
        changed.set()

    named = spawn_context_thread(named_tool, name="delayed-identity")
    changer = spawn_context_thread(change_binding, name="change-browser-binding")
    named.start()
    try:
        assert started.wait(2)
        changer.start()
        assert changed.wait(2), "binding change blocked on the identity registry"
        current = cf._sessions.get("target")
        current_state = dict(current) if current is not None else None
        before = list(camofox.server.calls)
    finally:
        finish.set()
        for thread in (named, changer):
            if thread.ident is not None:
                thread.join(5)
    assert not named.is_alive() and not changer.is_alive()
    assert results[0]["success"] is False
    assert "binding changed" in results[0]["error"]
    assert cf._sessions.get("target") is current
    assert current == current_state
    assert camofox.server.calls == before, "stale lookup must not navigate or create a replacement"
    if old is not None:
        assert old["tab_id"] in cf._protected_tabs_on_disk()


def test_slow_owned_tab_delete_does_not_block_sibling_tool(camofox):
    cf = camofox.cf
    session = cf._get_session("slow-owner")
    session["tab_id"] = "slow-tab"
    cf._record_owned_tab(session, "slow-tab")
    started = threading.Event()
    finish = threading.Event()
    tool_done = threading.Event()
    results = []

    def slow_delete(*_args, **_kwargs):
        started.set()
        assert finish.wait(5)
        return {}

    def sibling_tool():
        results.append(_dispatch("browser_navigate", {"url": "https://sibling.example.test/"}, "sibling"))
        tool_done.set()

    with patch.object(cf, "_delete", side_effect=slow_delete):
        # Exercise release_task_bindings too: it must not retain its outer RLock
        # while invoking the owned-tab DELETE helper.
        closer = threading.Thread(target=cf.release_task_bindings,
                                  args=({"slow-owner"},), kwargs={"close_created_tabs": True})
        closer.start()
        assert started.wait(1)
        sibling = threading.Thread(target=sibling_tool)
        sibling.start()
        try:
            assert tool_done.wait(1), "sibling browser tool blocked on slow DELETE"
            assert results[0]["success"]
        finally:
            finish.set()
            closer.join(5)
            sibling.join(5)
    assert not closer.is_alive() and not sibling.is_alive()


def test_agent_close_releases_kept_tab_ownership_without_deleting(camofox):
    cf = camofox.cf
    from tools import browser_camofox_leases as leases

    session = cf._get_session("kept-owner")
    session["tab_id"] = "kept-tab"
    cf._record_owned_tab(session, "kept-tab")
    cf.keep_task_tabs("kept-owner")
    key = (cf.hermes_home_key(), "kept-owner", camofox.server.url, "kept-tab")
    lease_key = (camofox.server.url, session["user_id"], "kept-tab")
    assert leases.claim(*lease_key)
    assert key in cf._owned_tabs and lease_key in leases._handles

    with patch.object(cf, "_delete") as delete:
        cf.release_task_bindings({"kept-owner"}, close_created_tabs=True)

    assert key not in cf._owned_tabs
    assert lease_key not in leases._handles
    delete.assert_not_called()


def test_kept_owner_close_retains_live_sibling_lease(camofox):
    cf = camofox.cf
    from tools import browser_camofox_leases as leases

    session = cf._get_session("owner")
    session["tab_id"] = "shared-kept-tab"
    cf._record_owned_tab(session, session["tab_id"])
    cf.keep_task_tabs("owner")
    lease_key = (camofox.server.url, session["user_id"], session["tab_id"])
    assert leases.claim(*lease_key)
    with cf._sessions_lock:
        cf._sessions["sibling"] = {**session, "task_id": "sibling"}
    cf.release_task_bindings({"owner"}, close_created_tabs=True)
    assert not cf._owned_tabs
    assert lease_key in leases._handles
    cf._drop_session("sibling")
    assert lease_key not in leases._handles


def test_closing_fence_survives_listing_disappearance_and_handoff(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/", "account": "brianle"}, "owner")["success"]
    tab = cf._sessions["owner"]["tab_id"]
    user_id = cf._sessions["owner"]["user_id"]
    started = threading.Event()
    finish = threading.Event()

    def slow_delete(*_args, **_kwargs):
        started.set()
        assert finish.wait(5)
        return {}

    with patch.object(cf, "_delete", side_effect=slow_delete):
        closer = threading.Thread(target=cf._delete_owned_tabs, args=("owner",))
        closer.start()
        assert started.wait(1)
        try:
            # GET can temporarily omit a closing tab, clearing the ordinary stale cache.
            camofox.server.tabs[user_id] = []
            assert cf._adopt_existing_tab(cf._get_session("sibling", "brianle")).get("tab_id") is None
            assert tab not in cf._stale_tab_ids.get(user_id, set())
            camofox.server.seed(user_id, {"tabId": tab, "url": "https://a.example.test/", "listItemId": SHARED})
            assert cf._adopt_existing_tab(cf._get_session("sibling", "brianle")).get("tab_id") is None
            camofox.server.open_tab_ids.append(tab)
            handoff = _dispatch("browser_handoff", {"account": "brianle"}, "sibling")
            assert handoff["success"] and handoff["modelDetached"]
            assert cf._sessions["sibling"]["tab_id"] is None
        finally:
            finish.set()
            closer.join(5)
    assert (camofox.server.url, user_id, tab) not in cf._closing_tabs
