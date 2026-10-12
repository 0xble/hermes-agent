# Background review memory delete opt-in

Load this unit when changing the background-review memory delete gate
(`tools/memory_tool.py::_background_delete_gate`) or the `memory` config
section. The patch identity is `background-review-memory-delete`.

## Behavior

`memory.background_review_allow_delete: true` lets the unattended background
review fork apply `replace` and `remove` (single or batched) directly **only if a
durable memory-transaction observer is registered for the current profile**. A
missing journal plugin, an old plugin without the durability declaration, or an
observer registered for another profile leaves the operation staged. The
default (`false` or unset) keeps upstream #105921 behavior: those ops are staged
for `/memory pending`. `memory.write_approval` still gates every write when on.

Admission is not trusted at commit. A destructive write admitted on the durable
path runs under `require_durable_memory_observer()`, and
`observe_memory_transaction` re-checks the observer snapshot it actually uses
for that commit, under the registry lock and before any write. If a plugin
unload or non-durable replacement removed the durable observer in between, it
raises `DurableMemoryObserverUnavailable`; nothing is written or journaled and
the tool stages the operation exactly as an admission without a journal would.
Attended, non-review, `add`-only writes and `compare_and_restore` do not set the
requirement and are unchanged.

The personal profile enables it so near-limit consolidation proposals land
without a manual approve step.

## Provenance

Fork-only. Upstream #105921 made the gate unconditional with no opt-out. Existing
own issues [#106919](https://github.com/NousResearch/hermes-agent/issues/106919)
and [#106918](https://github.com/NousResearch/hermes-agent/issues/106918) cover
opt-in policy and effective-policy visibility. Both remain open on 2026-09-26.
Neither supplies a transactional rollback journal.

## Verification

`scripts/run_tests.sh tests/tools/test_memory_tool.py tests/tools/test_memory_transactions.py tests/agent/test_background_review_memory_scope.py`.
`test_owner_opt_in_applies_unattended_delete` writes the flag to a temp
`config.yaml` and proves a batch applies through the real config loader with a
durable before/after journal. Regression tests prove absent, non-durable, and
other-profile observers stage remove/replace/batches without changing memory.
`test_durable_admission_cannot_commit_after_journal_disappears` unregisters or
non-durably replaces the observer at the approval-gate checkpoint (after
admission, before commit) and proves remove/replace/batch stage with the file
unchanged and zero journal callbacks.

The observer API defaults `durable=False`; a plugin promises durable rollback by
registering with `durable=True` and must refuse the write if it cannot prepare
its journal. The agents repo's `memory-journal` plugin makes this declaration.
Land the Hermes gate first, then the plugin update: the intermediate old plugin
stages consolidation rather than silently permitting an unjournaled delete.

## Retirement and rollback

Retire when upstream ships an equivalent per-profile opt-out. Rollback reverts
this commit. An existing `background_review_allow_delete` key then becomes an
unread no-op and the gate stages again.
