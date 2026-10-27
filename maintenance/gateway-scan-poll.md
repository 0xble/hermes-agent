# Gateway scan poll

Fork patch identity: `gateway-scan-poll`.

## Required behavior

A long-lived gateway may reuse a skills-tree manifest and a catalog directory
signature for a few seconds instead of walking them on every prompt build,
slash lookup, or change-watcher tick. An install or removal is visible on the
next check after that interval. `clear_skills_system_prompt_cache` and
`clear_skills_cache` drop the memo so an explicit reload is immediate.
`config.yaml` stays signature-checked on every load: a short memo hid an edit
from a running gateway, including a fresh session and the dashboard.
Tests and a gateway whose home is a pytest directory keep a zero interval.
Refused watch events keep the bounded retry in `gateway/run_notifications.py`;
this patch does not drop a completion.

## Provenance and adoption

The live gateway was stating the external skills tree and config.yaml on hot
paths that already had an in-process result cache. The cache key was a fresh
filesystem signature, so the cache never avoided the stat. Upstream has the
same uncached signature checks. No upstream submission. Roll back by reverting
the commit carrying `Fork-Patch: gateway-scan-poll`.
