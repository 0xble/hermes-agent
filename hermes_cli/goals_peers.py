"""Parent-owned peer dependencies on the native goal snapshot and evidence boundary."""
from __future__ import annotations

import re
import time

from hermes_cli.lifecycle_snapshot import snapshot_control


def validate_peer_completion(state):
    if state.status == "done" and state.pending_peers:
        raise ValueError("pending peer work requires parent verification before goal completion")


def peer_address(value):
    # A routing identity, not a process/delegation id. Preserve it verbatim.
    if not isinstance(value, str) or not re.fullmatch(r"hermes:[^/\s]+/[^/\s]+", value):
        raise ValueError("peer must be an exact hermes:<profile>/<session> address")
    return value


def parse_pending_peers(value):
    if not isinstance(value, list):
        raise ValueError("pending peers must be a list")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"peer", "ref", "result"}
                or any(not isinstance(v, str) or not v.strip() for v in item.values())):
            raise ValueError("invalid pending peer work")
        peer_address(item["peer"])
    return value


class GoalPeersMixin:
    @snapshot_control
    def wait_on_peer(self, peer, ref, requested_result, *, expected_instance):
        from hermes_cli.goals_revisions import revise_goal_snapshot

        return revise_goal_snapshot(self, _peer="wait", peer=peer, ref=ref,
                                    requested_result=requested_result, expected_instance=expected_instance)

    @snapshot_control
    def resolve_peer(self, peer, ref, evidence, *, expected_instance):
        from hermes_cli.goals_revisions import revise_goal_snapshot

        return revise_goal_snapshot(self, _peer="resolve", peer=peer, ref=ref,
                                    evidence=evidence, expected_instance=expected_instance)

    def _peer_change(self, *, operation, peer, ref, expected_instance,
                     requested_result="", evidence=""):
        from gateway.session_context import bound_session_env
        from hermes_cli.goals_autonomy import completion_proof_is_concrete
        from hermes_cli.session_controls import _current_profile

        state = self._require_goal()
        caller = bound_session_env("HERMES_SESSION_ID", "")
        profile = bound_session_env("HERMES_SESSION_PROFILE", "")
        if caller != self.session_id or (profile and profile != _current_profile()):
            return {"ok": False, "error": "only the current parent goal context may change peer waits"}
        if state.created_at != expected_instance:
            return {"ok": False, "error": "stale goal instance"}
        peer_address(peer)
        if not isinstance(ref, str) or not ref.strip():
            return {"ok": False, "error": "an existing work reference is required"}
        match = next((row for row in state.pending_peers if (row["peer"], row["ref"]) == (peer, ref)), None)
        if operation == "wait":
            if not isinstance(requested_result, str) or not requested_result.strip():
                return {"ok": False, "error": "the requested result and verification are required"}
            if match:
                return {"ok": match["result"] == requested_result,
                        "error": "" if match["result"] == requested_result else "reference already has a different requested result"}
            item = {"peer": peer, "ref": ref, "result": requested_result}
            state.pending_peers.append(item)
            record = {"kind": "peer_wait", **item}
        else:
            if match is None:
                return {"ok": False, "error": "no exact pending peer reference"}
            if not isinstance(evidence, str) or not completion_proof_is_concrete(evidence):
                return {"ok": False, "error": "cite concrete parent-recorded tool proof for the requested result"}
            if failure := self._recorded_evidence_error(evidence, state):
                return {"ok": False, **failure}
            state.pending_peers.remove(match)
            record = {"kind": "peer_resolved", **match, "evidence": evidence}
        state.revisions.append({"at": time.time(), "actor": "agent", "authority": "parent", **record})
        self._save()
        return {"ok": True, "revision": state.revisions[-1]}

    @snapshot_control
    def mark_done(self, reason):
        def apply(state):
            if state is None:
                return
            if state.pending_peers:
                raise ValueError("pending peer work requires parent verification before goal completion")
            state.clear_wait()
            state.status = "done"
            state.last_verdict = "done"
            state.last_reason = reason
        self._mutate_current(apply)

    def _finish_judged_goal(self, reason):
        from hermes_cli.goals import _decision

        if self.state.pending_peers:
            return self._apply_wait_directive({"seconds": 1200},
                "Pending peer work requires parent verification before goal completion")
        self.mark_done(reason)
        return _decision("done", False, None, "done", reason,
                         f"✓ Goal achieved: {reason}")
