# Background review memory delete opt-in

Load this unit when changing the background-review memory delete gate
(`tools/memory_tool.py::_background_delete_gate`) or the `memory` config
section. The patch identity is `background-review-memory-delete`.

## Behavior

`memory.background_review_allow_delete: true` lets the unattended background
review fork apply `replace` and `remove` (single or batched) directly. The
default (`false` or unset) keeps upstream #105921 behavior: those ops are staged
for `/memory pending`. `memory.write_approval` still gates every write when on.

The personal profile enables it so near-limit consolidation proposals land
without a manual approve step.

## Provenance

Fork-only. Upstream #105921 made the gate unconditional with no opt-out. No
upstream issue or PR proposes one as of 2026-09-24.

## Verification

`scripts/run_tests.sh tests/tools/test_memory_tool.py tests/agent/test_background_review_memory_scope.py`.
`test_owner_opt_in_applies_unattended_delete` writes the flag to a temp
`config.yaml` and proves a batch applies through the real config loader.

## Retirement and rollback

Retire when upstream ships an equivalent per-profile opt-out. Rollback reverts
this commit. An existing `background_review_allow_delete` key then becomes an
unread no-op and the gate stages again.
