# Cron prompt/tool requirement diagnostics

Patch identity: `cron-tool-requirements` (`Fork-Patch:` trailer).

## Contract and placement

`hermes cron doctor` warns when an active agent job has a non-empty
`enabled_toolsets` restriction incompatible with explicit shell or relay steps
in its stored prompt or attached on-disk skills. It returns exit 1 and names the
missing capability. Never silently widen a restricted job or run a skill's
preprocessing/install hooks during this read-only diagnosis. Script-only jobs
run outside the agent tool surface and are not checked.

The motivating operational steps are authored by personal cron policy in
`agents/sources/skills/hermes/references/operations/cron.md` (Daily Brief findings
and relay routing) and by the scripts repository's
`src/hermes/convert-pinned-cron-lanes.py` (send-time relay paragraph). There is no
single Hermes-core generator for both. Extending the existing doctor catches
old jobs and later edits regardless of author, without changing delivery policy
or adding a new scheduler/service. This is a fork-first diagnostic for personal
operational steps; no upstream contribution is published with this patch.

## Proof and limits

`tests/cron/test_tool_requirements.py` creates a synthetic file-only finding job
in an isolated HERMES_HOME and checks the real doctor's nonzero status and
read-only behavior. It covers relay routing, terminal composites, unrestricted
and script-only jobs, generic explicit shell steps, raw attached skills, and
attached skill bundles (members plus bundle instruction, bundle-over-skill
precedence like the scheduler). Project-tier quarantine still applies during
diagnosis, but `agent.skill_utils.project_skill_scans_read_only()` makes those
scans uncached so doctor writes no attestations under HERMES_HOME.

This is an advisory static scan, not a complete natural-language verifier. It
does not execute pre-run scripts or discover remote MCP capabilities, does not
scan unattached skill references or generated script output, and does not prove
that a permitted CLI is installed. Absent/empty toolsets are unchanged by this
check; their execution semantics remain owned by the existing job normalizer.

Retire after an accepted upstream release supplies equivalent doctor coverage
and these regressions pass there. Roll back by reverting this patch and its
maintenance row; there is no persisted schema migration or runtime mutation.
Activation belongs to the main session's normal Hermes promotion path; this
change does not update or restart a running Hermes instance.
