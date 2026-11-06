"""Declarative lifecycle authority. See maintenance/goal-autonomy.md for the contract.

Columns are agent callers: user-owned/off, agent-owned/off, user-owned/on,
agent-owned/on, then trusted-user (any owner/config). A valid action/target/item-scoped
quote satisfies Q, never D. Unknown ownership is user ownership. State validation,
quote authentication and transaction/CAS fencing remain the managers' jobs.
"""
from itertools import product

A, Q, D = "allow", "require_quote", "deny"
POLICY_ROWS = {
    ("goal", "outcome"): (Q, A, Q, A, A),
    ("goal", "verification"): (Q, Q, Q, Q, A),
    ("goal", "constraints"): (Q, Q, Q, Q, A),
    ("goal", "boundaries"): (Q, Q, Q, Q, A),
    ("goal", "subgoals"): (Q, Q, Q, Q, A),
    ("goal", "stop_when"): (Q, A, Q, A, A),
    ("goal", "max_turns"): (Q, A, Q, A, A),
    ("goal", "objective"): (Q, A, A, A, A),
    ("goal", "revise"): (A, A, A, A, A),
    ("goal", "replace"): (Q, A, A, A, A),
    ("goal", "pause"): (Q, A, A, A, A),
    ("goal", "resume"): (Q, A, A, A, A),
    ("goal", "resume_user_pause"): (Q, Q, Q, Q, A),
    ("goal", "stop"): (Q, Q, D, D, A),
    ("goal", "clear"): (Q, Q, D, D, A),
    ("loop", "outcome"): (Q, A, A, A, A),
    ("loop", "stop_when"): (Q, A, A, A, A),
    ("loop", "budget"): (Q, A, A, A, A),
    ("loop", "cadence_faster"): (Q, A, A, A, A),
    ("loop", "cadence_slower"): (A, A, A, A, A),
    ("loop", "budget_reduce"): (A, A, A, A, A),
    ("loop", "revise"): (A, A, A, A, A),
    ("loop", "replace"): (Q, A, A, A, A),
    ("loop", "pause"): (Q, A, A, A, A),
    ("loop", "resume"): (Q, A, A, A, A),
    ("loop", "resume_user_pause"): (Q, Q, Q, Q, A),
    ("loop", "stop_user_pause"): (Q, Q, Q, Q, A),
    ("loop", "stop"): (Q, A, A, A, A),
    ("loop", "stop_user_condition"): (Q, Q, A, A, A),
    ("loop", "clear"): (D, D, D, D, D),
}


def _cell(columns, owner, actor, autonomous, quoted):
    value = columns[4] if actor == "user" else columns[2 * int(autonomous) + int(owner == "agent")]
    return A if quoted and value == Q else value


# Materialize the full Cartesian table so callers and the boundary test exercise
# exactly the same finite domain, not independently reconstructed permission ladders.
POLICY = {
    (kind, operation, owner, actor, autonomous, quoted):
        _cell(columns, owner, actor, autonomous, quoted)
    for (kind, operation), columns in POLICY_ROWS.items()
    for owner, actor, autonomous, quoted in product(
        ("user", "agent"), ("user", "agent"), (False, True), (False, True))
}


def decision(kind, operation, state, *, actor="agent", autonomous=False, quoted=False, owner=None):
    """Fail closed for unknown operations/actors; ownership comes only from stored provenance."""
    from hermes_cli.lifecycle_authority import agent_owned, user_stop_condition

    if state is not None and state.status == "paused" and state.paused_by != "agent":
        if operation == "resume":
            operation = "resume_user_pause"
        elif kind == "loop" and operation == "stop":
            operation = "stop_user_pause"
    if kind == "loop" and operation == "stop" and state is not None and state.until and user_stop_condition(state):
        operation = "stop_user_condition"
    owner = owner or ("agent" if agent_owned(state) else "user")
    return POLICY.get((kind, operation, owner, actor, autonomous, quoted), D)
