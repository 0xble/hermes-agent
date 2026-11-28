# Goal and loop gate repair

## Identity

- **Fork patch identity:** `goal-loop-gate-repair`
- **Contract tests:** `tests/hermes_cli/test_goal_ownership_lints.py`

## Required behavior

Agent-owned goals and loops may revise wording, cadence, stop conditions, pause, resume their own pause, and finish without an unnecessary user quote. A goal replacement retains unrelated criteria and subgoals. Clearing or stopping an agent-owned activity is allowed only for that activity; a user pause or user-owned constraint remains protected.

A user-set constraint, hard limit, verification criterion, or due date cannot be removed or relaxed without a verbatim current user instruction. Goal completion still requires concrete evidence for every criterion. Ownership authoring lints require `due` and `done_when`/`verification`, reject cache or missing `plan_path`/`log_path`, and reject unsourced approval wording. Existing rows are reported as warnings by the read-only `scripts/goal_hygiene.py` report.
