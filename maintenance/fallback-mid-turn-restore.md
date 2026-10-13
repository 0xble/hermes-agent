# Mid-turn fallback restore

Owns the `fallback-mid-turn-restore` fork patch. It adopts the narrow upstream
fix from [NousResearch/hermes-agent#88628](https://github.com/NousResearch/hermes-agent/pull/88628),
commit `aa4e600a8e7bd33687c657c9f6edc38cc46159e5`, on the fork's release-based
main. The conversation loop reuses `restore_primary_runtime` before each next
model request, so its shared cross-process cooldown record remains authoritative:
an active record prevents a mid-turn primary probe, while a record cleared by a
successful probe in another session lets this session restore at its next call.

The request-local system prompt is synchronized after restoration. The restore
check runs at the start of the iteration-entry phase and does not alter the
messages list, so assistant tool calls remain paired with their tool results. The optional,
provider-independent per-entry `max_stay_seconds` also re-walks the fallback
chain from entry zero while the shared primary cooldown still blocks a restore.
A concrete regression against the upstream-only port kept the last entry active
for the next tool-loop request even though the first fallback was available;
primary recovery alone cannot cover this case. Unset limits retain the normal
cooldown-gated primary restoration without an extra fallback-chain walk.

The check skips a request rebuild that is still retrying a just-activated
fallback, so an uncooled generic-500 primary cannot starve that fallback before
it makes its first request. The existing shared-cooldown regression exercises
this integration rule. Runtime re-evaluation failures roll back the route and
reset the per-entry timer instead of disrupting the turn.

## Proof and retirement

The proof surface is `tests/agent/test_mid_turn_primary_restore.py`, including
prompt provenance, active shared-cooldown blocking, cross-session shared-record
clearance, ordered-chain recovery after a failed primary retry, stay-limit
re-evaluation, unchanged behavior for unset limits, and rollback on errors.
Retire this patch when the selected upstream release contains equivalent
mid-turn restore and optional stay-limit behavior and the regressions pass
without the local implementation.

Fork patch identities: `fallback-mid-turn-restore`.
