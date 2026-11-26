# Exact memory context notes

Load when changing `agent/memory_manager.py::_INTERNAL_NOTE_RE` or the known
memory wrapper wording. Patch identity: `memory-context-note-stripping`.

## Contract

Strip only the exact current untrusted-history wrapper and the exact historical
upstream informational/authoritative notes. Same-prefix quoted notes containing
recalled facts must survive unchanged, including whitespace after a note.
The existing memory-context fence and block suppression remains unchanged.
No provider synchronization, prompt cache, redaction, or undo policy changes.

## Provenance

Adapted the exact-note matching approach from yingliang-zhang's open upstream
[PR #64421](https://github.com/NousResearch/hermes-agent/pull/64421), inspected at
`7e16b919d113a870af7302e2e513f77f46f54298`. The fork has its own current wrapper;
this patch does not adopt that PR's other prefetch or provider changes.

## Verification

`scripts/run_tests.sh tests/agent/test_memory_provider.py`.
Same-prefix synthetic passport/lease facts failed before the fix. Tests also
cover exact current and legacy notes, intervening text, and adjacent whitespace.

## Retirement and rollback

Retire when an upstream release preserves same-prefix recalled facts and strips
these exact fork/legacy notes. Revert only the exact-note matcher and its tests;
keep the separate durable background-delete gate intact.
