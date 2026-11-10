"""Topical helpers extracted from gateway/run_startup.py."""
from __future__ import annotations
from typing import Any, Optional

class GatewayStartupHandoffMixin:
    async def _handoff_repoint(self, dest, session_key: str, cli_session_id: str) -> None:
        """Point the destination route at the handed-off conversation; ``switch_session`` ends the
        route's prior session and reopens this one. A handoff carries the conversation the user is
        looking at: a local chat's latest ``/new`` segment, then that segment's compression tip
        (``handoff_head``), so its whole chain moves onto the lane together with the local receipt
        that guards it, and a viewer still holding it open (the Desktop that ran /handoff) follows
        it (#143286)."""
        from gateway.session_route_recovery import conversation_head, follow_hand_off
        authority, local_owner, target = await conversation_head(self, cli_session_id)
        # Ensure a session_store entry exists for this key; switch_session then re-points it.
        await self.async_session_store.get_or_create_session(dest.source)
        switched = await self.async_session_store.switch_session(session_key, target, preserve_prompt_pin=False)
        if switched is None:
            raise RuntimeError(f"could not switch session key {session_key} → {target}")
        if authority is not None:
            follow_hand_off(authority, dest.source, session_key, authority.logical_owner(target), local_owner)


    async def _process_handoff(self, row: dict[str, Any], profile_name: Optional[str] = None) -> None:
        """Execute one handoff row; raises on failure (caller marks failed). ``profile_name`` (None =
        root) is the profile whose store queued it — load-bearing under multiplex: secondaries live in
        ``_profile_adapters`` and the key must be namespaced ``agent:<profile>:...`` or nobody reads it."""
        from gateway.run_startup import (MessageEvent, _send_error, logger)
        from gateway.run_startup import (
            Any, Optional
        )
        cli_session_id = row["id"]
        dest = await self._handoff_resolve_destination(row, profile_name)
        session_key = self._handoff_session_key(dest, profile_name)
        await self._handoff_repoint(dest, session_key, cli_session_id)
        # Evict the cached AIAgent (rebuild against the CLI session_id, like /resume) and clear stale
        # running-agent state so the synthetic turn isn't queued behind it.
        self._evict_cached_agent(session_key)
        self._release_running_agent_state(session_key)
        cli_title = row.get("title") or cli_session_id[:8]
        synthetic_event = MessageEvent(
            text=(
                f"[Session was just handed off from CLI (\"{cli_title}\") to this "
                f"channel. The full prior conversation history is loaded above. "
                f"Briefly confirm you're working here and summarize what we were "
                f"working on, so the user can continue from this device.]"
            ),
            source=dest.source,
            internal=True,
        )
        logger.info(
            "Handoff: dispatching synthetic turn for CLI session %s → %s "
            "(home=%s, thread=%s, session_key=%s)",
            cli_session_id, dest.platform_name, dest.home.chat_id, dest.effective_thread_id, session_key,
        )
        # Inline _handle_message keeps success/failure observable (handle_message would detach it).
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            # Streaming may have delivered inline; the agent ran without raising — success.
            return
        # Reply into the new thread (else the home channel) via the resolved transport, so a relay-fronted
        # logical platform is stamped on the outbound frame.
        send_metadata = {"thread_id": dest.effective_thread_id} if dest.effective_thread_id else None
        try:
            result = await dest.transport.send(
                dest.platform, str(dest.home.chat_id), response_text, send_metadata,
            )
        except Exception as exc:
            raise RuntimeError(f"adapter.send failed: {exc}") from exc
        if not getattr(result, "success", True):
            raise RuntimeError(f"adapter.send failed: {_send_error(result)}")
