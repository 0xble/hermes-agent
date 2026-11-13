# Incognito Topics

Fork patch identity: `incognito`.

This is a private fork implementation, not an upstream contribution. The upstream
[temporary-chat request](https://github.com/NousResearch/hermes-agent/issues/52341)
and [related implementation](https://github.com/NousResearch/hermes-agent/pull/19448)
are comparison candidates, not substitutes for the accepted mid-session contract.
The CLI's safe mode and rule opt-outs do not establish a persistence boundary.

## Ownership And Adoption

The user-facing contract and configuration belong to the Incognito Gateway Topics
section in `website/docs/user-guide/configuration.md`. The privacy decision is
shared through `agent/incognito.py`, with gateway ingress and RAM-store routing in
`gateway/incognito.py`. Read the Hindsight memory, Telegram ingress non-blocking,
and immutable-release units when changing those dependencies.

Retain the local implementation until an upstream replacement covers existing
saved prefixes, entry and exit within the same session, optional model routing,
and all Hermes-owned content sinks. A feature named temporary chat that merely
avoids the primary transcript is not equivalent. Compare lifecycle callbacks,
plugin dispatch, pending writes, delayed workers and pre-gateway media handling,
not only the command reply.

## Divergence To Preserve

- Restoring the pre-entry agent and transcript snapshot is intentional. Starting
  a new public session on exit, retaining the private tail, or rebuilding its
  frozen prompt changes the accepted behavior.
- SQLite admission precedes SQL execution and is serialized against registration
  changes. Rollback, deletion and WAL cleanup cannot substitute for withholding
  the write, because private bytes may already have reached storage. Writes share
  `agent.incognito.persistence_admission()` and entering a boundary is exclusive.
  Registry reads, including the logging filter, must never wait for a commit.
- Entering incognito delivers public work queued before the boundary instead of
  dropping it: queued memory writes, usage deltas and Hindsight retains or
  buffered turns. Only work created while private is refused.
- A stale sidecar record refuses only its own topic and keeps its recorded
  session private. Remove the record to repair that topic.
- `/new` in a private topic ends the interval, then binds, titles and announces
  the new session as public work. `/topic` is refused while private.
- The content-free sidecar preserves privacy routing across restart. Model
  overrides pass through the existing sanitizer. Do not persist a private model
  selection or credentials as recovery metadata.
- Plugin content observation is opt-in through an explicit `incognito_safe: true`,
  including tools, hooks, middleware and event subscribers. An omitted declaration
  refuses the plugin tool. A declared-safe tool runs with `plugin_data_dir` and
  `PluginState` redirected into the private interval; direct `HERMES_HOME` writes
  remain the plugin author's responsibility. New execution paths need the same
  admission owner, not an independently maintained exception list. Hindsight read
  operations remain available. A queued recall keeps the route it was queued
  under, and pending recall is discarded at every boundary. A refused private
  recall shows in the recall indicator instead of returning silently empty.
- Hindsight's private transport in `plugins/memory/hindsight/private_reads.py`
  deliberately bypasses public SDK transports and embedded-daemon startup. Its
  bodyless `/version` handshake requires `features.private_reads: true`, then
  only `/v1/default/banks/{bank_id}/private/recall` or `/private/reflect` may receive
  the query. Require `X-Hindsight-Private-Read: 1` on success. Never substitute a
  public read or preserve provider exception text when this protocol fails.
- Native session authorities persist complete input envelopes and worker receipts.
  Private input and plugin injections must stay on the in-process RAM lane, and
  delayed admission, worker, local-mutation and compression writers must carry
  every affected session identity into SQLite admission.
- Relay instrumentation, cached public Relay leases and shared metrics must not
  observe a private turn. Refuse instrumentation before discovering or creating
  a host, not after the subscriber has queued its payload.
- Manual compression keeps the session identity and rewrites the RAM transcript.
  The normal public compression rotation guard remains in force.
- Intake policy belongs to the receiving adapter. Observed groups whose command
  and conversation keys differ are refused, rather than changing actor identity
  or claiming privacy on only one of the lanes.
- Media without a persistent store may use the interval's private directory, which is
  deleted on `/incognito off` and `/new`; no private artifact may outlive that interval.
  A SIGKILL of the outgoing zero-cut gateway can leave its interval until the next startup sweep.
  The default parent is outside `HERMES_HOME`, in the account cache root and scoped to the
  process Hermes home (`~/Library/Caches/Hermes/incognito/<home-hash>/` on macOS,
  `$XDG_CACHE_HOME/Hermes/incognito/<home-hash>/` elsewhere), because the startup sweep
  removes everything under the parent and must never reach another home's live interval.
  `incognito.private_dir` may override that parent (for example, to a RAM-backed volume)
  and must not be shared between Hermes homes. Kernel scripts, spill files and temporary
  content stay inside the interval. The random 0600 AF_UNIX RPC endpoint uses
  `socket_safe_tmpdir()` because deep private paths exceed the OS socket-path limit.
  Socket nodes contain no persisted payload and are unlinked on kernel teardown.
  Resolver identity is `agent.incognito_private_dir`: a bound public session is never
  redirected into another topic's directory. A thread with no bound identity, while any
  private interval is active, uses a shared `unattributed` disposable directory under the
  same parent, removed when the last interval ends and by the startup sweep. Durable
  plugin data redirects only on a positive private identity. Cleanup that cannot delete
  a directory records a content-free failure through `log_private_failure` and does not
  fail `/incognito off`; the startup sweep reports how many directories it removed.
- Picker dispatch and ingress share 1 callback-family registry. Only exact chat
  and sent-message IDs may resolve existing RAM prompt state and its session lane.
  Stale buttons cannot run or consume a newer prompt. Unmatched pickers remain
  privacy-ambiguous, not guessed public topics.
- A failed private turn or route resolution records only its exception type and
  innermost Hermes frame, each name checked against a source-code identifier
  pattern, created outside the private scope by `agent.incognito.log_private_failure`.
  It never raises into the reply path. Every other record from the turn stays
  redacted whole, so never add the message, arguments or locals to that line.
- Credential-pool status keeps only classifier fields while private. Provider
  error text can echo the prompt, so it never reaches `auth.json` from a private
  turn.
- A private agent prints nothing to raw stdout. A gateway redirects stdout to
  `gateway.log`, which the privacy log filter never sees. Agent status output,
  including turn-recovery diagnostics, goes through `_safe_print`, which owns
  that guard. New output on a turn path must use it rather than bare `print`.
  Status and warnings still reach the user through `status_callback`.
- Async ingress reads the loaded store's published routing index on the event
  loop. Only an unloaded store resolves off-loop. Routing every message through
  the default executor queues ingress behind unrelated jobs and lets held
  Telegram input miss handover admission.

## Proof And Retirement

The focused proof surfaces are `tests/agent/test_incognito.py`,
`tests/agent/test_incognito_main_sinks.py`, the
`tests/gateway/test_incognito*` tests, `tests/hermes_state/test_incognito*`,
`tests/hermes_cli/test_incognito_plugins.py`,
`tests/plugins/platforms/telegram/test_incognito_ingress.py`, and
`tests/tools/test_delegate_tool.py`. The real Telegram admission and multiplex
transport tests exercise the receiving adapter rather than a synthetic shortcut.
`tests/gateway/test_incognito_turn_e2e.py` is the end-to-end contract. Its
resolver test runs real gateway turns through the production agent resolver and
cache, replacing only the provider client, Hindsight's cloud client and the
summary call: a configured `incognito.model` reached through a model alias,
compaction in RAM anchored on real usage, agent reuse across private turns, a
classified provider failure, no retain, and `/incognito off` restoring the route
and saved prefix. The `/new` test runs the real reset and observes only the
parked agent's release. The failure-record tests replace `_run_agent` to inject
an exception. Do not replace the gateway methods these tests cross with plain
mocks, which is how the private persistence call shipped with the wrong
signature. Where a test must stub one, use `create_autospec`.
Compare the complete suite's failures against the exact base before promotion.
Off-mode differential proofs must compare the merged main revision, retaining
exact SQL traces, SQLite pages and plugin callback assertions. A historical base
with an older schema cannot establish current main compatibility.
`tests/hermes_state/test_incognito_off_regression.py` runs only where git
history shows the unmerged feature on its pinned `BASE`, so shallow hosted CI
skips it. Run it from a full checkout before promotion and re-pin `BASE` after
any rebase.

Runtime proof needs both an ordinary topic and a private interval. Audit the
interval's exact session and canary hash, not unrelated bank-wide activity. Arm
and probe the filesystem observer before minting the canary in sender RAM. Keep
its literal out of command arguments, transcripts and evidence artifacts. Verify
recall independently, unchanged saved-prefix and memory-note bytes, no private
content under the Hermes home or in Hindsight, route switching and restoration,
and resumed ordinary persistence after exit.

The control socket's `session-snapshot` witness must use the already resident,
bound SessionDB handle without reviving a closed handle or waiting for its lock.
Private recall proof is a correlated tool-return observed in RAM, not an
assistant's assertion or an audit row. Persist only hashes and match counts.

Retire this unit with its implementation only after a replacement passes that
same boundary proof. A source rollback removes the feature, it does not retract
content already sent to messaging or model providers. Never turn an active
private lane public merely by ignoring or deleting its routing sidecar.
