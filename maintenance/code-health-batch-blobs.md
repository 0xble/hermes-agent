# code-health-batch-blobs

Fork patch identity: `code-health-batch-blobs`.

## Problem
Our checkout is a `blob:none` partial clone. `scripts/code_health/gitio.read_file` reads each file at a tree with `git show <tree>:<path>`, and each missing blob is lazily fetched on its own, leaving one tiny promisor pack per file. CI gates in many worktrees share one object store, which grew to 10,745 packs. At that size every git process opened about 43k pack files.

## Fix
`gitio.prefetch_blobs(repo, tree, paths)` runs before `Measurer.measure` reads a snapshot. In a partial clone only, it lists the blob OIDs with `ls-tree -r -z`, finds the absent ones with `cat-file --batch-check` under `GIT_NO_LAZY_FETCH=1`, and fetches them all with one `fetch --stdin`. Without `GIT_NO_LAZY_FETCH`, the probe would itself fetch each blob. This follows the same pattern as `scripts/ci/prefetch_tree.py`. The per-file `git show` reads are unchanged, so content, decoding and missing-path behavior are exactly as before. A failed prefetch only loses the batching.

## Upkeep
If upstream changes how code health reads snapshots, keep one batched hydration before any per-file read in a blobless clone. Plain clones skip the prefetch entirely.

Contract tests: `tests/scripts/test_code_health.py::test_measure_in_blobless_clone_fetches_snapshot_blobs_in_one_pack`
