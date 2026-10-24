# Requested And Served Speed Tiers

`speed-tier-audit` keeps requested versus served tiers observable and allows an Ultrafast parent to pass that preference to an eligible delegated route.

## Behavior

Each completed `codex_responses` or `chat_completions` model response with a requested or reported service tier emits one INFO record:

```text
speed tier: session=<id> model=<wire-model> provider=<provider> route=<api-mode> requested=<tier|none> served=<tier|unreported>
```

The request value comes from the execution middleware's outgoing kwargs, after Responses preflight, not `agent.service_tier`. SDK `extra_body.service_tier` takes precedence over the top-level field. A missing echo is `unreported`, not the requested tier. Logging happens before response validation and usage accounting, including replies without token usage and responses that will be retried.

`agent.usage_pricing.with_served_service_tier` is the canonical served-value normalizer. `agent.turn_usage.record_response_usage` already uses it for pricing. Codex streaming reads the terminal `response.service_tier` in `agent.codex_runtime._CodexResponseAssembler` and preserves it on the completed response. Chat streaming preserves it from both ordinary and usage-only frames, including Relay's separately accumulated final response.

The existing per-call token persistence writes counters and costs, not the raw usage's tier. This patch adds an audit log, not database fields or config keys.

## Delegation

Static tiers use `agent.fast_mode.STATIC_TIERS`, alongside existing bounded-mode inheritance. Ultrafast inheritance uses `ultrafast_offered` for the child's resolved model, provider, endpoint and credential, then `resolve_fast_mode_overrides(tier="ultrafast")`. An unsupported child gets no inherited tier, never an implicit paid Priority fallback. Bounded modes remain bounded. Explicit delegation overrides, including nested `extra_body` fields, win. Parent wire-tier keys are stripped at both levels before rederiving the child's overlay.

Upstream `ed11601539`, `572ecbe43d`, `5d7fea742c` and `0214eb4fdc` add explicit backend-route tiers, refusal/reporting and route-account validation. They do not extend this fork's `_INHERITABLE_FAST_MODES` or its `_resolve_child_request_overrides` path. Importing that backend-route work would not fix the inherited Ultrafast gap on its own. This patch therefore extends the existing fork inheritance path and reuses the same route/account gates without importing the larger delegation-route API change.

## Proxy Evidence

CLIProxyAPI's Codex-to-Responses translator returns the terminal `response` object rather than rebuilding a whitelist of its fields (`internal/translator/codex/openai/responses/codex_openai-responses_response.go`, `ConvertCodexResponseToOpenAIResponsesNonStream`). Its streaming converter also retains the response object. An upstream `service_tier` can therefore pass through this route. This is transport evidence, not proof that a particular account was served the requested tier. Only a real completed response echo establishes that.

The inspected proxy source matches the installed revision `09e3ea1005c46b124117a41191e3369622e7ad14` (`v8.0.36`). At that revision, request translation preserves `priority` and `ultrafast` (request translator, lines 31–48), streaming preserves terminal event bytes (response translator, lines 15–39), and non-streaming returns `responseResult.Raw` (lines 75–97). The usage extractor separately reads `response.service_tier` and `service_tier` in `internal/runtime/executor/helps/usage_helpers.go`.

The earlier hand-written spike returned HTTP 400, `invalid_request_error`, for both tiers and discarded `error.message` and the raw error body. That attempt cannot distinguish a request-shape failure from route/account rejection.

A separately authorized retry used the live Hermes `ResponsesApiTransport.build_kwargs` and `preflight_kwargs` shape: explicit instructions, typed message input, `store=false`, low reasoning effort, encrypted-reasoning include, and the selected tier. Both requests completed with HTTP 200 and returned `service_tier: default`:

| Requested | Served | Response ID |
| --- | --- | --- |
| `priority` | `default` | `resp_098e2eb909ab0804016adcf815d93c87d0a902dbabedf496a8` |
| `ultrafast` | `default` | `resp_0dbc69458e7e3abe016adcf8175f1887d086c6b39b3e097c45` |

The Ultrafast request was sent only after `ultrafast_offered` accepted `gpt-6.1-sol` on `custom:codex-proxy` at the configured proxy URL. Neither response contained an error. This verifies passthrough and the `default` served-tier echoes for these 2 requests, not that all accounts always receive Default, or that paid acceleration was delivered. The original 400's precise field-level cause remains unknown.

## Verification And Retirement

Run the focused tier logging and delegation Ultrafast tests, plus delegation, fast-mode, Codex, chat-stream and pricing regression suites. Remove the fork patch when upstream both covers the inherited-mode path and emits an equivalent per-response requested-versus-served audit, preserving route-specific entitlement checks and explicit-override precedence.
