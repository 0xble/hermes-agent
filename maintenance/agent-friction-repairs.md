# Agent friction repairs

Fork patch identity: `agent-friction-repairs`.

## Scope

This unit owns small, evidence-backed fixes where runtime bookkeeping makes the
model repeat work or misread execution state. It covers context-pruning dedupe,
skill_view reload-marker preservation across compaction and tail demotion, the
idempotent-read guardrail at content-loss boundaries, local execute_code session-kernel
eviction reporting and local/remote kernel lifetime and attribution, terminal retry hints,
and execute_code sandbox file-tool failures.

## Idempotent-read guardrail at content-loss boundaries

When compaction, committed tool-result pruning, micro-compaction, or a provider
checkpoint removes earlier read results from model-visible context, the next
`skill_view` or `read_file` call is a recovery read. The boundary hook re-arms
read deduplication and clears only successful-read repeat state, including stale
read halt decisions. Failed-call counters, mutation progress, and same-turn
loop caps remain unchanged.

Focused proof includes `tests/agent/test_tool_guardrails.py` and
`tests/agent/test_read_dedup_rewrite_boundaries.py`. A same-turn 5-call
`skill_view` loop still blocks, while the first read after a content-loss
boundary is allowed and serves full content.

Related prior art is upstream issue [#109683](https://github.com/NousResearch/hermes-agent/issues/109683)
and commit [`f7e565d`](https://github.com/eventh0riz0n/hermes-agent/commit/f7e565d887722458b7666627a405f0f525f907a7).
This fork keeps the smallest fixed behavior: boundary resets apply to successful-read state only,
without adding a user-facing mode switch.

## Terminal retry hints

The host-exit result preserves `[host is exiting: command not started]` and now
states that Hermes is restarting or shutting down, the command was not run, work
will resume after restart, and the model must not retry in the current turn. A
missing pytest result identifies the interpreter problem and selects, from the
command cwd/project, `scripts/run_tests.sh <files>`, `.venv/bin/python -m pytest`,
`uv run pytest`, or a generic project-virtualenv instruction without rerunning.

This was prompted by the last two days of state.db evidence: the host-exit marker
returned 306 times, with 104 identical retries reaching the same-tool-failure
warning; `No module named pytest` appeared about 400 times and `pytest: command not
found` 181 times. Focused verification: `scripts/run_tests.sh tests/tools/test_base_environment.py tests/tools/test_terminal_hints.py`.

## Proof surface

- `tests/agent/test_context_compressor.py::TestSummarizeToolResultRefusals::test_dedupe_does_not_stub_unread_pending_results`
- `tests/agent/test_context_compressor_tail.py`
- `tests/agent/test_ghost_skill_pruning.py`
- `tests/tools/test_skill_view_dedup.py`
- `tests/tools/test_skill_view_dedup.py::TestSkillViewDedupRaces`
- `tests/tools/test_code_kernel.py::TestKernelOwnershipAndLifecycle::test_lru_eviction_reports_state_reset`
- `tests/tools/test_code_execution_file_rpc.py::test_generated_tool_errors_raise_and_block_empty_fallback`
- `tests/agent/test_protected_tail_pressure.py`
- `tests/tools/test_code_kernel.py`

The focused regression suite must preserve unread pending tool results verbatim,
report `kernel.state_reset` with an eviction note after LRU loss (including when
the replacement kernel fails to spawn), keep existing protected-tail and kernel
lifecycle behavior passing, and make the file tools (`read_file`, `write_file`,
`patch`, `search_files`) raise `HermesToolError` for error envelopes on both the
UDS and file-based transports so failed reads cannot feed empty fallback content
into a later write. UDS is proven end to end; the file transport is proven by
the shared `_parse_tool_result` path, which also rejects a `read_file` result
that omits its content via the dedup stub. Skill-view coverage must prove that
compaction and tail demotion retain reload markers for all served skill bodies,
failed views do not create reload obligations, and byte-identical repeat views
are the only ones that return `content_returned: false`. Sandbox RPC reads bypass
normal read-result deduplication so a script always receives real file content.

A sandbox `read_file` not-found result is a backend-namespace failure, not a
successful empty read: project-mode execute_code and its RPC file tools use the
active terminal backend's filesystem. An absolute host path can therefore be
missing inside a container or remote backend even when it exists on the Hermes
host; `HERMES_HOME` scoping and the write guard are not the cause.

Eviction notes end at the owner's `clear_session` boundary. That boundary serves
/new, /reset and auto-reset, which reuse the session key for a different
conversation, so a carried note would tell it to recreate variables it never had.
A conversation resumed across that boundary gets a fresh kernel whether its old
kernel was evicted earlier or killed at the boundary. Remote kernels share the
cap without an eviction note; a dead or disconnected remote kernel reports
`kernel.state_lost`.

## Session-Kernel Lifetime And Attribution

The configured `max_session_kernels` remains an unpinned cache budget, not a
hard active-runner ceiling. Local and remote registries share idle-LRU selection
and final-detach reconciliation. The existing reaper also reconciles overshoot
and promptly removes exited local processes. Live child namespaces are exempt
from the host idle TTL and remain owned by child teardown. A pinned remote runner
still self-exits after `PINNED_RUNNER_IDLE_FACTOR` times the idle timeout, so a
gateway killed without teardown cannot leave it running on the remote host.
A runner measures idle from its last published result, not from pickup. While a
result is missing, the host probes every 5 s. If the runner is gone, it renames
the unclaimed request. A successful rename proves the code never ran, so the cell
reports "did NOT run, safe to retry" instead of waiting out its timeout. The same
rename at the deadline separates an unstarted cell from a real timeout. The
runner treats a vanished request as reclaimed and never runs it. A dead runner
whose request was already taken reports a possible partial run without waiting.

Resource setup and process publication are fenced by each record's lifetime
lock. Teardown signals pending spawn before waiting on that fence, closes the
record permanently and claims cleanup once. Remote acquisition registers an
attached pending record before transport work and serializes cells per record,
without holding the registry lock across transport calls. The remote fallback
remains spawn-only: after cell shipping starts, an ambiguous transport failure
returns an error and retires state rather than replaying code. A retired file-RPC
poller rechecks cancellation after a slow read before dispatching a tool.

Structured lifecycle events use a kernel-instance ID, owner/current task/caller,
attachment and pin state, retirement reason, PID and local process-start identity.
RSS sampling uses the existing process utilities outside registry locks. Remote
process-start identity and RSS are explicitly unknown rather than treating a
remote PID as a host PID. Identity strings pass forced redaction, and touched
lifecycle failure paths omit exception text, tracebacks, code and transport output.
Attribution never raises: spawn and teardown log after claiming their cleanup, so
an escaping failure would orphan the child and make a retry a no-op.

Proof is `tests/tools/test_code_kernel_lifetime.py`, existing real-process
`tests/tools/test_code_kernel.py`, transport-stub
`tests/tools/test_code_kernel_remote.py`, file-RPC tests and
`tests/tui_gateway/test_session_finalize_reaps_kernels.py`.

Related upstream work, not adopted implementations, is
[#105213](https://github.com/NousResearch/hermes-agent/issues/105213) and
[#117169](https://github.com/NousResearch/hermes-agent/issues/117169).
[#136403](https://github.com/NousResearch/hermes-agent/issues/136403) proposes a
separate memory-limit concern, not authority for an RSS kill or hard active cap.
Upstream guidance was checked at
`55ee4acdd822b73fb9573a2321531ae3a9d0b3e0`, and the fork adaptation is based on
`a6521fc59857ceddead3083f34a730c7f9d0d040`. The route retains a bounded fork
repair without adding a policy knob or service. Retire this divergence only
when released upstream preserves the complete lifetime, cache, authority and
secret-safe attribution contract with these regressions passing.

## Foreground heavy-slot queue waits

Interactive foreground local commands yield after
`min(120, max(10, effective_timeout // 3))` while waiting for shared capacity.
[Ticket-preserving handoff](heavy-slot-ticket-handoff.md) superseded the earlier
`HEAVY_SLOT_MAX_WAIT` expiry and exit-75 replay: it adopts the original shell
without losing queue age or repeating compound-shell side effects. Background
commands keep the helper's unbounded wait.
Provenance: 1,196 terminal calls timed out over two days, including 432 pytest and
202 unittest calls; dotfiles#941 added the host helper support.

The 2026-10-29 `sync-hermes-fork` run (`1c9eee960a24`) failed before its tests
started because bounded admission expired under queue contention. Cron has no
later turn to receive a background completion, so foreground cron commands now
keep waiting inline until their own effective timeout, including requests above
the interactive foreground timeout cap. `cron/scheduler.py::_CronRunScope`
binds `turn_origin="cron"`; terminal reads `bound_turn_origin()` from that
task-local session context, never ambient environment state. The same binding
survives `execute_code` RPC dispatch through its per-cell authority. The helper
receives no `HEAVY_SLOT_MAX_WAIT`, and terminal does not automatically promote
or adopt cron work. A terminal timeout with admission still pending kills the
queued process and explicitly reports that the heavy command was not started.
Interactive and gateway handoff budgets remain unchanged. Regressions live in
`tests/tools/test_terminal_tool_heavy_slot.py` and
`tests/tools/test_terminal_heavy_slot_handoff.py`.

## Actionable failure hints

The recurring five-day audit adds no-retry and next-action guidance at the producer:

- `pre_tool_call` timeout blocks explicitly say not to retry the same tool call in the current turn. The fail-closed policy is unchanged.
- `terminal` tells the model to use the process wait action instead of a standalone foreground sleep or timer.
- `execute_code` explains that Hermes tool results are structured dictionaries, not JSON strings, and points JSON parsing at one isolated value or `json_parse()`.
- Plugin skill-file misses list available files, show close matches, and explain the `SKILL.md` fallback when no linked files exist.

The proof surface is `tests/hermes_cli/test_plugins.py`, `tests/tools/test_sandbox_failure_hints.py`, `tests/tools/test_skills_tool.py`, `tests/tools/test_terminal_tool.py`, and `tests/ci/test_prompt_surface.py`.

## Retirement

Retire when an upstream equivalent preserves unread pending tool results and
reports process-wide session-kernel eviction loss with a model-actionable reason,
gives the terminal host-exit and missing-pytest failures equivalent no-retry and
runner guidance, and makes execute_code file-tool error envelopes raise before
user code can continue, with the proof surface passing without this fork
implementation.
