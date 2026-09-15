# Telegram payload projection and rendering

This responsibility covers Telegram ingress/egress, typed Business identity, Markdown/rich normalization and media payloads. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

HERMES-128 narrows HERMES-098: only explicitly bracketed numeric citations receive presentation brackets; ordinary numeric links stay ordinary links.

## Coupled current adaptations

Follow-up review reproduced loss of authored Telegram link destinations when
valid Markdown supplies an optional title. Both standard and rich rendering now
parse the destination before checking supported schemes. Malformed destinations
and unsupported schemes still degrade to visible labels. Regression coverage:
`tests/gateway/test_telegram_unsupported_link_targets.py`.

- Rich Telegram projection carries server-typed positive user IDs into existing mention gates. Rendered links alone confer no identity. Regression: `tests/plugins/test_telegram_rich_mention_ingress.py`, through native SDK ingress. Retire when upstream preserves equivalent identity evidence.

- Telegram Business identity survives final-response ledger recording, boot/runtime/flood recovery, detached update markers, and the persisted parent-session origin. The additive nullable ledger column preserves legacy non-Business routes. Regressions: `tests/gateway/test_business_durable_routes.py`, `test_agent_update_launcher.py`, and `test_update_lifecycle_notifications.py`. Source rollback leaves the additive column in place and must preserve already-recorded Business routing.

- Telegram conformance vectors are regenerated for normalized CommonMark code spans. Fenced blocks at line boundaries remain fenced.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-029 | Active |
| HERMES-034 | Active |
| HERMES-035 | Active |
| HERMES-040 | Active |
| HERMES-045 | Active |
| HERMES-062 | Active |
| HERMES-063 | Active |
| HERMES-068 | Active |
| HERMES-095 | Active |
| HERMES-098 | Active |
| HERMES-111 | Active |
| HERMES-127 | Active |
| HERMES-128 | Active |
| HERMES-135 | Active |

## Patch records

### HERMES-029 — Provider-directed retries for idempotent media downloads

- **Summary:** Shares bounded retry policy across idempotent image and audio URL-cache GETs. Provider Retry-After seconds or HTTP-date is a minimum delay; excessive delays fail/defer instead of sleeping past the media-download budget. Only 429/502/503/504, connect errors, and connect timeouts retry. Permanent HTTP failures and post-connect/read failures fail closed. Retry logs sanitize signed URLs.
- **Surfaces:** `gateway/platforms/base.py`; `tests/gateway/test_media_download_retry.py`; `tests/gateway/test_media_download_retry_after.py`.
- **Upstream tracking:** No equivalent implementation was found on current official `main` (`30c469b15313711d47c45e7175d6ef5c8437f1ed`) after source/history and issue/PR/commit searches for the two cache helper symbols plus Retry-After on 2026-08-15. Upstream already supplies the shared seconds/HTTP-date parser in `agent.retry_utils`, which this patch reuses.
- **Upstream PR:** None after checked 2026-08-15.
- **Regression:** `.venv/bin/python -m pytest tests/gateway/test_media_download_retry_after.py tests/gateway/test_media_download_retry.py tests/gateway/test_platform_base.py -q`.
- **Rollback:** Remove `_download_media_from_url` and its media retry constants/helpers, restore the separate image/audio request loops and prior timeout fixture, and remove `tests/gateway/test_media_download_retry_after.py`. Preserve SSRF validation, redirect hooks, streaming byte limits, cache validation, and all outbound platform retry semantics.
- **Retirement:** Retire after released upstream shares equivalent image/audio URL-cache GET retry behavior that honors delta/date Retry-After as a minimum, bounds cumulative waiting, retries only the same safe statuses/transports, sanitizes logs, and passes the focused regression.
- **Source references from initial investigation:** ` after fixed 1.5s/3s delays. A 429/503 carrying `.
- **Additional historical subjects (optional provenance):** `fix(media): honor provider retry delays for downloads`.

### HERMES-034 — Add configurable Telegram Rich Message routing modes

- **Summary:** Adds `rich_messages: auto|always|never` to Telegram configuration. `auto` is the default adaptive route, `always` attempts Rich Messages for every final response that passes capability, client-risk, and size guards, and `never` forces legacy MarkdownV2. Existing booleans remain compatible: `true` maps to `auto`, `false` to `never`. Rich draft previews remain separately controlled by `rich_drafts`.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `hermes_cli/config_defaults.py`; `agent/system_prompt.py`; `cli-config.yaml.example`; Telegram messaging documentation; Rich Message tests.
- **Upstream tracking:** Fork-owned routing control. Current released upstream retains adaptive Rich Message delivery but does not provide the explicit `auto|always|never` contract after checked 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `uv run pytest -q tests/gateway/test_telegram_rich_messages.py tests/agent/test_system_prompt.py tests/gateway/test_config.py tests/gateway/test_telegram_rich_newlines.py tests/gateway/test_telegram_visual_spacing.py` — 117 passed. `git diff --check` passed.
- **Activation:** The active personal profile now has `gateway.platforms.telegram.extra.rich_drafts: true`. The running gateway could not self-restart; an external `hermes gateway restart` is still required. The new `always` mode is not active until the patched Hermes runtime is published and activated.
- **Rollback:** Set `rich_drafts: false`; set `rich_messages: true` or `auto` for adaptive routing; or revert the patch and restart externally. Do not force-push over the current fork/remote divergence.
- **Retirement:** Retire after released upstream exposes equivalent validated routing modes across config, system guidance, adapter behavior, backward-compatible booleans, and the focused Rich Message regressions.
- **Additional historical subjects (optional provenance):** `feat(telegram): configure rich message routing mode`.

### HERMES-035 — Preserve exact legacy Telegram paragraph boundaries

- **Summary:** The original patch expanded every Markdown paragraph boundary with an explicit non-breaking-space line at the Telegram transport boundary. That transport-wide expansion made ordinary paragraphs excessively tall and was removed by `fix(telegram): remove excessive paragraph spacing`; the remaining fork surface is the regression guard `tests/gateway/test_telegram_visual_spacing.py`, which pins the narrower contract that legacy MarkdownV2 and plain Bot API payloads preserve the author's paragraph boundaries exactly. HERMES-095 owns the separate Rich Message renderer correction and inserts exactly one hard-broken spacer row only between prose paragraphs. (Record narrowed 2026-09-01 after the live Rich Message renderer failure was isolated.)
- **Surfaces:** `tests/gateway/test_telegram_visual_spacing.py` (test-only guard; no shipped adapter delta remains).
- **Upstream tracking:** Upstream never carried the retired transport-wide NBSP expansion, so the legacy/plain guard remains upstream-equivalent. Rich Message paragraph rendering is tracked in issue #100664.
- **Upstream PR:** PR #100686 covers only the Rich Message renderer correction; it does not alter this legacy/plain contract.
- **Regression:** `uv run pytest -q tests/gateway/test_telegram_visual_spacing.py tests/gateway/test_telegram_rich_newlines.py`.
- **Rollback:** Delete the test file; there is no adapter code to revert.
- **Retirement:** Retire (delete the guard) once an upstream-owned test pins the same exact-paragraph-boundary payload contract.
- **Additional historical subjects (optional provenance):** `fix(telegram): repair transport safety regressions`.

### HERMES-040 — Extract eligible local Markdown images at the gateway boundary

- **Summary:** Detects eligible local Markdown image references in gateway output and routes only files inside explicit media-delivery roots through the bounded native-media extraction path, leaving out-of-root paths as text instead of turning them into attachments.
- **Surfaces:** `gateway/platforms/base.py`; `tests/gateway/test_platform_base.py`.
- **Upstream tracking:** No equivalent released gateway extraction behavior was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_platform_base.py -k 'markdown and image'`.
- **Rollback:** Remove only local Markdown image extraction and its tests; preserve remote-image, attachment, path-validation, and media-size safeguards.
- **Retirement:** Retire after released upstream safely extracts equivalent local Markdown images at the delivery boundary.
- **Additional historical subjects (optional provenance):** `fix(gateway): extract local markdown images`; `fix(gateway): restrict markdown images to trusted roots`.

### HERMES-045 — Keep rich Telegram tables narrow

- **Summary:** Makes the rich-message prompt objective rather than advisory: Telegram replies may use compact one- or two-column tables, but must not generate tables with three or more columns. Wider data is transposed or split into vertically stacked records with labeled fields. Existing Telegram rendering and the transport fallback for clients without rich-table support remain unchanged.
- **Surfaces:** `agent/prompt_builder.py`; `tests/agent/test_system_prompt.py`.
- **Upstream tracking:** Closed issue #14160 and merged PR #16997 introduced Telegram table-to-row-group fallback. Closed issue #47095 tracks native Bot API 10.1 table support and was marked duplicate. Closed issue #46009 repaired rich formatting during streamed edits. No exact upstream issue or pull request requires the model prompt to avoid wide native tables as of upstream `0159b51f2b1cdaa8fcf65d181fc0527692724fae` on 2026-08-23.
- **Upstream PR:** None for narrow rich-table generation after checked 2026-08-23. Related merged PR: #16997.
- **Regression:** `.venv/bin/python -m pytest tests/agent/test_system_prompt.py tests/agent/test_prompt_builder.py -q`; `git diff --check`. The focused rich-message test requires the exact three-or-more-column prohibition.
- **Published commit identity:** Stable subjects `fix(telegram): keep rich tables narrow` and `fix(telegram): keep wide tables out of mobile replies`; source, regression, and manifest ship together.
- **Rollback:** Remove the exact three-or-more-column prohibition, restore the advisory many-column wording, and remove the focused assertion. Preserve rich-message activation, native table rendering, transport fallback, and unrelated formatting guidance.
- **Retirement:** Retire after released upstream gives agents an objective no-three-or-more-column Telegram rule, uses vertically stacked labeled records for wider data, preserves existing rich-message behavior, and passes the focused prompt regression.

### HERMES-062 — Normalize inbound Telegram checklists

- **Independent hypothesis (2026-08-28):** Telegram delivers native checklist, `ChecklistTasksAdded`, and `ChecklistTasksDone` payloads as message-like updates, but `_register_handlers()` has no matching filter and `_build_message_event()` reads only ordinary text. The group-99 observer does not dispatch them, so a valid update is acknowledged and silently lost. The correction belongs at Telegram ingress: use PTB 22.8's typed checklist filters and objects, project bounded human-readable text plus structured metadata into one `MessageEvent`, preserve ordinary authorization/topic gates, treat collaboration status changes as observed context instead of unsolicited turns, and register a final group-0 `filters.ALL` guard that logs sanitized shape metadata for any future unmatched message without duplicating matched handlers or exposing content.
- **Summary:** Normalizes initial checklists, added tasks, and done/undone task IDs through the existing Telegram event pipeline; disables gateway-command interpretation for generated text; preserves task IDs, completion attribution/timestamps, collaboration flags, and referenced checklist message identity; and surfaces future unsupported message-like updates through bounded metadata-only logs. Status updates remain observable even when Telegram omits the optional referenced checklist message. QA hardening observes edited checklist updates without new turns, reapplies authorization and topic-routing gates before observation, persists structured observe-only metadata with Telegram timestamps, and bounds malformed task identifiers or completion dates instead of crashing the handler.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_checklist_ingress.py`; `tests/gateway/test_gateway_platform_event_hook.py`; this record.
- **Upstream tracking:** Canonical open issue #78718 tracks inbound checklist and checklist-service-message loss. Canonical open issue #78696 tracks the broader subscribed-but-unhandled Telegram update class. Open Rich Message PRs #63491, #81369, #94899, and #95292 independently demonstrate the same allowlist fallthrough for one content family but do not normalize checklists or establish a generic message guard. Upstream `main` at `3340bbbdad8368e7f3d9f6827d61692adbbd87d6` still drops the reproduced checklist update as of 2026-08-28.
- **Upstream PR:** None found for inbound checklists. Related generic/rich-message PRs only: #63491, #81369, #94899, and #95292.
- **Regression:** RED reproduced one real PTB 22.8 checklist matching only the group-99 observer. GREEN: `scripts/run_tests.sh tests/gateway/test_gateway_platform_event_hook.py tests/gateway/test_telegram*.py -q` passed `682` tests across `63` files; the focused checklist file passed `7` tests, including missing-reference status events and a slotted-message fallback; and a real PTB 22.8 canary proved the final guard handles `telegram.Message` without `__dict__` and reports only `dice`. `git diff --check`, bytecode compilation, Ruff, and manifest validation passed. Independent Claude autoreview found and the patch fixed the slotted-PTB `vars()` crash; confirmation review exited `0` with no P0/P1 findings. Follow-up adversarial QA added edited-update, routing-gate, observe-only metadata, malformed-ID, and malformed-completion regressions; the focused ingress file passed `13` tests, a 500-case JSON-shape fuzz canary raised no exceptions, and the 29-case real PTB 22.8 matrix passed completely.
- **Published commit identity:** Stable subjects `fix(telegram): normalize inbound checklists` and `fix(telegram): harden checklist support`; source, regressions, and this record ship together.
- **Rollback:** Revert `fix(telegram): harden checklist support` first, then revert `fix(telegram): normalize inbound checklists`, removing the checklist classifier/handler, final unmatched-message guard, focused regressions, and this record. Preserve existing text/media/location/topic handlers and the group-99 platform observer.
- **Retirement:** Retire after a released upstream version uses typed PTB checklist objects to preserve equivalent content and metadata under existing authorization/session gates, treats status-only updates without unsolicited replies, and guarantees unmatched message-like updates cannot disappear silently, with equivalent regressions.

### HERMES-063 — Support Telegram Business checklists

- **Independent hypothesis (2026-08-28):** PTB 22.8 exposes typed `send_checklist` and `edit_message_checklist`, while Hermes carries no `business_connection_id` from inbound `Message` through `SessionSource`, session identity, reply metadata, or Telegram API calls. A Business conversation can therefore collide with ordinary Telegram session routing and Hermes cannot reliably reply, send, or edit on behalf of the connected account. The correction belongs in the platform-neutral source wire contract plus Telegram's metadata/send boundary: persist an explicit Business connection discriminator, include it in Telegram session keys, stamp it onto every turn-scoped reply, and expose validated typed checklist send/edit methods that require that identity and reuse the adapter's cooldown/error-classification boundary.
- **Summary:** Preserves Business connection identity across inbound event construction, source serialization, session isolation, and ordinary Telegram send/edit/chat-action metadata; adds native checklist send/edit methods backed by PTB's typed `InputChecklist` objects with bounded title/task validation, unique task IDs, explicit collaboration flags, fail-closed missing-connection behavior, and ambiguity-aware retry classification. QA hardening propagates Business identity through persistent media and interactive control messages, skips draft APIs that cannot carry the connection discriminator, and rejects lossy or non-positive checklist message-ID coercions.
- **Surfaces:** `gateway/session.py`; `gateway/run.py`; `plugins/platforms/telegram/adapter.py`; focused session-routing and Telegram Business/checklist regressions; this record.
- **Upstream tracking:** Canonical open issue #78714 tracks native checklist send/edit and identifies Telegram Business support as a dependency. Open issues #26653 and #26858 track Business connections, and #42400 tracks owner-authored Business messages. Their issue text predates PTB 22.8 and incorrectly recommends a raw API escape hatch because PTB 22.6 lacked the types. Current PTB provides typed methods; upstream `main` at `3340bbbdad8368e7f3d9f6827d61692adbbd87d6` still has no `business_connection_id` carrier or checklist send/edit surface as of 2026-08-28.
- **Upstream PR:** None found that satisfies the complete connection-aware routing plus typed checklist send/edit contract.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_business_checklists.py -q` passed `13` tests covering source serialization, Business-isolated session keys, turn metadata, ordinary text send propagation, fail-closed missing identity and malformed payloads, typed send/edit calls, returned message IDs, and no retry authorization for ambiguous sends. The combined Telegram suite passed `682` tests across `63` files. A real PTB 22.8 canary constructed `InputChecklist` and preserved Business identity on inbound events. The repository-wide canonical runner completed with `61` failures: two stale handler-count expectations corrected by this patch, `59` failures in nine unrelated files, and two unrelated no-collection/timeouts; a clean baseline rerun reproduced `57` failures across seven unchanged dependency/environment-sensitive files. The final affected Telegram suite passed completely. Static checks, manifest validation, and independent confirmation review passed. Follow-up QA proved and repaired missing Business routing on media and interactive controls, fail-closed unsupported draft behavior, and strict positive message-ID validation; `test_telegram_business_checklists.py` passed `28` tests, and the final `test_telegram*.py` run passed `669` tests across `62` files.
- **Published commit identity:** Stable subjects `feat(telegram): support business checklists` and `fix(telegram): harden checklist support`; source, regressions, and this record ship together.
- **Rollback:** Revert `fix(telegram): harden checklist support` first, then revert `feat(telegram): support business checklists`, removing the source discriminator, Telegram session-key component, reply metadata propagation, native checklist methods, focused regressions, and this record. Preserve HERMES-062 inbound normalization and ordinary non-Business Telegram routing.
- **Retirement:** Retire after released upstream preserves Business connection identity end to end, isolates sessions by connection, passes it through ordinary replies, and exposes typed validated native checklist send/edit operations with equivalent transport-safety regressions.

### HERMES-068 — Normalize inbound Telegram Rich Messages

- **Summary:** Adds a dedicated Rich Message filter after established text/media handlers and before the final unsupported-message guard; accepts PTB 22.8 `api_kwargs` payloads and future typed Telegram objects; projects headings, inline formatting, lists/check states, details, quotations, tables, links, commands, media markers, and unknown nested blocks into bounded Markdown; preserves bounded block metadata; reuses the normal authorization, group/topic, Business-session, text-batching, reply-media, and observation paths; makes Rich Message text participate in mention/wake-word routing; and reuses the same bounded renderer for reply context. Depth, node, type, and character limits prevent malformed payloads from consuming unbounded work.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `plugins/platforms/telegram/rich_messages.py`; `tests/gateway/test_telegram_rich_message_ingress.py`; this record.
- **Upstream tracking:** Canonical open issue #63485 reports the same inbound Rich Message loss. Open PRs #63491, #81369, #94899, and #95292 propose overlapping fixes. No new issue or duplicate PR was created. Upstream `main` at `586672532347252e3df7d893223ce003070ee434` still has no inbound `rich_message` classifier or normalizer as of 2026-08-28.
- **Upstream PR:** Direct existing candidates: #63491, #81369, #94899, and #95292. The fork patch keeps the narrow ingress contract while adding bounded rendering, future typed-object compatibility, structured metadata, group gating, observe-only persistence, media non-interference, and malformed-payload limits not satisfied together by any one candidate.
- **Regression:** RED: the focused file produced eight expected failures and one environment skip because no Rich Message filter/handler or group mention source existed. GREEN: the focused file passed ten tests with one intentional mocked-environment skip; combined Rich Message, event-hook, and outgoing Rich Message coverage passed 87 tests with zero failures and one skip; the final Telegram/event-hook suite passed 726 tests across 65 files with zero failures and one intentional mocked-environment skip; and a clean real PTB 22.8 canary proved `filters.TEXT == False`, the new classifier matched, and exactly one Markdown event entered the normal text pipeline. Ruff, bytecode compilation, `git diff --check`, and maintenance-manifest validation passed before publication.
- **Published commit identity:** Stable subject `fix(telegram): normalize inbound rich messages`; source, regressions, and this record ship together.
- **Rollback:** Revert `fix(telegram): normalize inbound rich messages`, removing the dedicated filter/handler, bounded renderer, focused regressions, and this record. Preserve HERMES-062's checklist normalization and final sanitized unsupported-message guard.
- **Retirement:** Retire after a released upstream version recognizes Rich Messages on the supported PTB version, preserves equivalent readable content under existing authorization/session/topic gates, cannot steal ordinary text/media updates, and passes equivalent malformed-payload and routing regressions.
- **Source references from initial investigation:** 261380292; ` / message `.

### HERMES-095 — Render Telegram Rich Message paragraph spacing

- **Summary:** In Rich Message Markdown only, convert a prose `\\n{2,}` boundary into one non-breaking-space row bounded by idempotent Markdown hard breaks. Leave structural boundaries, backtick and tilde fences, tables, details, block math, blockquotes, lists, headings, and indented continuations without inserted spacers. Count the normalized Markdown against Telegram's 32,768-character Rich Message limit before choosing the rich path. Legacy MarkdownV2 and plain sends retain HERMES-035's exact source-boundary contract.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_rich_newlines.py`; this record.
- **Upstream tracking:** NousResearch/hermes-agent#100664 documents the user-visible rendering gap and distinguishes it from the isolated-newline fix in #46070 / PR #50196.
- **Upstream PR:** Open PR #100686 carries the source and focused regressions. The fork mirrors that reviewed implementation while upstream maintainers retain merge authority.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_rich_newlines.py tests/gateway/test_telegram_rich_messages.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_send_draft_format.py -q`; coverage must prove one prose spacer, repeated-boundary collapse, idempotence, structural-block preservation, inline angle-bracket prose, normalized-length fallback, and unchanged shared rich send/edit/draft payload construction. Activation additionally requires a live Telegram iOS comparison after promotion.
- **Expected published commit identity:** Stable subject `fix(telegram): render rich prose paragraph spacing`; source, focused regressions, and this record ship together.
- **Rollback:** Revert only `fix(telegram): render rich prose paragraph spacing`, remove HERMES-095's index row and record, and restore HERMES-035's transport-wide wording. As an immediate runtime mitigation, set `gateway.platforms.telegram.extra.rich_messages: auto` or `never`, then promote and restart through the normal release path.
- **Retirement:** Retire after PR #100686, or an equivalent implementation, is merged and released upstream, preserves one visible prose spacer without structural-block regressions, passes equivalent regressions, and succeeds in live Telegram iOS QA. Remove the fork implementation and duplicate tests rather than retaining parallel behavior.
- **Source references from initial investigation:** `state/rich_sent_index.json`.
- **Additional historical subjects (optional provenance):** `fix(telegram): render rich prose paragraph spacing (#32)`; `fix(telegram): preserve rich structural blocks (#34)`.

### HERMES-098 — Keep Telegram citation brackets visible

- **Summary:** At the Telegram presentation boundary, recognize an already-linked all-numeric label and escape an inner pair of brackets so Telegram renders the complete clickable marker `[3]`. Preserve ordinary authored links, unsupported-target degradation, code and table protection, URLs, persisted assistant text, and all non-Telegram surfaces.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_unsupported_link_targets.py`; this record.
- **Upstream tracking:** Open issue #87729 and open PR #87732 establish the same complete-visible-marker requirement for standalone numeric references resolved through a Sources block. Their implementation does not cover the already-linked `[3](url)` shape reproduced here across both Telegram delivery paths.
- **Upstream PR:** No upstream change was found that guards already-linked numeric citation labels in both legacy MarkdownV2 and Bot API rich-message output as of 2026-09-01.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_unsupported_link_targets.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_rich_newlines.py -q`; the two focused cases fail before the patch with `[3](url)` and pass with `[\\[3\\]](url)` in both outbound payloads, while ordinary links and unsupported targets retain their existing behavior.
- **Expected published commit identity:** Stable subject `fix(telegram): keep citation brackets visible`; source, regressions, and this record ship together.
- **Rollback:** Revert only `fix(telegram): keep citation brackets visible`, restoring bare numeric labels in both Telegram Markdown renderers, then remove the HERMES-098 index row and this record. No schema, configuration, or persistent-data rollback is required.
- **Retirement:** Retire after a released upstream version preserves the complete bracketed marker for already-linked numeric citations in both Telegram delivery paths and passes equivalent URL, ordinary-link, unsupported-target, code, and table regressions. Remove the fork implementation and duplicate tests rather than retaining parallel behavior.

### HERMES-111 — Preserve math and currency across Telegram routes

- **Summary:** The adapter already protected currency in always-rich sends, final edits and drafts. The remaining defects were closed numeric math being rewritten as currency, and numeric dollar entities surviving literally into legacy/plain fallbacks. Protect complete math spans alongside code and normalize decimal/hex dollar entities outside code at send, edit and draft boundaries. Currency protection stays adapter-owned, not in a competing output-transform rule.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_rich_messages.py`. Companion repository `0xble/hermes-output-guard` removes its already-unregistered `dollar_math` module, duplicate tests and stale documentation. Installed plugin promotion is a separate checkpoint.
- **Upstream tracking:** Checked against upstream `b51c055a12220f8c7c18660e8599365012e19532` and current fork source on 2026-09-05. No verified complete replacement is claimed. This record separates the existing native currency projection from HERMES-044's unrelated transform-composition contract.
- **Upstream PR:** No publication requested. Source-level defect reproduction and adapter-boundary regression tests justify this residual fix; an open proposal is not a replacement.
- **Regression:** `python -m pytest -q tests/gateway/test_telegram_rich_messages.py`. Verify numeric atoms, decimals, coefficients, fractions and display math survive beside multiple currency amounts; dollar entities become currency before rich rejection fallback on send/edit/draft; code entities remain literal. Run the Output Guard suite before removing the dead rule, preserving session-reference and Slack behavior.
- **Rollback:** Revert only `fix(telegram): preserve math and normalize currency entities` to restore the previous projection. Do not revive the unregistered plugin currency rule or weaken HERMES-044's transform-composition protections.
- **Retirement:** Retire when the adopted upstream adapter passes the same math, currency, code and capability-fallback contracts on all three boundaries, then remove the private projection delta rather than retain two owners.

### HERMES-127 — Literal hash references in Rich Messages

- **Summary:** Escape literal block-start hashes such as `#89`, including list and blockquote prefixes, before Telegram's permissive rich parser consumes them as headings. Preserve genuine spaced headings, inline references, code, math, and existing escapes. The shared payload boundary covers sends, finalized edits, and optional rich drafts. Keep the fork's currency/link/paragraph normalization and `rich_messages: always` behavior intact.
- **Upstream tracking:** [NousResearch/hermes-agent#105483](https://github.com/NousResearch/hermes-agent/issues/105483). Live Bot API reproduction returns a heading for raw `#89` and a paragraph retaining the hash for `\#89`. Related typography issue #45762 and line-break PR #76368 do not cover this literal-prefix defect.
- **Upstream PR:** [NousResearch/hermes-agent#105487](https://github.com/NousResearch/hermes-agent/pull/105487), proposed fix closing #105483. Fork adaptation keeps the same normalizer and transport regressions, composed with the existing fork-only currency/link/paragraph normalizers.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_telegram*.py` passed 849 tests across 71 files with 0 failures and 3 skips. New send/edit/draft regressions fail on unpatched upstream. Live Bot API send and exact stored-message forward readback passed for both upstream and fork payloads, proving preserved paragraph references, list references, real headings, inline code, and native tables. Ruff, diff checks, and maintenance validation pass. The upstream-only broad suite has two existing DNS-dependent image timeout failures also reproduced on its clean base. Independent Claude review of the upstream candidate found no P0–P2 defects.
- **Published commit identity:** Stable subject `fix(telegram): preserve literal hash references in rich messages`.
- **Rollback:** Revert this subject, removing `rich_markdown.py`, its adapter call, and focused regressions. Preserve the existing currency/link/paragraph normalizers and all runtime configuration. Runtime rollout and rollback remain separate.
- **Retirement:** Replace with released upstream behavior after equivalent literal-prefix, code/heading preservation, transport regressions, and live rich-message readback pass. Remove the fork-only implementation rather than retaining duplicate normalization.

### HERMES-128 — Scope Telegram citation brackets to explicit markers

- **Summary:** Convert only explicitly bracketed numeric citations into one Telegram-clickable visible `[n]` marker. Preserve ordinary `[label](URL)` links, including numeric labels, in legacy MarkdownV2 and rich-message paths. Unsupported targets still degrade to readable label text, and protected code/table regions remain literal.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_unsupported_link_targets.py`; this manifest.
- **Upstream tracking:** Fork-only correction to HERMES-098. The current upstream adapter contains neither HERMES-098's numeric-label branch nor this explicit-marker correction.
- **Upstream PR:** Not applicable — the local-only source means no upstream issue or PR is applicable.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_unsupported_link_targets.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_rich_messages.py tests/gateway/test_telegram_send_draft_format.py -q`. Coverage proves rich, legacy, draft/fallback format parity, visible clickable `[[n](URL)]` markers, ordinary numeric commit/PR links, and existing unsupported-target/code protections.
- **Published commit identity:** Stable subject `fix(telegram): scope citation brackets to explicit markers`.
- **Rollback:** Revert only this stable-subject commit, restoring HERMES-098's numeric-label inference. Remove this index row and record with the same revert; no schema, configuration, credentials, or persistent data changes are involved.
- **Retirement:** Retire if HERMES-098 itself is removed or an upstream release owns the same explicit-marker-only contract across both Telegram delivery paths and equivalent regressions pass; remove the fork-only implementation rather than retaining duplicate behavior.

### HERMES-135 — Valid nested and multiline legacy Telegram emphasis

- **Status:** Published-source candidate only. No managed checkout mutation, promotion, configuration change, or restart.
- **Contract and provenance:** Reproduced sequential bold/italic regex corruption of `***bold italic***`, `**bold *italic* text**`, and multiline bold on upstream `2db0c7a2d8f29debe7d1cbfb4a72f4f98dc00808`. The earlier investigation had already inspected prior art; this is not an unanchored hypothesis. Reuse the installed markdown-it delimiter resolver rather than inventing a tokenizer; preserve code placeholders, unsupported/literal markers and existing surrounding syntax. Valid syntax repair does not repair malformed model Markdown or the separate rich-message renderer.
- **Source surfaces:** `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/emphasis.py`, `tests/gateway/test_telegram_emphasis.py`. No new dependency, state, schema, or configuration. The legacy send path remains the integration boundary.
- **Upstream tracking:** [Issue #106891](https://github.com/NousResearch/hermes-agent/issues/106891). Related open #55887 strips leftover markers and does not fulfill nested-emphasis/literal-preservation semantics; #11287 proposes a broad entities rewrite. Quote issue #90773 and PR #90781 have a different contract. No matching nested-emphasis issue or emphasis PR was found in the immediate pre-filing search on 2026-09-09.
- **Upstream PR:** [#106906](https://github.com/NousResearch/hermes-agent/pull/106906), contribution commit `37f872bad1706c6c50ccdccb825fc4d5ffd2c246`. Same implementation, without fork-specific metadata. Publication is not upstream acceptance; do not merge upstream automatically.
- **Regression:** `scripts/run_tests.sh -j 8 tests/gateway/test_telegram*.py tests/gateway/test_table_helpers.py` passed 904 tests across 74 fork files, zero failures, three platform skips. Two parametrized invariants exercise actual legacy send payloads and preservation of literals/protected syntax. Final upstream red: ten failing cases and twenty passing preservation cases; final focused suite: 72 passed. Final broader upstream suite: 695 passed, two image-timeout failures also reproduced on clean baseline, two skips. No live Telegram server/rendering or full-repository test claim.
- **Rollback:** Revert only the scoped HERMES-135 landed commit (adapter emphasis hunk, helper, focused tests and this entry), preserving all rich-message and other Telegram fork patches. No persistent state needs rollback.
- **Retirement:** Replace this implementation when released upstream passes equivalent valid combined/nested/multiline asterisk emphasis and literal/code/link/bullet/quote preservation regressions. Do not retain duplicate fork code or tests.
- **Source references from initial investigation:** `. Policy selected Claude Fable/Opus but that route was unavailable, so the supported isolated Codex Astra fallback completed review. Earlier passes identified seven findings, all reproduced and fixed (underscore and block boundaries, crossing strike/spoiler spans, hardbreak preservation, quote continuation). Same lineage continued throughout. Final receipt/evidence retained under `; f2d17067772a4c14bef722ce05bf0dd2.
- **Additional historical subjects (optional provenance):** `fix(telegram): preserve nested and multiline legacy emphasis`.
