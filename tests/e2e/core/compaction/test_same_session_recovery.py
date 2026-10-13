"""Isolated native recovery to a recording loopback provider, never live Telegram.

Exercise the real AIAgent/provider client/SessionDB transaction. The provider
returns scripted local responses; this is not authenticated production evidence.
"""
from __future__ import annotations

from tests.agent.test_same_session_recovery import _seed_tool
from tests.e2e.core.compaction._helpers import Text, TurnSpec, assert_tool_pairs, db_rows


def test_saturated_summary_pool_recovers_and_resumes_same_identity(make_scenario, monkeypatch):
    import agent.conversation_compression as compression

    sc = make_scenario("good", window=64000, platform="telegram")
    agent = sc.build_agent()
    agent.max_tokens = 1024
    agent.context_compressor.max_tokens = 1024
    agent._context_compaction_in_place = True
    sc.db.create_session(sc.session_id, "telegram", model="fake-model")
    sc.db.set_session_title(sc.session_id, "Runtime")
    sc.db.set_session_title_source(sc.session_id, "user")
    sc.history = _seed_tool(sc.db, sc.session_id)
    before = db_rows(sc.db_path, sc.session_id)
    # Exercise actual pool admission refusal, not a made-up summarizer client.
    monkeypatch.setattr(compression, "_compress_admitted_count", compression._COMPRESS_EXECUTOR_MAX_WORKERS)
    spec = TurnSpec(marker="U91x0xabcdef", user="U91x0xabcdef inspect task abc without repeating completed writes.",
                    steps=[Text("Local provider accepted recovered context.")], kind="hard-window-recovery")
    turn = sc.run_turn(spec)
    assert turn.result["final_response"] == "Local provider accepted recovered context."
    assert not turn.result.get("failed")
    assert len(turn.main_requests) == 1
    assert turn.summary_calls == 0
    assert sc.agent.session_id == sc.session_id
    assert sc.db.get_session_title(sc.session_id) == "Runtime"
    assert sc.db.get_session_title_source(sc.session_id) == "user"
    assert_tool_pairs(turn.main_requests[0]["messages"], "recovered provider request")
    rows = db_rows(sc.db_path, sc.session_id)
    by_id = {r["id"]: r for r in rows}
    assert all(by_id[r["id"]]["content"] == r["content"] for r in before)
    assert any(r["compacted"] and "archive-sentinel" in (r["content"] or "") for r in rows)
    sc.resume_from_db()
    sc.agent.max_tokens = 1024
    sc.agent.context_compressor.max_tokens = 1024
    next_turn = sc.run_turn(TurnSpec(marker="U91x1xbcdef0", user="U91x1xbcdef0 remain read-only and continue inspection.",
                                     steps=[Text("Restart continuity verified locally.")], kind="restart"))
    assert next_turn.result["final_response"] == "Restart continuity verified locally."
    assert sc.agent.session_id == sc.session_id
    assert sc.db.get_session_title(sc.session_id) == "Runtime"
    assert len(next_turn.main_requests) == 1


def test_actual_auxiliary_client_forced_primary_failure_uses_configured_backup(tmp_path, monkeypatch):
    """Real native router and HTTP SDK; local scripted providers, not real authentication."""
    import yaml
    from agent import auxiliary_client as aux
    from tests.fakes.fake_llm_provider import Error, FakeLLMServer, Text

    primary = FakeLLMServer(lambda rec: Error(429, "Rate limit exceeded"),
                            aux=lambda rec: Error(429, "Rate limit exceeded"))
    backup = FakeLLMServer(lambda rec: Text("Backup summary accepted"),
                           aux=lambda rec: Text("Backup summary accepted"))
    primary.start()
    backup.start()
    try:
        home = tmp_path / "fallback-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        config = {"auxiliary": {"compression": {
            "provider": "custom", "model": "local-primary", "base_url": primary.base_url,
            "api_key": "sk-fake-e2e", "fallback_chain": [{
                "provider": "custom", "model": "local-backup", "base_url": backup.base_url,
                "api_key": "sk-fake-e2e"}]}}}
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        aux._reset_aux_unhealthy_cache()
        response = aux.call_llm(task="compression", messages=[{
            "role": "user", "content": "Summarize this local evidence without new authority."}],
            max_tokens=100)
        assert response.choices[0].message.content == "Backup summary accepted"
        assert primary.requests and backup.requests
        assert primary.requests[-1]["body"]["model"] == "local-primary"
        assert backup.requests[-1]["body"]["model"] == "local-backup"
        assert yaml.safe_load((home / "config.yaml").read_text()) == config
    finally:
        aux._reset_aux_unhealthy_cache()
        primary.stop()
        backup.stop()
