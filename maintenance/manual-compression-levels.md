# Manual compression levels

Load this unit when changing `/compress` argument parsing, `/compress here N`, or the manual compression budgets.

## Required behavior

`/compress --level N` (N in 1–3, clamped, parsed anywhere in the arguments and never treated as a focus topic) tightens the built-in `ContextCompressor` for that one manual run only. Level 1 is the configured compressor. Level 2 keeps a quarter of the configured verbatim-tail token budget and caps the summary target at 5K tokens. Level 3 keeps only the latest request and reply and caps the summary target at 2K tokens. Each level must keep strictly fewer messages verbatim and target a shorter summary than the level below. Budgets revert to lazy derivation afterwards, and `protect_last_n` and `min_tail_user_messages` are restored, so automatic compaction keeps its configured policy. Plugin context engines ignore the level. A pending first-attempt aux-feasibility probe runs before the override is applied, because its threshold clamp re-derives the retention budgets and would otherwise discard the level.

`/compress here N` summarizes the head at level 3, so the compressor's own recent-tail window does not survive past the summary. At most the head's latest exchange rides along, because the compressor always keeps a real user turn after its summary.

The user's `/cc` and `/ccc` quick-command aliases map to `/compress --level 2` and `/compress --level 3`.

## Provenance and patches

Fork patch identity: `manual-compression-levels`.

Fork-original. Upstream NousResearch/hermes-agent has no per-run compression strength or `/compress here` head-tail fix as of 2026-09-27.

## Verification

`scripts/run_tests.sh tests/hermes_cli/test_compress_flags.py tests/agent/test_conversation_compression_manual.py tests/hermes_cli/test_partial_compress.py tests/hermes_cli/test_compress_here.py`

## Retirement and rollback

Retire if upstream ships an equivalent per-run strength and a `here N` head that does not keep its own tail. Roll back the patch commit and its regressions, and remove the `cc`/`ccc` quick commands from `~/.hermes/config.yaml`. No persistent-data migration is required.
