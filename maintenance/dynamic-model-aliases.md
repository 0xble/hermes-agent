# Dynamic model aliases

Fork patch identity: `dynamic-model-aliases`.

Persist upstream model_aliases names in topic `model_role`, cron `model`, and
Advanced Delegation model_map options. Resolve each name from the request's
current profile config through upstream model_switch. Editing an alias changes
later requests without rewriting every stored consumer. Role options store role alias names. Their model_aliases entries contain the
exact decided provider/model IDs. Keeping alias labels separate from wire IDs
avoids redirecting main-chat models that happen to share the same wire ID.

The fork retains only this persisted-name behavior. Core and the official plugin
own lifecycle, routing schema, effort, tier and actual-route result reporting.
The general parent map and uncensored-parent exception are fully retired.

Migration: `python -m hermes_cli.config_migrations_model_aliases --home HOME --dry-run`,
then without `--dry-run`. Automatic config migration v54 shares the transformation.
It rewrites cron/jobs.json, the legacy sessions JSON mirror and authoritative
gateway_routing topic rows in the selected home's state.db. Dry run opens an
existing selected database read-only without initializing it. Apply uses the native
SessionDB writer guard and one transaction, comparing original routing rows so a
concurrent edit aborts rather than being overwritten. Transcripts are untouched.
No live state.db was opened during preparation. Isolated database proof uses
newly created topic rows.

The migration installs the decided worker, explorer and coder options, enables
advanced-delegation, and leaves 5 Claude options commented until the proxy protects
Brian's 25% reserve. Install the official plugin in the target profile before
activation. The supplied custom proxy IDs and fallback IDs are preserved. The
shared proxy's paid API-key pool remains an activation blocker for a strict
subscription-only guarantee. Isolated proofs use a disposable OAuth-only proxy.
Old GPT preset aliases use native openai-codex with no paid fallback. The optional
`--codex-auth-source PATH` imports a cached unexpired grant without refreshing it.

Retire this seam when upstream persists and resolves alias names at all these
boundaries. Contract tests: tests/hermes_cli/test_model_alias_migration.py and
tests/tools/test_dynamic_model_aliases.py.

Known effort gap: the retained explicit-session-reasoning inheritance patch gives
an explicit parent pick precedence over a model_map route's effort. There is no
configuration switch to reverse it. Route defaults work when the parent has no
explicit pick. A parent pick of max can therefore override these non-max routes.
No core precedence change is included in this migration.

Gateway model confirmation and reasoning status use the same `AliasRoute` value for an empty default route. The retired preset DTO is not retained as a compatibility shim. Existing model-confirmation and reasoning-command tests exercise these consumers.

Preset-local cron fallback metadata and its unused route field are retired. Cron retains its existing explicit `cron.fallback_providers` and pinned-job fallback policy. Named delegation options use the official plugin fallback schema.
