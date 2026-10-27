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
- Preset effort remains on the selected route, never projected into
  `agent.reasoning_effort`. The matching default route outranks per-model/global
  effort. Other models keep their own policy. Normalize disabled aliases through
  the existing reasoning parser to `none`, retain enabled dictionaries (including
  provider-specific tiers), and keep absent/null effort unspecified.
- Cron preflight and execution consume the same `_CronJobConfig` and primary
  provider kwargs. Standalone doctor checks use the config-only resolver, without
  credential I/O until the existing preflight probe. Keep the exfil guard before
  preflight and preserve cron override, preset and pinned fallback boundaries.
- `persist_model_selection` is the single shared CLI, TUI and gateway writer.
  Concrete picks remove every authored reference spelling in 1 atomic file
  replacement, preserve sibling settings/comments and never persist runtime
  credentials. Derive the full state from raw config and merge through the
  shared round-trip writer, not a blind replacement of the `model:` block.
- Unrelated saves preserve authored references, even after defaults merge.
- Single-field platform writers round-trip raw YAML, not the runtime-expanded
  dict. The shared config write boundary validates a disposable expansion before
  replacing any bytes, including `config set --force`, `save_config`, and schema
  migrations. A preset plus inline route fields must fail without changing the
  file or stamping a newer schema version.
- Guard: `tests/hermes_cli/test_config_write_guards.py` exercises raw round-trip,
  every conflicting delegation field through the CLI setter, all shared writers,
  and an invalid schema-step result.
- Carry resolved main fallback entries to auxiliary request planning. Re-reading
  config per candidate can lose the selected route's reasoning effort.
- `get_fallback_chain` is the shared preset-aware fallback boundary. Feed it the
  complete effective user config, not just a detached list of named references.
  Raw and already-expanded inputs must yield identical ordered concrete routes.
  Cron overrides carry the registry but never borrow global or legacy routes.
- Expand the complete CLI user route before defaults merge destroys its authored
  identity. Do not independently resolve just the primary model. Turn refreshes
  and TUI readers use the shared fallback boundary without their own parser.
- Keep a pre-agent cron fallback's `ModelPresetRoute` beside its resolved runtime
  until setup consumes its reasoning. Absent fallback effort defers to per-model
  and global policy, even for the same model ID on a different provider. A valid
  per-job reasoning pin still wins.

## Boundary Rationale

The recurring failure class was partial normalization: a reader replaced a
preset's model/provider while discarding its fallback or typed reasoning policy.
Deleting references or narrowing their supported forms would break the reuse
contract. Adding more independent reader patches would leave the same loss
possible at the next consumer. The existing `expand_model_presets` therefore
owns fallback expansion at `get_fallback_chain`, where the full config and preset
registry are available. CLI initialization additionally expands before defaults
merge because that merge irreversibly replaces the primary reference. Raw
write-back primitives remain unexpanded so references, siblings and comments
survive unrelated saves. No parallel parser or configuration mechanism is added.
The auxiliary boundary parses the original reasoning value with
`parse_reasoning_effort`, and cron carries the selected route instead of
reconstructing its policy from the old primary.

## Reader Inventory

Primary readers: CLI config/init/new/main/oneshot, ACP session, TUI server,
gateway run/config-loaders/inbound/turn, profiles/status/prompt-size/context
switch guard, and cron jobs/scheduler. Secondary readers: delegation config,
auxiliary tasks/fallbacks and supported MoA slots. `hermes_constants` owns shared
reasoning precedence, including preset effort ahead of global effort.

### Consumer Boundaries

- MoA authors: `_expand_moa_slot` uses the same shorthand lifting for primary
  and fallback slots. `resolve_moa_preset` retains normalized `fallback_models`,
  including an explicit empty list. Expansion is not execution proof.
- MoA execution: references and completion/streaming aggregators pass slot chains
  to `auxiliary_client.call_llm`. A request-local chain uses the existing shared
  recovery ladder, not a MoA retry engine. Apply it before dispatch when the primary
  has no usable client, as well as after a transport failure. `None` inherits task
  policy, `[]` disables provider hops, and a declared chain never appends unrelated
  routes or starts autodetection. Unusable declared entries are skipped in order.
  A selected pre-dispatch fallback goes through the same destination request planner
  as transport recovery. Carry its selection provenance on the request, not just a
  cached client tag, so a later primary call cannot inherit fallback transforms.
  Destination preparation re-trims canonical reference input, re-plans cache
  markers/tools and reasoning, and records the actual selected accounting route.
  Retain the selected destination planner on the prepared request for credential
  refresh and pool rotation. Same-route recovery must keep that destination's
  input, reasoning (including `none` and `false`) and timeout, never restore the
  unavailable primary's controls. A different fallback destination still plans
  from the original logical input, not an already-trimmed or decorated request.
  Streaming recovery is allowed only before a chunk has escaped. Dispatch remains
  eager. Completed adapter responses retain identity and release permits with the
  call, even when the SDK response is iterable. Only actual stream iterators carry
  request-local policy and permits across pulls. Scope fallback/runtime/progress
  and Relay identity during advancement and close, never while yielding control
  to the consumer. Explicit close, exhaustion, failure and abandonment release
  stream resources and permits exactly once, including before the first pull.
- Runtime references: raw mappings and builtin `@default`/`@attended` wrappers
  use the same site-level fallback opt-out as static expansion. Cron preflight,
  provider setup and in-agent execution must see the same empty chain.
- Gateway context pins: resolve named custom endpoints through the existing
  provider registry, then compare with `route_identity.should_clear_context_pin`.
  A matching preset route retains its pin. An identical model on another
  endpoint does not own that pin.

## Proof And Retirement

Run preset/persistence tests plus config, model-switch, CLI/TUI startup,
gateway runtime, cron and auxiliary suites. The persistence regression uses all
4 authored main-route shapes, reloads after every actual atomic file replacement
and requires exactly 1 complete new route, with no invalid or partial selection.
All 3 command commit seams also reload real scratch config. Sibling comments,
quoting, ambiguous YAML scalars and owner-only permissions survive. `/new` with an
invalid preset must remain best effort. `tests/hermes_cli/test_model_preset_route_consumers.py`
exercises raw/expanded fallback equivalence, idempotence and legacy order, real
CLI/TUI initialization and file-backed turn refresh, scalar/named `@default`
through cron, typed auxiliary OpenRouter wire kwargs, and cron auth fallback
reasoning with and without explicit job pins.

`tests/hermes_cli/test_model_presets.py` primarily proves expansion/validation.
`tests/hermes_cli/test_model_preset_failure_consumers.py` crosses runtime
normalization and then forces mocked transport failures to execute reference,
completion aggregator and streaming aggregator backups. It also proves empty
chains suppress execution, streaming never replays emitted content, destination
context trimming and liveness survive, raw/builtin opt-outs agree, cron setup
and provider resolution never visit an opted-out backup, and named-endpoint pins
are retained only on the identical route. These are failure-path consumer
proofs, not assertions that expanded kwargs happen to contain a list.
`tests/agent/test_auxiliary_stream_contract.py` crosses the real `call_llm`
boundary with isolated config and mocked transports. It proves completed-response
identity (including eager recovery), eager dispatch/options, permit lifetime,
close-before-pull, cleanup failure, abandonment, scoped Relay identity and no
replay after even a role-only chunk. Keep the preexisting auxiliary client,
relay, concurrency and MoA streaming assertions unchanged alongside this proof.

Retire when upstream covers flat references, every reader, round-trip
preservation and shared concrete-selection persistence. Keep session policy
separate when comparing or retiring the unit.
