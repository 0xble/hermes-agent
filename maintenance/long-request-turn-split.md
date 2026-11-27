# Retired long request turn split

Fork patch identity: `long-request-turn-split`.

Retired on 2026-11-27. Upstream commit `41c67294217` ("fix(compression): split oversized turns whose request is long"), released in `v0.23.2` and still in `v0.23.4`, makes the token soft ceiling the only size guard on a split request, so a long `/goal` prompt no longer pins the whole turn in the tail. That is the complete contract of this unit. It came from the fork's own upstream contribution, [#132487](https://github.com/NousResearch/hermes-agent/pull/132487), first delivered here in [#294](https://github.com/0xble/hermes-agent/pull/294).

No private implementation remains. Later edits to the same split gate in `agent/context_compressor.py` belong to other units. `tests/agent/test_split_turn_compaction.py` is upstream-owned and keeps covering the behavior.

This record stays so published `Fork-Patch: long-request-turn-split` trailers keep an accountable owner.

Contract tests: none, retired. The behavior and its regression are upstream-owned.
