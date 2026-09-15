# Hindsight Memory Provider

Long-term memory with knowledge graph, entity resolution, and multi-strategy retrieval. Supports cloud, local embedded, and local external modes.

## Requirements

- **Cloud:** API key from [ui.hindsight.vectorize.io](https://ui.hindsight.vectorize.io)
- **Local Embedded:** API key for a supported LLM provider (OpenAI, Anthropic, Gemini, Groq, OpenRouter, MiniMax, Ollama, or any OpenAI-compatible endpoint). Embeddings and reranking run locally — no additional API keys needed.
- **Local External:** A running Hindsight instance (Docker or self-hosted) reachable over HTTP.

## Setup

```bash
hermes memory setup    # select "hindsight"
```

The setup wizard installs dependencies automatically via `uv`, walks you through configuration, and offers to seed the bank with a **starter memory template** (a curated set of dispositions/instructions for common agent roles) — you can skip it, and it warns before overwriting an already-configured bank.

Or manually (cloud mode with defaults):
```bash
hermes config set memory.provider hindsight
echo "HINDSIGHT_API_KEY=your-key" >> ~/.hermes/.env
```

### Cloud

Connects to the Hindsight Cloud API. Requires an API key from [ui.hindsight.vectorize.io](https://ui.hindsight.vectorize.io).

### Local Embedded

Hermes spins up a local Hindsight daemon with built-in PostgreSQL. Requires an LLM API key for memory extraction and synthesis. The daemon starts automatically in the background on first use and stops after 5 minutes of inactivity.

Supports any OpenAI-compatible LLM endpoint (llama.cpp, vLLM, LM Studio, etc.) — pick `openai_compatible` as the provider and enter the base URL.

Daemon startup logs: `~/.hermes/logs/hindsight-embed.log`
Daemon runtime logs: `~/.hindsight/profiles/<profile>.log`

To open the Hindsight web UI (local embedded mode only):
```bash
hindsight-embed -p hermes ui start
```

### Local External

Points the plugin at an existing Hindsight instance you're already running (Docker, self-hosted, etc.). No daemon management — just a URL and an optional API key.

## Config

Config file: `~/.hermes/hindsight/config.json`

### Connection

| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `cloud` | `cloud`, `local_embedded`, or `local_external` |
| `api_url` | `https://api.hindsight.vectorize.io` | API URL (cloud and local_external modes) |

### Memory Bank

| Key | Default | Description |
|-----|---------|-------------|
| `bank_id` | `hermes` | Memory bank name (static fallback used when `bank_id_template` is unset or resolves empty) |
| `bank_id_template` | — | Optional template to derive the bank name dynamically. Placeholders: `{profile}`, `{workspace}`, `{platform}`, `{user}`, `{session}`. Example: `hermes-{profile}` isolates memory per active Hermes profile. Empty placeholders collapse cleanly (e.g. `hermes-{user}` with no user becomes `hermes`). |
| `bank_mission` | — | Reflect mission (identity/framing for reflect reasoning). Applied via Banks API. |
| `bank_retain_mission` | — | Retain mission (steers what gets extracted). Applied via Banks API. |

### Recall

| Key | Default | Description |
|-----|---------|-------------|
| `recall_budget` | `mid` | Recall thoroughness: `low` / `mid` / `high` |
| `recall_prefetch_method` | `recall` | Auto-recall method: `recall` (raw facts) or `reflect` (LLM synthesis) |
| `recall_max_tokens` | `4096` | Maximum tokens for recall results |
| `recall_max_input_chars` | `800` | Maximum input query length for auto-recall |
| `recall_prompt_preamble` | — | Custom preamble for recalled memories in context |
| `recall_tags` | — | Tags to filter when searching memories |
| `recall_tags_match` | `any` | Tag matching mode: `any` / `all` / `any_strict` / `all_strict` |
| `recall_types` | `observation` | Fact types surfaced by recall (both auto-recall and the `hindsight_recall` tool). Comma-separated string or JSON list. **Default narrowed to `observation` only** (see "Behavior change" below). Set to `observation,world,experience` to also include raw facts. |
| `auto_recall` | `true` | Automatically recall memories before each turn |
| `recall_sync` | `false` | Recall synchronously against the *current* message each turn (higher relevance, adds recall latency). Default off: recall runs in the background and is injected on the next turn. |
| `recall_indicator` | `true` | Show a `💭 Recalled N memories` status line when auto-recall injects memory. Turn off for customer-facing agents. |

> **Behavior change — `recall_types` defaults to `observation` only.**
>
> Previously recall returned all three fact types. It now returns only observations.
>
> Per [Hindsight's docs](https://hindsight.vectorize.io/developer/observations), observations are the **consolidated** knowledge layer Hindsight builds on top of raw facts: deduplicated beliefs grounded in evidence, refined as new facts arrive, with proof counts and freshness signals. Raw `world` / `experience` facts are the individual supporting evidence that feeds them. For per-turn context injection, observations are denser per token and avoid feeding the model multiple raw facts that one observation already summarizes.
>
> Restore the broad recall with `"recall_types": "observation,world,experience"` (string or JSON list) in `~/.hermes/hindsight/config.json`. This supplies the default for **both** auto-recall and the `hindsight_recall` tool. Explicit tool calls can override it with `types`.

### Retain

| Key | Default | Description |
|-----|---------|-------------|
| `auto_retain` | `true` | Automatically retain conversation turns |
| `retain_async` | `true` | Process retain asynchronously on the Hindsight server |
| `retain_every_n_turns` | `1` | Retain every N turns (1 = every turn) |
| `retain_context` | `conversation between Hermes Agent and the User` | Context label for retained memories |
| `retain_tags` | — | Default tags applied to retained memories; merged with per-call tool tags |
| `retain_source` | — | Opt-in `metadata.source` attached to retained memories (identifies the storing client, e.g. `hermes`). Empty by default — no attribution tag ships unless you set it. |
| `retain_indicator` | `true` | Show a `🧠 Hindsight — saving to memory…` status line when a turn is saved. Turn off for customer-facing agents. |
| `retain_user_prefix` | `User` | Label used before user turns in auto-retained transcripts |
| `retain_assistant_prefix` | `Assistant` | Label used before assistant turns in auto-retained transcripts |

`observation_scopes` applies to text retention, including explicit `[[]]` for
one shared consolidation scope. The pinned Hindsight 0.9.1 file-retention API
cannot carry this setting. Raw attachments continue to use the server's existing
file-retention behavior. Hermes does not change bank policy to approximate
per-item scopes.

### Automatic source ordering and recovery

Public source URLs omit credentials, query strings and fragments. Generic query
parameters named `key` or `code` can identify either documents or credentials,
and extraction results provide no trustworthy canonical document identifier.
For these ambiguous URLs, private identity includes the extracted content hash
after credential filtering. Distinct contents remain separate documents instead
of overwriting each other. Identical content at the same sanitized identity
coalesces, and changed content gets a new immutable identity rather than a known
document revision. This preserves evidence without claiming to recover the true
locator identity. Explicit token rotation and ordinary query-selector versioning
retain their existing semantics. No raw credential values are added to metadata
or the journal, and existing remote documents are not migrated or deleted.

Automatic source replacements coordinate through
`$HERMES_HOME/memories/hindsight-source-operations.sqlite`. Providers sharing that
journal, endpoint and bank reserve a source before submission. Accepted operation
IDs are persisted before admission can pass to another version. Every known
operation must have positive terminal status before another replacement starts.
Different homes or hosts do not share this ordering guarantee.

A request whose acceptance is unknown stays reserved across process restarts.
There is no timeout that clears it and no automatic replay. Exact content readback
alone does not prove that an asynchronous operation has stopped writing. The
existing drain reports unresolved work, and later versions of that source stay
blocked. Recovery of an unreceipted reservation requires operator reconciliation
with server operation evidence. There is currently no automated reset command.
The same limitation applies if a known operation disappears before its terminal
status was recorded. The server's missing-operation response does not identify
the deletion cause, and this plugin has no supported CLI to settle that missing
receipt. Restarting or repairing credentials alone does not clear it.
Do not clear reservations based only on process death, elapsed time or a matching
document hash.

Deferred source bytes remain in memory and are retained when submission raises.
They are not persisted or reconstructed after restart. A local file validation
failure before any request can release admission. A transport exception after a
request begins cannot. SDK HTTP 401/403 responses from the submission call and
structured FastAPI 422 request-validation responses are definite prequeue
rejections and release the matching reservation. Generic 400/422 errors and
errors after a submission response remain unresolved. A later drain can retry
the preserved source after the credential or input problem is repaired. This
trades availability after an unknown outcome for preserving source write order.

### Integration

| Key | Default | Description |
|-----|---------|-------------|
| `memory_mode` | `hybrid` | How memories are integrated into the agent |

**memory_mode:**
- `hybrid` — automatic context injection + tools available to the LLM
- `context` — automatic injection only, no tools exposed
- `tools` — tools only, no automatic injection

### Local Embedded LLM

| Key | Default | Description |
|-----|---------|-------------|
| `llm_provider` | `openai` | `openai`, `anthropic`, `gemini`, `groq`, `openrouter`, `minimax`, `ollama`, `lmstudio`, `openai_compatible` |
| `llm_model` | per-provider | Model name (e.g. `gpt-4o-mini`, `qwen/qwen3.5-9b`) |
| `llm_base_url` | — | Endpoint URL for `openai_compatible` (e.g. `http://192.168.1.10:8080/v1`) |

The LLM API key is stored in `~/.hermes/.env` as `HINDSIGHT_LLM_API_KEY`.

The embedded daemon is a subprocess that cannot see the per-turn secret
scope, so it reads the key from `~/.hindsight/profiles/<profile>.env`
(materialized owner-only at setup and on config change). Key resolution
order is explicit config → secret scope → the on-disk profile env, and the
rewrite path is fail-closed: a build with no key never clobbers a profile
file that already holds one.

## Tools

Available in `hybrid` and `tools` memory modes:

| Tool | Description |
|------|-------------|
| `hindsight_retain` | Store information with auto entity extraction; supports optional per-call `tags` |
| `hindsight_recall` | Multi-strategy search (semantic + entity graph) |
| `hindsight_reflect` | Cross-memory synthesis (LLM-powered) |

Explicit `hindsight_recall` calls can select `types`, `tags`, and `tags_match`,
request entity context, source chunks, or source facts with their corresponding
`include_*` flags and token budgets, and control local provenance formatting with
`include_provenance`. Expansions remain opt-in. `offset` (0–500) and `limit`
(1–50) slice the returned memories locally and do not fetch server pages.

The pinned 0.9.1 SDK imports a missing model when `tag_groups` is supplied to
`arecall`, so the tool schema does not advertise that filter. Existing direct
requests with unsupported filters fail closed instead of retrying without them.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HINDSIGHT_API_KEY` | API key for Hindsight Cloud |
| `HINDSIGHT_LLM_API_KEY` | LLM API key for local mode |
| `HINDSIGHT_API_LLM_BASE_URL` | LLM Base URL for local mode (e.g. OpenRouter) |
| `HINDSIGHT_API_URL` | Override API endpoint |
| `HINDSIGHT_BANK_ID` | Override bank name |
| `HINDSIGHT_BUDGET` | Override recall budget |
| `HINDSIGHT_MODE` | Override mode (`cloud`, `local_embedded`, `local_external`) |

## Client Version

Requires `hindsight-client==0.9.1`. The plugin re-pins the client on session
start if a different version is detected. Local embedded setup installs
`hindsight-all` alongside the same exact client constraint.
