# Gateway scan poll

Fork patch identity: `gateway-scan-poll`.

## Required behavior

A long-lived gateway may reuse a skills-tree manifest, a catalog directory
signature, and a config.yaml file signature for a few seconds instead of
stating them on every prompt build, slash lookup, change-watcher tick, or
hook. An install, removal, or config edit is visible on the next check after
that interval. `clear_skills_system_prompt_cache`, `clear_skills_cache`, and
`save_config` drop the matching memo so an explicit reload or in-process write
is immediate. Tests keep the interval at zero, so a write is still visible on
the next read in the suite. Refused watch events keep the bounded retry in
`gateway/run_notifications.py`; this patch does not drop a completion.

## Provenance and adoption

The live gateway was stating the external skills tree and config.yaml on hot
paths that already had an in-process result cache. The cache key was a fresh
filesystem signature, so the cache never avoided the stat. Upstream has the
same uncached signature checks. No upstream submission. Roll back by reverting
the commit carrying `Fork-Patch: gateway-scan-poll`.
