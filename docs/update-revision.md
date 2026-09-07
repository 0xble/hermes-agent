# Immutable revision updates

`hermes update --revision <sha>` accepts only an exact 40-character lowercase Git commit SHA. It fetches and validates that commit and its tree while refusing a dirty checkout, then detaches the source tree at that exact object. It does not resolve a branch tip, merge an upstream remote, stash local changes, or fall back to a ZIP update.

Before checkout, Hermes records the requested commit/tree in the update receipt and creates a verified Git ref under `refs/hermes-update-backups/revision-*` pointing to the prior source commit. The receipt also inventories hashes of dependency manifest files that existed before checkout.

## Rollback scope

The retained rollback ref is **source-only**: it can anchor a Git checkout of the prior source revision. It does **not** roll back the virtual environment, installed Python/Node dependencies, managed runtime, service state, configuration, or data. A dependency-environment rollback is not implemented by this feature.

Repeating a revision already checked out does not reapply source changes. Hermes verifies HEAD, then runs pending repair and fleet-restart catch-up, then refuses completion if a live runtime SHA is missing or wrong. Machine-made lockfile and line-ending churn is discarded before the dirty-tree check so a retry is not refused for files the updater itself rewrites.
