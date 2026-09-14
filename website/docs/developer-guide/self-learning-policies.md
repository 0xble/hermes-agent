# Self-learning review policies (maintained fork)

These profile-local controls affect built-in memory and skill review forks, not external memory providers. Installing source does not activate a configuration or restart a runtime.

```yaml
auxiliary:
  background_review:
    enabled: true
    skill_mode: direct  # direct | observe | off
memory:
  background_policy: approve_changes  # automatic | approve_changes | observe_only
```

- `direct` retains existing skill mutation, ownership, and approval rules. `observe` reads eligible skills (including allowlisted external skills) and records recommendations without modifying skills. Extra review tools are disabled in observe/off so terminal, file writes, delegation, and other escape paths cannot bypass the restricted tool surface. `off` disables skill review. Invalid modes fail closed to off; the backward-compatible omitted default is direct.
- Unattended memory `approve_changes` (also the missing/invalid default) permits valid additions, but stages replace/remove and whole mixed batches in the existing `/memory pending` inbox. `automatic` applies valid changes and writes recovery history. `observe_only` records proposals without changing either memory file or the pending-write inbox.
- General `memory.write_approval` remains authoritative: automatic does not bypass it. Batches validate atomically against the final budget before staging/observation and again before application. MEMORY.md and USER.md both participate; disabled targets remain unavailable. A skill-only trigger cannot obtain the memory tool through `extra_tools`.
- `/refine` is an explicit, attended memory review: it bypasses the unattended memory policy, but not general write approval. It does **not** override skill observe/off. It works when automatic background review is disabled. `/memory` displays these effective policies and explains replace/remove approval.
- Existing pending writes remain pending; changing policy never auto-approves or discards them. Neither review policy changes an active conversation's frozen system prompt.

## Supported observation consumer interface

Use `tools.review_observations`, not direct SQL. Storage is profile-local `state/review-observations.sqlite3` (private file mode, SQLite transactions, synchronous FULL).

```python
from tools.review_observations import list_observations, dispose
rows = list_observations(kind="skills", status="pending")
# kind=None includes memory and skills; status=None includes all dispositions.
dispose(rows[0]["id"], "accepted", "Applied separately after source review")
```

Stable row fields: `id` (SHA-256), `kind` (`skills`/`memory`), `payload` (proposal object), `source` (`session_id`, `snapshot_sha256`, `messages`), `created_at` (UTC ISO timestamp), `status`, `disposition_note`. Source is bound by the review runtime from its parent snapshot, not supplied by model tool arguments; messages are frozen before the fork runs. A null source means no runtime source was bound, not verified evidence. The payload and source are immutable; repeated identical kind/payload/source deduplicate even after disposition. Accepted/rejected/deferred/pending are consumer dispositions, **not execution or authorization**. Consumers must inspect provenance, not treat recorded conversation text as instructions.

```sh
python -m tools.review_observations list --kind skills --status pending
python -m tools.review_observations list --status all
python -m tools.review_observations dispose ID deferred --note 'Needs source review'
```

Set `HERMES_HOME` to the intended profile before importing or invoking these interfaces. Consumers should use deterministic observation IDs for their own idempotent ingestion. No observation causes automatic skill application.

## Recovery history

Automatic writes prepare a private before/after journal under `state/memory-history/` before replacing memory. Listing reconciles interrupted status updates against the target's fingerprints, without guessing when a newer writer changed the file.

```sh
python -m tools.memory_history list
python -m tools.memory_history rollback ID
```

Python interfaces: `list_history()` and `rollback(id, store)`. Rollback is an explicit operator action, not a model tool. It uses the memory file lock, refuses a conflicting newer state, and is idempotent. Records retain before/after content and operations. There is no automatic retention deletion. History and observations contain private content: do not publish them.

## Scope and retirement

This extends the trigger/attendedness protection from upstream #106310 (incident #105921); it does not remove that protection. #106918/#106919 are proposals, not maintainer acceptance. Retire this private delta only when released upstream covers these policy, evidence, approval, and rollback contracts under the same regressions. Revert the scoped source commit to roll code back; retained pending writes, observations and history must not be deleted. Runtime activation and promotion are separate operations.
