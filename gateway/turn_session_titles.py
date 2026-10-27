"""Wire shared session-title generation into each platform's rename lane."""
from __future__ import annotations

import logging
from typing import Any

from gateway.run_turn_progress import local_viewer_turn

logger = logging.getLogger("gateway.run")  # log-record parity with the origin module


class TurnSessionTitlesMixin:
    _runner: Any
    _approval_owner: Any

    def _attach_session_title_callback(self, agent, ctx) -> None:
        """Wire the platform thread-rename lane onto the agent as `_on_session_title`.

        The titler runs in the turn prologue, so attach before the run, not after it.
        """
        try:
            # Gateway auto-title failures are not user-actionable, so never surface them as messages;
            # overriding the failure sink keeps CLI on _emit_auxiliary_failure while gateway logs debug.
            agent._title_failure_callback = lambda task, exc: logger.debug(
                "Gateway auto-title failure suppressed (not user-visible): %s: %s", task, exc,
            )
            agent._title_style = "noun"
            session_id = getattr(agent, "session_id", None)
            runner = self._runner
            session_key = getattr(ctx, "session_key", None)

            def session_current() -> bool:
                # Fence on session identity, not run generation: a quick follow-up starts a new
                # run but keeps the session, and the titler never fires again for it.
                peek = getattr(getattr(runner, "session_store", None), "peek_session_id", None)
                return bool(session_id) and callable(peek) and peek(session_key) == session_id

            # A platform adapter may own its session-title lane (Slack DM thread upgrades).
            adapter = getattr(ctx, "_status_adapter", None)
            configure = getattr(type(adapter), "configure_session_titles", None)
            if configure is not None and configure(adapter, agent, ctx, session_current):
                return
            if local_viewer_turn(self):
                # Main's CLI repainted its status-bar title when the auto-title landed.
                authority, canonical_id, _generation = self._approval_owner

                def republish(title, title_source, **_kwargs):
                    from gateway.session_contract import SessionRef
                    from gateway.session_local import local_session_info
                    ref = SessionRef(getattr(authority, "profile_id", ""), canonical_id)
                    authority.sessions[canonical_id].event_stream.publish(
                        canonical_id, local_session_info(authority, ref), event_type="session.info")
                agent._on_session_title = republish
                return
            source = runner._recover_discord_auto_thread_source(ctx.source, ctx.session_key)
            # Both lanes spend a rate-limited platform call per title, so they use the model's title
            # only (TitleCallback); renaming twice burns Discord's 2-per-10-min budget on a throwaway.
            # Relay Discord predicate is shape-only: whether the connector auto-threaded our reply is
            # only knowable AFTER delivery, so register eagerly and let the rename lane look up the
            # cache at fire time — gating registration on the cache read meant it never registered.
            if runner._is_telegram_topic_lane(source):
                # Capture now: _prepare_turn_message may prepend resume/model notes to
                # ctx.message before the titler fires, and the icon heuristic must score
                # the user's words, not recovery guidance.
                opening_message = getattr(ctx, "message", "") or ""
                if not isinstance(opening_message, str):
                    opening_message = ""

                def _schedule_telegram_title(title, title_source, *, display_title=None, icon=None):
                    if title_source != "llm":
                        return False
                    return runner._schedule_telegram_topic_title_rename(
                        source, session_id, display_title or title,
                        user_message=opening_message, model_icon=icon,
                    )
                agent._on_session_title = _schedule_telegram_title
                agent._title_icon_context = runner._telegram_topic_icon_context(source)
            elif runner._is_discord_auto_thread_lane(source) or runner._is_relay_discord_channel_lane(source):
                lane = "_schedule_discord_semantic_thread_rename"
                agent._on_session_title = lambda title, title_source: (
                    title_source == "llm" and getattr(runner, lane)(source, session_id, title)
                )
            else:
                return
        except Exception:
            logger.debug("Failed to attach session title callback", exc_info=True)
