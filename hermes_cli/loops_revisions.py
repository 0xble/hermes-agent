"""Versioned loop revisions; authority policy is unchanged by snapshot/CAS settlement."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from hermes_cli.loops import LoopState


class LoopRevisionsMixin:
    @staticmethod
    def _revision_result(*, ok: bool, revision: Optional[Dict[str, Any]], version: int,
                         error_code: str = "", error: str = "") -> Dict[str, Any]:
        return {
            "ok": ok,
            "error_code": error_code,
            "error": error,
            "revision": revision,
            "version": version,
        }

    def _revision_quote(self, state: LoopState, user_quote: str,
                        user_messages: Optional[List[str]], *, required: bool) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
        """Validate a verbatim user quote using the goal lifecycle's shared source rules."""
        from hermes_cli.goals import (
            _REVISION_QUOTE_MIN_CHARS,
            _REVISION_SOURCE_MAX_CHARS,
            user_messages_since,
        )

        quote = " ".join((user_quote or "").split())
        if required and not quote:
            return None, "user_authority_required", {
                "error": (
                    f"this change needs user_quote: a verbatim excerpt "
                    f"({_REVISION_QUOTE_MIN_CHARS}+ chars) of the user's instruction in this session"
                )
            }
        if not quote:
            return "", None, None
        if len(quote) < _REVISION_QUOTE_MIN_CHARS:
            return None, "user_quote_too_short", {
                "error": f"user_quote must be at least {_REVISION_QUOTE_MIN_CHARS} characters"
            }
        pool = user_messages if user_messages is not None else user_messages_since(
            self.session_id, state.created_at
        )
        sources = [" ".join(message.split()) for message in pool if quote in " ".join(message.split())]
        if not sources:
            return None, "user_quote_not_found", {
                "error": "user_quote does not match any user message sent since the loop was set"
            }
        source = next((message for message in sources if len(message) <= _REVISION_SOURCE_MAX_CHARS), "")
        if not source:
            return None, "user_message_too_long", {
                "error": (
                    f"the quoted user message exceeds {_REVISION_SOURCE_MAX_CHARS} characters, too long to "
                    "judge whether it authorizes this change; ask the user to state the change in a short "
                    "message and quote that"
                )
            }
        return quote, None, {"user_message": source}

    def revise(
        self,
        *,
        reason: str,
        actor: str = "agent",
        prompt: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        self_paced: bool = False,
        times: Optional[int] = None,
        until: Optional[str] = None,
        user_quote: str = "",
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Apply a versioned, authority-checked edit to the existing loop.

        Commit only against the durable snapshot this revision read.
        """
        from hermes_cli.loops_snapshot import mutate_loop_snapshot
        return mutate_loop_snapshot(self, "_revise", reason=reason, actor=actor, prompt=prompt, interval_seconds=interval_seconds, self_paced=self_paced, times=times, until=until, user_quote=user_quote, user_messages=user_messages)

    def _revise(
        self,
        *,
        reason: str,
        actor: str = "agent",
        prompt: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        self_paced: bool = False,
        times: Optional[int] = None,
        until: Optional[str] = None,
        user_quote: str = "",
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Apply a versioned, authority-checked edit to the existing loop."""
        from hermes_cli.loops import min_interval_seconds, self_paced_floor_seconds
        state = self._state
        version = state.version if state is not None else 1
        reason = (reason or "").strip()
        if not reason:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="reason_required", error="a revision needs a reason",
            )
        if state is None or state.status not in {"active", "paused"}:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="no_loop", error="there is no active or paused loop to revise",
            )
        if interval_seconds is not None and self_paced:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="invalid_cadence",
                error="interval_seconds and self_paced are mutually exclusive",
            )

        before = {
            "prompt": state.prompt,
            "mode": state.mode,
            "interval_seconds": state.interval_seconds,
            "current_delay": state.current_delay,
            "times": state.times,
            "until": state.until,
        }
        after = dict(before)
        clamped_from: Optional[int] = None
        if prompt is not None:
            new_prompt = (prompt or "").strip()
            if new_prompt:
                after["prompt"] = new_prompt
        if interval_seconds is not None:
            requested = int(interval_seconds)
            applied = max(requested, min_interval_seconds())
            clamped_from = requested if applied != requested else None
            after.update({
                "mode": "interval",
                "interval_seconds": float(applied),
                "current_delay": float(applied),
            })
        elif self_paced:
            floor = float(self_paced_floor_seconds())
            after.update({"mode": "self_paced", "interval_seconds": 0.0, "current_delay": floor})
        if times is not None:
            after["times"] = max(0, int(times))
        if until is not None:
            after["until"] = (until or "").strip()

        changed = [key for key in before if after[key] != before[key]]
        if not changed:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="no_change", error="the revision changes nothing",
            )

        current_effective_delay = (
            state.interval_seconds if state.mode == "interval"
            else (state.current_delay or float(self_paced_floor_seconds()))
        )
        new_effective_delay = (
            after["interval_seconds"] if after["mode"] == "interval"
            else float(self_paced_floor_seconds())
        )
        # Switching to self-paced starts at the floor, so it is faster whenever the floor is below the
        # current delay; it gets the same authority check as any other cadence change.
        faster = new_effective_delay < current_effective_delay
        raising_times = (
            "times" in changed
            and state.times > 0
            and (after["times"] == 0 or after["times"] > state.times)
        )
        needs_authority = (
            ("prompt" in changed)
            or ("until" in changed)
            or faster
            or raising_times
        )
        quote, quote_error, quote_detail = self._revision_quote(
            state, user_quote, user_messages, required=needs_authority
        )
        if quote_error:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code=quote_error, error=(quote_detail or {}).get("error", "invalid user quote"),
            )

        now = time.time()
        revision = {
            "at": now,
            "actor": actor,
            "reason": reason,
            "user_quote": quote or "",
            "user_message": (quote_detail or {}).get("user_message", "") if quote else "",
            "before": {key: before[key] for key in changed},
            "after": {key: after[key] for key in changed},
        }
        if clamped_from is not None:
            revision["clamped"] = {
                "interval_seconds": clamped_from,
                "applied": after["interval_seconds"],
            }
        for key in changed:
            setattr(state, key, after[key])
        state.backoff_level = 0
        state.last_response_digest = ""
        state.current_delay = (
            float(self_paced_floor_seconds()) if state.mode == "self_paced" else state.interval_seconds
        )
        reset_schedule = state.mode == "self_paced" or any(
            key in changed for key in ("mode", "interval_seconds", "current_delay")
        )
        if reset_schedule and not state.awaiting_response:
            state.next_due_at = max(
                now,
                (state.last_fired_at or now) + new_effective_delay,
            )
        state.revisions.append(revision)
        self._save()
        return self._revision_result(
            ok=True, revision=revision, version=state.version,
        )

    def replace(
        self,
        *,
        prompt: str,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[Dict[str, str]] = None,
        reason: str,
        user_quote: str,
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Replace the loop definition while preserving its revision history and route.

        Commit only against the durable snapshot this replacement read.
        """
        from hermes_cli.loops_snapshot import mutate_loop_snapshot
        return mutate_loop_snapshot(self, "_replace", prompt=prompt, interval_seconds=interval_seconds, times=times, until=until, route=route, reason=reason, user_quote=user_quote, user_messages=user_messages)

    def _replace(
        self,
        *,
        prompt: str,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[Dict[str, str]] = None,
        reason: str,
        user_quote: str,
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Replace the loop definition while preserving its revision history and route."""
        from hermes_cli.loops import max_ticks_default, min_interval_seconds, self_paced_floor_seconds
        state = self._state
        version = state.version if state is not None else 1
        reason = (reason or "").strip()
        if not reason:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="reason_required", error="a replacement needs a reason",
            )
        if state is None or state.status not in {"active", "paused"}:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="no_loop", error="there is no active or paused loop to replace",
            )
        prompt = (prompt or "").strip()
        if not prompt:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="no_change", error="replacement prompt is empty",
            )
        quote, quote_error, quote_detail = self._revision_quote(
            state, user_quote, user_messages, required=True
        )
        if quote_error:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code=quote_error, error=(quote_detail or {}).get("error", "invalid user quote"),
            )
        now = max(time.time(), state.created_at + 1e-6)
        self_paced = interval_seconds is None
        interval = 0.0 if self_paced else float(max(int(interval_seconds), min_interval_seconds()))
        new_times = max(0, int(times or 0))
        new_until = (until or "").strip()
        old_snapshot = {
            "prompt": state.prompt,
            "mode": state.mode,
            "interval_seconds": state.interval_seconds,
            "current_delay": state.current_delay,
            "times": state.times,
            "until": state.until,
            "ticks_fired": state.ticks_fired,
        }
        new_snapshot = {
            "prompt": prompt,
            "mode": "self_paced" if self_paced else "interval",
            "interval_seconds": interval,
            "current_delay": float(self_paced_floor_seconds()) if self_paced else interval,
            "times": new_times,
            "until": new_until,
            "ticks_fired": 0,
        }
        revision = {
            "at": now,
            "actor": "agent",
            "reason": reason,
            "user_quote": quote or "",
            "user_message": (quote_detail or {}).get("user_message", "") if quote else "",
            "kind": "replace",
            "before": old_snapshot,
            "after": new_snapshot,
        }
        old_route = dict(state.route)
        # Resume is a user-only control: a replaced paused loop stays paused (with its reason) until /loop resume.
        was_paused = state.status == "paused"
        state.prompt = prompt
        state.status = "paused" if was_paused else "active"
        state.mode = new_snapshot["mode"]
        state.interval_seconds = interval
        state.current_delay = new_snapshot["current_delay"]
        state.times = new_times
        state.until = new_until
        state.max_ticks = max_ticks_default()
        state.ticks_fired = 0
        state.created_at = now
        state.last_fired_at = 0.0
        state.next_due_at = now
        state.awaiting_response = False
        state.last_response_digest = ""
        state.backoff_level = 0
        if not was_paused:
            state.paused_reason = None
        state.last_stop_reason = None
        state.route = dict(old_route if route is None else route)
        state.revisions.append(revision)
        self._save()
        return self._revision_result(ok=True, revision=revision, version=state.version)
