# S4.2 Router/Executor Feasibility Spike

This throwaway prototype is **not** the production split. It uses two executor processes running real `AIAgent` turns through the repository's fake OpenAI-compatible model server, a router process owning a synthetic HTTP poll/send transport, an authenticated local Unix socket, and a disposable SQLite admission/outbox store. Run `scripts/run_tests.sh tests/gateway/test_s4_spike.py -q` for the short integration case. The long run uses `.venv/bin/python -m spikes.s4_router_executor.run_minute <disposable-home>`. No real Telegram token, launchd label or live gateway is involved.

## Verdict: PARTIAL

### What Worked

- A real 60-second `terminal` tool call returned exit code 0 on executor A, while executor B answered another session before A completed.
- The router alone polled the stub transport and sent the final replies. Replacing it while A ran let A continue and deliver its final once in the controlled run.
- A's follow-up remained ordered after its first turn, and the stub saw three sends with unique IDs.

### What Didn't

- The executors import the same worktree and are not pinned to two distinct release directories.
- This does not exercise Hermes's Telegram adapter, either native busy guard, native approval registry or `tools.async_delegation` path. The local SQLite queue is a probe, not S4.1's schema.
- No stream-edit frames are forwarded or resumed. Only the final reply is delivered after router replacement.
- An earlier short test run saw one duplicate stub send after the first router was killed between HTTP `/send` and the SQLite `sent` write. Moving the controlled kill after B's receipt made the short test deterministic, but did not solve the ambiguity. Telegram sendMessage has no guaranteed idempotency key in this probe.

### Surprises

- The first minute probe did not run its tool: the cron approval gate blocked `python -c` and the test initially mistook the tool-call arguments for tool output. Replacing it with `sleep 60` and inspecting the actual tool result proved execution. Recovered evidence is in the designated disposable home.
- The test runner strips custom environment overrides. The minute run must be invoked separately to preserve its evidence in a named disposable home.

### Recommendation For The Real Build

Do not start S4.2 on this evidence. First land and exercise S4.1's durable outbox and ambiguous-send handling; then prototype real Telegram adapter forwarding, both busy guards, background delegation, scoped approval delivery, stream edits and separate pinned release imports. The alternative two-gateway overlap still needs its own token and send-receipt proof. Keep S1–S3 in service meanwhile.
