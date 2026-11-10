# Dynamic model aliases

Fork patch identity: `dynamic-model-aliases`.

Persist model_aliases names in topic `model_role`, cron `model`, and official
Advanced Delegation model_map options. Resolve each name from the request's
current profile config, using upstream model_switch for provider/endpoint/key
semantics. Alias edits affect later requests without rewriting every consumer.

The fork retains only this persisted-name behavior and `inherit_parent_aliases`
for the configured uncensored-parent inheritance exception that the plugin's
parent-free API cannot represent. Core owns lifecycle, effort, tier, schema
validation, profile isolation and actual-route result reporting.

Migration: `python -m hermes_cli.config_migrations_model_aliases --home HOME --dry-run`,
then the same command without `--dry-run`. Automatic config migration v54 shares
the transformation. It rewrites cron/jobs.json, the legacy sessions JSON mirror
and authoritative gateway_routing topic rows in the selected home's state.db.
Dry run opens an existing selected database read-only without initializing it.
Apply uses the native SessionDB writer guard and one transaction, comparing each
original routing row so a concurrent edit aborts rather than being overwritten.
No transcript or schema change is made. The live state.db was never opened in
this preparation run. Isolated database proof uses newly created topic rows.
Subscription worker options carry an explicit empty fallback chain.
The real OpenAI proxy also exposes paid API-key accounts. GPT model routes are
therefore migrated to native `openai-codex` OAuth, with no paid-provider fallback.
The one-shot command accepts `--codex-auth-source PATH` to import an enabled,
unexpired cached Codex grant into the selected home without refreshing or
writing its source. Provision that profile's OAuth grant before activation.

Retire this seam when upstream persists and resolves alias names at all these
boundaries and its plugin API can represent the configured parent inheritance.

Contract tests: tests/hermes_cli/test_model_alias_migration.py tests/tools/test_dynamic_model_aliases.py
