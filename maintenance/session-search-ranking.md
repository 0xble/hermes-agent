# Session-search ranking

Load this unit when changing relevance, temporal ordering, or result selection for session-search discovery.

## Required behavior

Session-search discovery keeps BM25 relevance primary. `newest` and `oldest` provide deterministic timestamp and message-ID tie-breakers, so a weak recent match cannot displace a stronger older match while temporal intent remains stable for equally relevant hits.

## Provenance and patches

Fork patch identity: `session-search-ranking`.

The fork-only patch corrects session-search ordering so BM25 relevance remains primary and temporal intent is deterministic only among equally relevant hits.

## Verification

The synthetic regression uses a strong older match and a weak recent match for the same query. It fails on the fork base when the recent session wins and passes with relevance-first ordering. Focused session-search, FTS fallback, CJK, and web-server search suites cover the changed ordering and adjacent routes.

## Retirement and rollback

Retire after a released upstream version provides relevance-first temporal tie-breaking with equivalent coverage. Roll back by reverting the fork patch commit; no persisted data or schema migration.
