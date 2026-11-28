# Fork resolution

Lost-code audit for the v0.23.4 sync. Regenerate with `python scripts/fork_lost_code.py --write`; check with `python scripts/fork_lost_code.py`.

<!-- generated-by: scripts/fork_lost_code.py -->

## Items

### `MAINTENANCE.md`

- (module)
  - `MAINTENANCE.md@-0,0 +1,802` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/agent_init.py`

- def _load_tools(agent, enabled_toolsets, disabled_toolsets):
  - `agent/agent_init.py@-1048 +1043` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _init_session_state(agent, session_id, session_db, parent_session_id, reason
  - `agent/agent_init.py@-1082,0 +1078,3#moved` moved: `tests/gateway/test_incognito_commands.py`, `tests/gateway/test_incognito_model_pick.py`, `tests/gateway/test_routing_save_delta.py`, `tests/gateway/test_session_snapshot.py` (+3 more)
  - `agent/agent_init.py@-1087,0 +1086,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1097 +1099,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _init_memory(agent, _agent_cfg, skip_memory, platform, memory_manager=None):
  - `agent/agent_init.py@-1161,0 +1165#moved` moved: `agent/skill_review_gate.py`
  - `agent/agent_init.py@-1197,0 +1202` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1208,0 +1214` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1215 +1221,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _parse_compression_config(agent, _agent_cfg) -> CompressionSettings:
  - `agent/agent_init.py@-1374,0 +1386,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1410,2 +1424,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1410,2 +1424,4#moved` moved: `agent/coding_context.py`, `agent/context_compressor.py`, `apps/desktop/src/app/context-menu/app-context-menu.tsx`, `apps/desktop/src/app/session/hooks/use-session-actions/index.ts` (+28 more)
  - `agent/agent_init.py@-1426 +1442,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _build_context_engine(agent, _agent_cfg, cs, _custom_providers, _effective_c
  - `agent/agent_init.py@-1822 +1839` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1835 +1852` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1889 +1906` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-1901,0 +1919,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def init_agent(
  - `agent/agent_init.py@-2102,0 +2127,2#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)
  - `agent/agent_init.py@-2199 +2226` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-2207,0 +2235,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-2207,0 +2235,5#moved` moved: `tests/agent/test_cooldown_state_availability.py`, `tests/agent/test_shared_primary_cooldown.py`
- _SESSION_STATE: dict[str, Any] = {
  - `agent/agent_init.py@-517,0 +516,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _STREAM_STATE: dict[str, Any] = {
  - `agent/agent_init.py@-536 +539,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/agent_init.py@-538,3 +542` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _explicit_client_kwargs(agent, api_key, base_url, _provider_timeout) -> dict
  - `agent/agent_init.py@-743,2 +745,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/compaction_events.py`

- _TRIGGERS = frozenset({
  - `agent/compaction_events.py@-40 +40` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/context_compressor_prune.py`

- class ProactivePruneMixin:
  - `agent/context_compressor_prune.py@-106 +117` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/context_compressor_prune.py@-108 +119` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/context_compressor_prune.py@-113 +124` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import logging
  - `agent/context_compressor_prune.py@-12,0 +13#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2575 more)
- class ProactivePruneMixin:
  - `agent/context_compressor_prune.py@-126,0 +138,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/context_compressor_prune.py@-138,0 +154,2#moved` moved: `agent/context_compressor.py`
  - `agent/context_compressor_prune.py@-27,0 +29,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/context_compressor_prune.py@-58,0 +68,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/conversation_compression.py`

- _COMPRESSOR_ATTEMPT_STATE_FIELDS = (
  - `agent/conversation_compression.py@-187 +189` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from agent.usage_anchor import set_usage_anchor
  - `agent/conversation_compression.py@-44,0 +47#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)

### `agent/conversation_compression_manual.py`

- def compress_now(
  - `agent/conversation_compression_manual.py@-105,0 +143,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/conversation_loop.py`

- (module)
  - `agent/conversation_loop.py@-0,0 +1` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "
  - `agent/conversation_loop.py@-1006,3 +1013,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _get_continuation_prompt(is_partial_stub: bool, dropped_tools: Optional[list
  - `agent/conversation_loop.py@-1010,0 +1018,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1012,7 +1025,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _compression_deferred_result(agent, messages: list[dict], api_call_count: in
  - `agent/conversation_loop.py@-1180,4 +1188,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1191 +1205` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _arm_fallback_restart(agent, api_messages, active_system_prompt, _retry):
  - `agent/conversation_loop.py@-1272,0 +1305,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1272,0 +1305,6#moved` moved: `acp_adapter/commands.py`, `agent/anthropic_thinking_replay.py`, `agent/chat_completion_helpers.py`, `agent/codex_runtime.py` (+26 more)
- def _preflight_timeout_result(agent, exc, conversation_history) -> dict[str, Any
  - `agent/conversation_loop.py@-1450 +1488` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _run_conversation_turn(
  - `agent/conversation_loop.py@-1681 +1720,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1681 +1720,5#moved` moved: `agent/turn_finalizer.py`, `agent/turn_preflight_gate.py`
  - `agent/conversation_loop.py@-1740,0 +1785,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1758,0 +1807,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1766,0 +1818,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1769 +1837,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def run_conversation(
  - `agent/conversation_loop.py@-1833,0 +1905#moved` moved: `agent/moa_loop.py`, `tests/agent/test_proxy_request_class.py`
  - `agent/conversation_loop.py@-1839,2 +1911,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1843 +1930` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/conversation_loop.py@-1863,0 +1952,2#moved` moved: `agent/turn_finalizer.py`, `agent/turn_preflight_gate.py`
- from agent.message_sanitization import _repair_tool_call_arguments, _sanitize_su
  - `agent/conversation_loop.py@-23 +28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from __future__ import annotations
  - `agent/conversation_loop.py@-8,0 +10,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _restore_or_build_system_prompt(agent, system_message, conversation_history)
  - `agent/conversation_loop.py@-861,2 +864,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _stored_prompt_matches_runtime(agent, prompt: str) -> bool:
  - `agent/conversation_loop.py@-983,0 +987,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/fallback_notice.py`

- (module)
  - `agent/fallback_notice.py@-0,0 +1,94` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/fallback_notice.py@-0,0 +1,94#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+11783 more)

### `agent/memory_context.py`

- _INTERNAL_NOTE_RE = re.compile(
  - `agent/memory_context.py@-18,2 +18,6#moved` moved: `session_rules/turns.py`
- def build_memory_context_block(raw_context: str) -> str:
  - `agent/memory_context.py@-194,3 +198,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/memory_manager.py`

- from typing import Any, Callable, Dict, List, Optional
  - `agent/memory_manager.py@-20 +20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from agent.memory_manager_tools import (inject_memory_provider_tools, memory_pro
  - `agent/memory_manager.py@-28 +28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/micro_compaction.py`

- class MicroCompactionMixin:
  - `agent/micro_compaction.py@-203,0 +204,2#moved` moved: `agent/context_compressor.py`
  - `agent/micro_compaction.py@-212 +214` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-236 +238,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-250,0 +257` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-258 +265` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-260,2 +267,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-262,0 +271` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-266 +275,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-291 +301,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-295 +306,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-302,3 +317,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-302,3 +317,6#moved` moved: `agent/shared_primary_probe_runtime.py`, `apps/desktop/electron/challenge-window.ts`, `apps/desktop/electron/release-holder-consent.test.ts`, `apps/desktop/src/app/capabilities/connectors/data/hosted-actions.ts` (+36 more)
  - `agent/micro_compaction.py@-411 +429#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_unavailable.py`, `agent/bedrock_adapter.py`, `agent/context_compressor.py` (+71 more)
  - `agent/micro_compaction.py@-414,2 +432,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-436 +454,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-438,2 +457,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-448 +467` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-453,2 +472,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/micro_compaction.py@-512 +535,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/prompt_refresh.py`

- def rebuild_for_refresh(agent: Any, system_message: Any, conversation_history: A
  - `agent/prompt_refresh.py@-83,0 +85#moved` moved: `tests/tools/test_delegate_gateway_inline.py`
- the prompt is older, rebuilds it once through the normal builder: history, sessi
  - `agent/prompt_refresh.py@-9 +9,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def rebuild_for_refresh(agent: Any, system_message: Any, conversation_history: A
  - `agent/prompt_refresh.py@-93,0 +96,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/session_persistence.py`

- from agent.message_metadata import (
  - `agent/session_persistence.py@-27 +27` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/status_output.py`

- from agent.session_activity import ActivityProvenance
  - `agent/status_output.py@-11,0 +12#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- class StatusOutputMixin:
  - `agent/status_output.py@-110 +120` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-114 +124` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-116 +126` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-118 +128` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-130,0 +141` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-144 +155` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-255 +266` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-41,0 +43,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-43 +49` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-46,0 +53,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/status_output.py@-46,0 +53,4#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)

### `agent/title_generator.py`

- def generate_title(
  - `agent/title_generator.py@-670,9 +1035,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _has_upgraded_title(session_db, session_id: str) -> bool:
  - `agent/title_generator.py@-712 +1132` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def auto_title_session(
  - `agent/title_generator.py@-808,5 +1250,68` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/title_generator.py@-819 +1330,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def maybe_auto_title(
  - `agent/title_generator.py@-899 +1494,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/title_generator.py@-932 +1530` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/transcript_repair.py`

- def transcript_row_snapshot(row: Mapping[str, Any]) -> str:
  - `agent/transcript_repair.py@-73 +73,5#moved` moved: `persistence/sqlite/conversation/_store/transcript_repair.py`

### `agent/turn_finalizer.py`

- def _explain_abnormal_exit(agent, final_response, _turn_exit_reason, preserved_v
  - `agent/turn_finalizer.py@-445,0 +451` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/turn_finalizer.py@-449 +455` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `agent/vault_login_classifier.py`

- def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginCon
  - `agent/vault_login_classifier.py@-115 +136` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  - `agent/vault_login_classifier.py@-276 +566,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str
  - `agent/vault_login_classifier.py@-305,0 +623,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/vault_login_classifier.py@-314 +648` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _FILL_JS_TEMPLATE = """(() => {
  - `agent/vault_login_classifier.py@-351,2 +790,69` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `agent/vault_login_classifier.py@-351,2 +790,69#moved` moved: `apps/bootstrap-installer/src-tauri/src/events.rs`, `apps/bootstrap-installer/src-tauri/src/marker_tests.rs`, `apps/bootstrap-installer/src-tauri/src/powershell.rs`, `apps/desktop/electron/native/hud-modifier-monitor.m` (+16 more)

### `apps/desktop/e2e/core/webapp-stale-journal.spec.ts`

- async function withWebapp(
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-235,0 +240,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-238 +245,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-240,0 +249#moved` moved: `apps/desktop/electron/connection-registry-remove.test.ts`, `apps/desktop/electron/gateway-file-download.ts`, `apps/desktop/electron/linux-nvidia-egl-recovery.native.test.ts`, `apps/desktop/electron/window-reveal.ts` (+4 more)
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-244,0 +254,4#moved` moved: `apps/desktop/e2e/at-rest-connection-token.spec.ts`, `apps/desktop/e2e/bot-screen-stale-status-reply.spec.ts`, `apps/desktop/e2e/core/harness.ts`, `apps/desktop/e2e/core/known.ts` (+340 more)
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-257 +270,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-257 +270,26#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3162 more)

### `apps/desktop/src/app/session/hooks/use-prompt-actions/queue-if-busy.ts`

- export function queueKickoffIfSessionBusy({
  - `apps/desktop/src/app/session/hooks/use-prompt-actions/queue-if-busy.ts@-39 +41` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `apps/shared/src/gateway-contract.generated.ts`

- export interface BotRelayReplyParams {
  - `apps/shared/src/gateway-contract.generated.ts@-1973,0 +1974,37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `apps/shared/src/gateway-contract.generated.ts@-1973,0 +1974,37#moved` moved: `apps/desktop/e2e/core/webapp-gateway-attach.spec.ts`, `apps/desktop/src/app/types.ts`, `gateway/browser_control_broker.py`
- export interface PromptSubmitParams {
  - `apps/shared/src/gateway-contract.generated.ts@-2866,0 +2904` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface PromptSubmitResult {
  - `apps/shared/src/gateway-contract.generated.ts@-2890 +2930` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface SessionCorrectionParams {
  - `apps/shared/src/gateway-contract.generated.ts@-3727,0 +3768` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface CommandDispatchResult {
  - `apps/shared/src/gateway-contract.generated.ts@-3980,0 +4022,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface SlashExecResult {
  - `apps/shared/src/gateway-contract.generated.ts@-3999,0 +4043,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface BrowserManageParams {
  - `apps/shared/src/gateway-contract.generated.ts@-4153 +4199` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface BrowserManageResult {
  - `apps/shared/src/gateway-contract.generated.ts@-4157,0 +4204` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface SessionPruneParams {
  - `apps/shared/src/gateway-contract.generated.ts@-4840,0 +4888,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface CanonicalCorrectionParams {
  - `apps/shared/src/gateway-contract.generated.ts@-5369,0 +5419` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface CanonicalSubmitParams {
  - `apps/shared/src/gateway-contract.generated.ts@-5428,0 +5479` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface PaneRevealPayload {
  - `apps/shared/src/gateway-contract.generated.ts@-6274 +6325` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface BrowserProgressPayload {
  - `apps/shared/src/gateway-contract.generated.ts@-6292,0 +6344,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface RpcMethods {
  - `apps/shared/src/gateway-contract.generated.ts@-6380 +6445,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export const RPC_METHODS = [
  - `apps/shared/src/gateway-contract.generated.ts@-6892,0 +6966,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export interface BackendGatewayEventMap {
  - `apps/shared/src/gateway-contract.generated.ts@-7368,0 +7446,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- export const GATEWAY_EVENT_TYPES = [
  - `apps/shared/src/gateway-contract.generated.ts@-7523,0 +7605,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `contributors/emails/ghgoodreau@protonmail.com`

- (module)
  - `contributors/emails/ghgoodreau@protonmail.com@-0,0 +1,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `cron/scheduler.py`

- def _finalize_cron_session(session_db, agent, job_id: str, job_name: str, cron_s
  - `cron/scheduler.py@-2153,3 +2239,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `cron/scheduler.py@-2157 +2246` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def run_job(
  - `cron/scheduler.py@-2631,2 +2661,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `cron/scheduler.py@-2666,2 +2703,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `cron/scheduler.py@-2666,2 +2703,13#moved` moved: `agent/moa_loop.py`, `agent/primary_auth_fallback.py`, `gateway/platforms/api_server.py`, `gateway/run_config_loaders.py` (+33 more)

### `gateway/delivery_ledger.py`

- def flood_retry_delay(seconds: Any) -> float:
  - `gateway/delivery_ledger.py@-136,0 +168,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def retry_not_before(updated_at: Any, last_error: Any, attempts: Any) -> Optiona
  - `gateway/delivery_ledger.py@-158,0 +195,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _connect() -> sqlite3.Connection:
  - `gateway/delivery_ledger.py@-180 +218,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-180 +218,5#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+56 more)
  - `gateway/delivery_ledger.py@-182,4 +224,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _initialize_schema(conn: sqlite3.Connection) -> None:
  - `gateway/delivery_ledger.py@-204 +246,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-208,0 +256,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-208,0 +256,10#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2052 more)
- import time
  - `gateway/delivery_ledger.py@-21,0 +22#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_unavailable.py`, `agent/billing_usage.py` (+220 more)
- def compute_obligation_id(session_key: str, message_ref: str, content: str) -> s
  - `gateway/delivery_ledger.py@-269,0 +327,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-269,0 +327,21#moved` moved: `agent/account_usage.py`, `agent/account_usage_cache.py`, `agent/auxiliary_client.py`, `agent/codex_service_tiers.py` (+82 more)
- def record_obligation(*, obligation_id: str, session_key: str, platform: str, ch
  - `gateway/delivery_ledger.py@-271 +349,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-271 +349,4#moved` moved: `persistence/sqlite/gateway/_delivery_ledger.py`
  - `gateway/delivery_ledger.py@-279,2 +360,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-282 +365,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def record_crash_left_reply(*, obligation_id: str, session_key: str, platform: s
  - `gateway/delivery_ledger.py@-290 +374,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-293,2 +379,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-296,3 +384,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-301,2 +391,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-301,2 +391,10#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3121 more)
  - `gateway/delivery_ledger.py@-305,3 +403,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-305,3 +403,14#moved` moved: `agent/context_compressor.py`, `agent/curator.py`, `agent/repetition_guard.py`, `agent/verification_evidence.py` (+85 more)
- def mark_attempting(obligation_id: str) -> None:
  - `gateway/delivery_ledger.py@-314,2 +423,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-314,2 +423,9#moved` moved: `cron/scheduler_delivery_media.py`, `gateway/delivery_recovery.py`, `gateway/platforms/base.py`, `gateway/platforms/base_exec_approval.py` (+9 more)
- def mark_failed(obligation_id: str, error: str = "") -> None:
  - `gateway/delivery_ledger.py@-318,0 +435,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-318,0 +435,8#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3098 more)
- def _claimed_row(oid, session_key, platform, chat_id, thread_id, content, attemp
  - `gateway/delivery_ledger.py@-354 +478,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-356,10 +481,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-356,10 +481,14#moved` moved: `agent/display.py`, `agent/fallback_session_route.py`, `agent/plugin_live_notes.py`, `agent/session_persistence.py` (+88 more)
- _MAX_ROWS = 500
  - `gateway/delivery_ledger.py@-39,5 +40,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def sweep_recoverable(now: Optional[float] = None, *, deliverable_platforms: Opt
  - `gateway/delivery_ledger.py@-394 +523,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-396 +527,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-399 +531,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-425 +558,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-444,2 +582,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def sweep_failed_for_runtime(platform: str, now: Optional[float] = None, *,
  - `gateway/delivery_ledger.py@-470 +612,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-472 +617,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-474 +625,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-476 +633,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-481,0 +641,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-481,0 +641,14#moved` moved: `acp_adapter/server.py`, `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/agent_runtime_helpers_dead_connections.py` (+491 more)
  - `gateway/delivery_ledger.py@-489,0 +663` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/delivery_ledger.py@-504 +678,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _RETRY_BACKOFF_SECONDS = (30.0, 120.0)
  - `gateway/delivery_ledger.py@-59,0 +59,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- assert len(_RETRY_BACKOFF_SECONDS) == MAX_ATTEMPTS - 1
  - `gateway/delivery_ledger.py@-66,2 +69` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- FLOOD_RETRY_SLACK_SECONDS = 2.0
  - `gateway/delivery_ledger.py@-72,0 +75,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/lifecycle_ledger.py`

- def check_state_db_integrity(home: Optional[Path] = None, *, deadline_s: Optiona
  - `gateway/lifecycle_ledger.py@-268 +290,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run.py`

- def _hygiene_cooldown_for_failure(gateway, session_key: str, base_cooldown_secon
  - `gateway/run.py@-138,3 +141,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _profile_runtime_scope(
  - `gateway/run.py@-1800,2 +1870,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.run_busy import GatewayBusySessionMixin
  - `gateway/run.py@-2167 +2275,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_runtime_agent_kwargs() -> dict:
  - `gateway/run.py@-2262,34 +2374` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_hermes_bin() -> Optional[list[str]]:
  - `gateway/run.py@-2910,0 +3033,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayRunner(
  - `gateway/run.py@-3421,0 +3538,217` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run.py@-3741 +4072,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run.py@-3741 +4072,12#moved` moved: `agent/subagent_lifecycle.py`, `gateway/bot_loop_guard.py`, `hermes_cli/image_provenance.py`, `hermes_cli/sqlite_safe_read.py` (+4 more)
- def _start_gateway_housekeeping(
  - `gateway/run.py@-4412,7 +4818,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run.py@-4441 +4850,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run.py@-4455 +4869` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_busy.py`

- class GatewayBusySessionMixin:
  - `gateway/run_busy.py@-471 +396#moved` moved: `gateway/run_notifications_ownership.py`

### `gateway/run_notifications.py`

- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1002,532 +1829,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-1002,532 +1829,16#moved` moved: `gateway/run_notifications_completion.py`, `gateway/run_notifications_delegation.py`, `tests/gateway/test_attended_shared_busy_activity.py`, `tests/gateway/test_prompt_queued_transfer.py` (+1 more)
- def _mark_internal_notification(text: str) -> str:
  - `gateway/run_notifications.py@-110 +212` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _held_reason(source, platform: str, multiplexed: bool) -> str:
  - `gateway/run_notifications.py@-130,9 +232,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _raw_process_event_session_id(evt: dict) -> str:
  - `gateway/run_notifications.py@-141,33 +237` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import logging
  - `gateway/run_notifications.py@-15,0 +13#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+149 more)
- import time
  - `gateway/run_notifications.py@-16,0 +15,2#moved` moved: `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `acp_adapter/session.py` (+276 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1736 +1891,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-1745,0 +1903,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _watch_notification_metadata(evt: dict, source) -> dict[str, Any]:
  - `gateway/run_notifications.py@-176 +240,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1783,0 +1945` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-179,3 +253,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-1798,0 +1961,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from pathlib import Path
  - `gateway/run_notifications.py@-18 +18,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-18 +18,4#moved` moved: `acp_adapter/provenance.py`, `agent/anthropic_credentials.py`, `agent/api_error_summary.py`, `agent/api_request_hooks.py` (+86 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1801,2 +1965,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-1827,0 +1993,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-183,3 +261,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-187,7 +276,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-187,7 +276,12#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/auxiliary_health.py` (+272 more)
  - `gateway/run_notifications.py@-195,2 +289,125` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-195,2 +289,125#moved` moved: `acp_adapter/commands.py`, `agent/api_request_hooks.py`, `agent/aux_accounting.py`, `agent/azure_identity_adapter.py` (+654 more)
- (module)
  - `gateway/run_notifications.py@-2,4 +2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-219 +436,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-219 +436,5#moved` moved: `gateway/parked_route_refusal.py`, `gateway/run_inbound.py`, `gateway/run_shutdown.py`, `gateway/run_turn.py` (+5 more)
- from agent.retry_utils import jittered_backoff
  - `gateway/run_notifications.py@-22 +25,2#moved` moved: `cron/scheduler_delivery.py`, `cron/scheduler_delivery_sessions.py`, `evals/delivery_flood_wire.py`, `evals/postmortem/review_probes/goal_repaste_probe.py` (+485 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-226,3 +447,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-232 +454` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-242,0 +464,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-244,0 +470,2#moved` moved: `gateway/platforms/base.py`, `gateway/run_notifications_media.py`, `gateway/run_turn_persistence.py`
- from gateway.platforms.event import MessageEvent, MessageType
  - `gateway/run_notifications.py@-25 +29#moved` moved: `gateway/session_lifecycle.py`, `gateway/session_local_migration.py`, `gateway/session_local_recovery.py`, `gateway/session_persistence.py` (+26 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-268,5 +495,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-283,0 +509` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-288,2 +514,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-288,2 +514,6#moved` moved: `gateway/run_notifications_media.py`
- from gateway.run_notifications_update import GatewayUpdateNotificationsMixin
  - `gateway/run_notifications.py@-29 +33,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-296,2 +526,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-296,2 +526,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-298,0 +529#moved` moved: `gateway/run_notifications_media.py`
- from gateway.run_notifications_restart import GatewayRestartNoticeMixin
  - `gateway/run_notifications.py@-30,0 +38,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-30,0 +38,3#moved` moved: `tests/gateway/test_background_turn_fairness.py`
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-300 +531,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-300 +531,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-302 +534,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-302 +534,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-305,2 +538#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-310,0 +543` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-333,0 +567,4#moved` moved: `gateway/delivery_recovery.py`, `gateway/run_notifications_media.py`
- logger = logging.getLogger("gateway.run")
  - `gateway/run_notifications.py@-35,0 +46,78` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-35,0 +46,78#moved` moved: `agent/api_error_summary.py`, `agent/auxiliary_unavailable.py`, `agent/reasoning_summaries.py`, `apps/desktop/electron/window-below.ts` (+14 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-355,0 +593,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-356,0 +596#moved` moved: `gateway/run_notifications_copy.py`, `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-378,2 +618,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-385 +625,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-385 +625,12#moved` moved: `gateway/run_notifications_copy.py`, `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-389,2 +640,4#moved` moved: `gateway/run_notifications_media.py`, `gateway/run_turn_persistence.py`, `tests/gateway/test_copy_inline_quotes.py`, `tui_gateway/prompt_turn.py`
  - `gateway/run_notifications.py@-392,0 +646,7#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-397 +657` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-408,0 +669,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-408,0 +669,4#moved` moved: `gateway/run_busy.py`
  - `gateway/run_notifications.py@-410,2 +674` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-413,0 +677,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-419,0 +685,562` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-419,0 +685,562#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+3311 more)
  - `gateway/run_notifications.py@-472 +1299,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-540 +1368` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-587 +1415` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _COMPLETION_INJECTION_RETRY_MAX_DELAY = 60
  - `gateway/run_notifications.py@-73,3 +161,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-780,0 +1498,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-889,0 +1609,51` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-889,0 +1609,51#moved` moved: `agent/account_usage.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/credential_pool.py` (+119 more)
  - `gateway/run_notifications.py@-903 +1673,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-903 +1673,6#moved` moved: `gateway/run_notifications_completion.py`
  - `gateway/run_notifications.py@-910 +1685,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-910 +1685,2#moved` moved: `gateway/run_notifications_completion.py`
  - `gateway/run_notifications.py@-928,0 +1705,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- INTERNAL_NOTIFICATION_FOOTER = (
  - `gateway/run_notifications.py@-93,0 +173,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-93,0 +173,10#moved` moved: `tests/plugins/memory/test_hindsight_provider.py`
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-938 +1719,25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-950 +1755,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-950 +1755,9#moved` moved: `agent/secret_scope.py`, `gateway/session_cli_turn.py`
- INTERNAL_NOTIFICATION_FOOTER = (
  - `gateway/run_notifications.py@-96,0 +186,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-96,0 +186,13#moved` moved: `tests/plugins/memory/test_hindsight_provider.py`
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-967,0 +1781,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-988,2 +1815,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_notifications.py@-988,2 +1815,3#moved` moved: `hermes_cli/web_routers/_common.py`, `hermes_cli/web_server_sessions.py`, `tui_gateway/hosted_room_prepare_stall.py`
  - `gateway/run_notifications.py@-998,3 +1826,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_plugin_injection.py`

- class GatewayPluginInjectionMixin:
  - `gateway/run_plugin_injection.py@-309,0 +332,80#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+31 more)

### `gateway/run_runtime.py`

- async def _build_profile_authority(runner, name, home, *, register):
  - `gateway/run_runtime.py@-55 +55` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def open_profile_store(runner, home):
  - `gateway/run_runtime.py@-64,7 +64,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_runtime.py@-64,7 +64,4#moved` moved: `evals/delivery_flood_wire.py`, `gateway/session_dm_threads.py`, `gateway/session_snapshot.py`, `tests/acp_adapter/test_gateway_acp_session_end.py` (+167 more)
- async def recover_profile_native_sessions(runner, authority):
  - `gateway/run_runtime.py@-691,2 +752,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_runtime.py@-691,2 +752,4#moved` moved: `gateway/telegram_retire_work.py`
  - `gateway/run_runtime.py@-694 +757` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def open_profile_store(runner, home):
  - `gateway/run_runtime.py@-72,2 +69,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_runtime.py@-72,2 +69,8#moved` moved: `gateway/checkin_delivery.py`, `gateway/platforms/session_model.py`, `gateway/run_profile_reconcile.py`, `gateway/run_turn_runner.py` (+1 more)
- async def settle_gateway_runtime(runner, budget=None, reason=None):
  - `gateway/run_runtime.py@-819 +887,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_runtime.py@-822,3 +893,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_shutdown.py`

- class GatewayShutdownMixin(GatewaySessionEndMixin, GatewayHomeShutdownNoticeMixi
  - `gateway/run_shutdown.py@-1070 +1365,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1090,0 +1397,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1121,3 +1436,52` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1121,3 +1436,52#moved` moved: `gateway/cron_store_notices.py`, `gateway/handoff_target.py`, `gateway/kanban_watchers_notifier.py`, `gateway/platforms/webhook_ingress.py` (+22 more)
  - `gateway/run_shutdown.py@-1558,0 +2003,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1597,0 +2045,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1600 +2050,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1600 +2050,13#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4745 more)
  - `gateway/run_shutdown.py@-1604,3 +2066` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1608,4 +2068,120` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1608,4 +2068,120#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/auxiliary_client.py`, `agent/bedrock_adapter.py` (+547 more)
  - `gateway/run_shutdown.py@-1754,5 +2381,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1853 +2584,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-1908,7 +2787,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-2044,5 +2931,25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-217 +234,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_shutdown.py@-299,0 +322,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.run_shutdown_home_notice import (
  - `gateway/run_shutdown.py@-34,0 +40` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.run_shutdown_session_end import GatewaySessionEndMixin
  - `gateway/run_shutdown.py@-39 +45,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_startup.py`

- class GatewayStartupMixin:
  - `gateway/run_startup.py@-1620,0 +1902,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_startup.py@-555,7 +685,4#moved` moved: `gateway/run_restart_recovery.py`, `gateway/run_restored_delivery.py`
  - `gateway/run_startup.py@-563,49 +690,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_startup.py@-563,49 +690,11#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)
  - `gateway/run_startup.py@-613,36 +702,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_startup.py@-613,36 +702,19#moved` moved: `agent/context_compressor.py`, `agent/conversation_compression.py`, `agent/gemini_native_adapter.py`, `agent/reasoning_effort.py` (+56 more)
  - `gateway/run_startup.py@-650,21 +722,82` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_startup.py@-650,21 +722,82#moved` moved: `agent/conversation_compression.py`, `agent/verification_evidence.py`, `apps/desktop/electron/favicon.ts`, `apps/desktop/electron/git-binary-candidates.ts` (+16 more)

### `gateway/run_topics.py`

- class GatewayTopicThreadsMixin:
  - `gateway/run_topics.py@-150 +154,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-150 +154,21#moved` moved: `agent/coding_context.py`, `agent/curator.py`, `agent/prompt_builder.py`, `agent/trace_upload.py` (+37 more)
- from gateway.session import SessionSource
  - `gateway/run_topics.py@-22,0 +23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _TOPIC_SWITCH_BIND_LOCK = threading.Lock()
  - `gateway/run_topics.py@-35,0 +37,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayTopicThreadsMixin:
  - `gateway/run_topics.py@-373 +397,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-386,0 +413` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-388 +415,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-630,0 +666,127` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-630,0 +666,127#moved` moved: `acp_adapter/model_catalog.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/agent_runtime_helpers.py` (+388 more)
  - `gateway/run_topics.py@-652 +814,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-652 +814,9#moved` moved: `agent/auxiliary_unavailable.py`, `agent/background_review.py`, `agent/bounded_response.py`, `agent/chat_completion_helpers_cleanup.py` (+398 more)
  - `gateway/run_topics.py@-658 +828,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-661,0 +834,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-661,0 +834,23#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9734 more)
  - `gateway/run_topics.py@-669 +864,145` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-669 +864,145#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_unavailable.py`, `agent/background_review.py`, `agent/bounded_response.py` (+576 more)
  - `gateway/run_topics.py@-690 +1029#moved` moved: `gateway/run_notifications_ownership.py`
  - `gateway/run_topics.py@-697 +1036` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-704,0 +1044,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-704,0 +1044,22#moved` moved: `cron/scheduler_session_cleanup.py`
  - `gateway/run_topics.py@-708 +1069,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-708 +1069,6#moved` moved: `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/topic_creation.py`
  - `gateway/run_topics.py@-713,0 +1080,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-713,0 +1080,3#moved` moved: `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/topic_creation.py`
  - `gateway/run_topics.py@-715 +1084` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_topics.py@-717 +1086,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_turn_hygiene.py`

- class GatewayTurnHygieneMixin:
  - `gateway/run_turn_hygiene.py@-114 +223` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-120,0 +230` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-162,0 +273` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-178,4 +289,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-178,4 +289,6#moved` moved: `agent/activity_tracking.py`, `agent/context_compressor.py`, `hermes_cli/cli_process_notifications.py`, `run_agent.py`
  - `gateway/run_turn_hygiene.py@-198,13 +311,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-198,13 +311,16#moved` moved: `agent/activity_tracking.py`, `agent/context_compressor.py`, `hermes_cli/cli_process_notifications.py`, `run_agent.py`
  - `gateway/run_turn_hygiene.py@-314 +430,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-317 +434,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-327 +447` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-338 +458` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-341,3 +461,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-361,0 +486` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-363,0 +489` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-372,0 +499,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-379,4 +510,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-379,4 +510,21#moved` moved: `acp_adapter/auth.py`, `acp_adapter/events.py`, `acp_adapter/model_catalog.py`, `acp_adapter/session.py` (+487 more)
  - `gateway/run_turn_hygiene.py@-386,2 +534,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-389,0 +538` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- logger = logging.getLogger("gateway.run")
  - `gateway/run_turn_hygiene.py@-39,0 +40,36` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-39,0 +40,36#moved` moved: `agent/agent_runtime_helpers.py`, `agent/codex_runtime.py`, `agent/compression_facade.py`, `agent/context_references.py` (+28 more)
- class GatewayTurnHygieneMixin:
  - `gateway/run_turn_hygiene.py@-396,0 +546` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-402,2 +552,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-408 +558,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-417,3 +568,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-42,0 +79,71` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-42,0 +79,71#moved` moved: `agent/compact_context.py`, `agent/plugin_llm.py`, `apps/desktop/src/app/chat/hooks/use-composer-actions.ts`, `apps/desktop/src/lib/image-resize.ts` (+63 more)
  - `gateway/run_turn_hygiene.py@-426 +579` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-446 +599` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-490 +643,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-490 +643,3#moved` moved: `gateway/slash_commands_session.py`
  - `gateway/run_turn_hygiene.py@-492 +647` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-496,2 +651,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-55,0 +163,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-55,0 +163,2#moved` moved: `acp_adapter/session.py`, `gateway/run.py`, `gateway/run_config_loaders.py`, `gateway/run_inbound.py` (+7 more)
  - `gateway/run_turn_hygiene.py@-58 +167` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-60 +169` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-621 +778` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-65,2 +174,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_hygiene.py@-788 +955` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/run_turn_prepare.py`

- def _session_override_snapshot(override: dict) -> dict:
  - `gateway/run_turn_prepare.py@-168 +179,30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-168 +179,30#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2560 more)
- def _resolve_channel_runtime(runner, source, model, runtime_kwargs) -> tuple[str
  - `gateway/run_turn_prepare.py@-171,2 +211,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-174 +218#moved` moved: `gateway/session_info_route.py`
  - `gateway/run_turn_prepare.py@-189,0 +234,141` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-189,0 +234,141#moved` moved: `acp_adapter/auth.py`, `acp_adapter/session.py`, `agent/account_usage.py`, `agent/agent_init_compression_notice.py` (+120 more)
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-193 +378` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-197,10 +382,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-197,10 +382,5#moved` moved: `gateway/run_config_loaders.py`, `gateway/run_inbound.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_attended_turn_runner.py`
  - `gateway/run_turn_prepare.py@-217 +392,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-217 +392,4#moved` moved: `gateway/session_llm_oneshot.py`, `gateway/session_tui_side.py`, `tests/gateway/test_config_policy_recovery.py`, `tests/gateway/test_config_secret_scope.py` (+7 more)
  - `gateway/run_turn_prepare.py@-287,21 +398,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-287,21 +398,19#moved` moved: `gateway/run_turn/__init__.py`
  - `gateway/run_turn_prepare.py@-312,3 +421` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-324,0 +432,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-328,0 +438` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-361,0 +472` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-363,7 +474,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-390 +496,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-400,0 +510,2#moved` moved: `evals/gateway_status_render/iteration_ceiling_ab.py`, `gateway/run_busy.py`, `gateway/run_checkins.py`, `gateway/run_plugin_rewire.py` (+39 more)
  - `gateway/run_turn_prepare.py@-405,4 +516` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-432 +540,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _TIER_FROM_RUNNER = object()
  - `gateway/run_turn_prepare.py@-44 +45` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-466,0 +560,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-469,0 +565,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-469,0 +565,22#moved` moved: `gateway/run_busy.py`, `gateway/run_notifications_ownership.py`, `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py` (+5 more)
  - `gateway/run_turn_prepare.py@-472,0 +590,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-472,0 +590,2#moved` moved: `tests/gateway/test_busy_command.py`
  - `gateway/run_turn_prepare.py@-474,0 +594,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-474,0 +594,19#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+2050 more)
- def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, record_notic
  - `gateway/run_turn_prepare.py@-52 +53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-563 +701` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-568 +706,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-568 +706,12#moved` moved: `gateway/run_config_loaders.py`, `gateway/run_inbound.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_attended_turn_runner.py`
  - `gateway/run_turn_prepare.py@-573,0 +723,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-573,0 +723,2#moved` moved: `gateway/run_turn/__init__.py`
- def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, record_notic
  - `gateway/run_turn_prepare.py@-59,0 +61` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-649,2 +800,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-660,2 +828,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, record_notic
  - `gateway/run_turn_prepare.py@-68,4 +68,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-68,4 +68,15#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_busy.py`, `gateway/run_notifications.py`, `gateway/run_notifications_ownership.py` (+14 more)
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-702,4 +867,2#moved` moved: `agent/azure_identity_adapter.py`, `agent/bedrock_adapter.py`, `agent/outbound_webhooks.py`, `agent/route_binding.py` (+68 more)
  - `gateway/run_turn_prepare.py@-707,5 +870` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-773,0 +933,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-779,2 +940` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-784 +944` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-787,0 +948,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-787,0 +948,3#moved` moved: `gateway/reply_latency.py`, `gateway/run_background_admission.py`, `gateway/run_busy.py`, `gateway/run_goals.py` (+7 more)
  - `gateway/run_turn_prepare.py@-840,2 +995,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-862,0 +1029,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/run_turn_prepare.py@-862,0 +1029,9#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_busy.py`, `gateway/run_notifications.py`, `gateway/run_notifications_ownership.py` (+13 more)
  - `gateway/run_turn_prepare.py@-884,0 +1060` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session.py`

- class SessionStore(
  - `gateway/session.py@-1173 +1378,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1173 +1378,3#moved` moved: `hermes_cli/kanban_db.py`
  - `gateway/session.py@-1177,0 +1385` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1181 +1389,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1181 +1389,8#moved` moved: `gateway/slash_commands_incognito.py`
  - `gateway/session.py@-1187 +1402,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1187 +1402,4#moved` moved: `agent/conversation_loop.py`, `tests/gateway/test_local_viewer_events_turn.py`, `tests/monitoring/test_cron_health_export.py`
  - `gateway/session.py@-1189,0 +1408` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1191,3 +1410,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1205,0 +1424,103` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1205,0 +1424,103#moved` moved: `agent/agent_init.py`, `agent/agent_init_compression_notice.py`, `agent/agent_init_ollama.py`, `agent/agent_runtime_helpers.py` (+466 more)
  - `gateway/session.py@-1207,0 +1529,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1207,0 +1529,6#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+62 more)
  - `gateway/session.py@-1296,0 +1624,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1355 +1695` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1364 +1704,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1371,4 +1712,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1378 +1719` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1380,21 +1721,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from typing import Dict, List, Optional, Any
  - `gateway/session.py@-14,0 +16,2#moved` moved: `tests/gateway/test_session_store_lock_p1.py`
- class SessionStore(
  - `gateway/session.py@-1403,4 +1739,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1408,6 +1742,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-1417,2 +1747` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def build_session_context(
  - `gateway/session.py@-1478 +1807,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.session_identity import transport_profile_of
  - `gateway/session.py@-18,0 +22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.session_recovery import SessionRecoveryMixin
  - `gateway/session.py@-22 +26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- logger = logging.getLogger(__name__)
  - `gateway/session.py@-27,0 +31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def sanitize_model_override(override: Optional[dict[str, Any]]) -> Optional[dict
  - `gateway/session.py@-491,0 +496,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-491,0 +496,4#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_session_readback.py`, `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_attended.py`
  - `gateway/session.py@-494,0 +503,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-494,0 +503,9#moved` moved: `agent/auxiliary_client.py`, `agent/moa_loop.py`, `agent/voice_turn_route.py`, `cron/jobs.py` (+30 more)
- class SessionEntry:
  - `gateway/session.py@-538,0 +556,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-544 +574,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-546,0 +581,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-555,3 +591,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-558,0 +594,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-565 +626,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-572,0 +638,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-572,0 +638,3#moved` moved: `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py`, `gateway/session_automation.py` (+75 more)
  - `gateway/session.py@-575 +643` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-581 +649` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-582,0 +651,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-582,0 +651,10#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/retry/action.yml`, `.github/known-flakes.json`, `.prettierrc` (+1899 more)
  - `gateway/session.py@-585,0 +664,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-589,0 +683,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-626,3 +725,25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-626,3 +725,25#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/retry/action.yml`, `.github/known-flakes.json`, `.prettierrc` (+1974 more)
  - `gateway/session.py@-629,0 +751,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-633 +761` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-635,0 +764` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-637,0 +767` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-638,0 +769,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class AsyncSessionStore:
  - `gateway/session.py@-825,0 +973,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionStore(
  - `gateway/session.py@-827,2 +977,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-840 +990,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-846,0 +999,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-868,13 +1023,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import threading
  - `gateway/session.py@-9,0 +10#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/permissions.py`, `agent/i18n_layers.py`, `tests/cron/test_missed_execution_retention.py` (+1 more)
- class SessionStore(
  - `gateway/session.py@-904 +1049,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-929,0 +1077,55` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-929,0 +1077,55#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2060 more)
  - `gateway/session.py@-931,2 +1133` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session.py@-997,0 +1199,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_acp_lifecycle.py`

- async def reopen_if_reattached(authority, session_id, ended):
  - `gateway/session_acp_lifecycle.py@-67,2 +69,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_acp_lifecycle.py@-67,2 +69,4#moved` moved: `acp_adapter/entry.py`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py` (+691 more)
- A later ``session/load`` + prompt reopens the row (``session_local_recovery.reop
  - `gateway/session_acp_lifecycle.py@-7,0 +8,2#moved` moved: `agent/account_usage_nous.py`, `agent/message_sanitization.py`, `agent/relay_llm.py`, `agent/runtime_session_child.py` (+113 more)
- def end_idle_acp_sessions(authority):
  - `gateway/session_acp_lifecycle.py@-72 +76,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_acp_lifecycle.py@-72 +76,23#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+5425 more)
- import logging
  - `gateway/session_acp_lifecycle.py@-8,0 +11#moved` moved: `tests/agent/test_incognito.py`, `tests/agent/test_incognito_auxiliary_health.py`, `tests/agent/test_incognito_title_completion.py`, `tests/agent/test_skill_utils.py` (+6 more)

### `gateway/session_api_turn.py`

- def admit_api_steps(adapter, **kwargs):
  - `gateway/session_api_turn.py@-174,0 +216,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _admit_api_payload(authority, adapter, sid, request_id, payload, settings, d
  - `gateway/session_api_turn.py@-227 +273` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def prepare_api_execution(authority, ref, payload):
  - `gateway/session_api_turn.py@-450 +496,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def check_api_turn(authority, ref, payload):
  - `gateway/session_api_turn.py@-49,2 +49,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_api_turn.py@-51,0 +54,3#moved` moved: `tests/gateway/test_local_automation_reauthorization.py`
- def api_runtime_locked():
  - `gateway/session_api_turn.py@-555,0 +603,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_api_turn.py@-555,0 +603,14#moved` moved: `agent/lsp/install.py`, `agent/secret_sources/bitwarden.py`, `agent/secret_sources/onepassword.py`, `evals/desktop_bug_campaign/persistence_live.py` (+75 more)
- def prepare_api_runtime(model, runtime_kwargs):
  - `gateway/session_api_turn.py@-569,0 +631,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def check_api_turn(authority, ref, payload):
  - `gateway/session_api_turn.py@-65 +68,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _valid_owner_scope(value):
  - `gateway/session_api_turn.py@-77,0 +82,33` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_api_turn.py@-77,0 +82,33#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2523 more)
- def api_session_vars():
  - `gateway/session_api_turn.py@-80 +117,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_api_turn.py@-84 +122,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_authority.py`

- class SessionAuthority:
  - `gateway/session_authority.py@-1094,0 +1151,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1094,0 +1151,9#moved` moved: `hermes_state_local.py`, `hermes_state_local_lineage.py`, `hermes_state_runtime.py`
  - `gateway/session_authority.py@-1128,2 +1194,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1171 +1241,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1177,2 +1251,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1202 +1277,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1202 +1277,3#moved` moved: `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/session_authority.py@-1258,2 +1335,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def backfill_compression_created_sources(db):
  - `gateway/session_authority.py@-1467 +1559` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def initialize_session_authority(runner, *, profile_id, instance_id, db=No
  - `gateway/session_authority.py@-1475,2 +1572,47` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1475,2 +1572,47#moved` moved: `acp_adapter/entry.py`, `acp_adapter/gateway_server.py`, `acp_adapter/session.py`, `agent/agent_init.py` (+805 more)
  - `gateway/session_authority.py@-1477,0 +1620#moved` moved: `tests/hermes_state/test_bot_chat_archive_retire.py`
  - `gateway/session_authority.py@-1479 +1622,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1490,2 +1639,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1495,0 +1645,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-1495,0 +1645,11#moved` moved: `cron/detached_authority.py`, `cron/executions.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py` (+38 more)
  - `gateway/session_authority.py@-1507 +1662` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionAuthority:
  - `gateway/session_authority.py@-247 +247` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-255 +255,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-255 +255,3#moved` moved: `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/session_authority.py@-258 +260` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-262 +264` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-527 +536,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-529 +539,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_authority.py@-730 +779` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_automation.py`

- async def _admit_folded_completion(authority, adapter, event, evt, identity):
  - `gateway/session_automation.py@-115 +142` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-120 +147` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def prepare_heartbeat(runner, event):
  - `gateway/session_automation.py@-188,0 +216#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+1 more)
  - `gateway/session_automation.py@-190 +218` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-193 +221,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def producer_identity(runner, event):
  - `gateway/session_automation.py@-23 +24,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _fold_queued(admission_id, session_id, replaced, skip=()):
  - `gateway/session_automation.py@-254 +283,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def withdraw_process_ticks(runner, event):
  - `gateway/session_automation.py@-274,0 +307#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+1 more)
  - `gateway/session_automation.py@-276 +309` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-282,2 +315,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _owner(runner, event, authority):
  - `gateway/session_automation.py@-386,2 +419,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-397,0 +446,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-397,0 +446,13#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+157 more)
- def automation_display_metadata(metadata):
  - `gateway/session_automation.py@-420,0 +482,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _AUTOMATION_METADATA = frozenset({'gateway_session_key', 'gateway_session_id', '
  - `gateway/session_automation.py@-432 +506` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def admission_display(payload):
  - `gateway/session_automation.py@-449,0 +524,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def wake_route_envelope(authority, entry, requester_user_id=None):
  - `gateway/session_automation.py@-545 +634` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def snapshot_automation(authority, adapter, event, identity):
  - `gateway/session_automation.py@-547 +636,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-547 +636,3#moved` moved: `gateway/session_refusals.py`, `gateway/shutdown_flush.py`, `tests/gateway/test_goal_wait_durable_admission.py`
  - `gateway/session_automation.py@-549,3 +640,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-549,3 +640,8#moved` moved: `gateway/shutdown_goal_wakes.py`
  - `gateway/session_automation.py@-553,2 +649,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-557 +654,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-559,13 +657,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-559,13 +657,22#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3467 more)
  - `gateway/session_automation.py@-587 +694,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-588,0 +697,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-588,0 +697,4#moved` moved: `tui_gateway/agent_factory.py`
  - `gateway/session_automation.py@-597 +709` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def check_automation_route(runner, payload, session_id, available_source, adapte
  - `gateway/session_automation.py@-614 +725` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _local_automation_owner(authority, adapter, event, entry):
  - `gateway/session_automation.py@-624,2 +735,3#moved` moved: `agent/vault_store.py`, `gateway/platforms/api_server_bot_chat.py`, `gateway/session_bot.py`, `gateway/session_policy.py` (+4 more)
  - `gateway/session_automation.py@-633,2 +745,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-633,2 +745,26#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+5 more)
- def snapshot_local_automation(authority, adapter, event, identity, entry):
  - `gateway/session_automation.py@-638,0 +775,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-652,0 +786,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-652,0 +786,5#moved` moved: `gateway/shutdown_goal_wakes.py`
- def restore_local_automation(authority, ref, row):
  - `gateway/session_automation.py@-690,0 +829,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-694,0 +835,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-698,0 +841,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-702,17 +847,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def admit_automation(authority, adapter, event, identity, replaces=None, f
  - `gateway/session_automation.py@-721,13 +856,48` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-721,13 +856,48#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3468 more)
- def route_owner_lock(runner, session_id):
  - `gateway/session_automation.py@-749,0 +920,25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-749,0 +920,25#moved` moved: `gateway/platforms/api_server_bot_chat.py`, `gateway/run_goal_wait.py`, `gateway/session_authority.py`, `gateway/session_bot.py` (+15 more)
- async def move_route_wakes(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-781,0 +977,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def _reset_and_retarget_local(authority, runner, session_key, old_session_
  - `gateway/session_automation.py@-826,0 +1024,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def reset_and_rehome_messages(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-849 +1052,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def admit_process_notice(runner, authority, adapter, event, evt):
  - `gateway/session_automation.py@-88,0 +92,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-88,0 +92,21#moved` moved: `gateway/cron_review.py`, `gateway/platforms/base.py`, `gateway/platforms/webhook_ingress.py`, `gateway/plugin_messages.py` (+27 more)
- import logging
  - `gateway/session_automation.py@-9,0 +10#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+4247 more)
- async def reset_and_rehome_messages(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-918,44 +1122,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-918,44 +1122,5#moved` moved: `gateway/run_goal_wait.py`, `tests/gateway/test_goal_wait_durable_admission.py`
- async def admit_process_notice(runner, authority, adapter, event, evt):
  - `gateway/session_automation.py@-94 +118` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-96 +120` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-98 +122,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_automation.py@-98 +122,4#moved` moved: `cron/scheduler_delivery.py`, `gateway/platforms/webhook.py`, `gateway/run_goal_wait.py`, `gateway/session_api.py` (+14 more)

### `gateway/session_controls.py`

- class AuthorityConnection:
  - `gateway/session_controls.py@-638,0 +639,11#moved` moved: `hermes_state_local.py`, `hermes_state_mutation_chat_pick.py`, `hermes_state_runtime.py`, `tests/gateway/test_goal_wait_durable_admission.py`
  - `gateway/session_controls.py@-673 +684,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_cron.py`

- CRON_SESSION_READ_ONLY_SURFACE = {'layer': 'gateway', 'code': 'cron_session_read
  - `gateway/session_cron.py@-157,2 +166,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def operation(authority, name, params, actor=None):
  - `gateway/session_cron.py@-186,0 +194,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-208 +216` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _owners = {}
  - `gateway/session_cron.py@-21,0 +22,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def operation(authority, name, params, actor=None):
  - `gateway/session_cron.py@-217,0 +226,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-217,0 +226,5#moved` moved: `gateway/session_ancillary.py`, `gateway/session_complete_path.py`, `gateway/session_connections.py`, `gateway/session_ingress.py` (+3 more)
  - `gateway/session_cron.py@-228 +241` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-231,0 +245,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-231,0 +245,2#moved` moved: `cron/scheduler_control.py`
  - `gateway/session_cron.py@-232,0 +248,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-232,0 +248,9#moved` moved: `hermes_state_local.py`, `hermes_state_mutation_chat_pick.py`, `hermes_state_runtime.py`, `tests/cron/test_canonical_detached_survival.py` (+1 more)
  - `gateway/session_cron.py@-240,0 +265` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def execute(authority, ref, row, policy):
  - `gateway/session_cron.py@-314,4 +339,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_cron.py@-314,4 +339,10#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3466 more)
- async def _admit_local_delivery(authority, chat_id, text, metadata):
  - `gateway/session_cron.py@-54 +63` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_detached.py`

- async def follow_detached_pin(runner, evt):
  - `gateway/session_detached.py@-239 +253` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_envelope.py`

- def _validate_native(runner, event, provenance=None, fresh_roles=False):
  - `gateway/session_envelope.py@-149,0 +233,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-149,0 +233,15#moved` moved: `gateway/run_goals.py`, `gateway/run_pending_recovery.py`, `gateway/session_refusals.py`, `gateway/session_synthetic.py` (+4 more)
- async def prepare_native(runner, event):
  - `gateway/session_envelope.py@-231 +329` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-234 +332` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _snapshot_native(runner, event, provenance, fresh_roles=False, encoded_sourc
  - `gateway/session_envelope.py@-240,0 +339,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-240,0 +339,9#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9661 more)
  - `gateway/session_envelope.py@-259 +366,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-259 +366,4#moved` moved: `gateway/shutdown_goal_wakes.py`
  - `gateway/session_envelope.py@-260,0 +371,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-272 +384,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-275,11 +390,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-275,11 +390,16#moved` moved: `agent/account_usage.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/credential_pool.py` (+117 more)
  - `gateway/session_envelope.py@-288,0 +409,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-288,0 +409,20#moved` moved: `tests/gateway/test_session_store_lock_p1.py`
- _CONTEXT_FIELDS = ('auto_skill', 'channel_prompt', 'channel_context', 'reply_exp
  - `gateway/session_envelope.py@-30,2 +30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def restore_native_source(payload, runner=None):
  - `gateway/session_envelope.py@-301 +441,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-301 +441,15#moved` moved: `gateway/authz_mixin.py`
- def restore_native(payload, runner=None):
  - `gateway/session_envelope.py@-316,0 +471,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-318,0 +477,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-318,0 +477,2#moved` moved: `gateway/run_pending_recovery.py`, `tests/gateway/test_shutdown_followup_restart.py`
  - `gateway/session_envelope.py@-327,0 +488,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-327,0 +488,10#moved` moved: `tests/gateway/test_supervisor_checkins.py`
- async def check_native_route(runner, payload, session_id, available_source, adap
  - `gateway/session_envelope.py@-334,2 +504,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-347 +517,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-347 +517,2#moved` moved: `gateway/run_agent_cache.py`, `gateway/turn_session_titles.py`
- _WHATSAPP_NATIVE_MAX_BYTES = 16 * 1024
  - `gateway/session_envelope.py@-36,0 +36` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _MEDIA_FIELDS = ('media_urls', 'media_types', 'media_text_inlined', 'media_filen
  - `gateway/session_envelope.py@-40,2 +40,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _native_metadata(event):
  - `gateway/session_envelope.py@-64,0 +59,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-64,0 +59,53#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9665 more)
- def _adapter_event(event, **changes):
  - `gateway/session_envelope.py@-69 +116,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_envelope.py@-69 +116,35#moved` moved: `tests/contracts/conftest.py`, `tests/gateway/test_telegram_rich_message_admission.py`
- def _refuse(event, reason, clause):
  - `gateway/session_envelope.py@-93 +174,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_ingress.py`

- def row_turn_author(policy, row):
  - `gateway/session_ingress.py@-296,0 +419,45` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_ingress_context.py`

- (module)
  - `gateway/session_ingress_context.py@-1,0 +2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2202 more)
- from hermes_state_runtime import RuntimeStoreError
  - `gateway/session_ingress_context.py@-12,0 +15,2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+929 more)
- def _runtime_profile(runner, source, profile, owner_name, homes, relay):
  - `gateway/session_ingress_context.py@-132 +139` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _binding(runner, source, profile):
  - `gateway/session_ingress_context.py@-133,0 +141,44` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-133,0 +141,44#moved` moved: `acp_adapter/server.py`, `agent/auxiliary_client.py`, `agent/deadline.py`, `gateway/control_socket.py` (+159 more)
  - `gateway/session_ingress_context.py@-137,2 +188,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-141 +193` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-189,0 +242` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-191 +244` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def capture_provenance(runner, event):
  - `gateway/session_ingress_context.py@-214,0 +268,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-214,0 +268,15#moved` moved: `agent/memory_caller_scope.py`, `cron/bot_chat_legacy.py`, `cron/scheduler_control.py`, `cron/scheduler_delivery.py` (+29 more)
- def restore_provenance(runner, source, provenance):
  - `gateway/session_ingress_context.py@-226,0 +295,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-228 +301,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-228 +301,18#moved` moved: `gateway/session_automation_legacy.py`, `gateway/session_worker_discovery.py`
- import json
  - `gateway/session_ingress_context.py@-9,0 +11#moved` moved: `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py`, `acp_adapter/entry.py` (+2018 more)
- def carry_callback(event, rewritten):
  - `gateway/session_ingress_context.py@-95,0 +100,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_context.py@-95,0 +100,3#moved` moved: `tests/gateway/test_telegram_rich_message_admission.py`

### `gateway/session_ingress_media.py`

- def ingress_fingerprint(payload, raw=None):
  - `gateway/session_ingress_media.py@-192,0 +195,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError
  - `gateway/session_ingress_media.py@-23,0 +24,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-23,0 +24,2#moved` moved: `hermes_state_media.py`
- def _held_media_paths(conn):
  - `gateway/session_ingress_media.py@-434,2 +438` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-437,0 +441#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _held_file_identities(paths, root):
  - `gateway/session_ingress_media.py@-459,0 +464#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def release_admission_media(db, admission_id):
  - `gateway/session_ingress_media.py@-493,2 +498,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-497,0 +504,43` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def collect_unheld_api_images(db):
  - `gateway/session_ingress_media.py@-556,4 +605,33` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-556,4 +605,33#moved` moved: `agent/lsp/deferred.py`, `hermes_cli/plugins_incognito.py`, `hermes_cli/shared_session_attach.py`, `hermes_cli/update_host_obligation.py` (+6 more)
- def _history_mentions(conn, references, after_id=0):
  - `gateway/session_ingress_media.py@-564,0 +643#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
  - `gateway/session_ingress_media.py@-571,2 +650,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-571,2 +650,13#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/context_compressor.py` (+108 more)
  - `gateway/session_ingress_media.py@-574 +664,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-574 +664,2#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _collectable(references, root):
  - `gateway/session_ingress_media.py@-581,2 +672,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-581,2 +672,7#moved` moved: `hermes_cli/mcp_catalog.py`, `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py` (+3 more)
  - `gateway/session_ingress_media.py@-585 +681` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def release_unheld_media(db, references, *, retain_history=True):
  - `gateway/session_ingress_media.py@-591 +687,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-598 +697#moved` moved: `hermes_state_media.py`
  - `gateway/session_ingress_media.py@-602 +701,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_ingress_media.py@-617,0 +718#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`

### `gateway/session_local_migration.py`

- def legacy_launch_params(row):
  - `gateway/session_local_migration.py@-85,0 +86,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_migration.py@-85,0 +86,2#moved` moved: `gateway/platforms/api_server.py`

### `gateway/session_local_recovery.py`

- def _stray_route_target(authority, receipt, session_id):
  - `gateway/session_local_recovery.py@-173,3 +178` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def restore_local_session(authority, sid):
  - `gateway/session_local_recovery.py@-196,0 +198,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_recovery.py@-202,3 +211,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_recovery.py@-202,3 +211,17#moved` moved: `gateway/run_goal_wait.py`, `gateway/run_inbound.py`, `gateway/run_pending_recovery.py`, `gateway/session_ancillary.py` (+34 more)
  - `gateway/session_local_recovery.py@-241 +248` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _route_already_restored(store, route, target, lineage):
  - `gateway/session_local_recovery.py@-257,0 +261,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_recovery.py@-257,0 +261,53#moved` moved: `gateway/session_api.py`
- def restore_completion_route(runner, session_key):
  - `gateway/session_local_recovery.py@-440,0 +504,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def release_handed_off_receipts(db, *, epoch):
  - `gateway/session_local_recovery.py@-453 +528,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_recovery.py@-457,0 +536,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_local_recovery.py@-457,0 +536,9#moved` moved: `agent/context_compressor.py`, `agent/gemini_native_adapter.py`, `agent/reasoning_effort.py`, `agent/vault_login_classifier.py` (+77 more)
  - `gateway/session_local_recovery.py@-460,3 +547` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _refused_check(exc):
  - `gateway/session_local_recovery.py@-64,0 +66,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_persistence.py`

- import threading
  - `gateway/session_persistence.py@-10,0 +11#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2575 more)
- class SessionPersistenceMixin:
  - `gateway/session_persistence.py@-112 +119,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-152 +160,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-212 +222,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-224 +236,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _SESSIONS_JSON_README = (
  - `gateway/session_persistence.py@-34,0 +36,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionPersistenceMixin:
  - `gateway/session_persistence.py@-358,0 +373` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-467,0 +483,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-600 +617,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-625,0 +644,38` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-625,0 +644,38#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/auxiliary_client.py` (+293 more)
  - `gateway/session_persistence.py@-650 +706,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-650 +706,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)
  - `gateway/session_persistence.py@-666,0 +725,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_persistence.py@-666,0 +725,5#moved` moved: `tools/skill_manager_tool.py`
  - `gateway/session_persistence.py@-90 +97` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_picker_fields.py`

- def fill_picker_fields(db, items):
  - `gateway/session_picker_fields.py@-40 +40` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_prune.py`

- async def prune(connection, ref, params):
  - `gateway/session_prune.py@-37,0 +42,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_prune.py@-37,0 +42,11#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)
  - `gateway/session_prune.py@-40 +55,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_prune.py@-44 +64,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_prune.py@-44 +64,11#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)
- async def _apply(authority, filters, never_active):
  - `gateway/session_prune.py@-51,3 +81,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_prune.py@-58 +92` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- the store under a live gateway would be a second writer, so it never does.
  - `gateway/session_prune.py@-6,0 +7,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_queued_controls.py`

- async def replace_queued(connection, ref, params):
  - `gateway/session_queued_controls.py@-238,2 +238,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_queued_controls.py@-238,2 +238,2#moved` moved: `gateway/session_authority.py`

### `gateway/session_results.py`

- from hermes_state_terminal import RESULT_PREFIX as _RESULT_PREFIX, load_result
  - `gateway/session_results.py@-9,0 +10,25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_results.py@-9,0 +10,25#moved` moved: `cron/detached_authority.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py`, `gateway/session_pause_announce.py` (+47 more)

### `gateway/session_retry_admission.py`

- def unbind(token) -> None:
  - `gateway/session_retry_admission.py@-58,0 +59,8#moved` moved: `tests/gateway/test_local_automation_reauthorization.py`

### `gateway/session_settlement_recovery.py`

- def commit_and_publish(authority, live, ref, row, response, outcome, captured, s
  - `gateway/session_settlement_recovery.py@-180,0 +224` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def settle_with_retry(authority, live, ref, row, response, outcome, captur
  - `gateway/session_settlement_recovery.py@-215,0 +260,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def settle_and_deliver(authority, live, ref, row, response, outcome):
  - `gateway/session_settlement_recovery.py@-322,0 +369,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_settlement_recovery.py@-322,0 +369,3#moved` moved: `gateway/run_watchers.py`
- def release_media_after_delivery(authority, row):
  - `gateway/session_settlement_recovery.py@-348,19 +397,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_settlement_recovery.py@-348,19 +397,3#moved` moved: `gateway/session_authority.py`
- async def recover_failed_settlement(authority, row):
  - `gateway/session_settlement_recovery.py@-53,0 +54,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_transcript.py`

- class SessionTranscriptMixin:
  - `gateway/session_transcript.py@-164,0 +173,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-164,0 +173,6#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
  - `gateway/session_transcript.py@-450,0 +465,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-450,0 +465,4#moved` moved: `gateway/run_startup.py`
  - `gateway/session_transcript.py@-451,0 +470` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-508 +527` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-510,0 +530,32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-510,0 +530,32#moved` moved: `acp_adapter/model_catalog.py`, `acp_adapter/session.py`, `agent/acp_client.py`, `agent/agent_init.py` (+264 more)
  - `gateway/session_transcript.py@-526,0 +578,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-526,0 +578,11#moved` moved: `agent/prompt_refresh.py`, `hermes_cli/browser_connect.py`, `hermes_state_messages.py`
  - `gateway/session_transcript.py@-539,0 +602,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-548,0 +627,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-548,0 +627,5#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
  - `gateway/session_transcript.py@-631,0 +715,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-631,0 +715,7#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
- def transcript_append_kwargs(session_id: str, message: dict[str, Any], *, fallba
  - `gateway/session_transcript.py@-90,0 +91,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_transcript.py@-90,0 +91,8#moved` moved: `gateway/outbound_reply.py`, `hermes_state_coverage.py`, `session_rules/turns.py`

### `gateway/session_unattended_unknown.py`

- logger = logging.getLogger(__name__)
  - `gateway/session_unattended_unknown.py@-22,0 +25,32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-22,0 +25,32#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py` (+12709 more)
- def is_unattended(row) -> bool:
  - `gateway/session_unattended_unknown.py@-29,0 +62,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-29,0 +62,8#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/gateway_server.py`, `acp_adapter/permissions.py` (+2056 more)
  - `gateway/session_unattended_unknown.py@-34 +74,74` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-34 +74,74#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+5983 more)
- def settle_recovered_unattended(db, epoch) -> int:
  - `gateway/session_unattended_unknown.py@-52,0 +165,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-53,0 +172` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-56 +175` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- a cron run (``CRON_PRINCIPAL_ID``), a process completion or heartbeat (``automat
  - `gateway/session_unattended_unknown.py@-7,2 +7,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def settle_unattended_unknown(authority, ref) -> bool:
  - `gateway/session_unattended_unknown.py@-77,0 +196,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_unattended_unknown.py@-80 +204` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/session_worker.py`

- def _claim(connection, ref, params):
  - `gateway/session_worker.py@-25,0 +27,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- (module)
  - `gateway/session_worker.py@-3,2 +3,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _verify(connection, ref, params, claim):
  - `gateway/session_worker.py@-50,0 +59,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_worker.py@-62,0 +77,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_worker.py@-62,0 +77,2#moved` moved: `cron/detached_authority.py`, `gateway/session_teammate_waits.py`, `hermes_state_runtime.py`
- async def worker_request(connection, ref, params, *, operation):
  - `gateway/session_worker.py@-84,0 +100,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_worker.py@-84,0 +100,7#moved` moved: `cron/detached_authority.py`, `gateway/session_teammate_waits.py`, `hermes_state_runtime.py`

### `gateway/session_worker_reservation.py`

- def reserve_admission_worker(authority, *, admission_id, process, principal_id,
  - `gateway/session_worker_reservation.py@-110 +110,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/session_worker_reservation.py@-110 +110,3#moved` moved: `hermes_state_runtime.py`

### `gateway/shutdown_flush.py`

- logger = logging.getLogger(__name__)
  - `gateway/shutdown_flush.py@-27,0 +30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _remove_replayed(path: Path, session_id: str) -> None:
  - `gateway/shutdown_flush.py@-278,0 +382,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _order_flush_files(paths) -> list[Path]:
  - `gateway/shutdown_flush.py@-317 +444,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def recover_pending_to_db(session_db=None, *, session_resolver=None) -> int:
  - `gateway/shutdown_flush.py@-320 +459,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/shutdown_flush.py@-323 +463` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def recover_pending_spool(session_db=None, *, session_resolver=None) -> tuple[in
  - `gateway/shutdown_flush.py@-331,3 +471,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/shutdown_flush.py@-338,0 +480,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/shutdown_flush.py@-359,3 +509,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/shutdown_flush.py@-368 +524` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _recover_one_payload(session_db, path: Path, payload: dict[str, Any], *,
  - `gateway/shutdown_flush.py@-439 +695` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- All hooks write atomic JSON payloads under ``<hermes_home>/pending_messages/``:
  - `gateway/shutdown_flush.py@-7,2 +7,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/slash_commands.py`

- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1011 +1097` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1047 +1133` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1093 +1179` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _restart_notify_payload(event: MessageEvent) -> dict:
  - `gateway/slash_commands.py@-128,0 +134,27` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-128,0 +134,27#moved` moved: `hermes_cli/gateway.py`, `hermes_cli/tools_config_cua.py`, `optional-skills/software-development/ast-grep/scripts/ast_grep_helper.py`, `skills/productivity/google-workspace/scripts/google_api.py` (+6 more)
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-131,5 +163,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1362 +1447` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1374,0 +1460,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1374,0 +1460,2#moved` moved: `gateway/run_busy.py`, `gateway/run_turn.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/slash_commands.py@-1396,5 +1483,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1396,5 +1483,4#moved` moved: `gateway/update_launcher.py`
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-141,2 +170,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1411 +1494` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1416,4 +1499,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1416,4 +1499,4#moved` moved: `tests/gateway/test_agent_update_launcher.py`, `tests/gateway/test_update_lifecycle_notifications.py`, `tests/gateway/update_fixtures.py`
  - `gateway/slash_commands.py@-1421 +1504,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-1425,0 +1508,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-146,0 +176,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-152,3 +183,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-158,0 +191` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-160 +193,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _home_thread_from_source(source) -> Optional[str]:
  - `gateway/slash_commands.py@-175,0 +214,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-177 +218` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-178,0 +220` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import time
  - `gateway/slash_commands.py@-18 +20#moved` moved: `acp_adapter/server.py`, `acp_adapter/session.py`, `agent/credential_pool.py`, `agent/curator_backup.py` (+137 more)
- from gateway.platforms.event import MessageEvent
  - `gateway/slash_commands.py@-26 +28,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-26 +28,2#moved` moved: `gateway/relay/prompt_resolvers.py`, `gateway/run_inbound.py`
- from gateway.session_transcript import TranscriptReadError
  - `gateway/slash_commands.py@-27,0 +31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-280 +322,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-280 +322,2#moved` moved: `gateway/run_inbound.py`
  - `gateway/slash_commands.py@-442,0 +486,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-442,0 +486,19#moved` moved: `agent/acp_client.py`, `agent/auxiliary_managed_local.py`, `agent/auxiliary_plugin_oauth.py`, `agent/client_lifecycle.py` (+167 more)
  - `gateway/slash_commands.py@-443,0 +506` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-445 +508` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-483,0 +547,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-483,0 +547,3#moved` moved: `gateway/run_agent_cache.py`
  - `gateway/slash_commands.py@-485,2 +551,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-607 +675,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from __future__ import annotations
  - `gateway/slash_commands.py@-7,0 +8,2#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- with open(output_path, "wb") as f:
  - `gateway/slash_commands.py@-77,2 +81,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- with open(exit_code_path + ".tmp", "w", encoding="utf-8") as f:
  - `gateway/slash_commands.py@-80 +85` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-848,0 +923,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-848,0 +923,9#moved` moved: `gateway/slash_commands_model.py`, `gateway/slash_commands_session.py`
  - `gateway/slash_commands.py@-854,0 +938` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-857 +941,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-860 +945` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-878 +963` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands.py@-896 +981,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `gateway/slash_commands_session.py`

- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1019 +1082,48` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1019 +1082,48#moved` moved: `agent/display.py`, `agent/markdown_tables.py`, `agent/message_sanitization.py`, `agent/redact.py` (+103 more)
- def _manual_compression_reply_lines(summary: dict, compressor, focus_topic) -> l
  - `gateway/slash_commands_session.py@-105 +107` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1112,0 +1223` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1114 +1225,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1115,0 +1231,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1118 +1235,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1118 +1235,11#moved` moved: `hermes_cli/web_routers/files.py`
- def _compress_preview_reply(history, partial: bool, keep_last, focus_topic, agg_
  - `gateway/slash_commands_session.py@-113 +115` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1216 +1343` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1221,13 +1348,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1221,13 +1348,17#moved` moved: `gateway/run_agent_cache.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-1263,2 +1383,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1266 +1386` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1268,2 +1388,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1419 +1539,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-1428 +1550,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-294,0 +297,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-294,0 +297,20#moved` moved: `agent/background_review.py`, `agent/codex_responses_adapter.py`, `agent/conversation_compression.py`, `agent/conversation_compression_checkpoint.py` (+69 more)
  - `gateway/slash_commands_session.py@-295,0 +318,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-299,8 +324,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-299,8 +324,9#moved` moved: `gateway/run_agent_cache.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-307,0 +334,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-307,0 +334,21#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)
  - `gateway/slash_commands_session.py@-346,0 +389` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-550 +593` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-551,0 +595#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`
  - `gateway/slash_commands_session.py@-782,0 +827#moved` moved: `gateway/pairing.py`, `gateway/run_shutdown.py`, `gateway/slash_commands_incognito.py`, `gateway/slash_commands_model.py` (+8 more)
  - `gateway/slash_commands_session.py@-784,0 +830#moved` moved: `gateway/run_turn.py`, `gateway/run_turn_mcp.py`, `gateway/slash_commands_incognito.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-786,2 +832,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-786,2 +832,2#moved` moved: `gateway/run_adapters.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_slash_command_profile_scope.py`, `tests/gateway/test_status_command.py`
- from __future__ import annotations
  - `gateway/slash_commands_session.py@-8,0 +9,2#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-812 +858` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-830 +876,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-832 +879,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-837 +886,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-837 +886,3#moved` moved: `acp_adapter/content.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/agent_init_compression_notice.py` (+135 more)
  - `gateway/slash_commands_session.py@-847 +898` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-862,3 +913,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-862,3 +913,8#moved` moved: `acp_adapter/commands.py`, `acp_adapter/server.py`, `acp_adapter/tools.py`, `agent/activity_tracking.py` (+1182 more)
  - `gateway/slash_commands_session.py@-934 +990,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-936 +993,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `gateway/slash_commands_session.py@-970,0 +1031,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/cli_init_mixin.py`

- class CLIInitMixin:
  - `hermes_cli/cli_init_mixin.py@-508,0 +496,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/cli_model_switch_mixin.py`

- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-1005,0 +1038,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-1005,0 +1038,3#moved` moved: `gateway/slash_commands_model.py`
- def _print_switch_summary(cli, result, old_model, *, one_turn: bool, strict_cont
  - `hermes_cli/cli_model_switch_mixin.py@-176 +182` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _apply_reasoning_after_switch(cli, effort: str, *, persist_global: bool, one
  - `hermes_cli/cli_model_switch_mixin.py@-332,0 +339` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-339,0 +347,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_cli_reasoning(cli) -> None:
  - `hermes_cli/cli_model_switch_mixin.py@-49 +49,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def stored_session_route(session_meta, *, current_model, current_provider):
  - `hermes_cli/cli_model_switch_mixin.py@-57,0 +59,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-57,0 +59,2#moved` moved: `gateway/platforms/api_server.py`
- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-727,0 +737,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def stored_session_route(session_meta, *, current_model, current_provider):
  - `hermes_cli/cli_model_switch_mixin.py@-73,0 +77,3#moved` moved: `agent/agent_init_memory_provider.py`, `agent/delegation_backends.py`, `agent/memory_manager.py`, `agent/terminal_env_registry.py` (+26 more)
- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-742,0 +754,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-745,6 +758,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-745,6 +758,13#moved` moved: `agent/route_binding.py`, `tests/agent/test_switch_model_reasoning_override.py`, `tui_gateway/model_switch.py`
  - `hermes_cli/cli_model_switch_mixin.py@-757,0 +778` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-769,0 +791` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-878,0 +899,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_model_switch_mixin.py@-878,0 +899,12#moved` moved: `agent/agent_init_fallback.py`, `gateway/run_config_loaders.py`, `hermes_cli/cli_agent_setup_mixin.py`, `hermes_cli/runtime_provider.py` (+2 more)

### `hermes_cli/cli_session_mixin.py`

- def _reset_model_to_config_default(cli, silent: bool) -> None:
  - `hermes_cli/cli_session_mixin.py@-68,4 +68,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_session_mixin.py@-68,4 +68,9#moved` moved: `acp_adapter/session.py`, `gateway/run.py`, `gateway/run_config_loaders.py`, `gateway/run_inbound.py` (+9 more)
- class CLISessionMixin:
  - `hermes_cli/cli_session_mixin.py@-782,2 +792,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/cli_session_mixin.py@-782,2 +792,2#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`

### `hermes_cli/config_migrations.py`

- from hermes_cli.config_migrations_browser import migrate_to_52
  - `hermes_cli/config_migrations.py@-19,0 +20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- MIGRATIONS: tuple[tuple[int, Callable[[dict[str, Any], bool], None]], ...] = (
  - `hermes_cli/config_migrations.py@-928,0 +930` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/config_migrations.py@-940 +942` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/gateway.py`

- import logging
  - `hermes_cli/gateway.py@-11,0 +12#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+149 more)
- def _reaper_candidate_is_supervisor_owned(pid: int) -> bool:
  - `hermes_cli/gateway.py@-1707,0 +1789,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-1707,0 +1789,19#moved` moved: `CONTRIBUTING.md`, `agent/battery.py`, `agent/deadline.py`, `agent/managed_worker.py` (+254 more)
- def _get_service_pids(all_profiles: bool = False) -> set:
  - `hermes_cli/gateway.py@-174,0 +177` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _reap_unsupervised_gateway_orphans(
  - `hermes_cli/gateway.py@-1749 +1849,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-1751 +1853,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _get_service_pids(all_profiles: bool = False) -> set:
  - `hermes_cli/gateway.py@-196,0 +200` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-209,0 +214` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-217,0 +223,33` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-217,0 +223,33#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/context_compressor.py` (+234 more)
- def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
  - `hermes_cli/gateway.py@-247,0 +286,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-251,0 +298#moved` moved: `hermes_cli/dashboard_procs.py`
  - `hermes_cli/gateway.py@-255 +302` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-256,0 +304#moved` moved: `tests/scripts/desktop_update/test_desktop_update_posix_marker.py`
- def get_systemd_linger_status(username: str | None = None) -> tuple[bool | None,
  - `hermes_cli/gateway.py@-2771,9 +2875,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def get_launchd_plist_path() -> Path:
  - `hermes_cli/gateway.py@-2781 +2881,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-2785,2 +2886,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-2785,2 +2886,8#moved` moved: `hermes_constants.py`
- def get_python_path() -> str:
  - `hermes_cli/gateway.py@-2846,0 +2954,51` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-2846,0 +2954,51#moved` moved: `agent/auxiliary_health.py`, `agent/auxiliary_task_config.py`, `agent/checkpoints/manager.py`, `agent/relay_runtime.py` (+83 more)
- def generate_systemd_unit(system: bool = False, run_as_user: str | None = None)
  - `hermes_cli/gateway.py@-3109 +3267,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _agent_timeout_setting(env_var: str, key: str, parse) -> float:
  - `hermes_cli/gateway.py@-3431,0 +3592,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _get_restart_exit_wait_budget() -> float:
  - `hermes_cli/gateway.py@-3447 +3619,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_cli.gateway_launchd import (
  - `hermes_cli/gateway.py@-3829,0 +4004` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from gateway.restart import (
  - `hermes_cli/gateway.py@-44,0 +46` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _respawn_storm_backoff() -> None:
  - `hermes_cli/gateway.py@-4456,0 +4632,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4456,0 +4632,17#moved` moved: `agent/file_safety.py`, `agent/lsp/workspace.py`, `agent/redact_commands.py`, `cron/lifecycle_guard.py` (+26 more)
- def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False, fo
  - `hermes_cli/gateway.py@-4458 +4650` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4488 +4680` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4497,0 +4690,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4521,5 +4714,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4521,5 +4714,6#moved` moved: `gateway/run_restart_recovery.py`, `tests/cron/test_ambiguous_admission.py`, `tests/gateway/status_report/test_gateway_wiring.py`, `tests/gateway/status_report/test_reporter_lock.py` (+5 more)
  - `hermes_cli/gateway.py@-4527,23 +4721,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4527,23 +4721,7#moved` moved: `acp_adapter/auth.py`, `acp_adapter/events.py`, `acp_adapter/model_catalog.py`, `acp_adapter/session.py` (+482 more)
- def _cmd_run(args):
  - `hermes_cli/gateway.py@-4950,0 +5129,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-4953,5 +5134,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _install_systemd_from_cli(args, *, force: bool, system: bool, run_as_user) -
  - `hermes_cli/gateway.py@-5071,25 +5249,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_install(args):
  - `hermes_cli/gateway.py@-5101,0 +5257,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def probe_gateway_loop_liveness(
  - `hermes_cli/gateway.py@-511 +559,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_install(args):
  - `hermes_cli/gateway.py@-5112,0 +5271,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5119 +5279` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _escalate_wedged_gateway(pid: int, *, term_grace: float = 5.0, kill_wait: fl
  - `hermes_cli/gateway.py@-518,7 +567,27` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-518,7 +567,27#moved` moved: `agent/auxiliary_client.py`, `agent/credential_pool_selection.py`, `agent/onboarding_record.py`, `agent/runtime_scope.py` (+70 more)
- def _print_unfolded_gateway_note(owner) -> None:
  - `hermes_cli/gateway.py@-5184,0 +5345,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5184,0 +5345,22#moved` moved: `agent/lsp/install.py`, `agent/secret_sources/bitwarden.py`, `agent/secret_sources/onepassword.py`, `evals/desktop_bug_campaign/persistence_live.py` (+84 more)
- def _cmd_start(args):
  - `hermes_cli/gateway.py@-5185,0 +5368` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5191,0 +5375,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5191,0 +5375,5#moved` moved: `hermes_cli/gateway_launchd.py`
- def _cmd_stop(args):
  - `hermes_cli/gateway.py@-5251,0 +5440,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5251,0 +5440,3#moved` moved: `hermes_cli/_early_recovery.py`, `hermes_cli/auth_device_flow.py`, `hermes_cli/cli_tui_runtime_mixin.py`, `hermes_cli/clipboard.py` (+37 more)
- def _escalate_wedged_gateway(pid: int, *, term_grace: float = 5.0, kill_wait: fl
  - `hermes_cli/gateway.py@-527 +596,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-527 +596,12#moved` moved: `gateway/run_shutdown.py`, `hermes_cli/gateway_guardian.py`, `pm/filesystem.py`, `tools/browser_supervisor_capture.py` (+2 more)
  - `hermes_cli/gateway.py@-531 +611,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_restart(args):
  - `hermes_cli/gateway.py@-5353,0 +5545` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5367,0 +5560,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5367,0 +5560,3#moved` moved: `hermes_cli/gateway_launchd.py`
- def _cmd_list(args):
  - `hermes_cli/gateway.py@-5550,0 +5747,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_migrate(args):
  - `hermes_cli/gateway.py@-5565,0 +5767,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _GATEWAY_SUBCOMMANDS = {
  - `hermes_cli/gateway.py@-5569 +5775` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _gateway_command_inner(args):
  - `hermes_cli/gateway.py@-5576 +5782,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway.py@-5576 +5782,21#moved` moved: `agent/lsp/cli.py`, `apps/desktop/electron/fixtures/source-backend.py`, `hermes_cli/_secrets_common.py`, `hermes_cli/connectors_cmd.py` (+24 more)

### `hermes_cli/gateway_client.py`

- async def connect_gateway(endpoint=None, subject=None, *, max_size=8 * 1024 * 10
  - `hermes_cli/gateway_client.py@-361 +397,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/gateway_runtime.py`

- def ensure_gateway_runtime(profile_home: str | Path, *, timeout: float | None =
  - `hermes_cli/gateway_runtime.py@-473 +512,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/gateway_session_store.py`

- class GatewaySessionStore:
  - `hermes_cli/gateway_session_store.py@-308,0 +314,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/gateway_session_store.py@-308,0 +314,8#moved` moved: `cron/detached_authority.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py`, `gateway/session_pause_announce.py` (+34 more)

### `hermes_cli/goals.py`

- def draft_contract(objective: str, *, timeout: Optional[float] = None) -> Option
  - `hermes_cli/goals.py@-1077,0 +2421,307` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/goals.py@-1077,0 +2421,307#moved` moved: `agent/prompt_cache_clock.py`, `hermes_cli/goals_authority.py`
- class GoalManager:
  - `hermes_cli/goals.py@-1152,0 +2825,99` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/goals.py@-1152,0 +2825,99#moved` moved: `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py`, `persistence/sqlite/conversation/_store/legacy_refusal.py`
- _JUDGE_RESPONSE_SNIPPET_CHARS = 4000
  - `hermes_cli/goals.py@-40,7 +434,39` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def load_goal(session_id: str) -> Optional[GoalState]:
  - `hermes_cli/goals.py@-654,0 +1348,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/goals.py@-654,0 +1348,53#moved` moved: `gateway/run_goal_wait.py`, `hermes_cli/goals_evaluation.py`, `hermes_cli/loops_snapshot.py`
- def save_goal(session_id: str, state: GoalState) -> None:
  - `hermes_cli/goals.py@-666 +1415,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/goals.py@-666 +1415,5#moved` moved: `hermes_cli/goals_evaluation.py`
  - `hermes_cli/goals.py@-670,0 +1426,30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/goals.py@-670,0 +1426,30#moved` moved: `gateway/shutdown_goal_wakes.py`, `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py` (+1 more)
- def _session_waiting(session_id: str) -> bool:
  - `hermes_cli/goals.py@-749,0 +1557,138` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _render_background_block(background_processes: Optional[list[dict[str, Any]]
  - `hermes_cli/goals.py@-877,0 +1868,330` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/loops.py`

- def load_loop(session_id: str) -> Optional[LoopState]:
  - `hermes_cli/loops.py@-282,2 +491,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/loops.py@-282,2 +491,8#moved` moved: `gateway/run_goal_wait.py`, `hermes_cli/goals_evaluation.py`, `hermes_cli/loops_snapshot.py`
- def save_loop(session_id: str, state: LoopState) -> None:
  - `hermes_cli/loops.py@-295,2 +541,42` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/loops.py@-295,2 +541,42#moved` moved: `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py`, `persistence/sqlite/conversation/_store/legacy_refusal.py`
- def migrate_loop_to_session(old_session_id: str, new_session_id: str, *, reason:
  - `hermes_cli/loops.py@-348 +634,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/session_recovery.py`

- def _init_delivery_ledger_schema(conn: sqlite3.Connection) -> None:
  - `hermes_cli/session_recovery.py@-37,0 +38,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/session_recovery.py@-37,0 +38,23#moved` moved: `apps/bootstrap-installer/src-tauri/build.rs`, `apps/bootstrap-installer/src-tauri/src/bootstrap.rs`, `apps/bootstrap-installer/src-tauri/src/install_script.rs`, `apps/bootstrap-installer/src-tauri/src/lib.rs` (+234 more)
- _AUXILIARY_TABLE_SCHEMAS: dict[str, Callable[[sqlite3.Connection], None]] = {
  - `hermes_cli/session_recovery.py@-43,0 +67` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/sessions_cmd.py`

- def _cmd_optimize(db, args):
  - `hermes_cli/sessions_cmd.py@-1001,0 +1035,24` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1001,0 +1035,24#moved` moved: `gateway/run.py`, `hermes_cli/console_engine.py`, `hermes_cli/kanban_transfer.py`, `hermes_cli/web_routers/profiles.py` (+9 more)
- def _cmd_repair_prompts(db, args):
  - `hermes_cli/sessions_cmd.py@-1205,0 +1263#moved` moved: `tests/hermes_cli/test_update_restore_authority.py`
  - `hermes_cli/sessions_cmd.py@-1213,0 +1272` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1238,0 +1298` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1257,0 +1318#moved` moved: `hermes_cli/worktree_archive.py`, `tests/gateway/test_multiplex_primary_token_scope.py`, `tests/persistence_sqlite/housekeeping/test_delete_session_write_guards.py`, `tests/test_hermes_dir_acl.py`
  - `hermes_cli/sessions_cmd.py@-1264,0 +1326` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1300,2 +1362,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1300,2 +1362,7#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2052 more)
  - `hermes_cli/sessions_cmd.py@-1305,0 +1373,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def cmd_sessions(args, sessions_parser=None):
  - `hermes_cli/sessions_cmd.py@-1413,0 +1483,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-1423,0 +1496,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_list(db, args):
  - `hermes_cli/sessions_cmd.py@-316 +316` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _NEVER_ACTIVE_DEFAULT_DAYS = 30.0
  - `hermes_cli/sessions_cmd.py@-697,0 +698,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-697,0 +698,9#moved` moved: `apps/desktop/src/api/client.ts`, `apps/desktop/src/plugins/hermes-bots/canonical-group-workspace.test.tsx`, `apps/desktop/src/store/session-request-router.ts`, `apps/shared/src/node/gateway-client.test.ts` (+15 more)
- def _prune_never_active_keyed(db, args):
  - `hermes_cli/sessions_cmd.py@-739 +748` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _note_pinned_skipped(db, filters, action):
  - `hermes_cli/sessions_cmd.py@-769,0 +779,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-769,0 +779,12#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_media.py`, `agent/acp_client.py`, `agent/auxiliary_client.py` (+186 more)
- def _cmd_prune_or_archive(db, args, action):
  - `hermes_cli/sessions_cmd.py@-771,0 +793,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-846 +870,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/sessions_cmd.py@-850 +875,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/web_routers/models.py`

- def get_model_info(profile: Optional[str] = None):
  - `hermes_cli/web_routers/models.py@-109 +108,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _MODEL_INFO_PROBE_BUDGET_S = 5.0
  - `hermes_cli/web_routers/models.py@-64 +64` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _bounded_context_length_probe(model: str, base_url: str, provider: str) -> i
  - `hermes_cli/web_routers/models.py@-77,2 +77` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_cli/web_routers/sessions.py`

- def _prune_sessions(body: SessionPrune):
  - `hermes_cli/web_routers/sessions.py@-116,3 +116,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/web_routers/sessions.py@-116,3 +116,7#moved` moved: `hermes_cli/web_routers/files.py`, `hermes_cli/web_routers/ops.py`, `hermes_cli/web_server_config.py`, `hermes_cli/web_server_sessions.py` (+1 more)
- async def prune_sessions_endpoint(body: SessionPrune):
  - `hermes_cli/web_routers/sessions.py@-988,0 +993,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_cli/web_routers/sessions.py@-988,0 +993,3#moved` moved: `hermes_cli/web_routers/ops.py`

### `hermes_state.py`

- class SessionDB(
  - `hermes_state.py@-1024,0 +1141,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1024,0 +1141,12#moved` moved: `hermes_state_transactions.py`, `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1030,0 +1159,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1033,0 +1165,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1033,0 +1165,3#moved` moved: `hermes_state_deletion.py`, `hermes_state_prune_budget.py`, `hermes_state_transactions.py`
  - `hermes_state.py@-1041,0 +1176` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1042,0 +1178,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1044,10 +1181,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1056,2 +1195,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1056,2 +1195,10#moved` moved: `cron/scheduler_tick.py`, `gateway/run_busy.py`, `gateway/run_goal_wait.py`, `hermes_cli/quiet_single_query.py` (+12 more)
  - `hermes_state.py@-1060,2 +1207,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1060,2 +1207,3#moved` moved: `cron/delivery_queue.py`, `cron/executions.py`, `gateway/hosted_room_work_storage.py`, `gateway/hosted_rooms_common.py` (+31 more)
  - `hermes_state.py@-1063,10 +1211,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1074,4 +1216,2#moved` moved: `agent/conversation_compression_publication.py`, `persistence/sqlite/conversation/_errors.py`
  - `hermes_state.py@-1092,0 +1233,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1092,0 +1233,4#moved` moved: `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1093,0 +1238#moved` moved: `agent/conversation_compression_publication.py`
  - `hermes_state.py@-1123,16 +1268,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1123,16 +1268,4#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+3750 more)
  - `hermes_state.py@-1142,0 +1276` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1147 +1281` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1149 +1283,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1157 +1292` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1167 +1302` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1169,0 +1305,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1179,2 +1316,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1179,2 +1316,5#moved` moved: `hermes_state_messages.py`, `hermes_state_transactions.py`, `persistence/sqlite/_holder.py`, `persistence/sqlite/conversation/_store/messages.py` (+4 more)
  - `hermes_state.py@-1194 +1334,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1210 +1351,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1225,0 +1368` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1227,4 +1370,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1227,4 +1370,9#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/tools.py`, `agent/agent_init.py` (+1891 more)
  - `hermes_state.py@-1337,0 +1486,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1337,0 +1486,5#moved` moved: `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1342 +1495,24` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1342 +1495,24#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2727 more)
  - `hermes_state.py@-1502,0 +1679,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1507,6 +1689,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1507,6 +1689,4#moved` moved: `hermes_cli/gateway_agent.py`, `hermes_state_compaction_prepare.py`, `hermes_state_display_fold.py`, `hermes_state_search.py` (+8 more)
  - `hermes_state.py@-1517,0 +1698,31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1517,0 +1698,31#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3197 more)
  - `hermes_state.py@-1561 +1772,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1587,0 +1801,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1590,0 +1808,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1597,0 +1817,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1669,0 +1891,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-1669,0 +1891,3#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3000 more)
  - `hermes_state.py@-1700 +1924#moved` moved: `hermes_state_gateway.py`, `hermes_state_telegram.py`
- from contextlib import contextmanager, suppress
  - `hermes_state.py@-25,0 +26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _secure_state_db_files(db_path: Path, *, create_main: bool = False) -> None:
  - `hermes_state.py@-315 +322,5#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2110 more)
- def _foreign_state_db_holders(db_path: Path) -> list[tuple[int, str]]:
  - `hermes_state.py@-475,0 +487,24` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-475,0 +487,24#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_health.py`, `agent/auxiliary_task_config.py`, `agent/background_review.py` (+1128 more)
- class SessionDB(
  - `hermes_state.py@-481 +516` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-515,0 +551,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-539 +576` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-541,3 +578,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-545,0 +584,41` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-545,0 +584,41#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3894 more)
  - `hermes_state.py@-596,0 +676` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-612 +692` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-618 +698` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-621 +701` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_state_lockowners import log_write_lock_holders
  - `hermes_state.py@-63 +64,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionDB(
  - `hermes_state.py@-646,0 +727,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-649,0 +735` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_state_dbfile import (
  - `hermes_state.py@-68 +70,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_state_search import SessionSearchMixin
  - `hermes_state.py@-87,0 +91,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionDB(
  - `hermes_state.py@-873 +967` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-875,5 +969,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-884 +978,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-888 +984,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-888 +984,4#moved` moved: `persistence/sqlite/_holder.py`, `tests/hermes_cli/test_refresh_singleflight.py`
  - `hermes_state.py@-901 +1000` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-926 +1025` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-929 +1028` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-930,0 +1030,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-934 +1035,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-934 +1035,11#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+4273 more)
  - `hermes_state.py@-937 +1048` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-941 +1052,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-943,3 +1055,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-943,3 +1055,18#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3015 more)
  - `hermes_state.py@-955,12 +1082` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state.py@-971 +1087#moved` moved: `hermes_state_display_fold.py`

### `hermes_state_common.py`

- from hermes_cli.timefmt import EPOCH_MAX, EPOCH_MIN
  - `hermes_state_common.py@-15 +15,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_common.py@-15 +15,2#moved` moved: `tests/tui_gateway/test_change_watcher_sessions.py`

### `hermes_state_errors.py` → `persistence/sqlite/_state/errors.py`

- PERSISTENCE_ERROR_CAUSES = (
  - `hermes_state_errors.py@-101 +116` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class SessionCompressionInProgressError(CompressionSessionBusyError):
  - `hermes_state_errors.py@-168,0 +184,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _MALFORMED_DB_MARKERS = (*_MALFORMED_SCHEMA_MARKERS, "database disk image is mal
  - `hermes_state_errors.py@-18,0 +19,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_errors.py@-18,0 +19,15#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/gateway_server.py`, `acp_adapter/permissions.py` (+2057 more)
- class SessionActiveWriteGuardError(RuntimeError):
  - `hermes_state_errors.py@-199,0 +219,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _PERSISTENCE_CAUSE_BY_TYPE = (
  - `hermes_state_errors.py@-269,0 +295` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _PERSISTENCE_CAUSE_BY_PHRASE = (
  - `hermes_state_errors.py@-279,0 +306` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def classify_persistence_error(exc_or_str) -> str:
  - `hermes_state_errors.py@-302 +329,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_state_gateway.py`

- class SessionGatewayMixin:
  - `hermes_state_gateway.py@-1030 +1188` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_gateway.py@-1034 +1192,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_gateway.py@-1034 +1192,4#moved` moved: `persistence/sqlite/_holder.py`

### `hermes_state_identity.py` → `persistence/sqlite/conversation/_store/identity.py`

- from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple
  - `hermes_state_identity.py@-10 +12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _stable_tool_key(row: Any) -> Optional[tuple[Any, ...]]:
  - `hermes_state_identity.py@-111,0 +172,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_identity.py@-111,0 +172,18#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+9396 more)
- from agent.message_sanitization import coalesce_tool_call_id
  - `hermes_state_identity.py@-12 +14,59` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_identity.py@-12 +14,59#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+10066 more)
- from __future__ import annotations
  - `hermes_state_identity.py@-6,0 +7#moved` moved: `acp_adapter/gateway_media.py`, `agent/account_usage_cache.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py` (+508 more)
- import json
  - `hermes_state_identity.py@-8 +9,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_identity.py@-8 +9,2#moved` moved: `agent/context_compressor.py`, `agent/context_compressor_durable.py`, `agent/conversation_compression_publication.py`, `agent/kanban_turn_recovery.py` (+805 more)

### `hermes_state_mutation_compress.py`

- def compress_in_transaction(db, conn, session_id, payload, prepared):
  - `hermes_state_mutation_compress.py@-21,0 +22,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_mutation_compress.py@-21,0 +22,3#moved` moved: `persistence/sqlite/authority/_mutation_publish.py`

### `hermes_state_profile_repair.py` → `persistence/sqlite/admin/profile_repair.py`

- class SessionProfileRepairMixin:
  - `hermes_state_profile_repair.py@-183 +183` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-206 +206` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-269 +269` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-291 +291#moved` moved: `hermes_state_sessions.py`
  - `hermes_state_profile_repair.py@-316 +316` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-327 +327` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-340 +340` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_profile_repair.py@-378 +378` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_state_telegram.py`

- class SessionTelegramTopicsMixin:
  - `hermes_state_telegram.py@-307 +564` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_state_timeline.py` → `persistence/sqlite/conversation/_store/timeline.py`

- from hermes_state_messages import DISPLAY_VISIBLE_SQL
  - `hermes_state_timeline.py@-11,0 +12#moved` moved: `gateway/session_incognito_boundary.py`, `hermes_state_display_kind.py`, `hermes_state_gateway.py`, `hermes_state_messages.py` (+3 more)

### `hermes_state_usage.py`

- class SessionUsageMixin:
  - `hermes_state_usage.py@-316,6 +327,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `hermes_state_worker_compression.py` → `persistence/sqlite/workers/_compression.py`

- def worker_history(db, conn, sid, payload):
  - `hermes_state_worker_compression.py@-201,0 +202,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_compression.py@-201,0 +202,4#moved` moved: `agent/runtime_session_compression.py`, `gateway/session_incognito_boundary.py`, `hermes_state_compression.py`, `hermes_state_messages.py`
  - `hermes_state_worker_compression.py@-204 +208,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_compression.py@-210 +215` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def archive_on_connection(db, conn, session_id: str, compacted_messages: list[di
  - `hermes_state_worker_compression.py@-216 +221#moved` moved: `hermes_state_messages.py`
- def worker_archive(db, conn, sid, payload):
  - `hermes_state_worker_compression.py@-256 +261` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_compression.py@-257,0 +263,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_compression.py@-257,0 +263,3#moved` moved: `gateway/platforms/base_session_command.py`, `gateway/session_bot.py`, `gateway/session_ingress.py`, `gateway/session_policy.py` (+4 more)
  - `hermes_state_worker_compression.py@-284,3 +292,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_compression.py@-284,3 +292,8#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)

### `hermes_state_worker_lifecycle.py` → `persistence/sqlite/workers/_lifecycle.py`

- def worker_next_title(db, conn, session_id, payload):
  - `hermes_state_worker_lifecycle.py@-110,0 +116,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_lifecycle.py@-110,0 +116,10#moved` moved: `gateway/session_handoff.py`, `gateway/session_teammate_waits.py`, `gateway/session_worker_discovery.py`, `hermes_state_local.py` (+5 more)
- WORKER_LIFECYCLE_HANDLERS = {
  - `hermes_state_worker_lifecycle.py@-196,0 +212` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def insert_session_row_in_transaction(
  - `hermes_state_worker_lifecycle.py@-225,2 +241,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_lifecycle.py@-292 +308` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def worker_title(db, conn, session_id, payload):
  - `hermes_state_worker_lifecycle.py@-64,2 +64,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `hermes_state_worker_lifecycle.py@-69 +73,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/adopt-worker-indexed-lookup.md`

- (module)
  - `maintenance/adopt-worker-indexed-lookup.md@-0,0 +1,57` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/compaction-publication.md`

- (module)
  - `maintenance/compaction-publication.md@-0,0 +1,103` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/compaction-write-amplification.md`

- (module)
  - `maintenance/compaction-write-amplification.md@-0,0 +1,146` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/cron-restart-survival.md`

- (module)
  - `maintenance/cron-restart-survival.md@-0,0 +1,277` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/desktop-idle-cpu.md`

- (module)
  - `maintenance/desktop-idle-cpu.md@-0,0 +1,65` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/fts-writer-automerge.md`

- (module)
  - `maintenance/fts-writer-automerge.md@-0,0 +1,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/gateway-loop-statedb-guard.md`

- (module)
  - `maintenance/gateway-loop-statedb-guard.md@-0,0 +1,341` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/hygiene-deferred-adoption.md`

- (module)
  - `maintenance/hygiene-deferred-adoption.md@-0,0 +1,79` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/incognito.md`

- (module)
  - `maintenance/incognito.md@-0,0 +1,176` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/nightly-regression-0926b.md`

- (module)
  - `maintenance/nightly-regression-0926b.md@-0,0 +1,76` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/preflight-input-archive.md`

- (module)
  - `maintenance/preflight-input-archive.md@-0,0 +1,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/provider-reported-cost.md`

- (module)
  - `maintenance/provider-reported-cost.md@-0,0 +1,56` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/release-defects.md`

- (module)
  - `maintenance/release-defects.md@-0,0 +1,1461` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/restart-recovery-bounded.md`

- (module)
  - `maintenance/restart-recovery-bounded.md@-0,0 +1,44` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/runtime-epoch-owner-fence.md`

- (module)
  - `maintenance/runtime-epoch-owner-fence.md@-0,0 +1,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/runtime-epoch-ownership.md`

- (module)
  - `maintenance/runtime-epoch-ownership.md@-0,0 +1,59` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/session-admission-adoption-index.md`

- (module)
  - `maintenance/session-admission-adoption-index.md@-0,0 +1,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/session-search-isolation.md`

- (module)
  - `maintenance/session-search-isolation.md@-0,0 +1,47` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/slack-context-notices.md`

- (module)
  - `maintenance/slack-context-notices.md@-0,0 +1,132` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/state-db-holder-scan.md`

- (module)
  - `maintenance/state-db-holder-scan.md@-0,0 +1,40` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/state-db-prune-lock-batching.md`

- (module)
  - `maintenance/state-db-prune-lock-batching.md@-0,0 +1,152` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/state-write-lock-hold-indexes.md`

- (module)
  - `maintenance/state-write-lock-hold-indexes.md@-0,0 +1,66` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/state-write-stall-attribution.md`

- (module)
  - `maintenance/state-write-stall-attribution.md@-0,0 +1,126` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `maintenance/telegram-topics.md`

- (module)
  - `maintenance/telegram-topics.md@-0,0 +1,210` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `model_tools.py`

- def handle_function_call(
  - `model_tools.py@-1002,2 +1117,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-1002,2 +1117,4#moved` moved: `tests/agent/test_browser_output_egress.py`, `tests/agent/test_vault_protected_fields.py`, `tests/tools/test_browser_cleanup.py`, `tests/tools/test_browser_protected_date_pixels.py` (+4 more)
  - `model_tools.py@-1006,2 +1123,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-1006,2 +1123,6#moved` moved: `tests/agent/test_browser_output_egress.py`, `tests/agent/test_vault_protected_fields.py`, `tests/tools/test_browser_cleanup.py`, `tests/tools/test_browser_protected_date_pixels.py` (+4 more)
  - `model_tools.py@-1009 +1130` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import time
  - `model_tools.py@-18,0 +19#moved` moved: `acp_adapter/events.py`, `acp_adapter/gateway_server.py`, `acp_adapter/session.py`, `acp_adapter/tools.py` (+342 more)
- def _discord_rewriter(schema_fn_name: str):
  - `model_tools.py@-386,0 +380,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _rewrite_browser_exec(td: dict[str, Any], available: set) -> Optional[dict[s
  - `model_tools.py@-399,13 +403,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-399,13 +403,8#moved` moved: `tests/hermes_cli/test_cli_custom_provider_vision.py`, `tests/tools/test_vision_native_fast_path.py`, `tools/browser_tool.py`
- def _rewrite_browser_vault(td: dict[str, Any], available: set) -> Optional[dict[
  - `model_tools.py@-445,2 +444` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-452,0 +451,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-459,3 +459,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-459,3 +459,18#moved` moved: `evals/prefix_rediet/worker.py`
- _DYNAMIC_SCHEMA_REWRITERS = {
  - `model_tools.py@-490,0 +506` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-492 +508,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_active_context_length() -> int:
  - `model_tools.py@-601,0 +619` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-609,0 +628` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-621 +640,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-632 +652` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _dispatch_bridge_tool(function_name: str, function_args: dict[str, Any],
  - `model_tools.py@-760,0 +781,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-778,0 +820,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-778,0 +820,12#moved` moved: `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py`, `agent/api_request_hooks.py` (+509 more)
- def _elapsed_ms(start: float) -> int:
  - `model_tools.py@-905,0 +959,49` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-905,0 +959,49#moved` moved: `acp_adapter/session.py`, `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py` (+512 more)
- def handle_function_call(
  - `model_tools.py@-925,0 +1028,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-925,0 +1028,4#moved` moved: `agent/title_link_context.py`, `agent/turn_recovery.py`, `cron/lifecycle_guard.py`, `gateway/session_tui_ops.py` (+24 more)
  - `model_tools.py@-937 +1043,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-937 +1043,3#moved` moved: `tests/agent/test_browser_output_egress.py`, `tests/agent/test_vault_protected_fields.py`, `tests/tools/test_browser_cleanup.py`, `tests/tools/test_browser_protected_date_pixels.py` (+4 more)
  - `model_tools.py@-949,0 +1058` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `model_tools.py@-950,0 +1060,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `plugins/platforms/telegram/adapter.py`

- class TelegramAdapter(TelegramContactMixin, TelegramHeldInboundMixin, TelegramRe
  - `plugins/platforms/telegram/adapter.py@-1149,2 +1647,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-1207 +1714,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-1374,11 +1874,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-1374,11 +1874,14#moved` moved: `agent/auxiliary_client.py`, `agent/context_compressor.py`, `agent/conversation_loop.py`, `agent/vision_message_prep.py` (+1 more)
- from plugins.platforms.telegram.telegram_audio_meta import (
  - `plugins/platforms/telegram/adapter.py@-180,2 +212,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from plugins.platforms.telegram.telegram_media_group import (
  - `plugins/platforms/telegram/adapter.py@-191 +225,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TelegramAdapter(TelegramContactMixin, TelegramHeldInboundMixin, TelegramRe
  - `plugins/platforms/telegram/adapter.py@-3326,4 +4408,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3407,2 +4555,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3414,2 +4582,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3417 +4589#moved` moved: `gateway/outbound/send.py`, `plugins/platforms/telegram/rich_parts.py`
  - `plugins/platforms/telegram/adapter.py@-3426,3 +4598,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3429,0 +4602,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3669,2 +4894` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3700,3 +4934` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3710,3 +4942,73` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-3710,3 +4942,73#moved` moved: `apps/desktop/electron/main.ts`, `gateway/platforms/helpers.py`, `gateway/stream_consumer.py`, `hermes_cli/web_routers/models.py` (+13 more)
  - `plugins/platforms/telegram/adapter.py@-4311,17 +5745,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-4875,3 +6364,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-5660 +7483` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-5877,2 +7710,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6110,2 +8038,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6134 +8061` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6141 +8068` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6168 +8095,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6199 +8130,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `plugins/platforms/telegram/adapter.py@-6249 +8181` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `pm/install_states.py`

- def _install_lock(state: Path):
  - `pm/install_states.py@-101,2 +122` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _remove_contents(state: Path) -> bool:
  - `pm/install_states.py@-148,0 +166,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def collect_orphan_install_states(installs: Path) -> list[Path]:
  - `pm/install_states.py@-161,13 +181,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `pm/runtime.py`

- def prepare_runtime(uv: Path, python: Path, root: Path, *, offline: bool = False
  - `pm/runtime.py@-144,31 +150,35#moved` moved: `pm/receipt.py`, `tests/pm/test_runtime_gc.py`
- def collect_runtime_generations(root: Path) -> list[Path]:
  - `pm/runtime.py@-200,15 +211,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `pm/runtime.py@-200,15 +211,18#moved` moved: `tests/pm/test_runtime_gc.py`

### `tests-js/bundle-smoke-metadata.test.mjs`

- import { buildStampPayload } from '../apps/desktop/scripts/write-build-stamp.mjs
  - `tests-js/bundle-smoke-metadata.test.mjs@-9,0 +11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/acp_adapter/test_gateway_catalog_reset_owner.py`

- (module)
  - `tests/acp_adapter/test_gateway_catalog_reset_owner.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_reset_child_maps_to_its_owner_while_legacy_reset_histories_stay_indepen
  - `tests/acp_adapter/test_gateway_catalog_reset_owner.py@-31 +32#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/acp_adapter/test_gateway_editor_mcp.py`

- def _peers_running(pid):
  - `tests/acp_adapter/test_gateway_editor_mcp.py@-217,2 +217,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from tests.gateway.test_normal_runtime_boot import control
  - `tests/acp_adapter/test_gateway_editor_mcp.py@-25 +25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/acp_adapter/test_gateway_retry_command.py`

- async def _editor(tmp_path, monkeypatch, wire_class=SlashWire):
  - `tests/acp_adapter/test_gateway_retry_command.py@-77 +80,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/lsp/test_lsp_owned_server.py`

- def test_a_live_owner_keeps_its_server_tree(tmp_path):
  - `tests/agent/lsp/test_lsp_owned_server.py@-171 +171,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/test_plugin_conversation_id.py`

- from hermes_state import SessionDB
  - `tests/agent/test_plugin_conversation_id.py@-13 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/agent/test_plugin_prompt_sections.py`

- def test_fresh_process_resume_restores_identical_full_prompt_without_callback(tm
  - `tests/agent/test_plugin_prompt_sections.py@-104,0 +107#moved` moved: `tests/agent/test_plugin_llm_task_routing.py`, `tests/gateway/outbound/test_send_message_plugin_extensibility.py`, `tests/gateway/test_post_gateway_admission.py`, `tests/gateway/test_pre_gateway_dispatch_relay_reader.py` (+7 more)
  - `tests/agent/test_plugin_prompt_sections.py@-125 +128#moved` moved: `tests/agent/test_plugin_llm_task_routing.py`, `tests/gateway/outbound/test_send_message_plugin_extensibility.py`, `tests/gateway/test_pre_gateway_dispatch_relay_reader.py`, `tests/hermes_cli/test_approval_transport.py` (+3 more)
- def _install_test_section(manager: PluginManager, content) -> None:
  - `tests/agent/test_plugin_prompt_sections.py@-31 +33#moved` moved: `tests/agent/test_plugin_llm_task_routing.py`, `tests/gateway/outbound/test_send_message_plugin_extensibility.py`, `tests/gateway/test_pre_gateway_dispatch_relay_reader.py`, `tests/hermes_cli/test_approval_transport.py` (+3 more)
- import textwrap
  - `tests/agent/test_plugin_prompt_sections.py@-9,0 +10,2#moved` moved: `tests/agent/test_plugin_llm_task_routing.py`, `tests/gateway/outbound/test_send_message_plugin_extensibility.py`, `tests/gateway/test_post_gateway_admission.py`, `tests/gateway/test_pre_gateway_dispatch_relay_reader.py` (+7 more)

### `tests/agent/test_pre_llm_block_hook.py`

- def test_retry_and_undo_after_a_block_resubmit_the_typed_text(provider_url, tmp_
  - `tests/agent/test_pre_llm_block_hook.py@-140,0 +141#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/agent/test_pre_llm_block_hook.py@-148 +149#moved` moved: `tests/persistence_sqlite/conversation_store/test_compression_created_source.py`, `tests/persistence_sqlite/core/test_prune_pass_bounds.py`

### `tests/agent/test_runtime_scope.py`

- def test_claim_fence_reads_the_claimed_admission(tmp_path):
  - `tests/agent/test_runtime_scope.py@-641 +642` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/test_runtime_session_store.py`

- def test_outbox_journal_survives_a_cross_device_rename(tmp_path, monkeypatch):
  - `tests/agent/test_runtime_session_store.py@-110 +112` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_worker_store_persists_and_restores_the_tools_prefix_pin(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-124 +126,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_outbox_freezes_failed_payload_and_reopens_exclusively(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-19 +19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_runtime_session_store.py@-37 +37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_outbox_capacity_failure_does_not_advance_or_discard(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-60,0 +61,2#moved` moved: `agent/managed_worker.py`, `tests/agent/test_worker_mandatory_capacity.py`, `tests/agent/test_worker_outbox_io_failure.py`, `tests/gateway/fixtures/agent_persistence_worker.py`

### `tests/agent/test_runtime_session_store_surface_parity.py`

- from hermes_state import SessionDB
  - `tests/agent/test_runtime_session_store_surface_parity.py@-15 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_tail_reads_answer_like_the_owner(tmp_path):
  - `tests/agent/test_runtime_session_store_surface_parity.py@-56,0 +58,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_runtime_session_store_surface_parity.py@-62,0 +66,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_runtime_session_store_surface_parity.py@-62,0 +66,7#moved` moved: `agent/prompt_cache_clock.py`, `agent/runtime_session_compression.py`, `agent/runtime_session_journal.py`, `agent/runtime_session_privacy.py` (+10 more)

### `tests/agent/test_session_prompt_refresh.py`

- def _agent(srv, home, session_id):
  - `tests/agent/test_session_prompt_refresh.py@-69 +78,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/test_title_generator.py`

- from agent.title_generator import (
  - `tests/agent/test_title_generator.py@-14,0 +15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestGenerateTitle:
  - `tests/agent/test_title_generator.py@-415,0 +417,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-417,0 +441,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-419,0 +448,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-419,0 +448,14#moved` moved: `tests/agent/test_auxiliary_client.py`, `tests/agent/test_compaction_archive_once.py`, `tests/agent/test_compaction_offload.py`, `tests/agent/test_compression_attempt_lifecycle.py` (+22 more)
  - `tests/agent/test_title_generator.py@-421,0 +464,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-421,0 +464,18#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3497 more)
  - `tests/agent/test_title_generator.py@-423,0 +484,55` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-423,0 +484,55#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3477 more)
- class TestAutoTitleSession:
  - `tests/agent/test_title_generator.py@-455,0 +571,49` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-455,0 +571,49#moved` moved: `tests/agent/test_compress_context_progress_timeout.py`, `tests/agent/test_fallback_session_reload.py`, `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_partial_stream_finish_reason.py` (+20 more)
  - `tests/agent/test_title_generator.py@-458,0 +623` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-469 +634` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestMaybeAutoTitle:
  - `tests/agent/test_title_generator.py@-516,0 +682,51` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-516,0 +682,51#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2593 more)
  - `tests/agent/test_title_generator.py@-582,0 +799,51` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-816,2 +1083,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-822 +1089,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-827,3 +1095,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-827,3 +1095,4#moved` moved: `tests/agent/test_compaction_events.py`, `tests/agent/test_task_title_style.py`, `tests/agent/test_title_completion_retry.py`
  - `tests/agent/test_title_generator.py@-831 +1100,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-831 +1100,3#moved` moved: `tests/agent/test_compaction_events.py`, `tests/agent/test_task_title_style.py`, `tests/agent/test_title_completion_retry.py`
- class TestAutoTitleDuplicateHandling:
  - `tests/agent/test_title_generator.py@-869 +1140,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-873 +1145` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-875,0 +1148` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-883,0 +1157,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-888 +1165` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-895,2 +1172,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-895,2 +1172,4#moved` moved: `tests/cron/test_scheduler.py`, `tests/gateway/test_display_admission.py`, `tests/gateway/test_multiplex_mcp_discovery.py`, `tests/gateway/test_reset_cleanup_profile_scope.py` (+4 more)
- class TestModelSwitchMarkerNotTitleable:
  - `tests/agent/test_title_generator.py@-997,0 +1277,117` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_title_generator.py@-997,0 +1277,117#moved` moved: `apps/desktop/src/components/assistant-ui/thread/timeline-position.ts`, `evals/completion_backlog_probe.py`, `evals/cron_error_diagnostics.py`, `evals/goal_command_parity.py` (+895 more)

### `tests/agent/test_turn_facade_lease.py`

- def test_interrupt_before_any_wait_does_not_blame_another_process():
  - `tests/agent/test_turn_facade_lease.py@-141,0 +142,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_turn_facade_lease.py@-141,0 +142,12#moved` moved: `hermes_cli/immutable_releases.py`, `tests/agent/test_413_compression.py`, `tests/agent/test_codex_app_server_persist.py`, `tests/agent/test_compaction_redaction_boundaries.py` (+112 more)
- def test_lock_tolerance_never_outlives_the_committed_row_expiry(tmp_path, monkey
  - `tests/agent/test_turn_facade_lease.py@-239,3 +251,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_turn_facade_lease.py@-248,4 +273,31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_turn_facade_lease.py@-248,4 +273,31#moved` moved: `agent/conversation_compression.py`, `apps/desktop/e2e/core/harness.ts`, `apps/desktop/e2e/core/interrupted-reply.spec.ts`, `apps/desktop/e2e/core/lineage-compaction-prompt.spec.ts` (+428 more)

### `tests/agent/test_vault_backend_browser.py`

- def test_fill_is_password_blind_and_redaction_registered(real_profile):
  - `tests/agent/test_vault_backend_browser.py@-115 +115,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/test_vault_backends.py`

- def test_unlock_uses_vendor_passwordenv_contract_then_fill_routes_by_prefix(fake
  - `tests/agent/test_vault_backends.py@-120 +120` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- if argv[:2] == ["item", "get"]:
  - `tests/agent/test_vault_backends.py@-250,0 +251,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_onepassword_service_account_reads_name_the_items_own_vault(fake_op_serv
  - `tests/agent/test_vault_backends.py@-271 +278,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_vault_backends.py@-271 +278,4#moved` moved: `agent/vault_backends/onepassword.py`
  - `tests/agent/test_vault_backends.py@-273 +283` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_vault_backends.py@-276 +286` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_vault_backends.py@-278,2 +288,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_vault_backends.py@-278,2 +288,4#moved` moved: `tests/agent/test_vault_onepassword_subprocess.py`
- def test_onepassword_service_account_refusals_name_the_real_cause(fake_op_servic
  - `tests/agent/test_vault_backends.py@-284,0 +297,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_vault_backends.py@-284,0 +297,3#moved` moved: `agent/vault_backends/onepassword.py`
  - `tests/agent/test_vault_backends.py@-286 +301` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/agent/test_worker_outbox_adoption.py`

- from hermes_state import SessionDB
  - `tests/agent/test_worker_outbox_adoption.py@-7,2 +7,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/agent/test_worker_outbox_adoption.py@-7,2 +7,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/conftest.py`

- def _close_leaked_session_dbs():
  - `tests/conftest.py@-552,6 +667` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/conftest.py@-568,0 +679,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/cron/test_cron_recovery.py`

- from hermes_state import SessionDB
  - `tests/cron/test_cron_recovery.py@-12 +12,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/cron/test_quota_hold.py`

- from datetime import datetime, timedelta, timezone, UTC
  - `tests/cron/test_quota_hold.py@-12 +12#moved` moved: `evals/completion_backlog_probe.py`, `evals/cron_error_diagnostics.py`, `evals/heartbeat_idle_wire.py`, `evals/postmortem/review_probes/context_cap_probe.py` (+828 more)

### `tests/cron/test_scheduler.py`

- class TestParallelTick:
  - `tests/cron/test_scheduler.py@-1794,0 +1795,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_scheduler.py@-1794,0 +1795,16#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)
- class TestBuildJobPromptExtraPrompt:
  - `tests/cron/test_scheduler.py@-2618,0 +2635,50` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_scheduler.py@-2618,0 +2635,50#moved` moved: `tests/agent/test_bot_chat_toolset_refresh.py`, `tests/agent/test_cross_process_turn_lease.py`, `tests/agent/test_system_prompt_restore.py`, `tests/agent/test_title_generator.py` (+20 more)
- def _run_owned_job(job, tmp_path, db=None):
  - `tests/cron/test_scheduler.py@-75,0 +62,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/cron/test_sessiondb_init_hang.py`

- (module)
  - `tests/cron/test_sessiondb_init_hang.py@-1 +1` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_owner_store_failure_never_runs_unpersisted_agent(owner_run, failure):
  - `tests/cron/test_sessiondb_init_hang.py@-109,0 +217,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-109,0 +217,2#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_cron_incidents.py`, `tests/cron/test_cron_incidents_resolve.py`, `tests/cron/test_cron_kanban_env_isolation.py` (+32 more)
  - `tests/cron/test_sessiondb_init_hang.py@-111,4 +220,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-111,4 +220,4#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_fire_claim_lost_after_delivery.py`, `tests/cron/test_inflight_stale_guard.py`, `tests/cron/test_parallel_pool.py` (+5 more)
- def test_guard_is_released_and_job_refires_after_sessiondb_hang(owner_run, monke
  - `tests/cron/test_sessiondb_init_hang.py@-116,4 +225,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-116,4 +225,11#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9666 more)
  - `tests/cron/test_sessiondb_init_hang.py@-121,5 +237,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-132,8 +241,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-132,8 +241,5#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_cron_incidents.py`, `tests/cron/test_cron_mcp_toolset_empty_block.py`, `tests/cron/test_cron_pinned_job_fallback.py` (+7 more)
  - `tests/cron/test_sessiondb_init_hang.py@-142,3 +248,75` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-142,3 +248,75#moved` moved: `apps/desktop/e2e/core/lineage-compaction-prompt.spec.ts`, `cron/scheduler_session_store.py`, `gateway/outbox.py`, `hermes_cli/goals.py` (+234 more)
  - `tests/cron/test_sessiondb_init_hang.py@-145,0 +324` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-147,6 +326,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-147,6 +326,12#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10183 more)
- import pytest
  - `tests/cron/test_sessiondb_init_hang.py@-15,2 +30,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-15,2 +30,12#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9676 more)
- def test_guard_is_released_and_job_refires_after_sessiondb_hang(owner_run, monke
  - `tests/cron/test_sessiondb_init_hang.py@-155 +340` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-158,9 +343,62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-158,9 +343,62#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9715 more)
- from gateway import session_cron
  - `tests/cron/test_sessiondb_init_hang.py@-19,19 +44,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- (module)
  - `tests/cron/test_sessiondb_init_hang.py@-3,2 +3,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def owner_run(tmp_path, monkeypatch):
  - `tests/cron/test_sessiondb_init_hang.py@-39,14 +49,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-39,14 +49,3#moved` moved: `tests/cron/test_script_context_propagation.py`
- def test_sessiondb_init_preserves_multiplex_profile_context(owner_run, tmp_path)
  - `tests/cron/test_sessiondb_init_hang.py@-54,2 +53#moved` moved: `gateway/session_cron.py`
  - `tests/cron/test_sessiondb_init_hang.py@-63,6 +58,122` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-63,6 +58,122#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10473 more)
- def test_run_job_does_not_hang_when_sessiondb_init_wedges(owner_run, tmp_path, m
  - `tests/cron/test_sessiondb_init_hang.py@-70,3 +181,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-70,3 +181,6#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10046 more)
  - `tests/cron/test_sessiondb_init_hang.py@-74,4 +188,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-74,4 +188,17#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2081 more)
  - `tests/cron/test_sessiondb_init_hang.py@-79,16 +206,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/cron/test_sessiondb_init_hang.py@-79,16 +206,5#moved` moved: `tests/cron/test_cron_empty_payload.py`, `tests/cron/test_cron_kanban_env_isolation.py`, `tests/cron/test_cron_mcp_toolset_empty_block.py`, `tests/cron/test_cron_pinned_job_fallback.py` (+12 more)
  - `tests/cron/test_sessiondb_init_hang.py@-97,12 +213,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/fakes/cron_owner.py`

- from hermes_state import SessionDB
  - `tests/fakes/cron_owner.py@-15 +15#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/conftest.py`

- import ast
  - `tests/gateway/conftest.py@-34,0 +35#moved` moved: `optional-skills/blockchain/hyperliquid/scripts/hyperliquid_client.py`, `optional-skills/research/osint-investigation/scripts/timing_analysis.py`, `plugins/platforms/discord/adapter.py`, `plugins/platforms/discord/recovery.py` (+6 more)
- import sys
  - `tests/gateway/conftest.py@-36,0 +38#moved` moved: `agent/incognito_agent.py`, `agent/memory_provider.py`, `agent/title_generator.py`, `gateway/incognito.py` (+20 more)
- import pytest
  - `tests/gateway/conftest.py@-42,0 +45,31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/conftest.py@-42,0 +45,31#moved` moved: `acp_adapter/entry.py`, `agent/anthropic_message_convert.py`, `agent/legacy_cli.py`, `agent/managed_worker.py` (+41 more)
- def owner(tmp_path):
  - `tests/gateway/conftest.py@-698 +731#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def hosted_owner(tmp_path, monkeypatch):
  - `tests/gateway/conftest.py@-736 +769#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
  - `tests/gateway/conftest.py@-757 +790` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/conftest.py@-759 +792` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/fixtures/api_authority_peer.py`

- async def probe(peer):
  - `tests/gateway/fixtures/api_authority_peer.py@-35 +35#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/authority_controls_peer.py`

- async def probe(peer, target):
  - `tests/gateway/fixtures/authority_controls_peer.py@-56 +56#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/authority_events_peer.py`

- def main():
  - `tests/gateway/fixtures/authority_events_peer.py@-56 +56#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/authority_stream_peer.py`

- async def probe(peer, target):
  - `tests/gateway/fixtures/authority_stream_peer.py@-110 +110#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/control_boundaries.py`

- async def exercise_control_boundaries(tmp_path, monkeypatch):
  - `tests/gateway/fixtures/control_boundaries.py@-15,2 +15,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/control_boundaries.py@-15,2 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/fixtures/control_boundaries.py@-45 +45,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/control_boundaries.py@-45 +45,5#moved` moved: `cron/scheduler_delivery.py`, `cron/scheduler_delivery_sessions.py`, `evals/delivery_flood_wire.py`, `evals/gateway_status_render/iteration_ceiling_ab.py` (+737 more)

### `tests/gateway/fixtures/native_ingress_media_peer.py`

- async def probe(mode, peer):
  - `tests/gateway/fixtures/native_ingress_media_peer.py@-22 +22#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/native_ingress_trust_peer.py`

- async def probe(mode, peer):
  - `tests/gateway/fixtures/native_ingress_trust_peer.py@-20 +20#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/native_role_peer.py`

- async def probe(peer):
  - `tests/gateway/fixtures/native_role_peer.py@-21 +21#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/fixtures/replay_gap_owner.py`

- class Owner:
  - `tests/gateway/fixtures/replay_gap_owner.py@-29 +28#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/fixtures/replay_gap_owner.py@-30,0 +30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/replay_gap_owner.py@-43,0 +44,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/replay_gap_owner.py@-63 +65,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/replay_gap_owner.py@-63 +65,4#moved` moved: `agent/acp_client.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py`, `agent/auxiliary_client.py` (+793 more)
  - `tests/gateway/fixtures/replay_gap_owner.py@-69,0 +74,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/fixtures/replay_gap_owner.py@-73,3 +82` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_admission_results.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_admission_results.py@-5,4 +5,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_admission_results.py@-5,4 +5,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_adversarial_responses_pending.py`

- async def test_unknown_retry_recovers_admission_after_pointer_commit_loss(api, o
  - `tests/gateway/test_adversarial_responses_pending.py@-58 +58,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_adversarial_responses_pending.py@-58 +58,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_adversarial_settlement_review.py`

- from gateway.session_contract import Submission
  - `tests/gateway/test_adversarial_settlement_review.py@-9 +9,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_api_admission_off_loop.py`

- async def test_admission_waits_for_the_writer_off_the_owner_loop(api, owner, mon
  - `tests/gateway/test_api_admission_off_loop.py@-42,2 +70` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_api_authority_store.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_api_authority_store.py@-12 +12#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_api_media_profile_scope.py`

- async def test_restart_recovery_keeps_every_profiles_held_images_and_collects_ea
  - `tests/gateway/test_api_media_profile_scope.py@-38 +38,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_api_media_profile_scope.py@-38 +38,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_api_unknown_resolution.py`

- from gateway.session import SessionStore
  - `tests/gateway/test_api_unknown_resolution.py@-15 +15#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/test_authority_approval_projection.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_approval_projection.py@-16 +16#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_busy_preempt_parity.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_authority_busy_preempt_parity.py@-10 +10,2#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+277 more)
- async def test_busy_voice_note_interrupts_at_once_in_interrupt_mode(tmp_path, mo
  - `tests/gateway/test_authority_busy_preempt_parity.py@-107,0 +109,356` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_authority_busy_preempt_parity.py@-107,0 +109,356#moved` moved: `tests/gateway/test_admission_adapter_reconnect_wait.py`, `tests/gateway/test_automation_runtime_retry.py`, `tests/gateway/test_automation_upgrade_admission.py`, `tests/gateway/test_goal_wait_durable_admission.py` (+13 more)
- class _RunningAgent:
  - `tests/gateway/test_authority_busy_preempt_parity.py@-20 +21#moved` moved: `hermes_cli/gateway_agent.py`, `tests/acp_adapter/test_acp_commands.py`, `tests/agent/test_subagent_lifecycle.py`, `tests/gateway/fixtures/subagent_controls_peer.py` (+9 more)

### `tests/gateway/test_authority_complete_persisted_turn.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_complete_persisted_turn.py@-17 +17#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_complete_reused_final.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_complete_reused_final.py@-14 +14#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_connection.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_connection.py@-12 +12#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_pause_recovery.py`

- from gateway.session_contract import SessionActor, SessionRef
  - `tests/gateway/test_authority_pause_recovery.py@-11,2 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_authority_pause_recovery.py@-11,2 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_principal.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_principal.py@-9 +9#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_automation_notification_category.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_automation_notification_category.py@-9 +10,2#moved` moved: `tests/cron/test_cron_recovery.py`, `tests/fakes/cron_owner.py`, `tests/gateway/conftest.py`, `tests/gateway/fixtures/supervisor_boundary_daemon.py` (+80 more)

### `tests/gateway/test_bot_mailbox_async.py`

- async def test_adopted_result_during_unknown_projection_is_not_lost(bot, monkeyp
  - `tests/gateway/test_bot_mailbox_async.py@-117,2 +117,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_bot_mailbox_async.py@-117,2 +117,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_corruption.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_bot_receipt_corruption.py@-11,5 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_bot_receipt_corruption.py@-11,5 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_recovery_logs.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_bot_receipt_recovery_logs.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_tombstone_expiry.py`

- re-read at every owner start would otherwise abort the gateway on every boot."""
  - `tests/gateway/test_bot_receipt_tombstone_expiry.py@-3,0 +4#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _receipt_then_deleted(tmp_path, *, status, days_ago, **fields):
  - `tests/gateway/test_bot_receipt_tombstone_expiry.py@-33 +34#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`

### `tests/gateway/test_busy_burst_serial_reader.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_busy_burst_serial_reader.py@-20 +20#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/test_busy_voice_note_transcribed_once.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_busy_voice_note_transcribed_once.py@-16 +17,2#moved` moved: `tests/cron/test_cron_recovery.py`, `tests/fakes/cron_owner.py`, `tests/gateway/conftest.py`, `tests/gateway/fixtures/supervisor_boundary_daemon.py` (+80 more)

### `tests/gateway/test_canonical_complete_path.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_canonical_complete_path.py@-16 +16#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_canonical_wire_contract.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_canonical_wire_contract.py@-10 +10#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_canonical_wire_contracts.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_canonical_wire_contracts.py@-9 +9#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_claim_reopen_off_loop.py`

- (module)
  - `tests/gateway/test_claim_reopen_off_loop.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_reopening_a_finalized_local_row_behind_a_contended_writer_keeps_t
  - `tests/gateway/test_claim_reopen_off_loop.py@-38 +39` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_completion_fold.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_completion_fold.py@-7 +7,2#moved` moved: `tests/cron/test_cron_recovery.py`, `tests/fakes/cron_owner.py`, `tests/gateway/conftest.py`, `tests/gateway/fixtures/supervisor_boundary_daemon.py` (+80 more)

### `tests/gateway/test_control_socket.py`

- def test_stop_removes_socket_and_pointer(home: Path):
  - `tests/gateway/test_control_socket.py@-201 +212,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _run(coro):
  - `tests/gateway/test_control_socket.py@-28,0 +30,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_control_socket.py@-28,0 +30,8#moved` moved: `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `tests/_fixtures/live_system_guard.py`, `tests/_fixtures/reply_latency.py`, `tests/acp_adapter/conftest.py` (+923 more)
- def test_home_refusing_sockets_listens_on_private_fallback_both_clients_reach(sh
  - `tests/gateway/test_control_socket.py@-305 +317,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_control_socket.py@-314 +327` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_collect_fleet_versions_falls_back_to_state_file(tmp_path: Path, monkeyp
  - `tests/gateway/test_control_socket.py@-446,0 +460#moved` moved: `tests/hermes_cli/test_fleet_matrix_down_state.py`
- def _serve(home: Path, handlers=None):
  - `tests/gateway/test_control_socket.py@-45,21 +54,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_runtime_inventory_prefers_socket_supervisor(tmp_path: Path, monkeypatch
  - `tests/gateway/test_control_socket.py@-556,0 +571,34` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_control_socket.py@-556,0 +571,34#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+5385 more)
- def test_long_home_uses_pointer_fallback(tmp_path: Path):
  - `tests/gateway/test_control_socket.py@-76,0 +72,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_control_socket.py@-76,0 +72,16#moved` moved: `evals/update_obligation_identity.py`, `tests/agent/test_corruption_recovery_guidance.py`, `tests/agent/test_generated_media_durable.py`, `tests/agent/test_memory_provider_caller_profile.py` (+123 more)
- from pathlib import Path
  - `tests/gateway/test_control_socket.py@-8,0 +9#moved` moved: `tests/acp_adapter/test_runtime_identity.py`, `tests/agent/test_callable_api_key.py`, `tests/agent/test_ssl_verify.py`, `tests/cron/test_cron_script.py` (+11 more)

### `tests/gateway/test_cron_owner_progress_stamp.py`

- def test_owner_progress_reaches_the_firers_ledger_row(tmp_path, monkeypatch):
  - `tests/gateway/test_cron_owner_progress_stamp.py@-26 +26,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)
- def test_owner_cron_run_binds_its_claim_fence(tmp_path, monkeypatch):
  - `tests/gateway/test_cron_owner_progress_stamp.py@-98 +99,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/gateway/test_delivery_ledger.py`

- class TestSchemaMigration:
  - `tests/gateway/test_delivery_ledger.py@-126,0 +136,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger.py@-126,0 +136,3#moved` moved: `tests/agent/test_compression_concurrent_fork.py`, `tests/agent/test_incognito.py`, `tests/gateway/test_base_topic_sessions.py`, `tests/gateway/test_run_progress_topics.py` (+13 more)
- class TestSweep:
  - `tests/gateway/test_delivery_ledger.py@-147,0 +160,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import time
  - `tests/gateway/test_delivery_ledger.py@-15,0 +16#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_unavailable.py`, `agent/billing_usage.py` (+220 more)
- class TestSweep:
  - `tests/gateway/test_delivery_ledger.py@-151,0 +175,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestRuntimeFailedSweep:
  - `tests/gateway/test_delivery_ledger.py@-296,0 +338,42` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger.py@-296,0 +338,42#moved` moved: `tests/agent/test_subagent_lifecycle.py`, `tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py`, `tests/gateway/test_reaction_outbound_identity.py`, `tests/gateway/test_telegram_send_path_recovery_sweep.py` (+2 more)
- class TestGatewayRedeliverySweep:
  - `tests/gateway/test_delivery_ledger.py@-413,0 +497,128` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger.py@-413,0 +497,128#moved` moved: `acp_adapter/commands.py`, `agent/acp_client.py`, `agent/acp_openai_bridge.py`, `agent/agent_init.py` (+2701 more)
- def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
  - `tests/gateway/test_delivery_ledger.py@-44,0 +46,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestGatewayRedeliverySweep:
  - `tests/gateway/test_delivery_ledger.py@-557 +768` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestOwnerAlivePidProbe:
  - `tests/gateway/test_delivery_ledger.py@-810,0 +1022,315` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger.py@-810,0 +1022,315#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+10095 more)
- def _orphan(oid):
  - `tests/gateway/test_delivery_ledger.py@-92 +101` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_delivery_ledger_fd_leak.py`

- class _TrackingConnection:
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-19 +19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-21,3 +21,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-31,15 +26,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _track_connections(monkeypatch):
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-57,0 +40,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-58,0 +43` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_headless_completion_io.py`

- (module)
  - `tests/gateway/test_headless_completion_io.py@-0,0 +1,207` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_heartbeat_inflight_owner_scan.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_heartbeat_inflight_owner_scan.py@-12 +12,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_synthetic_provenance_retry.py`

### `tests/gateway/test_hosted_document_custody.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_document_custody.py@-7 +7,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_hosted_room_attempt_home_moves.py`

- from gateway.session import SessionStore
  - `tests/gateway/test_hosted_room_attempt_home_moves.py@-15 +15#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_room_attempt_home_moves.py@-20,3 +20,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_hosted_room_attempt_home_moves.py@-20,3 +20,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_room_unserved_member_stop.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_room_unserved_member_stop.py@-15 +15#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_rooms.py`

- def test_room_log_pages_are_bounded_by_serialized_event_bytes(tmp_path, monkeypa
  - `tests/gateway/test_hosted_rooms.py@-738,5 +736,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_hosted_rooms_gateway_import.py`

- def test_gateway_startup_takes_over_the_standalone_rooms(tmp_path, monkeypatch):
  - `tests/gateway/test_hosted_rooms_gateway_import.py@-109 +109#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_stop_executor_recovery.py`

- def test_recovered_stop_does_not_discard_a_different_canonical_attempt(mux, monk
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-168 +168,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-168 +168,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cross_profile_stopping_recovery(mux, admission_state):
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-59,3 +59,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-59,3 +59,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_recovered_group_loads_and_answers_the_next_message_without_replaying_ol
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-97 +96,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py`

- def test_live_agent_compaction_still_rebuilds():
  - `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py@-61,0 +64,29` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py@-61,0 +64,29#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10286 more)

### `tests/gateway/test_kanban_finite_admission.py`

- def _lose_across_restart(authority, sid):
  - `tests/gateway/test_kanban_finite_admission.py@-215,2 +215,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_kanban_finite_admission.py@-215,2 +215,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_legacy_desktop_source.py`

- async def test_editing_ones_own_branch_leaves_another_logins_parent_alone(tmp_pa
  - `tests/gateway/test_legacy_desktop_source.py@-130 +130#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_renaming_ones_own_branch_leaves_another_logins_parent_title_alone
  - `tests/gateway/test_legacy_desktop_source.py@-175 +175#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_receipt_untracked_compression.py`

- async def _owner(tmp_path, monkeypatch, instance_id):
  - `tests/gateway/test_local_receipt_untracked_compression.py@-23 +23#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- def _unrecovered_owner(tmp_path, instance_id):
  - `tests/gateway/test_local_receipt_untracked_compression.py@-66 +66#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_session_recovery.py`

- async def test_branch_and_local_entries_keep_the_routing_prune_sweep_running(tmp
  - `tests/gateway/test_local_session_recovery.py@-117 +117#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- async def test_local_create_receipt_survives_cold_authority_atomically(tmp_path,
  - `tests/gateway/test_local_session_recovery.py@-12 +12#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- async def test_delete_retires_owner_receipts_and_never_resurrects_the_id(tmp_pat
  - `tests/gateway/test_local_session_recovery.py@-153 +153#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- def test_messaging_session_history_follows_the_compression_tip(tmp_path, monkeyp
  - `tests/gateway/test_local_session_recovery.py@-83 +83#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_viewer_events_submit.py`

- def test_a_classic_cli_receipt_carries_the_run_conversation_keys(classic):
  - `tests/gateway/test_local_viewer_events_submit.py@-207 +219,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_cli_turn_fields_are_admitted_and_bound_to_their_own_turn(tmp_path
  - `tests/gateway/test_local_viewer_events_submit.py@-22,0 +23#moved` moved: `gateway/reply_latency.py`, `gateway/run_background_admission.py`, `gateway/run_busy.py`, `gateway/run_goals.py` (+5 more)
  - `tests/gateway/test_local_viewer_events_submit.py@-27 +28,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_local_viewer_events_submit.py@-35 +37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_local_viewer_events_submit.py@-55 +57` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_local_viewer_events_submit.py@-59 +61` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_local_viewer_events_submit.py@-63,0 +66,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_local_viewer_events_submit.py@-63,0 +66,10#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)

### `tests/gateway/test_lost_worker_receipt_compaction.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_lost_worker_receipt_compaction.py@-13,4 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_lost_worker_receipt_compaction.py@-13,4 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_managed_worker_drain.py`

- async def test_reserved_worker_persists_while_runtime_drains(tmp_path):
  - `tests/gateway/test_managed_worker_drain.py@-16 +16,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_managed_worker_fencing.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_fencing.py@-13 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_managed_worker_finished_exit.py`

- def test_finished_worker_keeps_its_result_on_slow_or_nonzero_exit(tmp_path, monk
  - `tests/gateway/test_managed_worker_finished_exit.py@-44 +44,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_hard_stop_frame_fence.py`

- def test_worker_frames_read_no_ledger_per_frame(tmp_path, monkeypatch):
  - `tests/gateway/test_managed_worker_hard_stop_frame_fence.py@-91 +95,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_launch.py`

- from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
  - `tests/gateway/test_managed_worker_launch.py@-16 +16,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_managed_worker_launch.py@-16 +16,17#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `tests/gateway/test_managed_worker_stop_before_hello.py`, `tests/tui_gateway/test_survivor_notifications.py`
- def test_ordinary_owner_launches_tool_worker_and_detach_does_not_cancel(tmp_path
  - `tests/gateway/test_managed_worker_launch.py@-251,2 +267` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_managed_worker_reservation_off_loop.py`

- def test_reservation_and_loss_writes_never_block_the_loop(tmp_path, monkeypatch,
  - `tests/gateway/test_managed_worker_reservation_off_loop.py@-47 +47,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_stop_before_hello.py`

- def test_stop_acknowledged_during_spawn_never_bootstraps_the_worker(tmp_path, mo
  - `tests/gateway/test_managed_worker_stop_before_hello.py@-82 +81,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_tool_events.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_tool_events.py@-11 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_managed_worker_trampoline.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_trampoline.py@-23 +23,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_managed_worker_unknown_status.py`

- def _authority_with_a_lost_worker(tmp_path, monkeypatch):
  - `tests/gateway/test_managed_worker_unknown_status.py@-31 +31,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_model_confirmation_clients.py`

- class _Owner:
  - `tests/gateway/test_model_confirmation_clients.py@-41 +42` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- ask, re-send once with the token on yes, and leave the session untouched on no.
  - `tests/gateway/test_model_confirmation_clients.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_mutation_prepare_off_loop.py`

- (module)
  - `tests/gateway/test_mutation_prepare_off_loop.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_model_prepare_waiting_on_a_contended_writer_keeps_the_loop_live(t
  - `tests/gateway/test_mutation_prepare_off_loop.py@-37 +38` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_native_fifo_context_exhaustion.py`

- (module)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-1,4 +1,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-1,4 +1,4#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+9394 more)
- async def test_exhaustion_reset_is_ordered_with_redelivery_and_cleanup(native, m
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-107,68 +111,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-107,68 +111,7#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
- from gateway.run import GatewayRunner
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-15,2 +16,2#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+277 more)
- async def test_no_scheduler_starts_the_fresh_fifo_before_reset_cleanup_finishes(
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-182,5 +123,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-188 +128,33` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-188 +128,33#moved` moved: `agent/monitoring/emitter.py`, `evals/heartbeat_idle_wire.py`, `evals/postmortem/review_probes/deadline_probe.py`, `hermes_cli/win_pty_bridge.py` (+278 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-193,17 +165,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-213,4 +179` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from plugins.platforms.discord.adapter import DiscordAdapter
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-22 +23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_admission_reserved_on_the_old_route_commits_on_the_fresh_session(
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-222,2 +184,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-222,2 +184,3#moved` moved: `hermes_cli/status.py`, `tests/acp_adapter/test_gateway_catalog_offline.py`, `tests/agent/test_model_metadata.py`, `tests/agent/test_preflight_compression_pool_saturation.py` (+8 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-227 +190,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-233,7 +197#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-246,5 +203` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-252 +205` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-256,2 +209,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-261,4 +217,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-268,3 +225` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_exhaustion_reset_keeps_the_adapter_queue_for_the_fresh_session(na
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-273,2 +228` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-280 +234` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-282 +236` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-284,0 +239` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def native(monkeypatch):
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-44,0 +46,2#moved` moved: `tests/gateway/test_attended_session_opening.py`, `tests/gateway/test_compression_exhaustion_keeps_context.py`, `tests/gateway/test_internal_event_pin_wiring.py`, `tests/gateway/test_loop_thread_statedb_sweep.py` (+1 more)
- async def _admit(native, text, event=None):
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-54,2 +57` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_exhaustion_reset_runs_queued_native_messages_on_the_fresh_session
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-56,0 +59,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-56,0 +59,20#moved` moved: `tests/agent/test_codex_stream_supersession.py`, `tests/agent/test_moa_aggregator_cost_slot.py`, `tests/gateway/test_compression_exhaustion_keeps_context.py`
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-59,2 +81,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-59,2 +81,2#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
- from types import SimpleNamespace
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-6,0 +7#moved` moved: `agent/client_lifecycle.py`, `agent/turn_api_call.py`, `tests/agent/lsp/test_client_e2e.py`, `tests/agent/lsp/test_mock_pid_publication.py` (+159 more)
- async def test_exhaustion_reset_runs_queued_native_messages_on_the_fresh_session
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-67,6 +88,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-74 +91` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-78,14 +95,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-99,5 +108` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_native_media_budget.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_budget.py@-9 +9,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_headless_completion_io.py`, `tests/gateway/test_process_notice_committed_source.py`, `tests/gateway/test_synthetic_native_admission.py` (+2 more)

### `tests/gateway/test_native_media_history_retention.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_history_retention.py@-11 +12,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_native_media_ownership.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_ownership.py@-9 +9,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_native_operator_provenance.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_operator_provenance.py@-14,8 +14,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_operator_provenance.py@-14,8 +14,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_native_recovery_viewer_queue.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_recovery_viewer_queue.py@-14,7 +14,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_native_recovery_viewer_queue.py@-14,7 +14,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_offloop_write_retirement_join.py`

- def _owner(tmp_path, monkeypatch):
  - `tests/gateway/test_offloop_write_retirement_join.py@-36 +37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- takes the writer (another process holding state.db) until after the join or the
  - `tests/gateway/test_offloop_write_retirement_join.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_owner_loop_claim_read.py`

- async def test_claim_pending_read_keeps_the_owner_loop_ticking(tmp_path, monkeyp
  - `tests/gateway/test_owner_loop_claim_read.py@-25,0 +26#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/test_owner_loop_claim_read.py@-29 +30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_owner_loop_writes_class.py`

- async def _initialize(authority, owner, tmp_path, db):
  - `tests/gateway/test_owner_loop_writes_class.py@-121 +122#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- async def _first_read_adoption(authority, owner, tmp_path, db):
  - `tests/gateway/test_owner_loop_writes_class.py@-153 +154` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _owner(tmp_path, monkeypatch, handle):
  - `tests/gateway/test_owner_loop_writes_class.py@-33 +34` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- stall itself is shown by test_mutation_prepare_off_loop / test_claim_reopen_off_
  - `tests/gateway/test_owner_loop_writes_class.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_owner_loop_writes_cron_create.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_owner_loop_writes_cron_create.py@-15 +15,2#moved` moved: `gateway/run_checkins.py`, `gateway/session_pause_announce.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/agent/test_incognito_main_sinks.py` (+72 more)

### `tests/gateway/test_plugin_gateway_session_parity.py`

- async def test_keyless_injection_from_a_cli_session_turn_lands_in_that_session(t
  - `tests/gateway/test_plugin_gateway_session_parity.py@-25 +25,2#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)
- async def test_keyed_local_injection_stays_inside_the_plugins_own_profile(tmp_pa
  - `tests/gateway/test_plugin_gateway_session_parity.py@-76 +77#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_profile_retire_stops_turns.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_profile_retire_stops_turns.py@-17 +17,2#moved` moved: `gateway/run_checkins.py`, `gateway/session_pause_announce.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/agent/test_incognito_main_sinks.py` (+72 more)

### `tests/gateway/test_prompt_attachments.py`

- (module)
  - `tests/gateway/test_prompt_attachments.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_discarding_a_lost_image_turn_releases_its_committed_bytes(tmp_pat
  - `tests/gateway/test_prompt_attachments.py@-156 +161` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_prompt_attachments.py@-159,0 +165,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_prompt_attachments.py@-159,0 +165,2#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`
- async def _authority(tmp_path, monkeypatch, answer):
  - `tests/gateway/test_prompt_attachments.py@-16 +17,2#moved` moved: `gateway/session_api.py`, `gateway/session_automation.py`, `gateway/session_history_mount.py`, `gateway/session_local_recovery.py` (+247 more)
- async def test_staged_attachment_reaches_message_event_media(tmp_path, monkeypat
  - `tests/gateway/test_prompt_attachments.py@-70 +72,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_prompt_attachments.py@-70 +72,4#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_prompt_queued_controls.py`

- from gateway.session_authority import LiveSession, SessionAuthority
  - `tests/gateway/test_prompt_queued_controls.py@-11,4 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_prompt_queued_controls.py@-11,4 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_prompt_queued_transfer.py`

- from gateway.session_contract import SessionRef
  - `tests/gateway/test_prompt_queued_transfer.py@-15,4 +15,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_prompt_queued_transfer.py@-15,4 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_reset_tip_unknown_barrier.py`

- def _unknown_after_reset(tmp_path, runner):
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-24 +25` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-29 +30#moved` moved: `tests/hermes_state/test_worker_lineage_exclusion.py`
- free and silently re-run a turn whose canonical admission is ``unknown``."""
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-4,0 +5#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_restart_interrupt_notice.py`

- async def test_user_stop_is_not_relabelled_when_shutdown_starts_during_settlemen
  - `tests/gateway/test_restart_interrupt_notice.py@-100 +100,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_restart_interrupt_notice.py@-100 +100,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_managed_worker_turn_settles_with_the_cause_of_the_stop_that_reached_it(
  - `tests/gateway/test_restart_interrupt_notice.py@-184 +185,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)
- def _settle_on_real_store(tmp_path, cause, principal='person', payload=None):
  - `tests/gateway/test_restart_interrupt_notice.py@-339 +342,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_restart_interrupt_notice.py@-339 +342,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def _core_closed_turn(tmp_path, monkeypatch, *, principal='person', earlier_fail
  - `tests/gateway/test_restart_interrupt_notice.py@-415 +419,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_restart_interrupt_notice.py@-415 +419,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_shutdown_stop_before_the_managed_worker_spawned_carries_its_cause(tmp_p
  - `tests/gateway/test_restart_interrupt_notice.py@-484 +489,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)
- def test_a_stop_during_context_reference_expansion_never_spawns_the_worker(tmp_p
  - `tests/gateway/test_restart_interrupt_notice.py@-548 +554,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_restart_wait_prompt_reach.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_restart_wait_prompt_reach.py@-12 +12,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_headless_completion_io.py`, `tests/gateway/test_process_notice_committed_source.py`, `tests/gateway/test_synthetic_native_admission.py` (+2 more)

### `tests/gateway/test_resume_latest_local_cli.py`

- def _owner(home, monkeypatch):
  - `tests/gateway/test_resume_latest_local_cli.py@-35 +36` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- the gateway had created.
  - `tests/gateway/test_resume_latest_local_cli.py@-8,0 +9#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_retained_response_chain_after_delete.py`

- async def test_a_deleted_chain_continues_on_a_new_generation(api, owner, path, c
  - `tests/gateway/test_retained_response_chain_after_delete.py@-26 +26#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_retry_replacement.py`

- def test_rewind_session_retry_text_is_the_stored_bytes_for_multipart_carriers(
  - `tests/gateway/test_retry_replacement.py@-120 +120#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`

### `tests/gateway/test_room_work_service_capture.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_room_work_service_capture.py@-9 +9#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_run_runtime_hot_serve_cleanup.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_run_runtime_hot_serve_cleanup.py@-14 +16,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/gateway/test_runner_startup_failures.py`

- (module)
  - `tests/gateway/test_runner_startup_failures.py@-0,0 +1,2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2202 more)
- from gateway.status import read_runtime_status
  - `tests/gateway/test_runner_startup_failures.py@-12 +14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_start_gateway_verbosity_imports_redacting_formatter(monkeypatch,
  - `tests/gateway/test_runner_startup_failures.py@-128,2 +133,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_runner_startup_failures.py@-145,0 +151,46` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_runner_startup_failures.py@-145,0 +151,46#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+4363 more)
- def _host_attach_gate_is_not_under_test(monkeypatch):
  - `tests/gateway/test_runner_startup_failures.py@-18,0 +21,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_runner_startup_failures.py@-18,0 +21,3#moved` moved: `tests/gateway/test_auth_fallback.py`, `tests/gateway/test_busy_steer_origin.py`, `tests/gateway/test_busy_steer_sender_label.py`, `tests/gateway/test_busy_steer_subagent_fanout.py` (+10 more)
- async def test_start_gateway_replace_writes_takeover_marker_before_sigterm(
  - `tests/gateway/test_runner_startup_failures.py@-275,2 +326,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_start_gateway_propagates_fatal_config_exit_code(monkeypatch, tmp_
  - `tests/gateway/test_runner_startup_failures.py@-493,0 +545,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_runner_startup_failures.py@-493,0 +545,3#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/session_incognito_boundary.py`, `gateway/telegram_topic_sessions.py`, `hermes_cli/config_migrations_alias_routing.py` (+185 more)
  - `tests/gateway/test_runner_startup_failures.py@-500 +553#moved` moved: `cron/scheduler_bookkeeping.py`, `hermes_cli/gateway_agent.py`, `tests/agent/test_background_review_usage.py`, `tests/hermes_cli/test_handoff_slow_dispatch_timeout.py`
  - `tests/gateway/test_runner_startup_failures.py@-513,2 +566,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_token_lock_plus_retryable_peer_stays_alive(monkeypatch, tmp_path)
  - `tests/gateway/test_runner_startup_failures.py@-640,0 +694,203` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_runner_startup_failures.py@-640,0 +694,203#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3740 more)

### `tests/gateway/test_session_authority_cancel_settlement.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_authority_cancel_settlement.py@-10 +10,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_headless_completion_io.py`, `tests/gateway/test_process_notice_committed_source.py`, `tests/gateway/test_synthetic_native_admission.py` (+2 more)

### `tests/gateway/test_session_bot_state.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_bot_state.py@-13 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_session_create_tier_cwd.py`

- def _owner(home, monkeypatch, config):
  - `tests/gateway/test_session_create_tier_cwd.py@-31 +32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- NAMED profile's own ``terminal.cwd``). The owner freezes both into the LocalSess
  - `tests/gateway/test_session_create_tier_cwd.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_cron.py`

- async def test_owner_execution_binds_the_scheduler_cron_identity_inside_run_job(
  - `tests/gateway/test_session_cron.py@-104 +277,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_cron.py@-104 +277,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
  - `tests/gateway/test_session_cron.py@-15 +54,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+80 more)
- def test_draining_owner_still_answers_status_and_cancel_of_admitted_cron_work(tm
  - `tests/gateway/test_session_cron.py@-150,2 +324,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_cron.py@-150,2 +324,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
  - `tests/gateway/test_session_cron.py@-46,0 +87,132` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_cron.py@-46,0 +87,132#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+11745 more)
- async def test_cron_cancel_between_claim_and_execution_registration_stops_the_jo
  - `tests/gateway/test_session_cron.py@-57 +229,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_cron.py@-57 +229,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- import pytest
  - `tests/gateway/test_session_cron.py@-7,0 +10,37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_cron.py@-7,0 +10,37#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+14878 more)

### `tests/gateway/test_session_db_recovery.py`

- def test_recovered_db_rows_survive_fallback_structural_save(monkeypatch, tmp_pat
  - `tests/gateway/test_session_db_recovery.py@-333,0 +334,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_session_events_overflow.py`

- async def test_overflowed_subscriber_is_detached_and_told_to_resume(tmp_path):
  - `tests/gateway/test_session_events_overflow.py@-33 +33#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_follow_profile_config.py`

- def _owner(home):
  - `tests/gateway/test_session_follow_profile_config.py@-21,0 +21#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway/session_time_persistence_controls.py`, `evals/gateway_completion/delivery_lifecycle.py`, `evals/gateway_failure_ownership/probe.py` (+664 more)
  - `tests/gateway/test_session_follow_profile_config.py@-28,3 +28,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_follow_profile_config.py@-28,3 +28,9#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway/session_time_persistence_controls.py`, `evals/heartbeat_idle_wire.py`, `evals/token_accounting/display_provenance.py` (+403 more)
  - `tests/gateway/test_session_follow_profile_config.py@-33 +39` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- patched.
  - `tests/gateway/test_session_follow_profile_config.py@-9,2 +9#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_group_local_observation.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_group_local_observation.py@-13 +13,2#moved` moved: `gateway/run_checkins.py`, `gateway/session_pause_announce.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/agent/test_incognito_main_sinks.py` (+72 more)

### `tests/gateway/test_session_group_peers.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_group_peers.py@-25 +25#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_handoff_owner.py`

- async def test_handoff_waits_for_durable_work_the_owner_has_no_task_for(tmp_path
  - `tests/gateway/test_session_handoff_owner.py@-112,0 +113#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/test_session_handoff_owner.py@-122 +123` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_session_hosted_controls.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_hosted_controls.py@-8 +8,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+27 more)

### `tests/gateway/test_session_hosted_rpc.py`

- def test_unknown_info_is_inactive_with_hosted_not_canonical_generation(hosted_ow
  - `tests/gateway/test_session_hosted_rpc.py@-213 +214,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_discard_requires_exact_owned_unknown_tuple_without_replay(hosted_owner,
  - `tests/gateway/test_session_hosted_rpc.py@-233 +235,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hosted_rpc.py@-233 +235,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_local_hosted_member_revoked_during_preparation_is_not_admitted(hosted_o
  - `tests/gateway/test_session_hosted_rpc.py@-26,2 +26,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hosted_rpc.py@-26,2 +26,2#moved` moved: `tests/gateway/test_hosted_room_member_binding.py`, `tests/gateway/test_hosted_room_retired_admission.py`
- def test_room_binding_exact_retry_terminal_history_and_unknown(hosted_owner):
  - `tests/gateway/test_session_hosted_rpc.py@-96 +96,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hosted_rpc.py@-96 +96,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_hosted_service.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_hosted_service.py@-7 +7,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+80 more)

### `tests/gateway/test_session_hosted_transport.py`

- class _SourceTask:
  - `tests/gateway/test_session_hosted_transport.py@-104 +104#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_hygiene.py`

- async def test_hygiene_fence_cancel_records_cooldown_without_abort_flag(
  - `tests/gateway/test_session_hygiene.py@-1041 +1047` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_hygiene_skips_when_compression_already_in_flight(
  - `tests/gateway/test_session_hygiene.py@-1044 +1050,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene.py@-1071,2 +1078,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene.py@-1071,2 +1078,7#moved` moved: `tests/gateway/test_busy_relay_silence.py`, `tests/gateway/test_busy_wake_admission.py`, `tests/gateway/test_compression_deferred_retry.py`, `tests/gateway/test_compression_retry_lifecycle.py` (+10 more)
  - `tests/gateway/test_session_hygiene.py@-1074 +1086,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene.py@-1074 +1086,4#moved` moved: `tests/gateway/test_attended_busy_activity.py`, `tests/gateway/test_checkin_retry_disarm.py`, `tests/gateway/test_clarify_context_when_interim_off.py`, `tests/gateway/test_compression_deferred_retry.py` (+23 more)
- async def test_session_hygiene_preserves_transcript_when_no_rotation(monkeypatch
  - `tests/gateway/test_session_hygiene.py@-122,0 +123#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
  - `tests/gateway/test_session_hygiene.py@-156 +157` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_session_hygiene_preserves_transcript_when_in_place_configured_but
  - `tests/gateway/test_session_hygiene.py@-248,0 +250#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
  - `tests/gateway/test_session_hygiene.py@-282 +284` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_session_hygiene_timeout_continues_to_agent_and_sets_cooldown(monk
  - `tests/gateway/test_session_hygiene.py@-387,0 +390#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
- async def test_session_hygiene_forces_in_place_compaction_with_bound_session_db(
  - `tests/gateway/test_session_hygiene.py@-549,0 +553#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
- async def test_session_hygiene_honors_configurable_hard_message_limit(
  - `tests/gateway/test_session_hygiene.py@-682,0 +687#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
- def _make_cooldown_runner(monkeypatch, tmp_path, agent_cls, session_db, session_
  - `tests/gateway/test_session_hygiene.py@-783,0 +789#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)

### `tests/gateway/test_session_hygiene_turnhold_adoption.py`

- def _install_fakes(monkeypatch, gateway_run, tmp_path, agent_cls):
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-152,0 +154,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-152,0 +154,17#moved` moved: `agent/auxiliary_client.py`, `agent/chat_completion_helpers.py`, `agent/command_token_source.py`, `agent/gemini_native_adapter.py` (+330 more)
- async def _drain_deferred(runner, timeout=10.0):
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-154,5 +172,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_turn_hold_keeps_admission_and_adopts_watermark_fenced_summary(
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-172,0 +193` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-207 +228,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-234,3 +256,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-238 +260,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-241,0 +265,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-267,0 +294` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-273 +300` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-275,3 +302,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_turn_hold_kept_admission_arms_flat_retry_only_when_nothing_commit
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-358,0 +387,71` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-358,0 +387,71#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `agent/background_review.py`, `agent/checkpoints/undo_hint.py` (+205 more)
- async def test_turn_hold_without_watermark_fence_still_cancels(
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-431,0 +531,62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-431,0 +531,62#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `agent/background_review.py`, `agent/checkpoints/undo_hint.py` (+215 more)
- def _build_runner(gateway_run, adapter, fake_db):
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-91,0 +92#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)

### `tests/gateway/test_session_images.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_images.py@-12 +12#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_images_profile.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_images_profile.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_import_chain_ownership.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_import_chain_ownership.py@-10 +10#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_local_title.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_local_title.py@-12 +12,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+27 more)

### `tests/gateway/test_session_mutation_branch_boundary.py`

- async def test_branch_through_a_middle_message_excludes_later_and_concurrent_row
  - `tests/gateway/test_session_mutation_branch_boundary.py@-31 +32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- transcript inside the receipt transaction and part of the retry identity.
  - `tests/gateway/test_session_mutation_branch_boundary.py@-7,0 +8#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_mutation_compress_preview.py`

- async def test_canonical_compress_runs_the_live_agents_pre_compress_memory_hook(
  - `tests/gateway/test_session_mutation_compress_preview.py@-108 +119` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_mutation_compress_preview.py@-121,0 +133` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_input_is_admitted_while_compress_summarizes_but_not_claimed_until
  - `tests/gateway/test_session_mutation_compress_preview.py@-142 +154#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-154 +166` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_a_compress_that_removes_nothing_reports_mains_no_changes_without_
  - `tests/gateway/test_session_mutation_compress_preview.py@-198 +210#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-204 +216` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_no_op_compress_without_a_resident_agent_estimates_the_stored_prom
  - `tests/gateway/test_session_mutation_compress_preview.py@-241 +253#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-247 +259` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_preview_mutation_reports_without_summarizing_or_writing(tmp_path,
  - `tests/gateway/test_session_mutation_compress_preview.py@-38 +39#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-45 +46` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- surface uses must govern the payload, and a preview must not write a receipt, a
  - `tests/gateway/test_session_mutation_compress_preview.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_canonical_compress_runs_the_live_agents_pre_compress_memory_hook(
  - `tests/gateway/test_session_mutation_compress_preview.py@-90,2 +91,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_session_mutation_compress_preview.py@-90,2 +91,12#moved` moved: `acp_adapter/entry.py`, `acp_adapter/gateway_server.py`, `agent/agent_init.py`, `agent/agent_init_compression_notice.py` (+789 more)

### `tests/gateway/test_session_mutation_coordinator.py`

- (module)
  - `tests/gateway/test_session_mutation_coordinator.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_metadata_mutation_publishes_once_while_work_is_queued(tmp_path):
  - `tests/gateway/test_session_mutation_coordinator.py@-15 +16#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
- async def test_rewind_refuses_live_task_even_before_admission_claim(tmp_path):
  - `tests/gateway/test_session_mutation_coordinator.py@-41 +42#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/gateway/test_session_mutation_delete_fences.py`

- (module)
  - `tests/gateway/test_session_mutation_delete_fences.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_delete_cannot_remove_a_live_delegate_without_a_claim(tmp_path):
  - `tests/gateway/test_session_mutation_delete_fences.py@-15 +16#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/gateway/test_session_mutation_model.py`

- (module)
  - `tests/gateway/test_session_mutation_model.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_input_queued_while_a_model_receipt_commits_still_runs(tmp_path, m
  - `tests/gateway/test_session_mutation_model.py@-281 +285` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_guarded_model_target_commits_only_with_its_confirmation_token(tmp
  - `tests/gateway/test_session_mutation_model.py@-655 +659` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_large_context_guard_reads_the_persisted_anchor_with_no_resident_a
  - `tests/gateway/test_session_mutation_model.py@-721 +725` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_session_mutation_model_busy.py`

- (module)
  - `tests/gateway/test_session_mutation_model_busy.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _owner(tmp_path, monkeypatch, handle):
  - `tests/gateway/test_session_mutation_model_busy.py@-31 +32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_session_mutation_retirement.py`

- (module)
  - `tests/gateway/test_session_mutation_retirement.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_route_retirement_rolls_back_with_receipt_and_rejects_stale_mirror(tmp_p
  - `tests/gateway/test_session_mutation_retirement.py@-70 +71#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/gateway/test_session_mutation_retry_repair.py`

- (module)
  - `tests/gateway/test_session_mutation_retry_repair.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_exact_retry_repairs_runtime_after_post_commit_failure_without_sec
  - `tests/gateway/test_session_mutation_retry_repair.py@-18 +19#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
- async def test_rewind_replay_never_evicts_a_later_admissions_running_agent(tmp_p
  - `tests/gateway/test_session_mutation_retry_repair.py@-69 +70#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/gateway/test_session_mutation_rpc.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_mutation_rpc.py@-10 +10#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_policy_refusal_fields.py`

- async def test_the_frame_carries_the_field_and_the_gateway_logs_it(tmp_path, cap
  - `tests/gateway/test_session_policy_refusal_fields.py@-127 +127#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_a_bare_refusal_does_not_warn(tmp_path, caplog):
  - `tests/gateway/test_session_policy_refusal_fields.py@-160 +160#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_policy_verbs.py`

- def _owner(tmp_path, monkeypatch, handle):
  - `tests/gateway/test_session_policy_verbs.py@-32 +36` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- LocalSessionPolicy, so each is a revision-fenced ``session.mutate`` that rewrite
  - `tests/gateway/test_session_policy_verbs.py@-5,0 +6#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_shell.py`

- async def test_shell_exec_refusal_names_a_sandboxed_terminal_backend(tmp_path, m
  - `tests/gateway/test_session_shell.py@-50 +50#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_store_lock_p1.py`

- (module)
  - `tests/gateway/test_session_store_lock_p1.py@-0,0 +1,383` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_settlement_lock_liveness.py`

- async def test_submit_claim_and_discard_behind_a_held_writer_leave_the_loop_free
  - `tests/gateway/test_settlement_lock_liveness.py@-160 +160,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_settlement_lock_liveness.py@-160 +160,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/test_settlement_lock_liveness.py@-180,0 +182,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_settlement_lock_liveness.py@-180,0 +182,3#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_slack_replay_display_name.py`

- def _rows(authority):
  - `tests/gateway/test_slack_replay_display_name.py@-60,2 +62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/gateway/test_stop_claim_race_class.py`

- def _cron(hosted_owner, tmp_path, monkeypatch):
  - `tests/gateway/test_stop_claim_race_class.py@-43 +43,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/gateway/test_telegram_send_path_recovery_sweep.py`

- async def test_stopping_a_profile_mid_recovery_leaves_no_reply_stranded(tmp_path
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-132 +132#moved` moved: `evals/webhook_auth/standard_webhooks_ab.py`, `tests/acp_adapter/test_acp_reattach_unknown_notice.py`, `tests/acp_adapter/test_adversarial_unknown_follower.py`, `tests/acp_adapter/test_gateway_attach_mode_race.py` (+265 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-147,0 +148` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-161 +162,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-161 +162,2#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+2019 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-166 +168` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-172 +174#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway_completion/delivery_lifecycle.py`, `evals/heartbeat_idle_wire.py`, `tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py` (+36 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-174 +176,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-174 +176,2#moved` moved: `tests/gateway/test_attended_busy_activity.py`, `tests/gateway/test_auto_skill_title_input.py`, `tests/gateway/test_handoff_secondary_profile_adapter.py`, `tests/gateway/test_model_reset_all.py` (+5 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-177,0 +181,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-177,0 +181,2#moved` moved: `tests/gateway/test_autonomous_wake_pacing.py`
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-182,0 +188` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-184 +190` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-187 +193,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-187 +193,4#moved` moved: `tests/agent/test_gemini_long_decode.py`

### `tests/gateway/test_telegram_topic_profile_isolation.py`

- def test_legacy_rows_migrate_only_to_default(tmp_path: Path):
  - `tests/gateway/test_telegram_topic_profile_isolation.py@-62 +62#moved` moved: `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/core/test_telegram_icon_migration.py`

### `tests/gateway/test_turn_result_retention.py`

- from gateway.session_results import admission_result, finish_result
  - `tests/gateway/test_turn_result_retention.py@-29,2 +29,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_turn_result_retention.py@-29,2 +29,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_unknown_automation_settles.py`

- from gateway.session import SessionSource, SessionStore
  - `tests/gateway/test_unknown_automation_settles.py@-16 +16,2#moved` moved: `gateway/session_api.py`, `gateway/session_automation.py`, `gateway/session_history_mount.py`, `gateway/session_local_recovery.py` (+30 more)

### `tests/gateway/test_unknown_receipt_boundaries.py`

- from gateway.session_results import admission_result, finish_result
  - `tests/gateway/test_unknown_receipt_boundaries.py@-12,8 +12,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_unknown_receipt_boundaries.py@-12,8 +12,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_discard_closure_failure_keeps_unknown_fence_and_retry_is_exact(ow
  - `tests/gateway/test_unknown_receipt_boundaries.py@-61,0 +56,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_unknown_receipt_boundaries.py@-61,0 +56,2#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_unknown_resolution_physical_worker.py`

- (module)
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_resolve_unknown_retires_the_physical_worker_row(tmp_path):
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-50 +51#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-68 +69#moved` moved: `tests/hermes_state/test_worker_lineage_exclusion.py`

### `tests/gateway/test_unmanaged_idle_exit.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_unmanaged_idle_exit.py@-19 +19,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_unmanaged_idle_exit.py@-19 +19,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_unviewed_completion_resumes_parent.py`

- async def test_close_pending_across_a_restart_still_decides(tmp_path, monkeypatc
  - `tests/gateway/test_unviewed_completion_resumes_parent.py@-157 +157#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/test_webapp_owner_identity.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_webapp_owner_identity.py@-16 +16#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_webhook_durable_destination.py`

- async def test_fenced_webhook_retry_only_observes_existing_receipt(fence):
  - `tests/gateway/test_webhook_durable_destination.py@-176 +176#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_webhook_session_close.py`

- from gateway.run import GatewayRunner
  - `tests/gateway/test_webhook_session_close.py@-8 +8#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/gateway/test_worker_admission_reservation.py`

- async def test_input_queued_behind_a_live_worker_is_released_then_runs_when_it_f
  - `tests/gateway/test_worker_admission_reservation.py@-112 +114,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_worker_admission_reservation.py@-129 +132` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from hermes_state import SessionDB
  - `tests/gateway/test_worker_admission_reservation.py@-13,2 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_worker_admission_reservation.py@-13,2 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_input_queued_behind_a_live_worker_is_released_then_runs_when_it_f
  - `tests/gateway/test_worker_admission_reservation.py@-97,0 +98,2#moved` moved: `cron/scheduler_delivery_sessions.py`, `evals/delivery_flood_wire.py`, `evals/gateway_failure_ownership/probe.py`, `evals/heartbeat_idle_wire.py` (+606 more)

### `tests/gateway/test_worker_delegation_bridge.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_delegation_bridge.py@-18 +18,2#moved` moved: `tests/hermes_state/test_runtime_worker_operations.py`

### `tests/gateway/test_worker_delegation_receipts.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_delegation_receipts.py@-13,2 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/gateway/test_worker_delegation_receipts.py@-13,2 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_worker_register_restriction.py`

- async def test_worker_register_requires_the_owners_launched_worker(tmp_path, mon
  - `tests/gateway/test_worker_register_restriction.py@-22 +22,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+27 more)

### `tests/gateway/test_worker_reservation_automation_principal.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_reservation_automation_principal.py@-11 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/serve_handler_ledger.json`

- (module)
  - `tests/hermes_cli/serve_handler_ledger.json@-10 +10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/serve_handler_ledger.json@-401,0 +402,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_backup_stability.py`

- def test_failed_automatic_backup_preserves_previous_archive(tmp_path, monkeypatc
  - `tests/hermes_cli/test_backup_stability.py@-141,0 +142,89` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_backup_stability.py@-141,0 +142,89#moved` moved: `agent/acp_client.py`, `agent/curator.py`, `agent/redact.py`, `apps/desktop/e2e/update/seed.py` (+476 more)

### `tests/hermes_cli/test_cli_title_follows_compression.py`

- (module)
  - `tests/hermes_cli/test_cli_title_follows_compression.py@-0,0 +1,44` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_cli_title_follows_compression.py@-0,0 +1,44#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6923 more)

### `tests/hermes_cli/test_config_retired_key_migrations.py`

- _NOT_TRIGGERED_HERE = {
  - `tests/hermes_cli/test_config_retired_key_migrations.py@-147,0 +148,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_foreign_sessions.py`

- def gateway(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_foreign_sessions.py@-125 +125#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_imported_chat_resumes_writable_as_cli(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_foreign_sessions.py@-323 +323#moved` moved: `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_compressed_reopen.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py` (+225 more)

### `tests/hermes_cli/test_gateway_session_store.py`

- def test_rename_refusal_prints_mains_title_in_use_text(store, tmp_path, monkeypa
  - `tests/hermes_cli/test_gateway_session_store.py@-289,0 +290,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_gateway_session_store.py@-289,0 +290,28#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/control_socket.py`, `gateway/session_incognito_boundary.py`, `gateway/shutdown_watchdog.py` (+215 more)

### `tests/hermes_cli/test_goal_evidence_regressions.py`

- (module)
  - `tests/hermes_cli/test_goal_evidence_regressions.py@-0,0 +1,184` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_goal_evidence_regressions.py@-0,0 +1,184#moved` moved: `tests/hermes_cli/test_goal_adaptive.py`, `tests/hermes_cli/test_goal_capability_evidence.py`, `tests/hermes_cli/test_goal_judge_evidence.py`, `tests/hermes_cli/test_goal_round3_contract.py`

### `tests/hermes_cli/test_runtime_maintenance_exclusion.py`

- def test_recovery_output_exemption_excludes_canonical_activation(tmp_path):
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-179 +180#moved` moved: `evals/hosted_attachment_transfer/bench.py`, `gateway/run_bootstrap.py`, `gateway/run_runtime.py`, `gateway/status.py` (+54 more)
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-182 +183,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-188 +190#moved` moved: `tests/gateway/test_runtime_epoch_watch.py`, `tests/persistence_sqlite/core/test_runtime_epoch_foreign_owner.py`

### `tests/hermes_cli/test_session_controls.py`

- (module)
  - `tests/hermes_cli/test_session_controls.py@-0,0 +1,1534` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_session_controls.py@-0,0 +1,1534#moved` moved: `cron/scheduler_session_store.py`, `tests/cron/test_cron_named_dm_topic_owner_profile.py`, `tests/cron/test_script_context_propagation.py`, `tests/gateway/test_goal_continuation_identity.py` (+15 more)

### `tests/hermes_cli/test_session_mutation_http.py`

- from hermes_state import SessionDB
  - `tests/hermes_cli/test_session_mutation_http.py@-14 +14#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_session_mutation_snapshot.py`

- from hermes_state import SessionDB
  - `tests/hermes_cli/test_session_mutation_snapshot.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_session_mutation_standalone.py`

- from hermes_state import SessionDB
  - `tests/hermes_cli/test_session_mutation_standalone.py@-15 +15,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_synthetic_provenance_retry.py`

### `tests/hermes_cli/test_sessions_delete.py`

- (module)
  - `tests/hermes_cli/test_sessions_delete.py@-0,0 +1#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _run_prune(monkeypatch, capsys, argv_tail, candidates=None, skipped_open=0,
  - `tests/hermes_cli/test_sessions_delete.py@-129,0 +131,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_delete.py@-129,0 +131,2#moved` moved: `tests/cron/conftest.py`, `tests/gateway/test_housekeeping_profile_scope.py`, `tests/hermes_cli/test_sessions_failure_exit_codes.py`
- def test_sessions_delete_with_unfinished_turn_points_to_discard(tmp_path, monkey
  - `tests/hermes_cli/test_sessions_delete.py@-54 +55` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_delete.py@-58 +59` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_sessions_held_store_gate.py`

- def state_db(monkeypatch, tmp_path):
  - `tests/hermes_cli/test_sessions_held_store_gate.py@-37,0 +38,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_held_store_gate.py@-37,0 +38,2#moved` moved: `tests/cron/conftest.py`, `tests/gateway/test_housekeeping_profile_scope.py`, `tests/hermes_cli/test_sessions_failure_exit_codes.py`

### `tests/hermes_cli/test_sessions_prune_via_owner.py`

- def test_prune_under_the_live_owner_is_applied_by_the_owner_not_refused(owned_st
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-105 +108` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- async def test_owner_prune_is_local_operator_only(owned_store, grant):
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-137,0 +141,29` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-137,0 +141,29#moved` moved: `acp_adapter/provenance.py`, `acp_adapter/server.py`, `acp_adapter/tools.py`, `agent/account_usage.py` (+1428 more)
- def owned_store(monkeypatch, tmp_path):
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-52 +54#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
- as the gateway does; the owner end is the production ``AuthorityConnection`` dis
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-8,0 +9,2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2226 more)
- def test_prune_under_the_live_owner_is_applied_by_the_owner_not_refused(owned_st
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-90,0 +93` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_sessions_repair_prompts.py`

- def test_unpinned_row_is_unverifiable_and_apply_json_clears_only_verified(db, mo
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-63,0 +64,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-63,0 +64,9#moved` moved: `tests/hermes_cli/test_gateway_migrate_backends.py`, `tests/tools/test_computer_use_nodes.py`
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-69 +78` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-73,0 +83` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_update_channel_release_hold.py`

- def test_an_explicit_branch_to_a_pre_runtime_commit_is_still_refused(source, mon
  - `tests/hermes_cli/test_update_channel_release_hold.py@-136 +137` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- downgrade on every attempt. An explicit ``--branch`` to a pre-runtime commit is
  - `tests/hermes_cli/test_update_channel_release_hold.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _ledgered(home):
  - `tests/hermes_cli/test_update_channel_release_hold.py@-82 +83#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`

### `tests/hermes_cli/test_update_check_exit_reaps_git.py`

- _CHILD = textwrap.dedent('''
  - `tests/hermes_cli/test_update_check_exit_reaps_git.py@-49,2 +61,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_cli/test_update_check_exit_reaps_git.py@-52 +64,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_update_downgrade_guard.py`

- def _ledgered_store(home):
  - `tests/hermes_cli/test_update_downgrade_guard.py@-45 +46#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`
- names the pre-update snapshot that predates the cutover.
  - `tests/hermes_cli/test_update_downgrade_guard.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/hermes_cli/test_update_finish.py`

- def completion(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_update_finish.py@-47 +47` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_cli/test_web_server.py`

- def mutation_owner(monkeypatch, _isolate_hermes_home):
  - `tests/hermes_cli/test_web_server.py@-232 +232#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- class TestDeleteSessionEndpoint:
  - `tests/hermes_cli/test_web_server.py@-4170 +4170#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_web_server_sessiondb_eventloop.py`

- def test_session_rename_runs_writer_open_and_update_off_event_loop(monkeypatch,
  - `tests/hermes_cli/test_web_server_sessiondb_eventloop.py@-127 +127#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_web_server_sessions_compression.py`

- class TestDeleteSessionCompressionChain(_SessionsCompressionEndpoints):
  - `tests/hermes_cli/test_web_server_sessions_compression.py@-89 +89#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_webapp_socket_profile_scope.py`

- def gateway(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_webapp_socket_profile_scope.py@-58 +58#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_webapp_ticket_profile.py`

- def two_profiles(monkeypatch, tmp_path):
  - `tests/hermes_cli/test_webapp_ticket_profile.py@-24 +24#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_state/test_active_conversation_replay.py`

- (module)
  - `tests/hermes_state/test_active_conversation_replay.py@-0,0 +1,170#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+14391 more)

### `tests/hermes_state/test_adoption_indexed_lookup.py`

- (module)
  - `tests/hermes_state/test_adoption_indexed_lookup.py@-0,0 +1,218#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+18764 more)

### `tests/hermes_state/test_append_message_archive.py`

- (module)
  - `tests/hermes_state/test_append_message_archive.py@-0,0 +1,63#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+14683 more)

### `tests/hermes_state/test_append_messages_batch.py` → `tests/persistence_sqlite/conversation_store/test_append_messages_batch.py`

- class TestAppendMessagesBatch:
  - `tests/hermes_state/test_append_messages_batch.py@-136,5 +139,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_archive_compact_off_writer_lock.py`

- (module)
  - `tests/hermes_state/test_archive_compact_off_writer_lock.py@-0,0 +1,235#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+16881 more)

### `tests/hermes_state/test_auto_title_collisions.py`

- (module)
  - `tests/hermes_state/test_auto_title_collisions.py@-0,0 +1,233#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+17177 more)

### `tests/hermes_state/test_commit_tail_attribution.py`

- (module)
  - `tests/hermes_state/test_commit_tail_attribution.py@-0,0 +1,63#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+11156 more)

### `tests/hermes_state/test_compact_at_next_start.py`

- (module)
  - `tests/hermes_state/test_compact_at_next_start.py@-0,0 +1,278#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+16928 more)

### `tests/hermes_state/test_compaction_active_uid.py`

- (module)
  - `tests/hermes_state/test_compaction_active_uid.py@-0,0 +1,203#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+13035 more)

### `tests/hermes_state/test_compaction_insert_batches.py`

- (module)
  - `tests/hermes_state/test_compaction_insert_batches.py@-0,0 +1,91#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+11796 more)

### `tests/hermes_state/test_compaction_prepare_fallback.py`

- (module)
  - `tests/hermes_state/test_compaction_prepare_fallback.py@-0,0 +1,146#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+11634 more)

### `tests/hermes_state/test_compaction_prepare_snapshot.py`

- (module)
  - `tests/hermes_state/test_compaction_prepare_snapshot.py@-0,0 +1,169#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12257 more)

### `tests/hermes_state/test_compaction_write_metrics.py`

- (module)
  - `tests/hermes_state/test_compaction_write_metrics.py@-0,0 +1,192#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12303 more)

### `tests/hermes_state/test_compression_created_source.py` → `tests/persistence_sqlite/conversation_store/test_compression_created_source.py`

- def test_desktop_worker_heals_gui_to_desktop(tmp_path, child_created):
  - `tests/hermes_state/test_compression_created_source.py@-41 +42#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`
- is the write that fills the column, and it carries the surface to the NULL root.
  - `tests/hermes_state/test_compression_created_source.py@-5,0 +6#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/hermes_state/test_darwin_scan_bounds.py`

- (module)
  - `tests/hermes_state/test_darwin_scan_bounds.py@-0,0 +1,139#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+13361 more)

### `tests/hermes_state/test_db_identity_probe_off_lock.py`

- (module)
  - `tests/hermes_state/test_db_identity_probe_off_lock.py@-0,0 +1,61#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13478 more)

### `tests/hermes_state/test_delegation_startup_race.py`

- (module)
  - `tests/hermes_state/test_delegation_startup_race.py@-0,0 +1,364#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+13482 more)

### `tests/hermes_state/test_direct_probe_isolation.py`

- (module)
  - `tests/hermes_state/test_direct_probe_isolation.py@-0,0 +1,56#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+12683 more)

### `tests/hermes_state/test_display_fold_incremental.py`

- (module)
  - `tests/hermes_state/test_display_fold_incremental.py@-0,0 +1,355#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+19875 more)

### `tests/hermes_state/test_display_fold_one_step.py`

- (module)
  - `tests/hermes_state/test_display_fold_one_step.py@-0,0 +1,102` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_display_fold_one_step.py@-0,0 +1,102#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+12585 more)

### `tests/hermes_state/test_display_fold_writer_steps.py`

- (module)
  - `tests/hermes_state/test_display_fold_writer_steps.py@-0,0 +1,176#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13421 more)

### `tests/hermes_state/test_downgrade_delete_guard.py` → `tests/persistence_sqlite/housekeeping/test_downgrade_delete_guard.py`

- def test_an_older_release_delete_of_a_ledgered_session_is_refused_with_the_way_b
  - `tests/hermes_state/test_downgrade_delete_guard.py@-48 +49#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
- itself has to carry the refusal: their ``sessions delete`` / ``prune`` used to d
  - `tests/hermes_state/test_downgrade_delete_guard.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_an_upgraded_store_gains_the_guard_on_its_next_open(tmp_path):
  - `tests/hermes_state/test_downgrade_delete_guard.py@-71 +72#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/hermes_state/test_fts_automerge.py`

- (module)
  - `tests/hermes_state/test_fts_automerge.py@-0,0 +1,17#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6902 more)

### `tests/hermes_state/test_fts_runtime_rebuild.py` → `tests/persistence_sqlite/core/test_fts_runtime_rebuild.py`

- class TestRuntimeFtsRebuild:
  - `tests/hermes_state/test_fts_runtime_rebuild.py@-451 +449,2#moved` moved: `tests/persistence_sqlite/core/test_search_isolation.py`
  - `tests/hermes_state/test_fts_runtime_rebuild.py@-481 +476,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_gateway_routing_replace_diff.py`

- (module)
  - `tests/hermes_state/test_gateway_routing_replace_diff.py@-0,0 +1,113#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13460 more)

### `tests/hermes_state/test_generation_checkpoint_durability.py`

- (module)
  - `tests/hermes_state/test_generation_checkpoint_durability.py@-0,0 +1,61#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/tools.py`, `agent/agent_init.py` (+8908 more)

### `tests/hermes_state/test_holder_scan_bounded.py`

- (module)
  - `tests/hermes_state/test_holder_scan_bounded.py@-0,0 +1,64#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13535 more)

### `tests/hermes_state/test_incognito_off_regression.py`

- (module)
  - `tests/hermes_state/test_incognito_off_regression.py@-0,0 +1,129#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+13132 more)

### `tests/hermes_state/test_incognito_state.py`

- (module)
  - `tests/hermes_state/test_incognito_state.py@-0,0 +1,215#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md` (+16553 more)

### `tests/hermes_state/test_incognito_worker_write_contract.py`

- (module)
  - `tests/hermes_state/test_incognito_worker_write_contract.py@-0,0 +1,70#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/retry/action.yml`, `.github/known-flakes.json`, `.prettierrc` (+12009 more)

### `tests/hermes_state/test_lock_hold_indexes.py`

- (module)
  - `tests/hermes_state/test_lock_hold_indexes.py@-0,0 +1,216#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+20120 more)

### `tests/hermes_state/test_loop_guard.py`

- (module)
  - `tests/hermes_state/test_loop_guard.py@-0,0 +1,97#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13556 more)

### `tests/hermes_state/test_outbound_platform_message.py`

- (module)
  - `tests/hermes_state/test_outbound_platform_message.py@-0,0 +1,68#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+15379 more)

### `tests/hermes_state/test_provider_reported_cost.py`

- (module)
  - `tests/hermes_state/test_provider_reported_cost.py@-0,0 +1,294#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+20190 more)

### `tests/hermes_state/test_prune_cleanup_progress.py`

- (module)
  - `tests/hermes_state/test_prune_cleanup_progress.py@-0,0 +1,147#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/edit_approval.py` (+10897 more)

### `tests/hermes_state/test_prune_pacing_regressions.py`

- (module)
  - `tests/hermes_state/test_prune_pacing_regressions.py@-0,0 +1,184#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+12276 more)

### `tests/hermes_state/test_prune_pass_bounds.py`

- (module)
  - `tests/hermes_state/test_prune_pass_bounds.py@-0,0 +1,94#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+12755 more)

### `tests/hermes_state/test_prune_sql_deadline.py`

- (module)
  - `tests/hermes_state/test_prune_sql_deadline.py@-0,0 +1,253#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13480 more)

### `tests/hermes_state/test_prune_write_recovery.py`

- (module)
  - `tests/hermes_state/test_prune_write_recovery.py@-0,0 +1,151#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+13349 more)

### `tests/hermes_state/test_publication_rowcounts.py`

- (module)
  - `tests/hermes_state/test_publication_rowcounts.py@-0,0 +1,33#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+9663 more)

### `tests/hermes_state/test_publication_wait_budget.py`

- (module)
  - `tests/hermes_state/test_publication_wait_budget.py@-0,0 +1,250#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12604 more)

### `tests/hermes_state/test_restart_recovery_bounded.py`

- (module)
  - `tests/hermes_state/test_restart_recovery_bounded.py@-0,0 +1,129#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+19696 more)

### `tests/hermes_state/test_runtime_epoch_foreign_owner.py`

- (module)
  - `tests/hermes_state/test_runtime_epoch_foreign_owner.py@-0,0 +1,166#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+13374 more)

### `tests/hermes_state/test_runtime_privacy_journal.py`

- (module)
  - `tests/hermes_state/test_runtime_privacy_journal.py@-0,0 +1,155#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md` (+16439 more)

### `tests/hermes_state/test_runtime_privacy_ownership.py`

- (module)
  - `tests/hermes_state/test_runtime_privacy_ownership.py@-0,0 +1,204#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.github/workflows/prompt-surface-diff.yml` (+19411 more)

### `tests/hermes_state/test_runtime_worker_children.py` → `tests/persistence_sqlite/workers/test_runtime_worker_children.py`

- async def test_child_reserve_verifies_the_parent_claim_and_refuses_while_drainin
  - `tests/hermes_state/test_runtime_worker_children.py@-134 +136` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- caps them, scopes their writes to their own row, and retires them with their par
  - `tests/hermes_state/test_runtime_worker_children.py@-2,0 +3#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def ledger(tmp_path):
  - `tests/hermes_state/test_runtime_worker_children.py@-35 +36#moved` moved: `tests/hermes_state/test_runtime_admission.py`, `tests/hermes_state/test_runtime_adoption.py`, `tests/hermes_state/test_runtime_mutations.py`, `tests/hermes_state/test_runtime_recovery.py` (+2 more)
- def test_owner_restart_retires_in_flight_children_instead_of_recovering_them(led
  - `tests/hermes_state/test_runtime_worker_children.py@-80 +81` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_runtime_worker_children.py@-87,2 +88,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_runtime_worker_compaction.py` → `tests/persistence_sqlite/workers/test_runtime_worker_compaction.py`

- def test_micro_compaction_live_dicts_persist_through_the_worker_like_direct(work
  - `tests/hermes_state/test_runtime_worker_compaction.py@-95 +95` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_runtime_worker_compression_admission.py` → `tests/persistence_sqlite/workers/test_runtime_worker_compression_admission.py`

- from hermes_state import SessionDB
  - `tests/hermes_state/test_runtime_worker_compression_admission.py@-6,4 +6,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_runtime_worker_compression_annotations.py` → `tests/persistence_sqlite/workers/test_runtime_worker_compression_annotations.py`

- from hermes_state import SessionDB
  - `tests/hermes_state/test_runtime_worker_compression_annotations.py@-6 +6,2#moved` moved: `tests/agent/test_worker_outbox_capacity.py`, `tests/e2e/core/compaction/test_compaction_auto.py`, `tests/hermes_state/conftest.py`, `tests/hermes_state/test_runtime_worker_context.py` (+2 more)

### `tests/hermes_state/test_search_isolation.py`

- (module)
  - `tests/hermes_state/test_search_isolation.py@-0,0 +1,302#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+13062 more)

### `tests/hermes_state/test_search_isolation_review.py`

- (module)
  - `tests/hermes_state/test_search_isolation_review.py@-0,0 +1,349#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+19285 more)

### `tests/hermes_state/test_search_query_budget.py`

- (module)
  - `tests/hermes_state/test_search_query_budget.py@-0,0 +1,126#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+15605 more)

### `tests/hermes_state/test_session_db_read_conn_pool.py` → `tests/persistence_sqlite/core/test_holder_read_conn_pool.py`

- def test_fallback_to_locked_writer_when_read_conn_unavailable(db, monkeypatch):
  - `tests/hermes_state/test_session_db_read_conn_pool.py@-224 +227,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_session_delete_batches.py`

- (module)
  - `tests/hermes_state/test_session_delete_batches.py@-0,0 +1,378#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12482 more)

### `tests/hermes_state/test_session_signature_triggers.py`

- (module)
  - `tests/hermes_state/test_session_signature_triggers.py@-0,0 +1,132#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+15633 more)

### `tests/hermes_state/test_session_workspace_binding.py` → `tests/persistence_sqlite/conversation/test_session_workspace_binding.py`

- (module)
  - `tests/hermes_state/test_session_workspace_binding.py@-1 +1` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import hermes_state_sessions
  - `tests/hermes_state/test_session_workspace_binding.py@-10 +10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/hermes_state/test_sidecar_permissions.py`

- (module)
  - `tests/hermes_state/test_sidecar_permissions.py@-0,0 +1,80#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+11929 more)

### `tests/hermes_state/test_stale_held_generation_guard.py`

- (module)
  - `tests/hermes_state/test_stale_held_generation_guard.py@-0,0 +1,155#moved` moved: `.github/known-flakes.json`, `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md` (+17339 more)

### `tests/hermes_state/test_state_db_malformed_repair.py` → `tests/persistence_sqlite/core/test_state_db_malformed_repair.py`

- from hermes_state_repair import repair_state_db_schema
  - `tests/hermes_state/test_state_db_malformed_repair.py@-31,0 +32,3#moved` moved: `tests/persistence_sqlite/core/test_state_db_fts_segment_collision_probe.py`
  - `tests/hermes_state/test_state_db_malformed_repair.py@-505 +508,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _release_header_probe_fds() -> None:
  - `tests/hermes_state/test_state_db_malformed_repair.py@-519,0 +525,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_malformed_repair.py@-519,0 +525,35#moved` moved: `cron/jobs.py`, `hermes_cli/kanban_db_connect.py`, `hermes_state_common.py`, `persistence/sqlite/_state/common.py` (+7 more)

### `tests/hermes_state/test_state_db_repair_loop_cap.py` → `tests/persistence_sqlite/core/test_state_db_repair_loop_cap.py`

- from unittest.mock import patch
  - `tests/hermes_state/test_state_db_repair_loop_cap.py@-26,0 +27,2#moved` moved: `evals/kanban_pr_acceptance_live.py`, `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py` (+7709 more)
- from hermes_state_repair import _MAX_MALFORMED_BACKUPS, _MAX_PERSISTENT_REPAIR_A
  - `tests/hermes_state/test_state_db_repair_loop_cap.py@-31,0 +34,3#moved` moved: `tests/persistence_sqlite/core/test_state_db_fts_segment_collision_probe.py`

### `tests/hermes_state/test_state_db_repair_loop_mtime.py` → `tests/persistence_sqlite/core/test_state_db_repair_loop_mtime.py`

- from unittest.mock import patch
  - `tests/hermes_state/test_state_db_repair_loop_mtime.py@-30,0 +31,2#moved` moved: `evals/kanban_pr_acceptance_live.py`, `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py` (+7709 more)
- from hermes_state_repair import _MAX_MALFORMED_BACKUPS, _MAX_PERSISTENT_REPAIR_A
  - `tests/hermes_state/test_state_db_repair_loop_mtime.py@-35,0 +38,3#moved` moved: `tests/persistence_sqlite/core/test_state_db_fts_segment_collision_probe.py`

### `tests/hermes_state/test_state_db_repair_non_destructive.py` → `tests/persistence_sqlite/core/test_state_db_repair_non_destructive.py`

- def test_exhaustion_is_rechecked_after_acquiring_repair_lock(tmp_path, monkeypat
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-479 +482,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-481,5 +488,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-481,5 +488,8#moved` moved: `cron/jobs.py`, `hermes_cli/kanban_db_connect.py`, `hermes_state_common.py`, `persistence/sqlite/_state/common.py` (+7 more)
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-488 +498` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-501 +510` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-506,0 +515,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-506,0 +515,2#moved` moved: `tests/hermes_cli/test_doctor_structural_corruption.py`, `tests/persistence_sqlite/core/test_state_db_lock_fail_closed.py`, `tests/persistence_sqlite/core/test_state_db_malformed_repair.py`
- PAGE_SIZE = 4096
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-74,0 +75,3#moved` moved: `tests/persistence_sqlite/core/test_state_db_fts_segment_collision_probe.py`

### `tests/hermes_state/test_telegram_icon_migration.py`

- (module)
  - `tests/hermes_state/test_telegram_icon_migration.py@-0,0 +1,84#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12296 more)

### `tests/hermes_state/test_telegram_topic_selfheal.py` → `tests/persistence_sqlite/gateway/test_telegram_topic_selfheal.py`

- def test_topic_reads_selfheal_pre_v3_tables(tmp_path: Path):
  - `tests/hermes_state/test_telegram_topic_selfheal.py@-82 +82#moved` moved: `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/core/test_telegram_icon_migration.py`

### `tests/hermes_state/test_title_lineage.py`

- (module)
  - `tests/hermes_state/test_title_lineage.py@-0,0 +1,82#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py` (+14776 more)

### `tests/hermes_state/test_transcript_write_diagnostics.py`

- (module)
  - `tests/hermes_state/test_transcript_write_diagnostics.py@-0,0 +1,182#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+13889 more)

### `tests/hermes_state/test_wal_checkpoint_strategy.py` → `tests/persistence_sqlite/core/test_wal_checkpoint_strategy.py`

- class TestCheckpointFrequency:
  - `tests/hermes_state/test_wal_checkpoint_strategy.py@-225,0 +276,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_wal_checkpoint_strategy.py@-225,0 +276,35#moved` moved: `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/core/test_fts_automerge.py`

### `tests/hermes_state/test_worker_admission_index.py`

- (module)
  - `tests/hermes_state/test_worker_admission_index.py@-0,0 +1,117#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+16040 more)

### `tests/hermes_state/test_worker_message_archive.py`

- (module)
  - `tests/hermes_state/test_worker_message_archive.py@-0,0 +1,105#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+15763 more)

### `tests/hermes_state/test_write_lock_patience.py` → `tests/persistence_sqlite/core/test_write_lock_patience.py`

- from hermes_state import SessionDB
  - `tests/hermes_state/test_write_lock_patience.py@-30,2 +30,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _hold_write_lock(db_path, hold_s, started_evt):
  - `tests/hermes_state/test_write_lock_patience.py@-36 +37,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_write_lock_patience.py@-36 +37,4#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3466 more)
- class TestTranscriptWritePatience:
  - `tests/hermes_state/test_write_lock_patience.py@-89,0 +94,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_write_lock_patience.py@-89,0 +94,3#moved` moved: `tests/agent/test_account_usage_fetch.py`, `tests/agent/test_authorization_gate.py`, `tests/agent/test_auxiliary_explicit_cancellation.py`, `tests/agent/test_bedrock_no_sdk_retry.py` (+125 more)
  - `tests/hermes_state/test_write_lock_patience.py@-91 +98` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/hermes_state/test_write_lock_patience.py@-98,0 +106#moved` moved: `evals/heartbeat_idle_wire.py`, `gateway/run_notifications_completion.py`, `tests/acp_adapter/test_gateway_acp_session_end.py`, `tests/acp_adapter/test_gateway_attach_mode_race.py` (+441 more)

### `tests/hermes_state/test_write_stall_lifecycle.py`

- (module)
  - `tests/hermes_state/test_write_stall_lifecycle.py@-0,0 +1,154#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12015 more)

### `tests/hermes_state/test_write_stall_native.py`

- (module)
  - `tests/hermes_state/test_write_stall_native.py@-0,0 +1,47#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+13453 more)

### `tests/hermes_state/test_write_stall_sampling.py`

- (module)
  - `tests/hermes_state/test_write_stall_sampling.py@-0,0 +1,134#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+11990 more)

### `tests/pm/test_lease_reselection.py`

- def test_unlocked_reader_releases_a_lease_the_installer_moved_away_from(generati
  - `tests/pm/test_lease_reselection.py@-62,3 +53,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/scripts/test_release_artifacts.py`

- def test_candidate_smoke_survives_real_promotion_and_renderer(tmp_path, r2_serve
  - `tests/scripts/test_release_artifacts.py@-438 +445` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/scripts/test_termux_build_driver.py`

- def test_child_failure_stops_sequence_even_with_stale_product(build_fixture, sta
  - `tests/scripts/test_termux_build_driver.py@-161 +160` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/test_session_spillover_cleanup.py`

- def test_canonical_delete_retires_the_spill_for_collection(_home):
  - `tests/test_session_spillover_cleanup.py@-169 +170#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`
- Two halves under test:
  - `tests/test_session_spillover_cleanup.py@-9,0 +10#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/test_tests_tree_layout.py`

- TESTS_ROOT = REPO_ROOT / "tests"
  - `tests/test_tests_tree_layout.py@-30,0 +31,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _NON_MIRROR_DIRS = {
  - `tests/test_tests_tree_layout.py@-32 +34` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_async_delegation_fd_leak.py`

- def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
  - `tests/tools/test_async_delegation_fd_leak.py@-125 +109,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-125 +109,5#moved` moved: `tests/hermes_cli/test_backup_preflight.py`, `tests/hermes_cli/test_doctor_wal_checkpoint_guard.py`, `tests/persistence_sqlite/admin/test_backup_parity.py`, `tests/tools/test_async_delegation.py`
- def test_post_open_hardening_failure_closes_connection(monkeypatch, tmp_path):
  - `tests/tools/test_async_delegation_fd_leak.py@-154 +141,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-154 +141,7#moved` moved: `tests/hermes_cli/test_backup_preflight.py`, `tests/hermes_cli/test_doctor_wal_checkpoint_guard.py`, `tests/persistence_sqlite/admin/test_backup_parity.py`, `tests/tools/test_async_delegation.py`
- class _TrackingConnection:
  - `tests/tools/test_async_delegation_fd_leak.py@-19 +19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-21,3 +21,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-31,15 +26,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-31,15 +26,2#moved` moved: `agent/runtime_session_child.py`, `hermes_logging.py`, `tests/cron/test_cron_persistence_handle.py`, `tests/gateway/test_delivery_ledger_fd_leak.py` (+3 more)
- def _track_connections(monkeypatch):
  - `tests/tools/test_async_delegation_fd_leak.py@-57,0 +40,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_async_delegation_fd_leak.py@-58,0 +43` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_bot_mode_dm_outcomes.py`

- from hermes_state import SessionDB
  - `tests/tools/test_bot_mode_dm_outcomes.py@-9 +9,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_browser_vault.py`

- class TestClassifier:
  - `tests/tools/test_browser_vault.py@-173 +191` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class TestBrowserVaultTools:
  - `tests/tools/test_browser_vault.py@-266,2 +268,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-269 +271` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-271,3 +273,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
  - `tests/tools/test_browser_vault.py@-28,0 +28#moved` moved: `agent/vault_backend_registry.py`, `agent/vault_backends/base.py`, `tests/agent/test_vault_protected_fields.py`, `tests/tools/test_browser_protected_date_pixels.py` (+10 more)
- class TestBrowserVaultTools:
  - `tests/tools/test_browser_vault.py@-318 +421` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-366 +466` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-424,0 +525` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-488,3 +589,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-493,2 +594,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-500,2 +601,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-549 +650,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-549 +650,2#moved` moved: `tests/persistence_sqlite/core/test_state_db_holders.py`, `tests/tui_gateway/test_hosted_room_frozen_publication.py`
  - `tests/tools/test_browser_vault.py@-573 +675,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-586,0 +691` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-590 +695,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-592 +699#moved` moved: `tests/tools/test_browser_vault_manager_card.py`
- class TestTwoFactor:
  - `tests/tools/test_browser_vault.py@-825 +960` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_browser_vault.py@-832,0 +968,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_browser_vault_preview.py`

- class PreviewWindow:
  - `tests/tools/test_browser_vault_preview.py@-41 +41` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_cron_run_notice_usage.py`

- import pytest
  - `tests/tools/test_cron_run_notice_usage.py@-11 +13#moved` moved: `tests/cron/test_cron_run_metrics.py`, `tests/cron/test_hard_wall_real_path.py`, `tests/cron/test_job_wall_timeout.py`, `tests/cron/test_jobs_prefetch_legacy_claims.py` (+2 more)
- def owner(tmp_path, monkeypatch):
  - `tests/tools/test_cron_run_notice_usage.py@-18,0 +21,5#moved` moved: `evals/completion_backlog_probe.py`, `tests/agent/test_review_engine.py`, `tests/gateway/test_agents_command_delegations.py`, `tests/gateway/test_async_delegation_session_binding.py` (+56 more)
  - `tests/tools/test_cron_run_notice_usage.py@-20,2 +27,11#moved` moved: `gateway/platforms/webhook_ingress.py`, `pm/downloader.py`, `tests/agent/test_compression_worker_isolation.py`, `tests/agent/test_review_engine.py` (+69 more)
  - `tests/tools/test_cron_run_notice_usage.py@-24 +40` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _completion(job, run_one_job):
  - `tests/tools/test_cron_run_notice_usage.py@-30,0 +47,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_cron_run_notice_usage.py@-30,0 +47,8#moved` moved: `hermes_cli/skills_hub.py`, `tests/cron/test_execution_ledger.py`, `tests/cron/test_script_admission.py`, `tests/gateway/test_windows_proactor_accept.py`
  - `tests/tools/test_cron_run_notice_usage.py@-32,2 +56,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_cron_run_notice_usage.py@-32,2 +56,6#moved` moved: `agent/shell_hooks.py`, `hermes_cli/auth.py`, `hermes_cli/data_cleanup_holders.py`, `hermes_cli/skills_hub.py` (+24 more)
- async def test_notice_carries_the_run_sessions_model_and_api_calls(owner):
  - `tests/tools/test_cron_run_notice_usage.py@-56 +84` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- model pin (None for a job on the default model). The run's own session in state.
  - `tests/tools/test_cron_run_notice_usage.py@-6,0 +7,2#moved` moved: `gateway/run_turn_runner.py`, `hermes_cli/gateway_session_store.py`, `hermes_cli/sessions_cmd.py`, `hermes_cli/skills_hub.py` (+35 more)
- async def test_notice_carries_the_run_sessions_model_and_api_calls(owner):
  - `tests/tools/test_cron_run_notice_usage.py@-60 +88` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_delegate.py`

- import json
  - `tests/tools/test_delegate.py@-12,0 +13#moved` moved: `acp_adapter/content.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+3476 more)
- class TestDelegateTask(unittest.TestCase):
  - `tests/tools/test_delegate.py@-324 +325` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tools/test_react_tool.py`

- (module)
  - `tests/tools/test_react_tool.py@-0,0 +1,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_react_tool.py@-0,0 +1,22#moved` moved: `tests/agent/test_bot_chat_toolset_refresh.py`, `tests/agent/test_cross_process_turn_lease.py`, `tests/agent/test_system_prompt_restore.py`, `tests/agent/test_title_generator.py` (+17 more)

### `tests/tools/test_session_search.py`

- class TestDiscoveryTemporalNarrowing:
  - `tests/tools/test_session_search.py@-1158,0 +1426,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-1158,0 +1426,13#moved` moved: `tests/persistence_sqlite/core/test_search_isolation.py`, `tests/persistence_sqlite/core/test_search_isolation_review.py`, `tests/persistence_sqlite/core/test_search_query_budget.py`
- class TestBrowseShape:
  - `tests/tools/test_session_search.py@-118 +294` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-128,0 +305,17` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-128,0 +305,17#moved` moved: `acp_adapter/content.py`, `acp_adapter/model_catalog.py`, `acp_adapter/tools.py`, `agent/account_usage.py` (+1606 more)
- from hermes_state import SessionDB
  - `tests/tools/test_session_search.py@-17,0 +18,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-17,0 +18,2#moved` moved: `tests/hermes_state/test_hermes_state.py`
- class TestDiscoverySort:
  - `tests/tools/test_session_search.py@-295,6 +488,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-295,6 +488,21#moved` moved: `agent/activity_tracking.py`, `gateway/session_snapshot.py`, `hermes_state_messages.py`, `persistence/sqlite/conversation/_store/messages.py` (+78 more)
  - `tests/tools/test_session_search.py@-303,4 +511,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-303,4 +511,35#moved` moved: `tests/cron/test_review_subject.py`, `tests/hermes_cli/test_goal_external_wait_backoff_real_path.py`, `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/authority/test_mutate.py` (+33 more)
- class TestSessionLink:
  - `tests/tools/test_session_search.py@-499,0 +739,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _seed_modpack_sessions(db):
  - `tests/tools/test_session_search.py@-65 +67` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-68,0 +71,174` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tools/test_session_search.py@-68,0 +71,174#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/acp_client.py`, `agent/api_error_summary.py` (+614 more)

### `tests/tui_gateway/test_adversarial_sidecar_availability.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_adversarial_sidecar_availability.py@-8 +8#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_hosted_room_attachment_routes.py`

- _LEASE_TTL_SECONDS = 30.0
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-116 +116` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _legacy_runtime(db: Path, rpc: HostedRoomServerRPC) -> HostedRoomRuntime:
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-119 +119` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_legacy_route_still_runs_a_turn_without_a_file(db: Path):
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-184 +184,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-184 +184,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/auxiliary_health.py`, `agent/auxiliary_unavailable.py` (+217 more)

### `tests/tui_gateway/test_hosted_room_prepare_fence.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_hosted_room_prepare_fence.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-18,4 +18,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-18,4 +18,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def _legacy(monkeypatch, db):
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-56 +54` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tui_gateway/test_legacy_turn_canonical_barrier.py`

- from tui_gateway import server
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-12,0 +12,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_diagnostic_turn_end_to_end.py`, `tests/tui_gateway/test_failed_turn_retention.py` (+9 more)
- durable ledger: an ``unknown`` admission is never replayed, queued input runs in
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-3,0 +4#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_legacy_submit_refuses_while_canonical_ledger_holds_work(monkeypatch, tm
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-31,2 +32,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-31,2 +32,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_failed_turn_retention.py`, `tests/tui_gateway/test_goal_command.py`, `tests/tui_gateway/test_hosted_room_turn_slot_release.py` (+4 more)
- from hermes_state import SessionDB
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-7,4 +8,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-7,4 +8,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_notification_refusal_backoff.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_notification_refusal_backoff.py@-11 +11,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tui_gateway/test_reap_respects_canonical_ledger.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_reap_respects_canonical_ledger.py@-13 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tui_gateway/test_serve_exit_flush.py`

- def test_shutdown_mid_tool_kills_the_command_and_keeps_its_result(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-208,2 +210,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import pytest
  - `tests/tui_gateway/test_serve_exit_flush.py@-23,0 +24,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_shutdown_mid_tool_kills_the_command_and_keeps_its_result(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-232,2 +234` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_sigterm_grace_hard_exit_kills_a_sigterm_ignoring_command(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-257,2 +258,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_serve_exit_flush.py@-271,2 +272` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tui_gateway/test_tui_gateway_server.py`

- (module)
  - `tests/tui_gateway/test_tui_gateway_server.py@-0,0 +1#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_config_set_model_requires_confirmation_for_expensive_model(monkeypatch)
  - `tests/tui_gateway/test_tui_gateway_server.py@-10153 +10167,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_config_set_model_global_persists(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-10181 +10196,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-10181 +10196,4#moved` moved: `tests/agent/test_env_loader_secret_sources.py`, `tests/agent/test_relay_runtime_plugins.py`, `tests/agent/test_skill_utils.py`, `tests/gateway/test_auth_fallback.py` (+13 more)
  - `tests/tui_gateway/test_tui_gateway_server.py@-10201 +10219,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_sets_approval_session_key(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-11595 +11614#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_expands_context_refs(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-11648 +11667#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_history_version_mismatch_surfaces_warning(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13214 +13233#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_merges_on_model_switch_marker(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13319 +13338#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_merges_on_personality_pivot_marker(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13409 +13428#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_sanitizes_bracketed_paste_before_agent(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13475 +13494#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_history_version_match_persists_normally(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13516 +13535#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_snapshots_history_after_pending_model_switch(monkeypatch)
  - `tests/tui_gateway/test_tui_gateway_server.py@-13577 +13596#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_can_truncate_before_user_ordinal(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13651 +13670#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncate_ordinal_skips_display_kind_rows(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13877 +13896#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncate_translates_display_prefix_ordinal(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-13988 +14007#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_row_id_accepts_full_lineage_ordinal(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14112,0 +14132` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_run_prompt_submit_never_ticks_after_message_complete(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-1417 +1410#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_run_prompt_submit_registers_turn_thread_for_interrupt(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14388 +14408` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_interrupt_before_agent_ready_prevents_late_turn_start(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14457 +14477` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_cancelled_turn_before_agent_ready_emits_error_event(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14527 +14547` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_session_not_running_before_agent_ready_emits_error_event(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14599 +14619` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_slow_agent_build_delivers_prompt_instead_of_timing_out(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14685 +14705` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_slow_agent_build_emits_keyed_progress_notice(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14758 +14778` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_agent_build_failure_surfaces_error_and_drops_turn(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14832 +14852` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_releases_old_history_before_heap_trim(monkeypatch, tmp_pa
  - `tests/tui_gateway/test_tui_gateway_server.py@-17262 +17282#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_wires_live_title_rename_callback(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-17318 +17338#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_surfaces_backend_error_as_visible_text(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-17368 +17388#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_preserves_empty_response_without_error(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-17412 +17432#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_notification_poller_delivers_completion(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-18155 +18175#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_notification_poller_skips_consumed(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-18223 +18243#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_close_sessions_for_transport_closes_flagged_repoints_rest(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-18986 +19006` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_close_sessions_for_transport_skips_session_rebound_before_claim(
  - `tests/tui_gateway/test_tui_gateway_server.py@-19029 +19049` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-19046 +19066` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_passes_persist_user_message_to_agent(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-20677 +20636#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_personality_marker_does_not_shift_truncate_ordinal(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-21074 +21033#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncation_archives_instead_of_deleting(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-21170 +21129#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_row_id_resolves_via_db_when_memory_lacks_stamps(monkeypat
  - `tests/tui_gateway/test_tui_gateway_server.py@-21339,0 +21299` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_row_id_real_sessiondb_resolve_without_memory_stamps(
  - `tests/tui_gateway/test_tui_gateway_server.py@-21365 +21325` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-21406 +21364` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_row_id_misaligned_memory_role_shift_targets_real_turn(
  - `tests/tui_gateway/test_tui_gateway_server.py@-21562 +21520` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-21601 +21557` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_load_cfg_raw_sees_replacement_with_pinned_mtime_and_size(monkeypatch, t
  - `tests/tui_gateway/test_tui_gateway_server.py@-22235,2 +22191,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-22248,5 +22207#moved` moved: `tests/gateway/test_profile_serve_signature.py`, `tests/tools/test_model_tools.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-22253,0 +22209,3#moved` moved: `tests/tools/test_model_tools.py`
- def test_load_enabled_toolsets_filters_invalid_tui_env(monkeypatch, capsys):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2415 +2408#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_accepts_plugin_env_after_discovery(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2438 +2431` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_load_enabled_toolsets_rejects_disabled_mcp_env(monkeypatch, capsys):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2510 +2503#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_falls_back_when_tui_env_invalid(monkeypatch, caps
  - `tests/tui_gateway/test_tui_gateway_server.py@-2545 +2538#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_warns_when_config_fallback_fails(monkeypatch, cap
  - `tests/tui_gateway/test_tui_gateway_server.py@-2569 +2562#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def _dispatch_sync(req: dict, transport=None) -> dict | None:
  - `tests/tui_gateway/test_tui_gateway_server.py@-26,9 +27` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_load_enabled_toolsets_reports_disabled_mcp_separately(monkeypatch, caps
  - `tests/tui_gateway/test_tui_gateway_server.py@-2632 +2625#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_prompt_submit_fails_open_inline_when_compute_host_dispatch_breaks(monke
  - `tests/tui_gateway/test_tui_gateway_server.py@-398 +391#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_ws_orphan_reap_spares_turn_reattached_within_grace(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5142 +5135#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-5144 +5137` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_close_transport_detaches_when_no_viewers_remain(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5599,2 +5592` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-5616 +5608#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
- def test_close_transport_skips_dead_remaining_viewers(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5653 +5645#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
- def test_live_session_payload_registers_transport_as_viewer():
  - `tests/tui_gateway/test_tui_gateway_server.py@-5670 +5662` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_claim_or_reuse_live_winner_cancels_pending_reap(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5779 +5771` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-5788 +5780,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_superseded_runtime_finalized_without_reclaimed_broadcast(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5790 +5783` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-5792 +5785,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-5821,0 +5816,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-5825 +5821` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_lazy_unpersisted_resume_rebinds_transport_and_cancels_reap(monkeypatch)
  - `tests/tui_gateway/test_tui_gateway_server.py@-5921 +5917` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_refuses_unconfirmed_nonempty_truncation(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6669 +6665,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6669 +6665,18#moved` moved: `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py`, `skills/productivity/microsoft-office/tests/test_xlsx_skill.py`, `tests/AGENTS.md` (+2221 more)
- def test_prompt_submit_truncates_by_message_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6670,0 +6684,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_golden_transcript_matches_flag_off_and_on(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-669 +662#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncates_by_message_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6700 +6709` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6715,0 +6725,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6715,0 +6725,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-6719 +6732,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_truncation_falls_back_to_sid_when_session_key_null(monkey
  - `tests/tui_gateway/test_tui_gateway_server.py@-6758 +6768` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_refuses_ordinal_only_when_durable_history_is_unstamped(mo
  - `tests/tui_gateway/test_tui_gateway_server.py@-6926 +6936` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_truncates_by_row_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6927,0 +6938,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6956 +6964` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6974,0 +6983,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-6974,0 +6983,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-6978 +6990` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_truncates_by_string_row_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6979,0 +6992,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-7005 +7018` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-7019,0 +7033,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-7019,0 +7033,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
- def test_prompt_submit_row_id_not_found(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7107 +7124,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_resolves_row_id_absorbed_into_marker_merge(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7170 +7185` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _mk_merge_fake_db(verbatim, repaired, replaced):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7226 +7241,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_resolves_row_id_swallowed_by_marker_merge(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7283 +7297` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tests/tui_gateway/test_tui_gateway_server.py@-7313 +7327,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_prompt_submit_resolves_row_id_swallowed_by_plain_user_merge(monkeypatch
  - `tests/tui_gateway/test_tui_gateway_server.py@-7360 +7373` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_notification_poller_live_loop_requeues_foreign_completion_for_owner(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7520,0 +7534` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _configure_immediate_prompt_run(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7791 +7805#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_thread
  - `tests/tui_gateway/test_tui_gateway_server.py@-8040 +8054` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def test_session_create_drops_pending_title_on_valueerror(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-8711 +8725#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)

### `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py`

- def test_prompt_submit_empty_truncation_allowed_with_confirm(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-156 +161#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_unconfirmed_deep_truncation_is_warn_only_this_release(monkeypatch, capl
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-223 +228` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- anchor can still name the wrong, stale row)."""
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-4,0 +5#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_prompt_submit_refuses_deep_truncation_without_confirm(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-41 +46` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import types
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-7,0 +9,2#moved` moved: `evals/kanban_pr_acceptance_live.py`, `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py` (+7709 more)
- from tui_gateway import server
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-9,0 +13,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_diagnostic_turn_end_to_end.py`, `tests/tui_gateway/test_failed_turn_retention.py` (+9 more)
- def test_deep_truncate_counts_every_durable_row_a_merged_carrier_holds(monkeypat
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-93 +98` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tests/tui_gateway/test_tui_gateway_ws.py`

- def test_legacy_fallback_never_writes_an_authority_owned_session(monkeypatch, tm
  - `tests/tui_gateway/test_tui_gateway_ws.py@-551 +551#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_vault_preview_fill.py`

- def window(monkeypatch):
  - `tests/tui_gateway/test_vault_preview_fill.py@-54 +54` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/async_delegation.py`

- (module)
  - `tools/async_delegation.py@-1,0 +2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _dispatch_admitted(
  - `tools/async_delegation.py@-1002,4 +808,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1007 +815,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1012,3 +819,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1012,3 +819,7#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9661 more)
  - `tools/async_delegation.py@-1016,25 +827,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1016,25 +827,7#moved` moved: `agent/provider_registry.py`, `cron/executions.py`, `gateway/cron_review.py`, `gateway/platforms/base.py` (+43 more)
- _STALL_GRACE_SECONDS = 120.0
  - `tools/async_delegation.py@-104,0 +167,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _dispatch_admitted(
  - `tools/async_delegation.py@-1042,31 +835,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1042,31 +835,26#moved` moved: `tests/hermes_cli/test_user_providers_model_switch.py`
  - `tools/async_delegation.py@-1074,6 +862,77` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1074,6 +862,77#moved` moved: `agent/rate_limit_credits.py`, `gateway/platforms/webhook_ingress.py`, `hermes_cli/commands_platforms.py`, `hermes_cli/plugins_cmd_inspect.py` (+4 more)
- def dispatch_async_delegation(
  - `tools/async_delegation.py@-1086 +945,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1099,0 +961,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from __future__ import annotations
  - `tools/async_delegation.py@-11,0 +13#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_stream_watchdog.py`, `agent/context_compressor_summary.py`, `agent/context_default_record.py` (+120 more)
- _monitor_stop = threading.Event()
  - `tools/async_delegation.py@-110 +176` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _ACTIVE_STATES = ("running", "stalling")
  - `tools/async_delegation.py@-111,0 +178,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def dispatch_async_delegation_batch(
  - `tools/async_delegation.py@-1114,2 +977,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1117 +980,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1135 +1001,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _ACTIVE_STATES = ("running", "stalling")
  - `tools/async_delegation.py@-114 +188,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-114 +188,7#moved` moved: `gateway/run_notifications_sources.py`
- def dispatch_async_delegation_batch(
  - `tools/async_delegation.py@-1147,9 +1015,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _keep_finished_children(record: dict[str, Any], result: Any, status: str) ->
  - `tools/async_delegation.py@-1158,6 +1021,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _finalize(delegation_id: str, result: Any, status: str) -> None:
  - `tools/async_delegation.py@-1165,15 +1032,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1181,2 +1086,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1181,2 +1086,5#moved` moved: `gateway/relay/egress.py`, `gateway/run_goals.py`, `gateway/run_housekeeping_disk.py`, `gateway/run_turn_runner_notices.py` (+22 more)
  - `tools/async_delegation.py@-1185,2 +1093,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1187,0 +1097` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def completion_event(record: dict[str, Any], result: dict[str, Any], status: str
  - `tools/async_delegation.py@-1195,2 +1105` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1204,2 +1113,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1210,0 +1120` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _STALL_FIELD_MAP = (("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
  - `tools/async_delegation.py@-123,686 +203,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def completion_event(record: dict[str, Any], result: dict[str, Any], status: str
  - `tools/async_delegation.py@-1230,0 +1151` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1232 +1153` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _push_completion_event(record: dict[str, Any], result: dict[str, Any], statu
  - `tools/async_delegation.py@-1253,6 +1174,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1261,0 +1185,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1263,4 +1201,99` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1263,4 +1201,99#moved` moved: `agent/agent_runtime_helpers.py`, `agent/checkpoints/manager.py`, `agent/checkpoints/manager_profile_rename.py`, `agent/checkpoints/pruning.py` (+100 more)
- def push_task_failure_notice(delegation_id: str, entry: dict[str, Any], *, n_tas
  - `tools/async_delegation.py@-1277,2 +1310,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1282,0 +1318,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1299,0 +1338,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import contextvars
  - `tools/async_delegation.py@-13,0 +16#moved` moved: `acp_adapter/gateway_media.py`, `agent/account_usage_cache.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py` (+508 more)
- def _sweep_stale_locked(now: float):
  - `tools/async_delegation.py@-1327,0 +1371,71` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1327,0 +1371,71#moved` moved: `agent/context_compressor_window.py`, `agent/message_sanitization.py`, `cron/delivery_queue.py`, `cron/executions.py` (+14 more)
  - `tools/async_delegation.py@-1332,0 +1447,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1351,0 +1472` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1353 +1474` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1359 +1480` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _call_interrupt(fn, msg: str, *args) -> bool:
  - `tools/async_delegation.py@-1364 +1485,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1364 +1485,18#moved` moved: `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py`, `agent/api_request_hooks.py` (+435 more)
- def _stale_monitor_loop() -> None:
  - `tools/async_delegation.py@-1376,0 +1515,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1386 +1527,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1389,2 +1535,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1389,2 +1535,4#moved` moved: `acp_adapter/server.py`, `agent/aux_accounting.py`, `agent/azure_identity_adapter.py`, `agent/deadline.py` (+20 more)
  - `tools/async_delegation.py@-1392 +1540,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def list_async_delegations() -> list[dict[str, Any]]:
  - `tools/async_delegation.py@-1463 +1628` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1492,2 +1657,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1492,2 +1657,5#moved` moved: `agent/agent_runtime_helpers.py`, `agent/checkpoints/manager.py`, `agent/skill_utils.py`, `apps/desktop/electron/watch-storm-breaker.ts` (+36 more)
- def _interrupt_records(targets: list[dict[str, Any]], caller: str, reason: str,
  - `tools/async_delegation.py@-1495,6 +1663,81` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1495,6 +1663,81#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2495 more)
  - `tools/async_delegation.py@-1506,5 +1749,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def interrupt_all(reason: str = "shutdown", *, resumable: bool = False) -> int:
  - `tools/async_delegation.py@-1515,2 +1775,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def interrupt_for_session(
  - `tools/async_delegation.py@-1522,4 +1796` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1530 +1801` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1532,14 +1803,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def refused_for_session(token: Any) -> bool:
  - `tools/async_delegation.py@-1549 +1813` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _reset_for_tests() -> None:
  - `tools/async_delegation.py@-1583,0 +1833,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1589,0 +1841` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1592,0 +1845` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-1594,0 +1848,26` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import uuid
  - `tools/async_delegation.py@-20,0 +24#moved` moved: `acp_adapter/events.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/tool_guardrails.py` (+35 more)
- from tools.daemon_pool import DaemonThreadPoolExecutor
  - `tools/async_delegation.py@-26,0 +31,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _executor_max_workers: int = 0
  - `tools/async_delegation.py@-41 +48,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _FENCED_FIELDS = ("session_key", "parent_session_id")
  - `tools/async_delegation.py@-49,0 +60,27` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-49,0 +60,27#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9664 more)
- _DEFAULT_MAX_ASYNC_CHILDREN = 3
  - `tools/async_delegation.py@-50,0 +88,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _MAX_RETAINED_COMPLETED = 50
  - `tools/async_delegation.py@-52,0 +95,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _MAX_DURABLE_PENDING = 1000
  - `tools/async_delegation.py@-54,0 +101,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-54,0 +101,21#moved` moved: `cron/delivery_queue.py`, `cron/executions.py`, `cron/incidents.py`, `cron/notepad.py` (+15 more)
- def _get_executor(max_workers: int) -> ThreadPoolExecutor:
  - `tools/async_delegation.py@-884,0 +233,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-884,0 +233,21#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+1144 more)
- def active_count(*, home: Optional[Path] = None) -> int:
  - `tools/async_delegation.py@-887,0 +257,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-888,0 +260,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-890,2 +267,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-890,2 +267,5#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/server.py` (+3394 more)
- def active_task_count() -> int:
  - `tools/async_delegation.py@-903,0 +284,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _session_records(statuses, session_key: str, origin_ui_session_id: str, pare
  - `tools/async_delegation.py@-909,3 +295` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-918,0 +303,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _prune_completed_locked() -> None:
  - `tools/async_delegation.py@-931,2 +343,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _dispatch(**kwargs) -> dict[str, Any]:
  - `tools/async_delegation.py@-972 +387` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-975,0 +391,385` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-975,0 +391,385#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/gateway_server.py` (+5549 more)
- def _dispatch_admitted(
  - `tools/async_delegation.py@-982,0 +783` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-983,0 +785,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/async_delegation.py@-985,6 +791,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/browser_vault_tool.py`

- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-10 +11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _ensure_supervisor(task_id: str):
  - `tools/browser_vault_tool.py@-108,3 +210,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-116,2 +221,3#moved` moved: `agent/browser_output_egress.py`, `tools/browser_tool_vision.py`, `tools/browser_use_cli.py`
  - `tools/browser_vault_tool.py@-119 +225,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-12 +13,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _ensure_supervisor(task_id: str):
  - `tools/browser_vault_tool.py@-122 +229#moved` moved: `tools/browser_tool_cdp.py`, `tools/browser_use_cli.py`
  - `tools/browser_vault_tool.py@-124 +231` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-126,2 +233,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
  - `tools/browser_vault_tool.py@-132 +239` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-140,0 +248,30` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-140,0 +248,30#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/tools.py`, `agent/agent_init.py` (+1962 more)
  - `tools/browser_vault_tool.py@-155,2 +292,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _deep_probe(selector: str) -> str:
  - `tools/browser_vault_tool.py@-218,5 +350,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-218,5 +350,9#moved` moved: `scripts/releases/lanes.py`
- def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[str]:
  - `tools/browser_vault_tool.py@-228,3 +364,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-228,3 +364,4#moved` moved: `hermes_cli/tools_config_providers.py`, `tests/tools/test_browser_profile_live_cdp.py`
  - `tools/browser_vault_tool.py@-239,25 +376,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _bound_page_origin(task_id: str, meta: Any, probe_kind: str) -> str | dict[s
  - `tools/browser_vault_tool.py@-270,2 +392,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def browser_vault_list() -> str:
  - `tools/browser_vault_tool.py@-272,0 +408,43` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-272,0 +408,43#moved` moved: `agent/credential_pool.py`, `agent/credential_pool_admin.py`, `agent/credential_pool_plugin.py`, `agent/credential_pool_selection.py` (+139 more)
  - `tools/browser_vault_tool.py@-274 +452` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-277,0 +456,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-277,0 +456,9#moved` moved: `agent/vault_backends/browser.py`, `cron/blueprint_catalog.py`, `hermes_cli/desktop_plugin_halves.py`, `hermes_cli/plugin_host_wire.py` (+4 more)
  - `tools/browser_vault_tool.py@-283 +470` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-286 +473,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-286 +473,14#moved` moved: `agent/vault_backends/base.py`, `tests/agent/test_vault_backends.py`, `tests/agent/test_vault_connect.py`, `tests/agent/test_vault_onepassword_accounts.py` (+6 more)
  - `tools/browser_vault_tool.py@-289,0 +490,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-292 +502,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-295,2 +507` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-299,0 +511,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-299,0 +511,5#moved` moved: `gateway/run_config_loaders.py`
  - `tools/browser_vault_tool.py@-303,2 +519,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- import logging
  - `tools/browser_vault_tool.py@-34,0 +34,2#moved` moved: `acp_adapter/server.py`, `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/acp_client.py` (+2392 more)
- def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) ->
  - `tools/browser_vault_tool.py@-347,0 +575` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-350,2 +578,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from typing import Any, Dict, Optional
  - `tools/browser_vault_tool.py@-37 +38,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-37 +38,3#moved` moved: `tools/memory_tool.py`
- (module)
  - `tools/browser_vault_tool.py@-4,2 +4,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- logger = logging.getLogger(__name__)
  - `tools/browser_vault_tool.py@-40,0 +44,64` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-40,0 +44,64#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/commands.py` (+4277 more)
- def _saved_login(handle: str) -> tuple[Any, Any]:
  - `tools/browser_vault_tool.py@-400 +610` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None) ->
  - `tools/browser_vault_tool.py@-402,3 +612,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-405,0 +617,2#moved` moved: `tests/agent/test_vault_onepassword_accounts.py`, `tools/browser_vault_context.py`
  - `tools/browser_vault_tool.py@-407,0 +621#moved` moved: `tools/browser_vault_context.py`
  - `tools/browser_vault_tool.py@-410,9 +624,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-410,9 +624,28#moved` moved: `tests/gateway/test_streaming_tts_consumer.py`, `tests/scripts/desktop_update/test_desktop_update_windows_child_stage.py`, `tests/scripts/desktop_update/test_desktop_update_windows_log_exit_flush.py`, `tests/scripts/desktop_update/test_desktop_update_windows_marker.py` (+5 more)
  - `tools/browser_vault_tool.py@-420,4 +653,33` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-420,4 +653,33#moved` moved: `tools/browser_vault_aliases.py`
  - `tools/browser_vault_tool.py@-427 +689` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-434,2 +696,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-436,0 +703,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-438,0 +712,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-440 +717,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-450 +729,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-453 +734,2#moved` moved: `agent/vault_backends/unlock.py`, `hermes_cli/gateway.py`, `hermes_cli/gateway_setup_wizard.py`, `hermes_cli/web_routers/files.py` (+2 more)
- def _check_vault_available() -> bool:
  - `tools/browser_vault_tool.py@-47,3 +114,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-51,3 +118,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-51,3 +118,5#moved` moved: `tests/tools/test_browser_camofox.py`
- def _identifier_fill(classified: list, password_fill: list[dict[str, Any]], iden
  - `tools/browser_vault_tool.py@-532,4 +764,23` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-532,4 +764,23#moved` moved: `tests/tools/test_vault_origin_aliases.py`, `tools/browser_vault_context.py`
- def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, eff
  - `tools/browser_vault_tool.py@-545,0 +797,2#moved` moved: `tests/agent/test_vault_protected_fields.py`
  - `tools/browser_vault_tool.py@-547,2 +800,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-550,12 +804,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-576,8 +818` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-586,32 +821,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _check_vault_available() -> bool:
  - `tools/browser_vault_tool.py@-59,0 +130,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-59,0 +130,9#moved` moved: `agent/browser_output_egress.py`, `tools/browser_tool_vision.py`
- def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, eff
  - `tools/browser_vault_tool.py@-623,4 +827,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-632,8 +834,315` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-632,8 +834,315#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `agent/auxiliary_client.py`, `agent/context_compressor.py` (+171 more)
  - `tools/browser_vault_tool.py@-642 +1151,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-642 +1151,16#moved` moved: `evals/prefix_rediet/worker.py`, `tools/browser_vault_aliases.py`
- def _confirm_payment_fill(label: str, origin: str) -> bool:
  - `tools/browser_vault_tool.py@-645,0 +1170,35` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-645,0 +1170,35#moved` moved: `agent/agent_runtime_helpers.py`, `hermes_cli/web_server_memory.py`, `scripts/ci/cache_report.py`, `tests/cron/test_scheduler_provider.py` (+3 more)
  - `tools/browser_vault_tool.py@-647,0 +1207,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-647,0 +1207,7#moved` moved: `cli.py`, `hermes_cli/doctor_platform.py`, `hermes_cli/gateway_chat.py`, `hermes_cli/gateway_session_store.py` (+6 more)
  - `tools/browser_vault_tool.py@-649,4 +1215,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-649,4 +1215,3#moved` moved: `apps/desktop/e2e/core/oracle.ts`, `apps/desktop/src/app/artifacts/index.tsx`, `apps/desktop/src/app/chat/composer/status-stack/goal-indicator.test.tsx`, `apps/desktop/src/app/chat/hooks/use-composer-actions.ts` (+9 more)
  - `tools/browser_vault_tool.py@-658,0 +1224,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-658,0 +1224,14#moved` moved: `tests/fixtures/cua_driver_0_9_tools_list.json`, `tests/fixtures/prompt_surface/tools/browser_back.json`, `tests/fixtures/prompt_surface/tools/browser_cdp.json`, `tests/fixtures/prompt_surface/tools/browser_close.json` (+14 more)
- BROWSER_VAULT_SCHEMA = {
  - `tools/browser_vault_tool.py@-662,2 +1241,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-669,4 +1248,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-669,4 +1248,22#moved` moved: `evals/prefix_rediet/worker.py`
  - `tools/browser_vault_tool.py@-677,0 +1275,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-678,0 +1278,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-689,0 +1293,46` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-689,0 +1293,46#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4780 more)
- def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
  - `tools/browser_vault_tool.py@-691,2 +1340,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _fenced_page_op(task_id: Optional[str], fn) -> str:
  - `tools/browser_vault_tool.py@-697,3 +1347,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-697,3 +1347,4#moved` moved: `tests/tools/test_bot_desktop_browser_fence.py`, `tools/browser_use_cli.py`
- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-7 +8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _fenced_page_op(task_id: Optional[str], fn) -> str:
  - `tools/browser_vault_tool.py@-701 +1352,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _handle_vault(args: dict[str, Any], **kwargs) -> str:
  - `tools/browser_vault_tool.py@-705,0 +1358#moved` moved: `tools/browser_extension_router.py`, `tools/browser_tool_cdp.py`, `tools/browser_tool_eval_policy.py`, `tools/browser_tool_vision.py`
  - `tools/browser_vault_tool.py@-706,0 +1360,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-706,0 +1360,6#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4758 more)
  - `tools/browser_vault_tool.py@-710 +1369` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-714 +1373` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-716 +1375` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-733 +1392,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-733 +1392,10#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `agent/display.py`, `agent/inline_tool_executors.py` (+183 more)
- def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
  - `tools/browser_vault_tool.py@-92,2 +171,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-92,2 +171,11#moved` moved: `hermes_cli/tools_config_providers.py`, `tests/tools/test_browser_profile_live_cdp.py`, `tools/browser_tool.py`
  - `tools/browser_vault_tool.py@-97,8 +185,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/browser_vault_tool.py@-97,8 +185,22#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/entry.py`, `acp_adapter/model_catalog.py` (+2042 more)

### `tools/delegate_tool.py`

- def _build_child_agent(
  - `tools/delegate_tool.py@-314 +334` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/delegate_tool_child_run.py`

- def _build_tool_trace(messages: Any) -> list[dict[str, Any]]:
  - `tools/delegate_tool_child_run.py@-642,0 +664,27#moved` moved: `agent/preflight_output_fit.py`, `agent/skill_review_gate.py`, `hermes_state_rewind.py`, `run_agent.py` (+1 more)

### `tools/delegate_tool_resumable.py`

- def plan_recovery(path: Optional[Path], alive) -> dict[str, tuple[Any, Any, list
  - `tools/delegate_tool_resumable.py@-506 +506` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/delegate_tool_resumable.py@-515,0 +516,29` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/delegate_tool_resumable.py@-515,0 +516,29#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+4651 more)

### `tools/environments/base_session_env.py`

- def _export_dump_excluding_session_vars(tmp_path: str, excluded_names: Iterable[
  - `tools/environments/base_session_env.py@-117 +123,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _snapshot_bootstrap_script(
  - `tools/environments/base_session_env.py@-142 +161,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _wrap_command_script(
  - `tools/environments/base_session_env.py@-166 +186,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/environments/base_session_env.py@-179 +202,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/environments/base_session_env.py@-204 +236` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _split_cwd_marker(output: str, marker: str) -> tuple[str | None, str] | None
  - `tools/environments/base_session_env.py@-225 +264,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/process_registry.py`

- class ProcessRegistry(ProcessTerminationMixin, ProcessCheckpointMixin, ProcessOu
  - `tools/process_registry.py@-1192 +1261,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-1417,47 +1447` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-1573 +1562,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-1888 +1866` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2007,0 +1995` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2009,0 +1998` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2011,2 +2000,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2041,0 +2033` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2072,0 +2067` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2198 +2212` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2241 +2274,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2370,2 +2415,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2521,2 +2594,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2527,4 +2606,429` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- _SESSION_ACTIONS = {
  - `tools/process_registry.py@-2680,0 +3197,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry.py@-2680,0 +3197,8#moved` moved: `tools/process_registry_handoff.py`
- from tools.process_registry_env_log import fallback_exit_code, log_delta_command
  - `tools/process_registry.py@-44,0 +56` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/process_registry_results.py`

- def _owns_result(owner: str, parent: str | None) -> bool:
  - `tools/process_registry_results.py@-84 +279,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/process_registry_results.py@-84 +279,7#moved` moved: `cron/delivery_queue.py`, `cron/missed_runs.py`, `gateway/delivery_recovery.py`, `hermes_cli/sessions_inspect.py` (+3 more)

### `tools/session_search_tool.py`

- import logging
  - `tools/session_search_tool.py@-12,0 +13#moved` moved: `agent/context_compressor.py`, `agent/context_compressor_durable.py`, `agent/conversation_compression_publication.py`, `agent/kanban_turn_recovery.py` (+805 more)
- from datetime import datetime, timezone, UTC
  - `tools/session_search_tool.py@-16 +17#moved` moved: `cron/scheduler_ownership.py`, `gateway/stream_events.py`
- def _get_message_storage_state(db, message_id) -> Optional[dict[str, Any]]:
  - `tools/session_search_tool.py@-217 +222` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-219,3 +224` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _hydrate_hit(db, lineage_root: str, match_info: dict[str, Any], result_detai
  - `tools/session_search_tool.py@-419,0 +423,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _discover(db, query: str, role_filter: Optional[list[str]], limit: int, sort
  - `tools/session_search_tool.py@-461 +466,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _quiet(fn, default, msg, *log_args, with_exc: bool = False):
  - `tools/session_search_tool.py@-61 +62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-63,0 +65,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _scroll(db, session_id: str, around_message_id: int, window: int = 5,
  - `tools/session_search_tool.py@-728,0 +735,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-728,0 +735,7#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/session_incognito_boundary.py`, `gateway/telegram_topic_sessions.py`, `hermes_cli/config_migrations_alias_routing.py` (+185 more)
- def _loud(fn, log_msg, error_prefix, *log_args):
  - `tools/session_search_tool.py@-73,0 +77,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _dispatch(query, role_filter, limit, db, current_session_id, session_id,
  - `tools/session_search_tool.py@-732,21 +745,62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-732,21 +745,62#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+11626 more)
- def session_search(query: str = "", role_filter: str | None = None, limit: int |
  - `tools/session_search_tool.py@-756,3 +810,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-761,2 +817#moved` moved: `tests/persistence_sqlite/core/test_search_isolation_review.py`
  - `tools/session_search_tool.py@-791,4 +846,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-791,4 +846,4#moved` moved: `gateway/session_kanban.py`, `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_workspace.py`, `hermes_cli/sqlite_safe_read.py` (+18 more)
  - `tools/session_search_tool.py@-799,3 +854,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-799,3 +854,6#moved` moved: `hermes_cli/backup_restore.py`, `hermes_cli/data_cleanup_holders.py`, `hermes_cli/immutable_releases.py`, `hermes_cli/kanban_db_connect.py` (+11 more)
  - `tools/session_search_tool.py@-804 +862,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/session_search_tool.py@-804 +862,5#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+1471 more)
- SESSION_SEARCH_SCHEMA = {
  - `tools/session_search_tool.py@-857,3 +919,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- registry.register(
  - `tools/session_search_tool.py@-956,0 +1022` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/skills_tool_dedup.py`

- (module)
  - `tools/skills_tool_dedup.py@-2,6 +2,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/terminal_tool.py`

- def _plan_execution(
  - `tools/terminal_tool.py@-1334 +1392` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _run_foreground(
  - `tools/terminal_tool.py@-1422 +1452,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1425 +1456,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1427 +1460,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1428,0 +1463#moved` moved: `agent/azure_identity_adapter.py`, `agent/credential_pool.py`, `hermes_cli/plugins_cmd.py`, `hermes_cli/web_server_oauth.py` (+3 more)
  - `tools/terminal_tool.py@-1448,0 +1484,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1448,0 +1484,13#moved` moved: `tests/tools/test_terminal_tool_semaphore.py`, `tools/code_kernel.py`
  - `tools/terminal_tool.py@-1452,0 +1502,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1455,0 +1507,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def terminal_tool(
  - `tools/terminal_tool.py@-1597,0 +1644,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1602,0 +1653,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1603,0 +1657,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1603,0 +1657,3#moved` moved: `agent/acp_client.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py`, `agent/auxiliary_client.py` (+795 more)
  - `tools/terminal_tool.py@-1613,0 +1670,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1613,0 +1670,4#moved` moved: `agent/title_generator.py`, `gateway/run_plugin_injection.py`, `gateway/shutdown_flush.py`, `tools/cronjob_tools.py` (+2 more)
  - `tools/terminal_tool.py@-1618,0 +1681,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1669,0 +1736,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1673,0 +1742,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1674,0 +1751,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- TERMINAL_SCHEMA = {
  - `tools/terminal_tool.py@-1733 +1818` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1746 +1831` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1761,0 +1847,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1771,0 +1862,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-1771,0 +1862,5#moved` moved: `tests/tools/test_node_file_policy.py`, `tools/file_tools.py`
- def _handle_terminal(args, **kw):
  - `tools/terminal_tool.py@-1819,0 +1916,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Use read_file, search_files, patch and write_file instead of cat/head/tail, grep
  - `tools/terminal_tool.py@-183 +187,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _handle_terminal(args, **kw):
  - `tools/terminal_tool.py@-1830 +1931,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from dataclasses import dataclass
  - `tools/terminal_tool.py@-30 +30,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_container_task_id(task_id: Optional[str]) -> str:
  - `tools/terminal_tool.py@-542 +596,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-546 +603` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-553 +610` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-568,0 +632,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-574,2 +640,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _safe_parse_import_env(name: str, default: Any, converter, type_label: str):
  - `tools/terminal_tool.py@-77 +78,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _get_env_config() -> dict[str, Any]:
  - `tools/terminal_tool.py@-889 +963,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-911 +987` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-913 +989#moved` moved: `apps/desktop/electron/main.ts`, `apps/desktop/src/app/settings/delegation-settings-integration.test.tsx`, `evals/provider_fallback/probe_104260.py`, `evals/provider_fallback/probe_104360.py` (+74 more)
  - `tools/terminal_tool.py@-945,0 +1022` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-957,0 +1035,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool.py@-957,0 +1035,5#moved` moved: `agent/auxiliary_task_config.py`, `agent/azure_identity_adapter.py`, `agent/fallback_session_route.py`, `agent/transports/chat_completions.py` (+48 more)

### `tools/terminal_tool_background.py`

- def _spawn(process_registry, *, env, env_type, command, cwd, effective_task_id,
  - `tools/terminal_tool_background.py@-104 +169,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _apply_async_support(proc_session, result_data, notify_on_complete, watch_pa
  - `tools/terminal_tool_background.py@-127,0 +197,54` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool_background.py@-127,0 +197,54#moved` moved: `agent/checkpoints/pruning.py`, `agent/plugin_session_fork.py`, `agent/relay_runtime.py`, `agent/side_question_lease.py` (+58 more)
- def spawn_background_process(
  - `tools/terminal_tool_background.py@-172,0 +320,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/terminal_tool_background.py@-238,0 +387,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _looks_like_homebrew_ci_poller(command: str) -> bool:
  - `tools/terminal_tool_background.py@-84 +146` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _stamp_gateway_routing(proc_session, get_session_env) -> None:
  - `tools/terminal_tool_background.py@-92 +154,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _spawn(process_registry, *, env, env_type, command, cwd, effective_task_id,
  - `tools/terminal_tool_background.py@-96 +159,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tools/tool_result_storage.py`

- def referenced_spillover_names(spill_dir) -> set | None:
  - `tools/tool_result_storage.py@-108,0 +109#moved` moved: `hermes_state_media.py`
  - `tools/tool_result_storage.py@-118,3 +119,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/tool_result_storage.py@-118,3 +119,4#moved` moved: `cron/delivery_queue.py`, `cron/executions.py`, `cron/incidents.py`, `cron/notepad.py` (+125 more)
  - `tools/tool_result_storage.py@-128,0 +131#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _write_to_spillover(content: str, filename: str):
  - `tools/tool_result_storage.py@-203,0 +207,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+55 more)
- def maybe_persist_tool_result(content: str, tool_name: str, tool_use_id: str, en
  - `tools/tool_result_storage.py@-443 +449,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/tool_result_storage.py@-443 +449,5#moved` moved: `acp_adapter/edit_approval.py`, `agent/anthropic_adapter.py`, `agent/chat_completion_helpers.py`, `agent/history_commentary.py` (+57 more)
- def enforce_turn_budget(tool_messages: list[dict], env=None,
  - `tools/tool_result_storage.py@-524 +534` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/tool_result_storage.py@-538,0 +549` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/tool_result_storage.py@-541 +552` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tools/tool_result_storage.py@-549 +560` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/agent_factory.py`

- def _stored_session_runtime_overrides(row: dict | None) -> dict:
  - `tui_gateway/agent_factory.py@-129,0 +160,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-129,0 +160,2#moved` moved: `gateway/platforms/api_server.py`
- def _resolve_model() -> str:
  - `tui_gateway/agent_factory.py@-15 +15,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-15 +15,5#moved` moved: `hermes_cli/status.py`
- def _stored_session_runtime_overrides(row: dict | None) -> dict:
  - `tui_gateway/agent_factory.py@-174,2 +206,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _persist_live_session_runtime(session: dict | None) -> None:
  - `tui_gateway/agent_factory.py@-225,0 +260,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-225,0 +260,4#moved` moved: `agent/chat_completion_helpers.py`, `agent/fallback_route_recovery.py`, `hermes_state_mutation_prepared.py`, `hermes_state_sessions.py`
  - `tui_gateway/agent_factory.py@-232,0 +271,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _load_reasoning_config(model: str = "") -> dict | None:
  - `tui_gateway/agent_factory.py@-274,2 +314` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-277 +316,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_model() -> str:
  - `tui_gateway/agent_factory.py@-28,0 +33,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-28,0 +33,18#moved` moved: `acp_adapter/session.py`, `agent/context_compressor.py`, `agent/conversation_compression_feasibility.py`, `agent/reasoning_params.py` (+29 more)
- def _config_model_target() -> tuple[str, str]:
  - `tui_gateway/agent_factory.py@-49,2 +71,10` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _tool_lifecycle_required_for_ui(name: str) -> bool:
  - `tui_gateway/agent_factory.py@-510 +550` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_runtime_with_fallback(resolve_kwargs: dict | None = None) -> _Runti
  - `tui_gateway/agent_factory.py@-518 +558,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-518 +558,5#moved` moved: `agent/agent_init_fallback.py`, `cron/scheduler.py`, `gateway/run_config_loaders.py`, `hermes_cli/cli_agent_setup_mixin.py` (+3 more)
  - `tui_gateway/agent_factory.py@-531,2 +575,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-538,0 +590,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _resolve_agent_model_runtime(model_override, provider_override) -> tuple[str
  - `tui_gateway/agent_factory.py@-574 +632,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _make_agent(
  - `tui_gateway/agent_factory.py@-656,0 +716` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-661,0 +722,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-661,0 +722,2#moved` moved: `tui_gateway/model_switch.py`
  - `tui_gateway/agent_factory.py@-686,0 +749` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-692,0 +756,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/agent_factory.py@-692,0 +756,2#moved` moved: `gateway/platforms/api_server.py`

### `tui_gateway/change_watcher.py`

- def _pet_changed_payload() -> dict:
  - `tui_gateway/change_watcher.py@-191,14 +191,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _session_db_content_sig(db_path: Path):
  - `tui_gateway/change_watcher.py@-263,2 +238` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/change_watcher.py@-266,2 +240,38` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/change_watcher.py@-266,2 +240,38#moved` moved: `agent/agent_runtime_helpers.py`, `agent/kanban_turn_recovery.py`, `agent/shared_primary_cooldown.py`, `cron/scheduler_review.py` (+303 more)

### `tui_gateway/contracts/canonical_sessions.py`

- class CanonicalListParams(ProfileParams):
  - `tui_gateway/contracts/canonical_sessions.py@-185 +185` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/contracts/groups_bot_relay.py`

- (module)
  - `tui_gateway/contracts/groups_bot_relay.py@-1 +1,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- method("bot_relay.reply", params=BotRelayReplyParams, result=OkResult,
  - `tui_gateway/contracts/groups_bot_relay.py@-671,0 +673,65` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/contracts/groups_bot_relay.py@-671,0 +673,65#moved` moved: `agent/subagent_lifecycle.py`, `gateway/browser_control_broker.py`, `gateway/session_contract.py`, `hermes_cli/gateway_runtime.py` (+9 more)

### `tui_gateway/methods_prompt.py`

- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_prompt.py@-1211,0 +1226,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _final_response_text(result) -> str:
  - `tui_gateway/methods_prompt.py@-1430,11 +1446,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_prompt.py@-1430,11 +1446,3#moved` moved: `acp_adapter/server.py`, `gateway/platforms/webhook.py`, `hermes_cli/cli_chat_turn_mixin.py`, `hermes_cli/cli_single_query.py` (+4 more)
- def _validated_preview_cwd(cwd: str) -> str:
  - `tui_gateway/methods_prompt.py@-1446 +1454#moved` moved: `acp_adapter/content.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+3476 more)
- def _run_after_agent_ready(
  - `tui_gateway/methods_prompt.py@-676 +677` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_prompt.py@-707 +708,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _claim_submit_turn(
  - `tui_gateway/methods_prompt.py@-836,2 +838` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_prompt.py@-909,0 +911,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_prompt.py@-909,0 +911,12#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4747 more)
  - `tui_gateway/methods_prompt.py@-932 +945` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_prompt.py@-962 +975,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_prompt.py@-990 +1004` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/methods_tools.py`

- from pathlib import Path
  - `tui_gateway/methods_tools.py@-11,0 +16#moved` moved: `tui_gateway/agent_callbacks.py`, `tui_gateway/compute_host_bridge.py`, `tui_gateway/prompt_turn.py`, `tui_gateway/session_lifecycle.py`
- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_tools.py@-1210 +1232,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-1212 +1237` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_moa(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-684,2 +689,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-687,4 +692,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-687,4 +692,20#moved` moved: `tui_gateway/prompt_turn.py`
  - `tui_gateway/methods_tools.py@-698 +719,2#moved` moved: `tui_gateway/prompt_turn.py`
- Helper names must not collide with server.py's own (``_cmd_`` / ``_toolset_`` /
  - `tui_gateway/methods_tools.py@-7,0 +8,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_retry(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-736,0 +759#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`
  - `tui_gateway/methods_tools.py@-747 +770` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-754 +777,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _cmd_goal(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-781 +805,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-781 +805,2#moved` moved: `tui_gateway/prompt_turn.py`
  - `tui_gateway/methods_tools.py@-786,4 +811,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/methods_tools.py@-786,4 +811,6#moved` moved: `evals/process_result_receipt_probe.py`, `gateway/session_viewer_automation.py`, `gateway/slash_commands_goals.py`, `tests/gateway/test_goal_peer_rpc.py` (+6 more)

### `tui_gateway/model_switch.py`

- def _switch_request(raw_input: str, parsed_flags, persist_override) -> tuple[str
  - `tui_gateway/model_switch.py@-176,0 +190,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _snapshot_agent_model_runtime(agent) -> dict:
  - `tui_gateway/model_switch.py@-23 +24,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-29,0 +35,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/model_switch.py@-29,0 +35,8#moved` moved: `agent/route_binding.py`, `tests/agent/test_switch_model_reasoning_override.py`
- def _apply_model_switch(
  - `tui_gateway/model_switch.py@-294,0 +321` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-32,2 +45,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/model_switch.py@-41,2 +58` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _apply_switch_reasoning(sid: str, session, agent, effort: str, *, persist_gl
  - `tui_gateway/model_switch.py@-423,0 +452,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/model_switch.py@-423,0 +452,4#moved` moved: `gateway/slash_commands_model.py`
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-56,2 +70` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/prompt_turn.py`

- def _complete_turn_payload(session: dict, st: _TurnRun, status_note: str | None,
  - `tui_gateway/prompt_turn.py@-950,3 +1093,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/server.py`

- def _clear_pending(sid: str | None = None) -> None:
  - `tui_gateway/server.py@-1108,0 +1438,208` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1108,0 +1438,208#moved` moved: `acp_adapter/commands.py`, `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py` (+1177 more)
- _BOOL_WORDS = {
  - `tui_gateway/server.py@-1257,0 +1795,271` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1257,0 +1795,271#moved` moved: `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/session.py`, `acp_adapter/tools.py` (+541 more)
- def _session_info(agent, session: dict | None = None) -> dict:
  - `tui_gateway/server.py@-1505 +2313` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1507 +2315` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1511 +2319#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `agent/display.py`, `agent/inline_tool_executors.py` (+183 more)
  - `tui_gateway/server.py@-1515 +2323` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- class _RuntimeFallbackResolution(NamedTuple):
  - `tui_gateway/server.py@-1602,0 +2411,228` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1602,0 +2411,228#moved` moved: `acp_adapter/auth.py`, `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py` (+3130 more)
- def _hydrate_session_cwd(sid: str, key: str, session_db, profile_home: str | Non
  - `tui_gateway/server.py@-1656,0 +2693,443` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-1656,0 +2693,443#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2275 more)
- def _finish_reload(rid, params: dict, *, coalesced: bool) -> dict:
  - `tui_gateway/server.py@-1946,0 +3426,9#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/server.py` (+3358 more)
- _WORKER_BLOCKED_COMMANDS: frozenset[str] = frozenset({"snapshot", "snap"})
  - `tui_gateway/server.py@-1956,0 +3445,51#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/server.py`, `acp_adapter/session.py` (+984 more)
- from . import (
  - `tui_gateway/server.py@-2005 +3542` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-2017,0 +3555,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- for _m in (
  - `tui_gateway/server.py@-2024 +3563` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- del _m
  - `tui_gateway/server.py@-2031,0 +3571,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _shutdown_sessions() -> None:
  - `tui_gateway/server.py@-262,8 +277` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
  - `tui_gateway/server.py@-47 +47,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-47 +47,2#moved` moved: `hermes_state_gateway.py`
- def write_json(obj: dict) -> bool:
  - `tui_gateway/server.py@-584 +576,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- logger = logging.getLogger(__name__)
  - `tui_gateway/server.py@-62,0 +64,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-62,0 +64,14#moved` moved: `agent/acp_client.py`, `agent/auxiliary_health.py`, `agent/nous_rate_guard.py`, `agent/shared_primary_cooldown.py` (+176 more)
- def _current_session_steer_authority(session_id: str) -> tuple[Transport | None,
  - `tui_gateway/server.py@-817,0 +812,322` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-817,0 +812,322#moved` moved: `acp_adapter/commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/agent_init.py` (+577 more)
- def _active_config_path() -> Path:
  - `tui_gateway/server.py@-854 +1170,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _set_session_context(session_key: str, cwd: str | None = None, *, ui_session
  - `tui_gateway/server.py@-933,0 +1251` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-941,0 +1260,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-941,0 +1260,9#moved` moved: `tui_gateway/session_transports.py`
  - `tui_gateway/server.py@-944 +1271,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/server.py@-944 +1271,3#moved` moved: `gateway/platforms/api_server.py`

### `tui_gateway/session_compression.py`

- def _apply_live_compression_config(agent: Any, cfg: dict | None) -> None:
  - `tui_gateway/session_compression.py@-121,2 +134,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/session_compression.py@-123,0 +138,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/session_compression.py@-123,0 +138,3#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2064 more)
- def _derived_default_threshold_percent(agent: Any, compression: dict) -> float:
  - `tui_gateway/session_compression.py@-76 +76,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/session_compression.py@-76 +76,14#moved` moved: `acp_adapter/session.py`, `acp_adapter/tools.py`, `agent/api_error_summary.py`, `agent/api_request_hooks.py` (+395 more)
- _COMPRESSION_INT_KEYS = (
  - `tui_gateway/session_compression.py@-78,5 +91,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `tui_gateway/session_reaper.py`

- def _repair_missing_ws_orphan_reaps() -> None:
  - `tui_gateway/session_reaper.py@-308,3 +315,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- def _schedule_startup_orphan_sweep() -> None:
  - `tui_gateway/session_reaper.py@-582,0 +592,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `tui_gateway/session_reaper.py@-582,0 +592,16#moved` moved: `agent/relay_runtime.py`, `hermes_cli/cli_config_load.py`, `hermes_cli/cli_tui_runtime_mixin.py`, `hermes_cli/source_build.py` (+8 more)

### `tui_gateway/session_workdir.py`

- def _rewind_active_session_history(
  - `tui_gateway/session_workdir.py@-877 +877,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `uv.lock`

- wheels = [
  - `uv.lock@-1258,0 +1527,24` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-1329,0 +1622,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-1549 +1856,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-1727,0 +2063,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-1776,0 +2125,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-1807,0 +2165,194` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-1823,0 +2375,117` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-1832,0 +2501,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-1836,0 +2519,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- grpc = [
  - `uv.lock@-2123,0 +2893,57` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-2127,0 +2954,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-2318,0 +3158` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- documents = [
  - `uv.lock@-2406,5 +3246,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- google-meet = [
  - `uv.lock@-2444,2 +3293,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- kittentts = [
  - `uv.lock@-2451,3 +3306,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- piper = [
  - `uv.lock@-2490 +3348` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- stt-whisper = [
  - `uv.lock@-2504,2 +3362,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- voice = [
  - `uv.lock@-2554,2 +3412,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-2822,0 +3699,124` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-2996 +3989,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-3206,0 +4213,24` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-3215,0 +4246,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- requires-dist = [
  - `uv.lock@-3289,0 +4353,188` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-3314,0 +4566,144` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-3318,0 +4714,3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-3437,0 +4876,53` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-4002,0 +5625,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-4367 +6010` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-4446,0 +6090,14` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-4477,0 +6135,32` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-4534,0 +6272,54` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-4596,0 +6397,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-4775,0 +6598,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-4781 +6612,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- resolution-markers = [
  - `uv.lock@-5,2 +5,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-504,0 +585,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-5098,0 +6981,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-5497 +7442` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-5557,4 +7502,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-577,0 +680,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-6242,0 +8420,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-6292,0 +8490,13` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-6496,0 +8699,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-6590,0 +8802,48` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- dependencies = [
  - `uv.lock@-6822 +9146,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- wheels = [
  - `uv.lock@-697,0 +822,44` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-7051,0 +9389,9` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-7442,0 +9845,18` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-7637,0 +10067,133` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-966,0 +1131,19` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `uv.lock@-993,0 +1177,28` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `website/docs/developer-guide/programmatic-integration.md`

- Build on the methods the gateway serves. The methods answered by an older handle
  - `website/docs/developer-guide/programmatic-integration.md@-103 +103` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `website/docs/reference/mcp-config-reference.md`

- mcp_servers:
  - `website/docs/reference/mcp-config-reference.md@-36,0 +37` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/reference/mcp-config-reference.md@-60,0 +62` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `website/docs/user-guide/configuration.md`

- compression:
  - `website/docs/user-guide/configuration.md@-1114 +1185` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- The value is the **first rung** of an escalating ladder, not a fixed interval: c
  - `website/docs/user-guide/configuration.md@-1146 +1217` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-1160 +1231` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- When the summary streams (compression always streams over chat-completions and R
  - `website/docs/user-guide/configuration.md@-1210 +1281` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- default.
  - `website/docs/user-guide/configuration.md@-1241,0 +1313,15` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- For a user/plugin policy gate at the same point — keep the agent going with yo
  - `website/docs/user-guide/configuration.md@-1442 +1528` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- goals:
  - `website/docs/user-guide/configuration.md@-1446 +1532,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-1450,0 +1540,12` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- If you do not want Hermes to auto-generate titles after the first exchange, set
  - `website/docs/user-guide/configuration.md@-1605,0 +1707,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- still disables both stages.
  - `website/docs/user-guide/configuration.md@-1611,0 +1719,29` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- auxiliary:
  - `website/docs/user-guide/configuration.md@-1764,0 +1901` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Run `hermes setup --portal` — one OAuth gets you a model provider and all four
  - `website/docs/user-guide/configuration.md@-19 +19,66` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-19 +19,66#moved` moved: `CANDIDATE_SLICE2_CONFIG.yaml`, `hermes_cli/config_migrations_delegation_roles.py`
- agent:
  - `website/docs/user-guide/configuration.md@-2153,0 +2291,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-2163 +2302` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-2165 +2304` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Differences from `agent.service_tier`: the tier is always on for that provider (
  - `website/docs/user-guide/configuration.md@-2188,0 +2328,21` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-2188,0 +2328,21#moved` moved: `hermes_cli/models_fast_route.py`, `plugin-catalog/0bull.yaml`, `plugin-catalog/0g-provider.yaml`, `plugin-catalog/0xarchive.yaml` (+2607 more)
- web:
  - `website/docs/user-guide/configuration.md@-2984,0 +3145` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-2995,0 +3157,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3012,0 +3178,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3021 +3188` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- browser:
  - `website/docs/user-guide/configuration.md@-3030,0 +3198,7` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3050,0 +3225,5` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3050,0 +3225,5#moved` moved: `website/docs/user-guide/features/browser.md`
- instead of failing silently.
  - `website/docs/user-guide/configuration.md@-3060 +3239` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- See the [browser feature page](./features/browser.md#browser_dialog) for the ful
  - `website/docs/user-guide/configuration.md@-3071,9 +3250,8` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- notice, shown the same way, naming the path it pinned so you can check your brow
  - `website/docs/user-guide/configuration.md@-3088,0 +3267,11` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3088,0 +3267,11#moved` moved: `website/docs/developer-guide/browser-vault-plugin.md`, `website/docs/user-guide/features/credential-vault.md`
- delegation:
  - `website/docs/user-guide/configuration.md@-3262,0 +3452,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3262,0 +3452,2#moved` moved: `website/docs/user-guide/features/fallback-providers.md`
  - `website/docs/user-guide/configuration.md@-3268,0 +3460,31` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/configuration.md@-3268,0 +3460,31#moved` moved: `.github/ISSUE_TEMPLATE/bug_report.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/ISSUE_TEMPLATE/setup_help.yml`, `CANDIDATE_SLICE2_CONFIG.yaml` (+13 more)
  - `website/docs/user-guide/configuration.md@-3270,0 +3493,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- terminal:
  - `website/docs/user-guide/configuration.md@-430,0 +496` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Parallel subagents spawned via `delegate_task(tasks=[...])` share this one conta
  - `website/docs/user-guide/configuration.md@-510,0 +577,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Every key under `terminal:` has an env-var override of the form `TERMINAL_<KEY_U
  - `website/docs/user-guide/configuration.md@-522,0 +593` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Every terminal command on the **local** and **SSH** backends runs in a fresh `ba
  - `website/docs/user-guide/configuration.md@-851 +922` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `website/docs/user-guide/features/credential-vault.md`

- reaches a sign-in page it has no login for, it asks you, right there, in a
  - `website/docs/user-guide/features/credential-vault.md@-10,2 +10,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- with the same approval prompt as a dangerous command; declining writes nothing.
  - `website/docs/user-guide/features/credential-vault.md@-111,3 +150,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- Items live encrypted under `~/.hermes/vault/`, scoped to the profile. Labels,
  - `website/docs/user-guide/features/credential-vault.md@-123,2 +165,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- That is why keychain storage is opt-in rather than the default.
  - `website/docs/user-guide/features/credential-vault.md@-176,0 +221,64` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- vault:
  - `website/docs/user-guide/features/credential-vault.md@-194,0 +303,22` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
  - `website/docs/user-guide/features/credential-vault.md@-197,2 +327,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- refused unless the page origin exactly matches the saved origin, checked again
  - `website/docs/user-guide/features/credential-vault.md@-201,6 +331,4` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- against a compromised right one.
  - `website/docs/user-guide/features/credential-vault.md@-213,0 +342,6` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- title: Passwords & Logins
  - `website/docs/user-guide/features/credential-vault.md@-3 +3` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- registered with the redactor so a later page read cannot echo it back.
  - `website/docs/user-guide/features/credential-vault.md@-36,0 +39,16` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- CLI through its non-interactive channel (`op signin` on stdin, `bw unlock
  - `website/docs/user-guide/features/credential-vault.md@-78 +96,2` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line
- or the switch in **Settings → Passwords & Logins**.
  - `website/docs/user-guide/features/credential-vault.md@-85,0 +105,20` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

### `website/docs/user-guide/features/plugins.md`

- Plugins can register the 27 lifecycle events currently accepted by `hermes_cli.p
  - `website/docs/user-guide/features/plugins.md@-359 +359` intentionally dropped: no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line

