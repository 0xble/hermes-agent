"""compression.proactive_prune_* — config parse seam for the proactive prune.

Mirrors ``test_compression_max_attempts_config.py``: the three knobs are
parsed in ``agent_init`` with the same hardened semantics (booleans rejected,
fractional floats rejected — not truncated, integral floats and numeric
strings accepted) and attached to the built-in compressor. Default is
48000 / 8000 / 4096, so old oversized tool results are pruned deterministically
while the recent message tail stays protected.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from hermes_state import SessionDB
from run_agent import AIAgent


def _config(**prune_keys) -> dict:
    from hermes_cli import config as config_mod

    compression = dict(config_mod.DEFAULT_CONFIG["compression"])
    compression.update({
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    })
    compression.update(prune_keys)
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path, **prune_keys):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config(**prune_keys))

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: _config(**prune_keys))
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="proactive-prune-config-test",
        )
    return agent


class TestProactivePruneConfig:

    def test_default_values_enable_prune(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        cc = agent.context_compressor
        assert cc.proactive_prune_tokens == 48_000
        assert cc.proactive_prune_min_result_chars == 8_000
        assert cc.proactive_prune_min_reclaim_tokens == 4_096

    def test_default_agent_prunes_old_result_but_protects_recent_tail(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        agent.context_compressor._session_db.create_session(
            agent.context_compressor._session_id, source="test",
        )
        old_call_id = "old-call"
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old request"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": old_call_id,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"cmd":"old"}'},
                }],
            },
            {"role": "tool", "tool_call_id": old_call_id, "content": "O" * 30_000},
        ]
        tail = [
            {"role": "user", "content": f"recent-{idx}"}
            for idx in range(agent.context_compressor.protect_last_n + 3)
        ]
        messages.extend(tail)

        result, pruned = agent.context_compressor.prune_tool_results_only(
            messages, current_tokens=48_001,
        )

        assert pruned >= 1
        assert result is not messages
        old_result = next(m for m in result if m.get("tool_call_id") == old_call_id)
        assert len(old_result["content"]) < 30_000
        assert [m["content"] for m in result[-agent.context_compressor.protect_last_n:]] == [
            m["content"] for m in tail[-agent.context_compressor.protect_last_n:]
        ]

    def test_custom_values_are_honored(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch,
            tmp_path,
            proactive_prune_tokens=48_000,
            proactive_prune_min_result_chars=12_000,
            proactive_prune_min_reclaim_tokens=8_192,
        )
        cc = agent.context_compressor
        assert cc.proactive_prune_tokens == 48_000
        assert cc.proactive_prune_min_result_chars == 12_000
        assert cc.proactive_prune_min_reclaim_tokens == 8_192

    def test_boolean_is_rejected_not_coerced(self, monkeypatch, tmp_path):
        # bool subclasses int: YAML `proactive_prune_tokens: true` must fall
        # back to disabled, never coerce to 1 token.
        agent = _make_agent(monkeypatch, tmp_path, proactive_prune_tokens=True)
        assert agent.context_compressor.proactive_prune_tokens == 0




