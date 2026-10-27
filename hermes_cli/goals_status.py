"""Goal snapshot reads and status presentation shared by command surfaces."""

from hermes_cli.lifecycle_snapshot import snapshot_control

from agent.notice_style import ContextNotice


class GoalStatusMixin:
    @snapshot_control
    def refresh(self):
        """Read the durable winner without writing over another manager's changes."""
        from hermes_cli.goals import GoalPersistenceError, load_goal, logger

        try:
            self._state = (load_goal(self.session_id) if self._cursor is None else
                           load_goal(self.session_id, cursor=self._cursor))
        except GoalPersistenceError as exc:
            logger.warning("GoalManager: goal for %s is unreadable; treating it as inert: %s",
                           self.session_id, exc)
            self._state, self._unreadable = None, exc
        else:
            self._unreadable = None
        return self._state

    def status_line(self) -> str:
        from hermes_cli.goals import (GoalRowCorrupt, _goal_budget_label, contract_items,
                                      _session_waiting, _pid_alive, time)

        s = self._state
        if isinstance(self._unreadable, GoalRowCorrupt):
            return ("⚠ The stored goal is unreadable, so it is inert: no continuation or judge runs. "
                    "/goal clear discards it; then /goal <text> sets a new one.")
        if self._unreadable is not None:
            return "⚠ The stored goal could not be read right now, so it is inert this turn. Try again."
        if s is None or s.status == "cleared":
            return "No active goal. Set one with /goal <text>."
        turns = f"{_goal_budget_label(s.turns_used, s.max_turns)} turns"
        subgoal_items = contract_items("subgoals", s.subgoals)
        sub = f", {len(subgoal_items)} subgoal{'s' if len(subgoal_items) != 1 else ''}" if subgoal_items else ""
        con = ", contract" if self.has_contract() else ""
        gat = f", {len(s.gates)} gate{'s' if len(s.gates) != 1 else ''}" if s.gates else ""
        meta = f"{turns}{sub}{con}{gat}"
        if s.status == "active":
            if s.waiting_on_session and _session_waiting(s.waiting_on_session):
                return ContextNotice(f"⏳ Goal (parked on {s.waiting_reason or f'session {s.waiting_on_session}'}, {meta}): {s.goal}")
            if s.waiting_on_pid and _pid_alive(s.waiting_on_pid):
                return ContextNotice(f"⏳ Goal (parked on {s.waiting_reason or f'pid {s.waiting_on_pid}'}, {meta}): {s.goal}")
            if s.waiting_until and time.time() < s.waiting_until:
                remaining = int(s.waiting_until - time.time())
                wr = s.waiting_reason or f"{remaining}s"
                return ContextNotice(f"⏳ Goal (parked {remaining}s — {wr}, {meta}): {s.goal}")
            if s.waiting_on_user_message:
                return f"⏳ Goal (waiting for your message — {meta}): {s.goal}"
            return ContextNotice(f"⊙ Goal (active, {meta}): {s.goal}")
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {meta}{extra}): {s.goal}"
        if s.status == "done":
            return ContextNotice(f"✓ Goal done ({meta}): {s.goal}")
        return f"Goal ({s.status}, {meta}): {s.goal}"

