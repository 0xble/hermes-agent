# Agent friction repairs

Fork patch identity: `agent-friction-repairs`.

## Scope

This unit owns small, evidence-backed fixes where runtime bookkeeping makes the
model repeat work or misread execution state. It covers context-pruning dedupe,
skill_view reload-marker preservation across compaction and tail demotion, the
idempotent-read guardrail at content-loss boundaries, local execute_code session-kernel
eviction reporting, terminal retry hints, and execute_code sandbox file-tool failures.

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

## Foreground heavy-slot queue waits

Foreground local commands now bound their shared-slot admission wait to
`min(120, max(10, effective_timeout // 3))` via `HEAVY_SLOT_MAX_WAIT`, while
background commands keep the helper's unbounded wait. A single simple heavy
command that reaches exit 75 is promoted automatically to a tracked background
run without asking the model to retry; compound commands retain the explicit
not-started result because replaying their shell would repeat earlier side effects.
Provenance: 1,196 terminal calls timed out over two days, including 432 pytest and
202 unittest calls; dotfiles#941 added the host helper support.

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
