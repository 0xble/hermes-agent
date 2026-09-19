# Company overlay diff: personal candidate versus LPG and Meridian

The slice 16/17 decision artifact. Read on 2026-09-19 from the two release repositories
(`~/Repos/lpg/apps/agent/config/`, `~/Repos/meridian-next/apps/agent/config/`) and the personal
candidate profile, with secrets excluded. No host was touched. This table is what Brian decides
against; nothing here infers that a company policy should change to match the personal profile.

| key | personal candidate | LPG (deployed overlay) | Meridian (deployed overlay) | disposition |
|---|---|---|---|---|
| `model.provider` / `model.default` | `custom:claude-proxy` / `claude-fable-5-1` | `openai-codex` / `gpt-6-astra` | `openai-codex` / `gpt-5.6-sol` | **keep per company.** Company hosts use direct Codex OAuth, not the personal proxy. The proxy exists only on the Mac Studio. |
| `fallback_providers` | Astra then Opus via proxy | unset | unset | keep unset; the personal chain depends on the local proxy |
| `delegation.*` | Astra via proxy, depth 2, 10 children | **disabled**: `agent.disabled_toolsets` includes `delegation` | inherits parent, 5 children | keep per company. LPG explicitly forbids delegation; Meridian allows it on its own model. |
| `auxiliary.review.*` | Fable via proxy | **disabled**: `agent.disabled_toolsets` includes `review` | unset (inherits) | keep per company. The `review_candidate` extension must not be installed on LPG. |
| `auxiliary.background_review.enabled` | `false` | `false` | unset (upstream default `true`) | **decision needed for Meridian.** The upstream fork stages every memory replace/remove for approval when this is on. Meridian already has `memory.write_approval: true`, so staging is consistent there. Recommend leaving Meridian as is. |
| `memory.write_approval` | `false` | `true` | `true` | keep per company. Company memory writes stay attended. |
| `memory.user_profile_enabled` | default (`true`) | `false` | `false` | keep per company |
| `memory.provider` | `none` in the isolated profile; `hindsight` at cutover | `hindsight`, bank `lpg` | `hindsight`, bank `meridian` | keep; banks are explicit and distinct, which slice 7 requires |
| `skills.external_dirs` | `~/.local/share/dotfiles/skills/hermes` | `/opt/agent/current/skills` | `/opt/agent/current/skills` | keep per company. The canonical-skill-guard plugin applies to whatever directory is configured, so it is safe on company hosts, but the curation cron targets the dotfiles repo and is **personal only**. |
| `skills.write_approval` | `false` | `false` | unset | keep |
| `browser.camofox.accounts` (new, slice 8) | `brianle`, `lpg`, `meridian` | must be `[lpg]` | must be `[meridian]` | **set at cutover.** Without it a company host advertises the other owners' aliases. |
| candidate extensions | all four | `goal-lifecycle`, `memory-journal`, `request-update` only (no review, delegation is disabled) | all four, if delegation stays on | **decision needed.** Whether LPG wants agent-set goals at all is a company policy question. |
| scheduled jobs (slice 14/6) | sync, feature check, curation, snapshot | **none of these.** LPG updates through its signed release route; `hermes update` must report package-managed. | same as LPG | keep. `check_fork_patches.py` is still useful on company hosts as a read-only post-release check. |
| `cron.script_timeout_seconds` | default | `7200` | `7200` | keep |
| `agent.reasoning_effort` | `medium` | `medium` | `medium` | same |
| `agent.system_prompt` | none | "You are a concise assistant." | same | keep |
| `timezone` | `America/New_York` | unset (server local, UTC) | unset (server local, UTC) | **verify at rehearsal.** Company cron jobs written for a wall clock need either a profile zone or the slice 12 per-job zone. |

## What the table does not settle

- **Meridian lineage: settled by ancestry, 2026-09-19.** The running Agent release commit
  `7b30a730` is on `0xble/meridian-next` `origin/main` (pinned 2026-09-05, "chore(agent): pin
  reviewed Hermes runtime"). The standalone `0xble/meridian-agent` head `d80e5eef` (2026-08-10)
  appears nowhere in `meridian-next`'s 225-commit history, so the two share no lineage. The
  monorepo owns the next build; `meridian-agent` is an earlier, separate line to preserve, not a
  successor. Its unique history still needs classifying before any cleanup.
- **LPG restore evidence.** The Hindsight PostgreSQL backup and isolated restore passed on
  September 18. Complete Hermes profile recovery (sessions, cron, pending work) is still the
  slice 13 rehearsal, on a copy, under host authority.
- **Signed release route.** Both companies build through `apps/agent/release`. The candidate
  engine enters those pipelines as a pinned source SHA; this document does not change how.
