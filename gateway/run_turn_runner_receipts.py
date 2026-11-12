"""Persisted-turn receipt projection for gateway turn completion."""

from __future__ import annotations


def _persisted_turn(agent, agent_history, result, compressions) -> dict | None:
    """``message.complete.persisted_turn`` for this turn, the receipt the TUI gateway's own turns
    publish: clients bind the streamed reply to its stored row instead of comparing words. The
    agent copies ``conversation_history`` into its message list, so an untouched pre-turn prefix
    is the same dicts (transcript rows here carry no ``_row_id`` to compare)."""
    from agent.persisted_turn_receipt import persisted_turn_receipt
    if not isinstance(result, dict):
        return None
    status = ("interrupted" if result.get("interrupted")
              else "error" if result.get("failed") or result.get("error") else "complete")
    return persisted_turn_receipt(
        result.get("messages"), getattr(agent, "_persist_user_message_idx", None), agent_history,
        result.get("final_response"), status,
        compression_unchanged=type(compressions) is int and compressions == getattr(
            getattr(agent, "context_compressor", None), "compression_count", None),
        prefix_row_matches=lambda before, after: before is after)
