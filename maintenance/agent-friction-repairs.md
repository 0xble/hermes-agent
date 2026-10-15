# Agent friction repairs

Fork patch identity: `agent-friction-repairs`.

## Scope

This unit owns small, evidence-backed fixes where runtime bookkeeping makes the
model repeat work or misread execution state. It currently covers context-pruning
dedupe and local execute_code session-kernel eviction reporting.

## Proof surface

- `tests/agent/test_context_compressor.py::TestSummarizeToolResultRefusals::test_dedupe_does_not_stub_unread_pending_results`
- `tests/tools/test_code_kernel.py::TestKernelOwnershipAndLifecycle::test_lru_eviction_reports_state_reset`
- `tests/agent/test_protected_tail_pressure.py`
- `tests/tools/test_code_kernel.py`

The focused regression suite must preserve unread pending tool results verbatim,
report `kernel.state_reset` with an eviction note after LRU loss (including when
the replacement kernel fails to spawn), and keep the existing protected-tail and
kernel lifecycle behavior passing.

Eviction notes end at the owner's `clear_session` boundary. That boundary serves
/new, /reset and auto-reset, which reuse the session key for a different
conversation, so a carried note would tell it to recreate variables it never had.
A conversation resumed across that boundary gets a fresh kernel whether its old
kernel was evicted earlier or killed at the boundary. Remote kernels share the
cap without an eviction note; a dead or disconnected remote kernel reports
`kernel.state_lost`.

## Retirement

Retire when an upstream equivalent preserves unread pending tool results and
reports process-wide session-kernel eviction loss with a model-actionable reason,
and the proof surface passes without this fork implementation.
