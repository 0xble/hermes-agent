# Thread Status Continuity

## Contract and Independent Hypothesis

The native Slack assistant status belongs to a thread, not its DM channel.
Typing refresh, pause, phrase updates and cleanup must use the same scope. A
sibling thread finishing or waiting for input must not alter another thread.
Intermediate delivery must retain the running thread's status. Final delivery,
interruption and failure must still clear it.

Source inspection on 2026-10-24 found chat-only `_typing_paused` and `_status_text`
state in the shared adapter despite thread-scoped Slack status tracking. Final
delivery pauses the whole chat, and tool callbacks overwrite the whole chat's
phrase. Slack `send()` also clears status after every post, including interim
posts, while `_keep_typing` sets it again on its next 2-second tick. These are
separate, reproducible defects. The claim that Telegram's cadence hook changed
Slack cadence is falsified by the existing base cadence regression: the base
hook still returns False and the base interval remains 2 seconds.

The repair belongs in shared typing scope resolution and its existing consumers,
with Slack's persistent-status delivery behavior kept in its adapter. Tests must
exercise 2 threads sharing a DM, a paused/finalizing sibling, distinct phrases,
and intermediate delivery followed by another refresh. If those probes do not
fail before the change, this hypothesis must be revised. UI animation caused by
repeated identical API updates is a separate possibility, not established by
source inspection alone.

## Provenance

- Patch identity: `thread-status-continuity`.
- Source surfaces: `gateway/platforms/typing_state.py`, `gateway/platforms/base.py`, typing/status callers in `gateway/`, and the Slack and Telegram adapters.
- Regression: `tests/gateway/test_slack.py`, `tests/gateway/test_typing_refresh_interval.py`, `tests/gateway/test_telegram_typing_retrigger.py`, and legacy clarify/approval adapter tests.
- Metadata is forwarded only when an adapter override accepts it, preserving legacy pause, resume and status signatures.
- Prompt pause/resume records the original refresh surface by session on the delivery adapter. Typed approval, deny, clarify and relay answers consume that record instead of deriving a new surface from the answer event. Turn cleanup retires abandoned records.
- Typing uses `typing_metadata_for_source`, independently of message-delivery metadata. Top-level Mattermost and Buzz progress can target a synthetic root while their refresh transport remains chat-scoped. Slack's synthetic typing root must never flow into message delivery, including approval/clarify prompts and status notices. Preserve actual source threads while keeping `reply_in_thread: false` sends flat.

## Verification and Retirement

Regression commands and results are recorded with the delivery commit. Preserve
Telegram's topic re-arm isolation, its 4.5-second cadence and retry budget, Slack's
legacy free-text API, elapsed heartbeat and explicit untracked-status cleanup.
No configuration, dependency, schema or gateway activation change is needed.

Late Slack status requests must re-clear a surface removed during their await,
including explicit clears without a base refresh owner. The owner fence reads
`_typing_refresh_owners`, not the adapter transport's `_typing_tasks`. The Discord
boundary probe supplies concrete Route attributes because the optional SDK shim's
MagicMock does not preserve constructor fields. It still runs the real base
refresh and Discord transport loop and asserts the POST method, path and channel.

Remove the fork delta when released upstream provides equivalent per-thread
ownership and intermediate-post continuity. Roll back by reverting this focused
commit, without changing the separate Telegram cadence or Slack API patches.
