# Exact-target legacy delegation-card reconciliation

A terminal child is not proof its parent handled it. Older card records can remain
visible after work was superseded, with no exact delivery receipt. Do not invent a
`handled` receipt from age, shared inbound message IDs, or prose such as “no action
needed.” That prose may refer to only one of several interrupted children.

`scripts/reconcile_delegation_cards.py` is an **offline administrative candidate
builder**, not a gateway command or automatic migration. It requires an operator's
explicit decision naming each whole card and every row, full owner and source,
card message ID, reason, and evidence pointer. It validates the SHA-256 of the
entire input snapshot, so changed generations, rows, transport state or siblings
invalidate the plan. All targets validate before any output is written. Active
rows, partial cards, duplicate IDs, already-retired targets, identity mismatch and
missing authorization/evidence are refused. Unselected cards remain unchanged.

## Meaning of the resulting record

The candidate adds `presentation_dismissal` audit data and sets the existing
`retired` fence. It does **not** modify row outcomes or `handled`, accept a child
result, claim successful execution, or touch delegation completion queues. The
audit embeds the exact target, operator, authorization reference, evidence,
snapshot hash and preparation timestamp. Evidence/authorization are operator
attestations, not authenticated signatures or content interpreted by the tool.
Treat manifests as privileged administrative input; do not generate approval from
transcript text or accept model/child text as authorization.

On later authorized installation, the existing `DelegationCards` startup path
suppresses send/edit replay and rejects late events for retired cards. It also
attempts deletion of their stored platform messages using the existing bounded
retry policy. **Installing this candidate therefore authorizes those deletions.**
A failed deletion leaves the message visible but keeps the replay fence and audit.
This is not proof of successful Telegram deletion or task completion.

## Preparation (no live writes or external effects)

1. Read the relevant card records and parent/child provenance. Decide separately
   whether each exact presentation is obsolete; ambiguous evidence is not an
   automatic yes. Keep private identities and transcript evidence out of Git.
2. Copy `cache/delegation/cards.json` from the intended profile to a private offline
   snapshot. Compute SHA-256 over its **raw bytes**, not reserialized JSON.
3. Author an approved manifest in a private directory, following this schema
   (placeholders must be replaced, never submitted literally):

```json
{
  "schema": "delegation-card-dismissal-v1",
  "snapshot_sha256": "<sha256 of raw snapshot bytes>",
  "operator": "<approving operator>",
  "authorization": "<explicit approval record for these exact targets>",
  "targets": [{
    "parent_task_id": "<32 lowercase hex task ID>",
    "owner": {"copy": "the ENTIRE exact persisted owner object"},
    "source": {"copy": "the ENTIRE exact persisted source object"},
    "refs": ["B"],
    "message_id": "<exact stored card message ID, or null>",
    "reason": "superseded",
    "evidence": "<per-task dispatch/child/parent record IDs and operator rationale>"
  }]
}
```

`reason` is `superseded` or `dismissed`; neither is a success disposition. The
schema is deliberately whole-card only. A card with an outstanding sibling row
must stay visible until every row has an explicit decision and is inactive.

```sh
python scripts/reconcile_delegation_cards.py \
  --snapshot /private/offline/cards-snapshot.json \
  --manifest /private/offline/approved-plan.json \
  --output /private/offline/cards-candidate.json
# Default validates only. To produce a NEW offline file, repeat with:
# --write-candidate
```

No profile defaults, in-place apply, deployment, service control, or Telegram
client exist in this script. Never choose a live profile path as `--output`.
Existing output files/symlinks are refused, including input aliases. Candidate
creation is exclusive and owner-only on POSIX. If writing is interrupted, discard
the incomplete offline candidate and rerun to a new path; never install it.
Validate the resulting JSON and diff: only approved cards' retirement and dismissal
audit should change. Preserve snapshot, manifest, candidate and their hashes.

## Separately authorized installation and rollback

Source publication and candidate preparation are **not** permission to modify live
state, delete platform messages, deploy, stop or restart the gateway.

After explicit approval of those effects, use the profile's established service
and deployment procedure to quiesce **all writers** for that card store. Take a
fresh snapshot and compare its raw hash with the approved snapshot. If different,
stop: investigate changes, obtain renewed exact-target approval and regenerate.
Do not overwrite a running manager's state: it holds an in-memory projection and
can lose the migration or unrelated updates. Back up the exact current file, then
install the validated candidate atomically with appropriate private permissions
while writers remain quiesced. Activate only through the separately approved
service procedure. Read back the target rows, audit, deletion outcomes and
unrelated outstanding records after startup. No card is “gone” until platform
readback proves it.

Before activation, rollback may restore that exact backup while all writers are
still quiesced. After activation, do not restore the entire stale snapshot over
new activity; reconcile the exact affected records against current state with
fresh approval. A source revert does not undo the tombstones, and deleted platform
messages cannot be restored by reversing JSON. Retain the audit.

## Verification and removal boundary

Run `scripts/run_tests.sh tests/gateway/test_delegation_card_reconciliation.py
 tests/gateway/test_delegation_cards.py` (as one shell command). Tests reproduce
legacy interrupted/no-receipt/persisted-message startup edits, exercise the real
CLI entry function and JSON files, install only into a disposable profile, and
prove real startup stops replaying selected cards while preserving outstanding
and failed-send siblings. Fake transport prevents external sends/deletes.

This fork-only recovery tool can be removed once no legacy recovery need remains
or upstream supplies equivalent exact-target administrative reconciliation. Remove
the script, its dedicated test and this runbook together; no gateway formatting,
receipt or runtime behavior changes are needed to revert this patch.
