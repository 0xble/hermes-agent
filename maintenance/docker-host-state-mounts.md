# Docker Host-State Mounts

Load when changing Docker host-state collection, terminal config bridges, or
container reuse. Fork patch identity: `docker-host-state-mounts`.

## Provenance and Adoption

Agentkit's disposable-HOME skill-curation trials reject out-of-subtree binds.
Hermes automatically collected credentials, skills and cache/staging directories,
including creating missing cache directories, even when configured volumes were
empty. The consumer and failure are recorded in
[agents#1799](https://github.com/0xble/agents/pull/1799).

The upstream defaults are intentional: credential mounting (#3671), live skill
scripts/references (#3890), and uploaded cache files (#4846). Keep these working
by default. This fork adds `terminal.docker_auto_mount_host_state: false` for
callers that require no implicit host-state binds, while preserving explicit
volumes and the independent cwd-mount option.

An upstream profile-skills-only opt-out, such as
[hermes-agent#92344](https://github.com/NousResearch/hermes-agent/pull/92344), is not
an equivalent replacement: it retains credential and cache mounts. Adopt an
upstream equivalent only if it suppresses the entire collector before path
creation and enforces the policy on reuse, including explicit shared keys.
Disabled policy requires an explicit false label, so it cannot reuse an unlabeled
legacy container. Enabled policy accepts true or absent labels and rejects false:
default-policy upgrades keep existing writable layers. Old containers are left
intact, not deleted.

Cache-path translation reads the active profile's terminal policy before any
mount-root collection or directory creation. Under opt-out the forward mapper
returns no container mapping and the inverse leaves paths unchanged. Spill
footers request no host-path fallback, preserving their inline excerpts without
read_file hints to inaccessible files. Image results keep the host-deliverable
image and omit the agent-visible container hint. Explicit volumes remain the
caller's responsibility and are not inferred as automatic cache mounts.

## Verification and Retirement

`tests/tools/test_docker_auto_mount_host_state.py` exercises default-on behavior,
raising collector mocks when disabled, explicit volumes, both policy transitions,
legacy-to-default reuse (including shared keys), recovery, and real YAML through
CLI, gateway and profile scope. `tests/tools/test_docker_cache_path_policy.py`
covers scoped opt-out, absent-policy defaults, untouched SSH/Modal mapping,
side-effect-free forward/inverse misses, and web/browser/delegation/image
fallbacks through real caller imports. Run these with `tests/tools/test_credential_files.py`,
`tests/tools/test_truncation_footer_sandbox_paths.py`,
`tests/tools/test_image_generation_artifacts.py`, `tests/tools/test_docker*.py`,
`tests/tools/test_terminal_config_env_sync.py`, and
`tests/tools/test_terminal_scope_multiplex.py` under a disposable `HERMES_HOME`.

Retire the fork patch when upstream satisfies the same full-policy contract.
Rollback reverts the patch and returns to automatic collection. It cannot remove
mounts from an already-created container.
