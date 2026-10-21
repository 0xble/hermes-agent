# Model Presets

Fork patch identity: `model-presets`

## Scope And Provenance

Own the flat `model_presets` registry, `model_preset` and `@NAME` references,
static config expansion, runtime config readers and shared concrete-selection
persistence. Extracted from the config-only parts of the published
`10eb24ac03fd98b5aa09d5260c60ff673d52f20c` candidate. The attended switching unit
continues to own session lifecycle and unattended/attended policy, not config
syntax. No session role or hold state is introduced by this unit.

Related upstream proposal: [NousResearch/hermes-agent#107760](https://github.com/NousResearch/hermes-agent/pull/107760),
open at `43788daaf0a02fc20c016204a817bfe7328e5e9f`. Its flat, explicit static
reuse is the shared design. This fork also resolves author references in config
readers at use time. Prior rejected proposals #12658 and #15593 are not upstream
acceptance of the design. Reassess this divergence before adopting upstream work.

## Update Rules

- Keep definitions flat and reserve `attended` and `default` for builtin config
  routes. Provider model IDs such as `@cf/vendor/model` stay literal.
- All primary-route readers use `resolve_default_route` and the common reasoning
  resolver. Consumer-specific shims must not interpret references themselves.
- `persist_model_selection` is the single shared CLI, TUI and gateway writer.
  Concrete picks remove every authored reference spelling in 1 atomic file
  replacement, preserve sibling settings/comments and never persist runtime
  credentials. Derive the full state from raw config and merge through the
  shared round-trip writer, not a blind replacement of the `model:` block.
- Unrelated saves preserve authored references, even after defaults merge.
- Carry resolved main fallback entries to auxiliary request planning. Re-reading
  config per candidate can lose the selected route's reasoning effort.

## Reader Inventory

Primary readers: CLI config/init/new/main/oneshot, ACP session, TUI server,
gateway run/config-loaders/inbound/turn, profiles/status/prompt-size/context
switch guard, and cron jobs/scheduler. Secondary readers: delegation config,
auxiliary tasks/fallbacks and supported MoA slots. `hermes_constants` owns shared
reasoning precedence, including preset effort ahead of global effort.

## Proof And Retirement

Run preset/persistence tests plus config, model-switch, CLI/TUI startup,
gateway runtime, cron and auxiliary suites. The persistence regression uses all
4 authored main-route shapes, reloads after every actual atomic file replacement
and requires exactly 1 complete new route, with no invalid or partial selection.
All 3 command commit seams also reload real scratch config. Sibling comments,
quoting, ambiguous YAML scalars and owner-only permissions survive. `/new` with an
invalid preset must remain best effort.

Retire when upstream covers flat references, every reader, round-trip
preservation and shared concrete-selection persistence. Keep session policy
separate when comparing or retiring the unit.
