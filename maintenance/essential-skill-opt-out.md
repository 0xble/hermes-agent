# Essential skill opt-out

Load this unit when changing bundled or essential skill seeding, the
`skills.disabled` essential exemption, or the `skill_manage` essential delete
guard. The patch identity is `essential-skill-opt-out`.

## Behavior

`skills.seed_essentials: false` opts one home out of the essential
`hermes-agent` skill. With the `.no-bundled-skills` marker also present,
`sync_skills` seeds nothing. With the flag alone, `skills.disabled` honors an
essential name, the `hermes skills` writer keeps it, and the `skill_manage`
delete guard no longer refuses it. The default (`true` or unset) is unchanged.

The personal profile uses marker plus flag because its maintained `hermes`
skill in the dotfiles skill library supersedes the bundled `hermes-agent` copy.
When the skill is absent from the skills index, the system prompt already falls
back to the documentation-only Hermes guidance.

## Provenance

Exact backport of upstream PR
[#110749](https://github.com/NousResearch/hermes-agent/pull/110749) at head
`9b06258c288ccb25e40330bbf8147672d08c0cda`, for issue
[#110731](https://github.com/NousResearch/hermes-agent/issues/110731). The
competing upstream PR #110735 changes the marker's meaning for every home and
was not adopted. No local edits were made to the upstream diff.

## Verification

Run `scripts/run_tests.sh tests/agent/test_phantom_tool_references.py` and the
skill sync, skill config, and skill manager test modules. The upstream tests
cover the opt-out paths and pin the default essential contract.

## Retirement and rollback

Retire when a released upstream version contains `skills.seed_essentials` or an
equivalent per-home opt-out that passes the same tests. If upstream merges
#110735 instead, re-verify that the personal profile's marker alone keeps
`hermes-agent` unseeded and undeletable-guard-free, then drop this patch.
Rollback reverts this commit. A home that set the flag keeps working, but
`hermes-agent` is reseeded on the next sync and becomes undisableable again.
