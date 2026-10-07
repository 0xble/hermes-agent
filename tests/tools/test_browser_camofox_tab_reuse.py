"""Camofox tab reuse across agent turns (fork patch ``camofox-tab-reuse``).

End-of-turn cleanup used to drop a managed task's tab binding, so every turn opened a new tab,
including right after ``browser_handoff`` where the user had just logged into the visible tab.
These run through the real browser tool dispatch, the real end-of-turn cleanup and the real
compression handoff against a stub Camofox HTTP server, with a temporary ``HERMES_HOME``.
"""

from __future__ import annotations

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
                    tab = {"tabId": "visible-tab", "url": "https://login.example.test/", "listItemId": SHARED}
                    stub.tabs.setdefault(user_id, []).append(tab)
                    return self._send({"ok": True, "focused": True, "restarted": True, **tab})
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
    server = _Camofox()
    server.thread.start()
    monkeypatch.setenv("CAMOFOX_URL", server.url)
    monkeypatch.setattr(cf, "get_vnc_url", lambda: None)
    monkeypatch.setattr(policy, "_eval_ssrf_guard_active", lambda task_id: False)
    monkeypatch.setattr(bt, "_url_policy_error", lambda url, auto_local=False: None)

    def reset():
        with cf._sessions_lock:
            cf._sessions.clear()
            getattr(cf, "_stale_tab_ids", {}).clear()
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


def _end_turn(task_id):
    """The real per-turn cleanup every agent turn runs (headless mode)."""
    from agent.chat_completion_helpers import cleanup_task_resources
    with patch("tools.browser_tool_cloud._is_headed_mode", return_value=False), \
         patch("agent.chat_completion_helpers.is_persistent_env", return_value=False), \
         patch("run_agent.cleanup_vm"):
        cleanup_task_resources(SimpleNamespace(verbose_logging=False), task_id)


def _user_id(cf, task_id):
    return cf._sessions[task_id]["user_id"]


def test_tab_binding_survives_end_of_turn_cleanup(camofox):
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/a"}, "chat")["success"]
    first_tab = camofox.cf._sessions["chat"]["tab_id"]
    _end_turn("chat")

    # Next turn: page actions keep working without a new navigate, on the same tab.
    assert _dispatch("browser_snapshot", {}, "chat")["success"]
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/b"}, "chat")["success"]
    assert camofox.server.tab_posts() == ["/tabs"]
    assert set(camofox.server.acted_on()) == {first_tab}


def test_handoff_tab_persists_across_turns(camofox):
    handoff = _dispatch("browser_handoff", {"account": "brianle"}, "chat")
    assert handoff["success"] and handoff["tabId"] == "visible-tab"
    _end_turn("chat")  # the user logs in on the visible tab between turns

    assert _dispatch("browser_snapshot", {}, "chat")["success"]
    result = _dispatch("browser_navigate", {"url": "https://login.example.test/account"}, "chat")
    assert result["success"] and result["account"] == "brianle"
    assert camofox.server.tab_posts() == []
    assert camofox.server.acted_on() == ["visible-tab"]


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
        _end_turn("eph")
    assert "eph" not in camofox.cf._sessions
    assert len(deleted) == 1 and "/sessions/hermes_" in deleted[0]
    assert not any(path == "/tabs" and method == "GET" for method, path, _ in camofox.server.calls)


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
    with patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(SimpleNamespace(_process_owner_task_ids={"s1"}), "s1")
    assert "s2" not in cf._sessions  # never used: released with its origin
    assert "s3" in cf._sessions  # in use by another agent: kept


def test_agent_close_deletes_an_ephemeral_session_left_open(camofox):
    """A turn cut before its cleanup leaves an ephemeral session; agent close must still delete it."""
    from agent.client_lifecycle import ClientLifecycleMixin

    (camofox.home / "config.yaml").write_text("browser:\n  cloud_provider: camofox\n", encoding="utf-8")
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "cut-turn")["success"]
    user_id = _user_id(camofox.cf, "cut-turn")
    deleted = []
    with patch("tools.browser_camofox._delete", side_effect=lambda path, *a, **k: deleted.append(path) or {}), \
         patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"):
        ClientLifecycleMixin._close_task_resources(SimpleNamespace(_process_owner_task_ids={"cut-turn"}), "other")
    assert "cut-turn" not in camofox.cf._sessions
    assert deleted == [f"/sessions/{user_id}"]


def test_stale_tab_record_is_pruned_once_the_server_forgets_the_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    tab = cf._sessions["chat"]["tab_id"]
    user_id = _user_id(cf, "chat")
    camofox.server.gone.add(tab)
    assert not _dispatch("browser_snapshot", {}, "chat")["success"]
    assert cf._stale_tab_ids[user_id] == {tab}
    camofox.server.tabs[user_id] = [t for t in camofox.server.tabs[user_id] if t["tabId"] != tab]
    assert _dispatch("browser_navigate", {"url": "https://a.example.test/"}, "chat")["success"]
    assert user_id not in cf._stale_tab_ids


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


def test_handoff_detaches_another_task_bound_to_the_visible_tab(camofox):
    cf = camofox.cf
    assert _dispatch("browser_handoff", {"account": "brianle"}, "first")["success"]
    _end_turn("first")
    assert _dispatch("browser_handoff", {"account": "brianle"}, "second")["success"]
    assert cf._sessions["second"]["tab_id"] == "visible-tab"
    assert cf._sessions["first"]["tab_id"] is None
    assert not _dispatch("browser_snapshot", {}, "first")["success"]  # rebinds only by navigating
