# Fork resolution

Lost-code audit for the v0.23.4 sync. Regenerate with `python scripts/fork_lost_code.py --write`; check with `python scripts/fork_lost_code.py`.

<!-- generated-by: scripts/fork_lost_code.py -->

## Items

### `MAINTENANCE.md`

- (module)
  - `MAINTENANCE.md@-0,0 +1,802` UNMARKED (6 lines): - **Source surfaces:** 'hermes_cli/_launchers.py' and 'tests/hermes_cli/test_source_launch

### `agent/agent_init.py`

- def _load_tools(agent, enabled_toolsets, disabled_toolsets):
  - `agent/agent_init.py@-1048 +1043` UNMARKED (1 lines): agent._safe_print(f"🔒 Ephemeral system prompt: '{prompt_preview}' (not saved to trajectori
- def _init_session_state(agent, session_id, session_db, parent_session_id, reason
  - `agent/agent_init.py@-1087,0 +1086,4` UNMARKED (1 lines): agent._persist_disabled = getattr(agent, "incognito", False)
  - `agent/agent_init.py@-1097 +1099,2` UNMARKED (2 lines): agent._incognito_session_db = session_db if agent.incognito else None
- def _init_memory(agent, _agent_cfg, skip_memory, platform, memory_manager=None):
  - `agent/agent_init.py@-1161,0 +1165#moved` moved: `agent/skill_review_gate.py`
  - `agent/agent_init.py@-1197,0 +1202` UNMARKED (1 lines): read_only=getattr(agent, "incognito", False),
  - `agent/agent_init.py@-1208,0 +1214` UNMARKED (1 lines): memory_manager.set_read_only(getattr(agent, "incognito", False))
  - `agent/agent_init.py@-1215 +1221,6` UNMARKED (5 lines): from agent.memory_manager import parse_prefetch_max_age as _parse_prefetch_max_age
- def _parse_compression_config(agent, _agent_cfg) -> CompressionSettings:
  - `agent/agent_init.py@-1374,0 +1386,3` UNMARKED (2 lines): _default_prune_tokens = int(
  - `agent/agent_init.py@-1410,2 +1424,4` UNMARKED (3 lines): # Proactive tool-result prune trigger: absent, null or invalid -> DEFAULT_CONFIG; 0 or neg
  - `agent/agent_init.py@-1426 +1442,2` UNMARKED (2 lines): threshold_tokens=threshold_tokens, agent_request_floor_ratio=max(0.0, float(cfg.get("agent
- def _build_context_engine(agent, _agent_cfg, cs, _custom_providers, _effective_c
  - `agent/agent_init.py@-1822 +1839` UNMARKED (1 lines): _selected_engine = None if getattr(agent, "incognito', False) else _select_context_engine(
  - `agent/agent_init.py@-1835 +1852` UNMARKED (1 lines): custom_providers=_custom_providers, api_mode=agent.api_mode,
  - `agent/agent_init.py@-1889 +1906` UNMARKED (1 lines): agent.compression_checkpoint_required = cs.checkpoint_required and not getattr(agent, "inc
  - `agent/agent_init.py@-1901,0 +1919,2` UNMARKED (2 lines): agent.compression_agent_request_floor_ratio, agent.compression_agent_request_cooldown_api_
- def init_agent(
  - `agent/agent_init.py@-2199 +2226` UNMARKED (1 lines): _build_context_engine(agent, _agent_cfg, cs, _custom_providers, _effective_context_length,
  - `agent/agent_init.py@-2207,0 +2235,5` UNMARKED (4 lines): # Gateway eviction and independent workers construct fresh agents. Adopt a durable primary
- _SESSION_STATE: dict[str, Any] = {
  - `agent/agent_init.py@-517,0 +516,5` UNMARKED (5 lines): # The user"s explicit session-scoped reasoning pick (/reasoning, --reasoning, /model
- _STREAM_STATE: dict[str, Any] = {
  - `agent/agent_init.py@-536 +539,2` UNMARKED (2 lines): "_current_streamed_assistant_text": "", # any-consumer text: recovery and liveness
  - `agent/agent_init.py@-538,3 +542` UNMARKED (1 lines): # Single-writer guard fences stale stream attempts from the delta sink.
- def _explicit_client_kwargs(agent, api_key, base_url, _provider_timeout) -> dict
  - `agent/agent_init.py@-743,2 +745,2` UNMARKED (2 lines): if _ph and (_headers := _ph.get_default_headers()):

### `agent/compaction_events.py`

- _TRIGGERS = frozenset({
  - `agent/compaction_events.py@-40 +40` UNMARKED (1 lines): "gateway_hygiene", "between_turns", "proactive_prune", "agent_requested", "provider", "unk

### `agent/context_compressor_prune.py`

- class ProactivePruneMixin:
  - `agent/context_compressor_prune.py@-108 +119` UNMARKED (1 lines): optional_archive_method(session_db)(
  - `agent/context_compressor_prune.py@-113 +124` UNMARKED (1 lines): held_for_cas=messages, held_messages=messages)
- import logging
  - `agent/context_compressor_prune.py@-12,0 +13#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2575 more)
- class ProactivePruneMixin:
  - `agent/context_compressor_prune.py@-126,0 +138,4` UNMARKED (4 lines): from agent.runtime_session_journal import OutboxCapacityError
  - `agent/context_compressor_prune.py@-138,0 +154,2#moved` moved: `agent/context_compressor.py`
  - `agent/context_compressor_prune.py@-27,0 +29,8` UNMARKED (5 lines): def _record_optional_archive_capacity_failure(self) -> None:
  - `agent/context_compressor_prune.py@-58,0 +68,2` UNMARKED (1 lines): if self._optional_archive_cooldown_until > time.monotonic():

### `agent/conversation_compression.py`

- _COMPRESSOR_ATTEMPT_STATE_FIELDS = (
  - `agent/conversation_compression.py@-187 +189` UNMARKED (1 lines): "_proactive_prune_rearm_tokens", "_optional_archive_cooldown_until", "_consecutive_archive
- from agent.usage_anchor import set_usage_anchor
  - `agent/conversation_compression.py@-44,0 +47#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)

### `agent/conversation_compression_manual.py`

- def compress_now(
  - `agent/conversation_compression_manual.py@-105,0 +143,26` UNMARKED (2 lines): snapshot_is_current=snapshot_is_current)

### `agent/conversation_loop.py`

- (module)
  - `agent/conversation_loop.py@-0,0 +1` UNMARKED (1 lines): # health: allow FILE_LINES -- request start timestamp belongs at the existing turn-loop se
- _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "
  - `agent/conversation_loop.py@-1006,3 +1013,3` UNMARKED (3 lines): _DROPPED_TOOL_ARGUMENTS_LARGE_THRESHOLD_CHARS = 8_000 * CHARS_PER_TOKEN
- def _get_continuation_prompt(is_partial_stub: bool, dropped_tools: Optional[list
  - `agent/conversation_loop.py@-1010,0 +1018,6` UNMARKED (6 lines): if (dropped_tool_argument_chars > _DROPPED_TOOL_ARGUMENTS_LARGE_THRESHOLD_CHARS
  - `agent/conversation_loop.py@-1012,7 +1025,3` UNMARKED (3 lines): f"{_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX}({tool_list}) was cut off by a transport inte
- def _compression_deferred_result(agent, messages: list[dict], api_call_count: in
  - `agent/conversation_loop.py@-1180,4 +1188,10` UNMARKED (4 lines): if block == "pool_saturated":
  - `agent/conversation_loop.py@-1191 +1205` UNMARKED (1 lines): "⏳ Context compression is already running for this session. Please retry in a moment — "
- def _arm_fallback_restart(agent, api_messages, active_system_prompt, _retry):
  - `agent/conversation_loop.py@-1272,0 +1305,6` UNMARKED (4 lines): # The previous route"s usage no longer describes the rebuilt request. A
  - `agent/conversation_loop.py@-1272,0 +1305,6#moved` moved: `acp_adapter/commands.py`, `agent/anthropic_thinking_replay.py`, `agent/chat_completion_helpers.py`, `agent/codex_runtime.py` (+26 more)
- def _preflight_timeout_result(agent, exc, conversation_history) -> dict[str, Any
  - `agent/conversation_loop.py@-1450 +1488` UNMARKED (1 lines): # funnel). Turn setup archived the rejected input without adding it to model replay (#7100
- def _run_conversation_turn(
  - `agent/conversation_loop.py@-1681 +1720,5` UNMARKED (3 lines): # in-place boundary would make a later uncompressed result look compacted. A shrink
  - `agent/conversation_loop.py@-1681 +1720,5#moved` moved: `agent/turn_finalizer.py`, `agent/turn_preflight_gate.py`
  - `agent/conversation_loop.py@-1740,0 +1785,4` UNMARKED (2 lines): # The unattended same-provider window is per outage within a turn: a failed turn"s expired
  - `agent/conversation_loop.py@-1758,0 +1807,3` UNMARKED (3 lines): before_api_call = getattr(agent, "_gateway_before_api_call", None)
  - `agent/conversation_loop.py@-1766,0 +1818,17` UNMARKED (9 lines): after_preflight = getattr(agent, "_gateway_after_preflight", None)
  - `agent/conversation_loop.py@-1769 +1837,3` UNMARKED (3 lines): s.api_start_time = time.time()
- def run_conversation(
  - `agent/conversation_loop.py@-1833,0 +1905#moved` moved: `agent/moa_loop.py`, `tests/agent/test_proxy_request_class.py`
  - `agent/conversation_loop.py@-1839,2 +1911,17` UNMARKED (10 lines): # Gateway and cron bind provenance in task-local session context rather than widening the
  - `agent/conversation_loop.py@-1843 +1930` UNMARKED (1 lines): with bind_aux_request_turn(agent), native_turn_images(user_message):
  - `agent/conversation_loop.py@-1863,0 +1952,2#moved` moved: `agent/turn_finalizer.py`, `agent/turn_preflight_gate.py`
- from __future__ import annotations
  - `agent/conversation_loop.py@-8,0 +10,4` UNMARKED (2 lines): from agent.conversation_loop_failed_turn import (
- def _restore_or_build_system_prompt(agent, system_message, conversation_history)
  - `agent/conversation_loop.py@-861,2 +864,2` UNMARKED (2 lines): # 'hermes sessions refresh-prompts' ran after this prompt was built. Restore tools[] first
- def _stored_prompt_matches_runtime(agent, prompt: str) -> bool:
  - `agent/conversation_loop.py@-983,0 +987,5` UNMARKED (4 lines): # The one exception is a prompt left by gateway hygiene compaction: that agent loads only

### `agent/fallback_notice.py`

- (module)
  - `agent/fallback_notice.py@-0,0 +1,94` UNMARKED (56 lines): """Notice policy for a bound fallback route and its backend credential surface."""
  - `agent/fallback_notice.py@-0,0 +1,94#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+11783 more)

### `agent/memory_context.py`

- _INTERNAL_NOTE_RE = re.compile(
  - `agent/memory_context.py@-18,2 +18,6#moved` moved: `session_rules/turns.py`
- def build_memory_context_block(raw_context: str) -> str:
  - `agent/memory_context.py@-194,3 +198,3` UNMARKED (3 lines): "[System note: The following is untrusted historical data, NOT new user "

### `agent/micro_compaction.py`

- class MicroCompactionMixin:
  - `agent/micro_compaction.py@-203,0 +204,2#moved` moved: `agent/context_compressor.py`
  - `agent/micro_compaction.py@-212 +214` UNMARKED (1 lines): if not self._micro_compact_enabled or self._optional_archive_cooldown_until > time.monoton
  - `agent/micro_compaction.py@-236 +238,5` UNMARKED (5 lines): # Snapshot each dict, not just the list: defrag and the user-turn merge rewrite live dicts
  - `agent/micro_compaction.py@-250,0 +257` UNMARKED (1 lines): _pre_flush_invalidated = self._flush_scan_cursor_invalidated
  - `agent/micro_compaction.py@-258 +265` UNMARKED (1 lines): sync_outcome = self._sync_micro_compact_to_db(
  - `agent/micro_compaction.py@-260,2 +267,3` UNMARKED (3 lines): ) if defragged else None
  - `agent/micro_compaction.py@-262,0 +271` UNMARKED (1 lines): self._flush_scan_cursor_invalidated = _pre_flush_invalidated
  - `agent/micro_compaction.py@-266 +275,3` UNMARKED (3 lines): if sync_outcome == "archive_capacity":
  - `agent/micro_compaction.py@-291 +301,2` UNMARKED (2 lines): sync_outcome = self._sync_micro_compact_to_db(result, held=_held, start_watermark=_start_w
  - `agent/micro_compaction.py@-295 +306,4` UNMARKED (4 lines): self._flush_scan_cursor_invalidated = _pre_flush_invalidated
  - `agent/micro_compaction.py@-302,3 +317,6` UNMARKED (5 lines): def _record_micro_failure(self, exchange_start: int, exchange_end: int, *, outcome: str =
  - `agent/micro_compaction.py@-302,3 +317,6#moved` moved: `agent/shared_primary_probe_runtime.py`, `apps/desktop/electron/challenge-window.ts`, `apps/desktop/electron/release-holder-consent.test.ts`, `apps/desktop/src/app/capabilities/connectors/data/hosted-actions.ts` (+36 more)
  - `agent/micro_compaction.py@-411 +429#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_unavailable.py`, `agent/bedrock_adapter.py`, `agent/context_compressor.py` (+71 more)
  - `agent/micro_compaction.py@-414,2 +432,2` UNMARKED (2 lines): summary and the originals. Returns a refusal outcome (''stale_generation'' or
  - `agent/micro_compaction.py@-436 +454,2` UNMARKED (2 lines): from agent.conversation_compression_archive import optional_archive_method
  - `agent/micro_compaction.py@-438,2 +457,2` UNMARKED (2 lines): covered_ids=covered_ids, unresolved_held=unresolved_held, held_for_cas=held,
  - `agent/micro_compaction.py@-448 +467` UNMARKED (1 lines): return "stale_generation"
  - `agent/micro_compaction.py@-453,2 +472,6` UNMARKED (5 lines): return "stale_generation"
  - `agent/micro_compaction.py@-512 +535,5` UNMARKED (5 lines): if _plain_user(msg) and isinstance(prev, dict) and _plain_user(prev):

### `agent/prompt_refresh.py`

- def rebuild_for_refresh(agent: Any, system_message: Any, conversation_history: A
  - `agent/prompt_refresh.py@-83,0 +85` UNMARKED (1 lines): from tools.mcp_tool_agent import refresh_agent_delegation_tool
- the prompt is older, rebuilds it once through the normal builder: history, sessi
  - `agent/prompt_refresh.py@-9 +9,2` UNMARKED (2 lines): the existing tools[] pin is kept, except that an explicit gateway delegation mode is conve
- def rebuild_for_refresh(agent: Any, system_message: Any, conversation_history: A
  - `agent/prompt_refresh.py@-93,0 +96,3` UNMARKED (3 lines): # This boundary already breaks the prompt prefix, so converge the cached

### `agent/session_persistence.py`

- from agent.message_metadata import (
  - `agent/session_persistence.py@-27 +27` UNMARKED (1 lines): DB_ROW_SNAPSHOT, MERGED_TURN_PREFIX, REPAIR_BOOKKEEPING_FIELDS, REPLAY_PERSISTED_CONTENT,

### `agent/status_output.py`

- from agent.session_activity import ActivityProvenance
  - `agent/status_output.py@-11,0 +12#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- class StatusOutputMixin:
  - `agent/status_output.py@-110 +120` UNMARKED (1 lines): def _emit_diagnostic_status(self, message: str, *, context: bool = False) -> None:
  - `agent/status_output.py@-114 +124` UNMARKED (1 lines): self._emit_status(message if isinstance(message, DiagnosticText) else DiagnosticText(Conte
  - `agent/status_output.py@-116 +126` UNMARKED (1 lines): def _emit_status(self, message: str, *, context: bool = False) -> None:
  - `agent/status_output.py@-118 +128` UNMARKED (1 lines): self._emit_status_kind("lifecycle", ContextNotice(message) if context else message, origin
  - `agent/status_output.py@-130,0 +141` UNMARKED (1 lines): notice_style = getattr(message, "notice_style", None)
  - `agent/status_output.py@-144 +155` UNMARKED (1 lines): notice = AgentNotice(text=message, level=level, kind="sticky", key=key, id=key, notice_sty
  - `agent/status_output.py@-255 +266` UNMARKED (1 lines): self._emit_diagnostic_status(ContextNotice(item))
  - `agent/status_output.py@-41,0 +43,5` UNMARKED (4 lines): print_fn = getattr(self, "_print_fn", None) # unset while __init__ is still printing
  - `agent/status_output.py@-43 +49` UNMARKED (1 lines): (print_fn or print)(*args, **kwargs)
  - `agent/status_output.py@-46,0 +53,4` UNMARKED (2 lines): def _private_turn(self) -> bool:
  - `agent/status_output.py@-46,0 +53,4#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)

### `agent/title_generator.py`

- def generate_title(
  - `agent/title_generator.py@-670,9 +1035,21` UNMARKED (2 lines): # Reject model answers before _clean_title"s 80-character cut hides their shape.
- def _has_upgraded_title(session_db, session_id: str) -> bool:
  - `agent/title_generator.py@-712 +1132` UNMARKED (1 lines): def _persist_session_title(session_db, session_id, title, *, source, dedupe=True, reject_c
- def auto_title_session(
  - `agent/title_generator.py@-808,5 +1250,68` UNMARKED (1 lines): fallback_only = hold_until_reply and user_turns > 3 and not _session_is_untitled(session_d
  - `agent/title_generator.py@-819 +1330,2` UNMARKED (1 lines): reject_collision=source == "llm")
- def maybe_auto_title(
  - `agent/title_generator.py@-899 +1494,2` UNMARKED (1 lines): or (user_msg_count > 3 and not _session_is_untitled(session_db, session_id)
  - `agent/title_generator.py@-932 +1530` UNMARKED (1 lines): kwargs={**upgrade_kwargs, "icon_context": icon_context},

### `agent/transcript_repair.py`

- def transcript_row_snapshot(row: Mapping[str, Any]) -> str:
  - `agent/transcript_repair.py@-73 +73,5#moved` moved: `persistence/sqlite/conversation/_store/transcript_repair.py`

### `agent/turn_finalizer.py`

- def _explain_abnormal_exit(agent, final_response, _turn_exit_reason, preserved_v
  - `agent/turn_finalizer.py@-445,0 +451` UNMARKED (1 lines): from agent.fallback_endpoint_domain import attempted_routes_summary
  - `agent/turn_finalizer.py@-449 +455` UNMARKED (1 lines): model=str(getattr(agent, "model", "") or ""), routes=attempted_routes_summary(agent),

### `agent/vault_login_classifier.py`

- def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginCon
  - `agent/vault_login_classifier.py@-115 +136` UNMARKED (1 lines): if _RE_EXCLUDED_PASSWORD.search(searchable) or classify_otp_controls([control]):
- _LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  - `agent/vault_login_classifier.py@-276 +566,3` UNMARKED (2 lines): groupLabel: paymentContext.label,
- def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str
  - `agent/vault_login_classifier.py@-305,0 +623,5` UNMARKED (4 lines): ''remember'' (login and one-time-code fills only): after setting a valid input, before its
  - `agent/vault_login_classifier.py@-314 +648` UNMARKED (1 lines): _FILL_JS_TEMPLATE = """(async () => {
- _FILL_JS_TEMPLATE = """(() => {
  - `agent/vault_login_classifier.py@-351,2 +790,69` UNMARKED (58 lines): function tickRemember(field) {
  - `agent/vault_login_classifier.py@-351,2 +790,69#moved` moved: `apps/bootstrap-installer/src-tauri/src/events.rs`, `apps/bootstrap-installer/src-tauri/src/marker_tests.rs`, `apps/bootstrap-installer/src-tauri/src/powershell.rs`, `apps/desktop/electron/native/hud-modifier-monitor.m` (+16 more)

### `apps/desktop/e2e/core/webapp-stale-journal.spec.ts`

- async function withWebapp(
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-235,0 +240,3` UNMARKED (3 lines): // The 4 x 32 tool rounds cross the 64k sandbox window"s compaction threshold mid-turn, an
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-238 +245,2` UNMARKED (2 lines): '\nwebapp:\n enabled: true\n port: ${await freePort()}\ntools:\n tool_search:\n defer: []\
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-240,0 +249#moved` moved: `apps/desktop/electron/connection-registry-remove.test.ts`, `apps/desktop/electron/gateway-file-download.ts`, `apps/desktop/electron/linux-nvidia-egl-recovery.native.test.ts`, `apps/desktop/electron/window-reveal.ts` (+4 more)
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-244,0 +254,4#moved` moved: `apps/desktop/e2e/at-rest-connection-token.spec.ts`, `apps/desktop/e2e/bot-screen-stale-status-reply.spec.ts`, `apps/desktop/e2e/core/harness.ts`, `apps/desktop/e2e/core/known.ts` (+340 more)
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-257 +270,26` UNMARKED (8 lines): keepGatewayLogsOnFailure(sandbox, failed)
  - `apps/desktop/e2e/core/webapp-stale-journal.spec.ts@-257 +270,26#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3162 more)

### `apps/desktop/src/app/session/hooks/use-prompt-actions/queue-if-busy.ts`

- export function queueKickoffIfSessionBusy({
  - `apps/desktop/src/app/session/hooks/use-prompt-actions/queue-if-busy.ts@-39 +41` UNMARKED (1 lines): if (serverOwnsComposerQueue(sessionId) || (!moaToken && !isTargetSessionBusy(states, sessi

### `apps/shared/src/gateway-contract.generated.ts`

- export interface BotRelayReplyParams {
  - `apps/shared/src/gateway-contract.generated.ts@-1973,0 +1974,37` UNMARKED (20 lines): export interface BrowserControllerRegisterParams {
  - `apps/shared/src/gateway-contract.generated.ts@-1973,0 +1974,37#moved` moved: `apps/desktop/e2e/core/webapp-gateway-attach.spec.ts`, `apps/desktop/src/app/types.ts`, `gateway/browser_control_broker.py`
- export interface PromptSubmitParams {
  - `apps/shared/src/gateway-contract.generated.ts@-2866,0 +2904` UNMARKED (1 lines): moa_token?: string | null
- export interface PromptSubmitResult {
  - `apps/shared/src/gateway-contract.generated.ts@-2890 +2930` UNMARKED (1 lines): export type PromptSubmitStatus = "streaming' | "queued' | "steered' | "redirected' | "drop
- export interface SessionCorrectionParams {
  - `apps/shared/src/gateway-contract.generated.ts@-3727,0 +3768` UNMARKED (1 lines): human?: boolean
- export interface CommandDispatchResult {
  - `apps/shared/src/gateway-contract.generated.ts@-3980,0 +4022,2` UNMARKED (1 lines): moa_token?: string | null
- export interface SlashExecResult {
  - `apps/shared/src/gateway-contract.generated.ts@-3999,0 +4043,2` UNMARKED (1 lines): moa_token?: string | null
- export interface BrowserManageParams {
  - `apps/shared/src/gateway-contract.generated.ts@-4153 +4199` UNMARKED (1 lines): export type BrowserAction = "status' | "connect' | "disconnect' | "use'
- export interface BrowserManageResult {
  - `apps/shared/src/gateway-contract.generated.ts@-4157,0 +4204` UNMARKED (1 lines): browser_use?: boolean | null
- export interface SessionPruneParams {
  - `apps/shared/src/gateway-contract.generated.ts@-4840,0 +4888,2` UNMARKED (2 lines): now?: boolean | null
- export interface CanonicalCorrectionParams {
  - `apps/shared/src/gateway-contract.generated.ts@-5369,0 +5419` UNMARKED (1 lines): human?: boolean
- export interface CanonicalSubmitParams {
  - `apps/shared/src/gateway-contract.generated.ts@-5428,0 +5479` UNMARKED (1 lines): turn_origin?: string | null
- export interface PaneRevealPayload {
  - `apps/shared/src/gateway-contract.generated.ts@-6274 +6325` UNMARKED (1 lines): /** ''tools/react_tool.py''. */
- export interface BrowserProgressPayload {
  - `apps/shared/src/gateway-contract.generated.ts@-6292,0 +6344,14` UNMARKED (9 lines): /** ''gateway/browser_control_broker.py'' FRAME_COMMAND params. */
- export interface RpcMethods {
  - `apps/shared/src/gateway-contract.generated.ts@-6380 +6445,9` UNMARKED (9 lines): /** Hard-detach only the controller owned by this authenticated transport. */
- export const RPC_METHODS = [
  - `apps/shared/src/gateway-contract.generated.ts@-6892,0 +6966,4` UNMARKED (4 lines): "browser.controller.detach',
- export interface BackendGatewayEventMap {
  - `apps/shared/src/gateway-contract.generated.ts@-7368,0 +7446,4` UNMARKED (4 lines): /** Withdraw a pending controller command. */
- export const GATEWAY_EVENT_TYPES = [
  - `apps/shared/src/gateway-contract.generated.ts@-7523,0 +7605,2` UNMARKED (2 lines): "browser.controller.cancel',

### `contributors/emails/ghgoodreau@protonmail.com`

- (module)
  - `contributors/emails/ghgoodreau@protonmail.com@-0,0 +1,2` UNMARKED (1 lines): # Upstream v0.23.1 contributor

### `cron/scheduler.py`

- def _finalize_cron_session(session_db, agent, job_id: str, job_name: str, cron_s
  - `cron/scheduler.py@-2153,3 +2239,6` UNMARKED (1 lines): lambda: getattr(_session_db, "get_next_title_in_lineage", lambda b: b)(f"cron {job_id}"),
  - `cron/scheduler.py@-2157 +2246` UNMARKED (1 lines): if _set_cron_session_title(_session_db, _final_cron_session_id, _fallback()):
- def run_job(
  - `cron/scheduler.py@-2631,2 +2661,9` UNMARKED (1 lines): (False, "", "", f"{type(named).__name__}: {named!s}"), script_error, job=job)[3]
  - `cron/scheduler.py@-2666,2 +2703,13` UNMARKED (7 lines): lambda: release_or_close(_session_db), job_id=job_id,
  - `cron/scheduler.py@-2666,2 +2703,13#moved` moved: `agent/moa_loop.py`, `agent/primary_auth_fallback.py`, `gateway/platforms/api_server.py`, `gateway/run_config_loaders.py` (+33 more)

### `gateway/delivery_ledger.py`

- def flood_retry_delay(seconds: Any) -> float:
  - `gateway/delivery_ledger.py@-136,0 +168,5` UNMARKED (3 lines): def flood_wait_is_absurd(last_error: Any) -> bool:
- def retry_not_before(updated_at: Any, last_error: Any, attempts: Any) -> Optiona
  - `gateway/delivery_ledger.py@-158,0 +195,2` UNMARKED (1 lines): if flood_wait_is_absurd(last_error):
- def _connect() -> sqlite3.Connection:
  - `gateway/delivery_ledger.py@-180 +218,5` UNMARKED (1 lines): return ram_connect("delivery:" + str(_db_path()), _initialize_schema)
  - `gateway/delivery_ledger.py@-180 +218,5#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+56 more)
  - `gateway/delivery_ledger.py@-182,4 +224,4` UNMARKED (4 lines): # Shared state.db writers must participate in SessionDB startup serialization
- def _initialize_schema(conn: sqlite3.Connection) -> None:
  - `gateway/delivery_ledger.py@-204 +246,6` UNMARKED (6 lines): adapter_profile TEXT,
  - `gateway/delivery_ledger.py@-208,0 +256,10` UNMARKED (9 lines): columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")}
- import time
  - `gateway/delivery_ledger.py@-21,0 +22#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_unavailable.py`, `agent/billing_usage.py` (+220 more)
- def compute_obligation_id(session_key: str, message_ref: str, content: str) -> s
  - `gateway/delivery_ledger.py@-269,0 +327,21` UNMARKED (9 lines): def _resume_marker_from_row(session_id: Any, token: Any, marked_at: Any) -> Optional[tuple
  - `gateway/delivery_ledger.py@-269,0 +327,21#moved` moved: `agent/account_usage.py`, `agent/account_usage_cache.py`, `agent/auxiliary_client.py`, `agent/codex_service_tiers.py` (+82 more)
- def record_obligation(*, obligation_id: str, session_key: str, platform: str, ch
  - `gateway/delivery_ledger.py@-271 +349,4` UNMARKED (3 lines): resume_marker: Optional[tuple] = None,
  - `gateway/delivery_ledger.py@-271 +349,4#moved` moved: `persistence/sqlite/gateway/_delivery_ledger.py`
  - `gateway/delivery_ledger.py@-279,2 +360,4` UNMARKED (4 lines): owner_pid, owner_started_at, adapter_profile,
  - `gateway/delivery_ledger.py@-282 +365,2` UNMARKED (2 lines): content, now, now, pid, started, str(adapter_profile).strip() if adapter_profile else "def
- def record_crash_left_reply(*, obligation_id: str, session_key: str, platform: s
  - `gateway/delivery_ledger.py@-290 +374,3` UNMARKED (3 lines): adapter_profile: Optional[str] = None,
  - `gateway/delivery_ledger.py@-293,2 +379,4` UNMARKED (4 lines): redelivered once, with the recovered marker. A Hermes-authored turn boundary (''boundary''
  - `gateway/delivery_ledger.py@-296,3 +384,5` UNMARKED (5 lines): values = (obligation_id, session_key, platform, str(chat_id), str(thread_id) if thread_id
  - `gateway/delivery_ledger.py@-301,2 +391,10` UNMARKED (6 lines): owner_pid, owner_started_at, adapter_profile, resume_turn_id, supervisor_checkin)
  - `gateway/delivery_ledger.py@-301,2 +391,10#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3121 more)
  - `gateway/delivery_ledger.py@-305,3 +403,14` UNMARKED (8 lines): (*values, session_key, content, since))
  - `gateway/delivery_ledger.py@-305,3 +403,14#moved` moved: `agent/context_compressor.py`, `agent/curator.py`, `agent/repetition_guard.py`, `agent/verification_evidence.py` (+85 more)
- def mark_attempting(obligation_id: str) -> None:
  - `gateway/delivery_ledger.py@-314,2 +423,9` UNMARKED (6 lines): def mark_delivered(obligation_id: str, result: Any = None) -> None:
  - `gateway/delivery_ledger.py@-314,2 +423,9#moved` moved: `cron/scheduler_delivery_media.py`, `gateway/delivery_recovery.py`, `gateway/platforms/base.py`, `gateway/platforms/base_exec_approval.py` (+9 more)
- def mark_failed(obligation_id: str, error: str = "") -> None:
  - `gateway/delivery_ledger.py@-318,0 +435,8` UNMARKED (6 lines): if flood_wait_is_absurd(error):
  - `gateway/delivery_ledger.py@-318,0 +435,8#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3098 more)
- def _claimed_row(oid, session_key, platform, chat_id, thread_id, content, attemp
  - `gateway/delivery_ledger.py@-354 +478,2` UNMARKED (2 lines): last_error: Optional[str] = None, resume_marker: Optional[tuple] = None,
  - `gateway/delivery_ledger.py@-356,10 +481,14` UNMARKED (13 lines): the rate limit refused, possibly after accepting part of it) gets FLOOD_MARKER at boot or
  - `gateway/delivery_ledger.py@-356,10 +481,14#moved` moved: `agent/display.py`, `agent/fallback_session_route.py`, `agent/plugin_live_notes.py`, `agent/session_persistence.py` (+88 more)
- _MAX_ROWS = 500
  - `gateway/delivery_ledger.py@-39,5 +40,3` UNMARKED (3 lines): # post-rejection retry) — honest at-least-once. The marker is intentionally neutral: it pr
- def sweep_recoverable(now: Optional[float] = None, *, deliverable_platforms: Opt
  - `gateway/delivery_ledger.py@-394 +523,3` UNMARKED (3 lines): owner_pid, owner_started_at, adapter_profile, last_error, updated_at,
  - `gateway/delivery_ledger.py@-396 +527,2` UNMARKED (2 lines): WHERE state IN ("pending', "attempting', "failed')
  - `gateway/delivery_ledger.py@-399 +531,2` UNMARKED (2 lines): owner_pid, owner_started_at, adapter_profile, last_error, updated_at,
  - `gateway/delivery_ledger.py@-425 +558,6` UNMARKED (6 lines): "adopted": True, "not_before": flood_not_before(updated_at, last_error),
  - `gateway/delivery_ledger.py@-444,2 +582,6` UNMARKED (6 lines): adapter_profile or "default", needs_marker=state != "pending" or attempts > 0,
- def sweep_failed_for_runtime(platform: str, now: Optional[float] = None, *,
  - `gateway/delivery_ledger.py@-470 +612,4` UNMARKED (4 lines): owner_started_at, last_error, adapter_profile, updated_at,
  - `gateway/delivery_ledger.py@-472 +617,7` UNMARKED (7 lines): WHERE state IN ("pending', "attempting', "failed') AND platform=?
  - `gateway/delivery_ledger.py@-474 +625,7` UNMARKED (6 lines): owner_pid, owner_started_at, last_error, adapter_profile, updated_at,
  - `gateway/delivery_ledger.py@-476 +633,5` UNMARKED (4 lines): if owner_pid != pid or owner_started_at != started:
  - `gateway/delivery_ledger.py@-481,0 +641,14` UNMARKED (7 lines): if due is None and flood_wait_is_absurd(last_error):
  - `gateway/delivery_ledger.py@-481,0 +641,14#moved` moved: `acp_adapter/server.py`, `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/agent_runtime_helpers_dead_connections.py` (+491 more)
  - `gateway/delivery_ledger.py@-489,0 +663` UNMARKED (1 lines): held_chats.add(chat_key)
  - `gateway/delivery_ledger.py@-504 +678,5` UNMARKED (5 lines): flood=is_flood_error(last_error), last_error=last_error,
- _RETRY_BACKOFF_SECONDS = (30.0, 120.0)
  - `gateway/delivery_ledger.py@-59,0 +59,4` UNMARKED (4 lines): # How long an in-flight ("attempting') row of this process holds later rows for its chat.
- assert len(_RETRY_BACKOFF_SECONDS) == MAX_ATTEMPTS - 1
  - `gateway/delivery_ledger.py@-66,2 +69` UNMARKED (1 lines): # restart"s sweep, which then redelivered it hours late with the normal duplicate hint.
- FLOOD_RETRY_SLACK_SECONDS = 2.0
  - `gateway/delivery_ledger.py@-72,0 +75,13` UNMARKED (6 lines): # A penalty this long is not a wait to sit out: the reply would land hours after it was us

### `gateway/lifecycle_ledger.py`

- def check_state_db_integrity(home: Optional[Path] = None, *, deadline_s: Optiona
  - `gateway/lifecycle_ledger.py@-268 +290,3` UNMARKED (3 lines): if renew_startup_lease or deadline_at is not None:

### `gateway/run.py`

- def _hygiene_cooldown_for_failure(gateway, session_key: str, base_cooldown_secon
  - `gateway/run.py@-138,3 +141,4` UNMARKED (2 lines): increment, streak, state = getattr(_gateway_session_db_inner(gateway), "increment_hygiene_
- def _profile_runtime_scope(
  - `gateway/run.py@-1800,2 +1870,7` UNMARKED (7 lines): # ambient TERMINAL_* a prior turn set (first-writer-wins backend leak; #68559). An ingress
- def _resolve_runtime_agent_kwargs() -> dict:
  - `gateway/run.py@-2262,34 +2374` UNMARKED (1 lines): return resolve_default_runtime_agent_kwargs()
- def _resolve_hermes_bin() -> Optional[list[str]]:
  - `gateway/run.py@-2910,0 +3033,18` UNMARKED (2 lines): def _resolve_update_hermes_bin(home: Path | None = None, *, project_root: Path | None = No
- class GatewayRunner(
  - `gateway/run.py@-3421,0 +3538,217` UNMARKED (2 lines): # Process watchers currently holding the gateway"s restart-when-idle wait open.
  - `gateway/run.py@-3741 +4072,12` UNMARKED (8 lines): def _admit():
  - `gateway/run.py@-3741 +4072,12#moved` moved: `agent/subagent_lifecycle.py`, `gateway/bot_loop_guard.py`, `hermes_cli/image_provenance.py`, `hermes_cli/sqlite_safe_read.py` (+4 more)
- def _start_gateway_housekeeping(
  - `gateway/run.py@-4412,7 +4818,7` UNMARKED (2 lines): watch_delivery_queue = adapters is not None or runner is not None
  - `gateway/run.py@-4441 +4850,2` UNMARKED (1 lines): ), watch_delivery_queue), # Bootstrap maintenance follows the first delivery watch window.
  - `gateway/run.py@-4455 +4869` UNMARKED (1 lines): if watch_delivery_queue:

### `gateway/run_busy.py`

- class GatewayBusySessionMixin:
  - `gateway/run_busy.py@-471 +396#moved` moved: `gateway/run_notifications_ownership.py`

### `gateway/run_notifications.py`

- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1002,532 +1829,16` UNMARKED (10 lines): """Consume watches in their owning profile, backing off refusals even during drain."""
  - `gateway/run_notifications.py@-1002,532 +1829,16#moved` moved: `gateway/run_notifications_completion.py`, `gateway/run_notifications_delegation.py`, `tests/gateway/test_attended_shared_busy_activity.py`, `tests/gateway/test_prompt_queued_transfer.py` (+1 more)
- def _mark_internal_notification(text: str) -> str:
  - `gateway/run_notifications.py@-110 +212` UNMARKED (1 lines): if any(text.endswith(footer) for footer in _INTERNAL_NOTIFICATION_FOOTERS):
- def _held_reason(source, platform: str, multiplexed: bool) -> str:
  - `gateway/run_notifications.py@-130,9 +232,3` UNMARKED (2 lines): def _media_send_succeeded(result) -> bool:
- def _raw_process_event_session_id(evt: dict) -> str:
  - `gateway/run_notifications.py@-141,33 +237` UNMARKED (1 lines): from gateway.run_notifications_delegation import GatewayNotificationsMixin as GatewayDeleg
- import logging
  - `gateway/run_notifications.py@-15,0 +13#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+149 more)
- import time
  - `gateway/run_notifications.py@-16,0 +15,2#moved` moved: `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `acp_adapter/session.py` (+276 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1736 +1891,3` UNMARKED (2 lines): """True = accepted or unknown receipt. False = not delivered: a raised send, or an
  - `gateway/run_notifications.py@-1745,0 +1903,4` UNMARKED (3 lines): # Some adapters send successfully but return no receipt. Unknown is not
- def _watch_notification_metadata(evt: dict, source) -> dict[str, Any]:
  - `gateway/run_notifications.py@-176 +240,11` UNMARKED (4 lines): from gateway.run_notifications_completion import GatewayNotificationsMixin as GatewayCompl
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1783,0 +1945` UNMARKED (1 lines): "handoff_note": _redact_gateway_user_facing_secrets(getattr(session, "handoff_note", "") o
  - `gateway/run_notifications.py@-179,3 +253,7` UNMARKED (7 lines): # Coalescing keys. Both partition by the spawning session (''parent_session_id''): a batch
  - `gateway/run_notifications.py@-1798,0 +1961,2` UNMARKED (2 lines): provenance = {"completion_reason": getattr(session, "completion_reason", ""),
- from pathlib import Path
  - `gateway/run_notifications.py@-18 +18,4` UNMARKED (2 lines): _UpdatePaths = Any
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-1801,2 +1965,3` UNMARKED (3 lines): duration_seconds=_dur, **provenance)
  - `gateway/run_notifications.py@-1827,0 +1993,6` UNMARKED (6 lines): # Arm in a FRESH context, like a startup-recovered watcher. Scheduling captures the
  - `gateway/run_notifications.py@-183,3 +261,14` UNMARKED (14 lines): # Fan-in for routine completion wakes (#70300). While a session is mid-turn or started a t
  - `gateway/run_notifications.py@-187,7 +276,12` UNMARKED (6 lines): def _completion_notification_batch_window_from_config(self) -> float:
  - `gateway/run_notifications.py@-187,7 +276,12#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/auxiliary_health.py` (+272 more)
  - `gateway/run_notifications.py@-195,2 +289,125` UNMARKED (74 lines): def _completion_notification_window_seconds(self) -> float:
  - `gateway/run_notifications.py@-195,2 +289,125#moved` moved: `acp_adapter/commands.py`, `agent/api_request_hooks.py`, `agent/aux_accounting.py`, `agent/azure_identity_adapter.py` (+654 more)
- (module)
  - `gateway/run_notifications.py@-2,4 +2` UNMARKED (1 lines): ''gateway.run'' internals stay lazy for import cycles and facade patches.
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-219 +436,5` UNMARKED (4 lines): # Every platform notice is out-of-band status, never a turn final: without the interim mar
  - `gateway/run_notifications.py@-219 +436,5#moved` moved: `gateway/parked_route_refusal.py`, `gateway/run_inbound.py`, `gateway/run_shutdown.py`, `gateway/run_turn.py` (+5 more)
- from agent.retry_utils import jittered_backoff
  - `gateway/run_notifications.py@-22 +25,2` UNMARKED (1 lines): from agent.notice_style import ContextNotice, notice_metadata
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-226,3 +447,4` UNMARKED (2 lines): if _media_send_succeeded(result):
  - `gateway/run_notifications.py@-232 +454` UNMARKED (1 lines): footer_line: str = "", footer_captioned: Optional[list] = None,
  - `gateway/run_notifications.py@-242,0 +464,4` UNMARKED (4 lines): Returns True when at least one upload was confirmed delivered. ''footer_line'' rides the l
  - `gateway/run_notifications.py@-244,0 +470,2#moved` moved: `gateway/platforms/base.py`, `gateway/run_notifications_media.py`, `gateway/run_turn_persistence.py`
  - `gateway/run_notifications.py@-268,5 +495,3` UNMARKED (3 lines): _thread_meta = dict(thread_metadata) if thread_metadata is not None else self._thread_meta
  - `gateway/run_notifications.py@-283,0 +509` UNMARKED (1 lines): adapter_meta = _thread_meta or {}
  - `gateway/run_notifications.py@-288,2 +514,6` UNMARKED (5 lines): _res = await adapter.send_multiple_images(
  - `gateway/run_notifications.py@-288,2 +514,6#moved` moved: `gateway/run_notifications_media.py`
- from gateway.run_notifications_update import GatewayUpdateNotificationsMixin
  - `gateway/run_notifications.py@-29 +33,4` UNMARKED (2 lines): from gateway.run_notifications_ledger import (
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-296,2 +526,2` UNMARKED (1 lines): chat_id=chat_id, audio_path=media_path, metadata=adapter_meta, is_voice=is_voice,
  - `gateway/run_notifications.py@-296,2 +526,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-298,0 +529#moved` moved: `gateway/run_notifications_media.py`
- from gateway.run_notifications_restart import GatewayRestartNoticeMixin
  - `gateway/run_notifications.py@-30,0 +38,3` UNMARKED (3 lines): from gateway.run_notifications_sources import GatewayProcessEventSourcesMixin, _raw_proces
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-300 +531,2` UNMARKED (1 lines): _res = await adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=adapter_m
  - `gateway/run_notifications.py@-300 +531,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-302 +534,2` UNMARKED (1 lines): _res = await adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=adapter
  - `gateway/run_notifications.py@-302 +534,2#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-305,2 +538#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-310,0 +543` UNMARKED (1 lines): transcript_message_uid: Optional[str] = None,
  - `gateway/run_notifications.py@-333,0 +567,4#moved` moved: `gateway/delivery_recovery.py`, `gateway/run_notifications_media.py`
- logger = logging.getLogger("gateway.run")
  - `gateway/run_notifications.py@-35,0 +46,78` UNMARKED (30 lines): """Metadata carried by synthetic watch/completion notifications."""
  - `gateway/run_notifications.py@-35,0 +46,78#moved` moved: `agent/api_error_summary.py`, `agent/auxiliary_unavailable.py`, `agent/reasoning_summaries.py`, `apps/desktop/electron/window-below.ts` (+14 more)
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-355,0 +593,2` UNMARKED (2 lines): from gateway.reply_latency import current_receipt, record_final_reply
  - `gateway/run_notifications.py@-356,0 +596#moved` moved: `gateway/run_notifications_copy.py`, `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-378,2 +618,2` UNMARKED (2 lines): adapter, source, text_content, metadata, event_message_id, transcript_message_uid,
  - `gateway/run_notifications.py@-385 +625,12` UNMARKED (5 lines): from gateway.run_notifications_copy import deliver_queued_copy_blocks
  - `gateway/run_notifications.py@-385 +625,12#moved` moved: `gateway/run_notifications_copy.py`, `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-389,2 +640,4#moved` moved: `gateway/run_notifications_media.py`, `gateway/run_turn_persistence.py`, `tests/gateway/test_copy_inline_quotes.py`, `tui_gateway/prompt_turn.py`
  - `gateway/run_notifications.py@-392,0 +646,7#moved` moved: `gateway/run_notifications_media.py`
  - `gateway/run_notifications.py@-397 +657` UNMARKED (1 lines): event_message_id: Optional[str], transcript_message_uid: Optional[str], session_key: Optio
  - `gateway/run_notifications.py@-408,0 +669,4` UNMARKED (3 lines): text="", source=source, ledger_message_id=inbound_message_id)
  - `gateway/run_notifications.py@-408,0 +669,4#moved` moved: `gateway/run_busy.py`
  - `gateway/run_notifications.py@-410,2 +674` UNMARKED (1 lines): queued_event, session_key, text_content, _mark_notify_metadata(metadata), reply_to=event_m
  - `gateway/run_notifications.py@-413,0 +677,2` UNMARKED (2 lines): from gateway.reply_latency import current_receipt, record_final_reply
  - `gateway/run_notifications.py@-419,0 +685,562` UNMARKED (253 lines): def _schedule_update_notice_retry(self, loop: asyncio.AbstractEventLoop) -> None:
  - `gateway/run_notifications.py@-419,0 +685,562#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+3311 more)
  - `gateway/run_notifications.py@-472 +1299,2` UNMARKED (2 lines): send_metadata = notice_metadata(_non_conversational_metadata(metadata, platform=platform),
  - `gateway/run_notifications.py@-540 +1368` UNMARKED (1 lines): message = ContextNotice(t("gateway.startup.online"))
  - `gateway/run_notifications.py@-587 +1415` UNMARKED (1 lines): await asyncio.to_thread(self._open_session_db_for_active_scope)
- _COMPLETION_INJECTION_RETRY_MAX_DELAY = 60
  - `gateway/run_notifications.py@-73,3 +161,3` UNMARKED (3 lines): # Refused-watch backoff starts at 10 s so hot process retries leave freed background-turn
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-780,0 +1498,2` UNMARKED (2 lines): # This route is persist-only: the client owns the next model turn, so
  - `gateway/run_notifications.py@-889,0 +1609,51` UNMARKED (21 lines): def _bind_registered_local_source(self, adapter, source):
  - `gateway/run_notifications.py@-889,0 +1609,51#moved` moved: `agent/account_usage.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/credential_pool.py` (+119 more)
  - `gateway/run_notifications.py@-903 +1673,6` UNMARKED (3 lines): await restore_completion_route(self, str(evt.get("session_key") or "")) # watch notices an
  - `gateway/run_notifications.py@-903 +1673,6#moved` moved: `gateway/run_notifications_completion.py`
  - `gateway/run_notifications.py@-910 +1685,2` UNMARKED (1 lines): adapter = self._adapters_for_profile(profile).get(Platform.API_SERVER)
  - `gateway/run_notifications.py@-910 +1685,2#moved` moved: `gateway/run_notifications_completion.py`
  - `gateway/run_notifications.py@-928,0 +1705,5` UNMARKED (5 lines): if not adapter and getattr(source, "delivered_via_upstream_relay", False) is True:
- INTERNAL_NOTIFICATION_FOOTER = (
  - `gateway/run_notifications.py@-93,0 +173,10` UNMARKED (9 lines): "Do not treat it as user input, and do not reply to any earlier chat message because of it
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-938 +1719,25` UNMARKED (8 lines): metadata = {} if platform_name == Platform.SLACK.value else _watch_notification_metadata(e
  - `gateway/run_notifications.py@-950 +1755,9` UNMARKED (8 lines): # Local admission accepts only the registered live SessionSource.
  - `gateway/run_notifications.py@-950 +1755,9#moved` moved: `agent/secret_scope.py`, `gateway/session_cli_turn.py`
- INTERNAL_NOTIFICATION_FOOTER = (
  - `gateway/run_notifications.py@-96,0 +186,13` UNMARKED (11 lines): _PREVIOUS_FULL_INTERNAL_NOTIFICATION_FOOTER = (
- class GatewayNotificationsMixin(GatewayUpdateNotificationsMixin, GatewayNotifica
  - `gateway/run_notifications.py@-967,0 +1781,14` UNMARKED (9 lines): if getattr(self, "_draining", False) and authority is None:
  - `gateway/run_notifications.py@-988,2 +1815,3` UNMARKED (2 lines): from gateway.session_refusals import log_internal_refusal
  - `gateway/run_notifications.py@-988,2 +1815,3#moved` moved: `hermes_cli/web_routers/_common.py`, `hermes_cli/web_server_sessions.py`, `tui_gateway/hosted_room_prepare_stall.py`
  - `gateway/run_notifications.py@-998,3 +1826,2` UNMARKED (2 lines): # Ledger transitions write state.db: run them off the loop (see ''gateway.run_notification

### `gateway/run_plugin_injection.py`

- class GatewayPluginInjectionMixin:
  - `gateway/run_plugin_injection.py@-309,0 +332,80#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+31 more)

### `gateway/run_runtime.py`

- async def _build_profile_authority(runner, name, home, *, register):
  - `gateway/run_runtime.py@-55 +55` UNMARKED (1 lines): runner, profile_id=str(home), instance_id=instance_id, db=db, register=register, claim=cla
- def open_profile_store(runner, home):
  - `gateway/run_runtime.py@-64,7 +64,4` UNMARKED (1 lines): """Resolve once off-loop under the owning scope; a pending open is not a broken store."""
  - `gateway/run_runtime.py@-64,7 +64,4#moved` moved: `evals/delivery_flood_wire.py`, `gateway/session_dm_threads.py`, `gateway/session_snapshot.py`, `tests/acp_adapter/test_gateway_acp_session_end.py` (+167 more)
- async def recover_profile_native_sessions(runner, authority):
  - `gateway/run_runtime.py@-691,2 +752,4` UNMARKED (3 lines): pending_rows = await asyncio.to_thread(authority.db._read_all,
  - `gateway/run_runtime.py@-691,2 +752,4#moved` moved: `gateway/telegram_retire_work.py`
  - `gateway/run_runtime.py@-694 +757` UNMARKED (1 lines): for entry in entries if entry.origin is not None
- def open_profile_store(runner, home):
  - `gateway/run_runtime.py@-72,2 +69,8` UNMARKED (7 lines): pinned = getattr(runner, "_session_db_pinned", _SESSION_DB_UNPINNED)
  - `gateway/run_runtime.py@-72,2 +69,8#moved` moved: `gateway/checkin_delivery.py`, `gateway/platforms/session_model.py`, `gateway/run_profile_reconcile.py`, `gateway/run_turn_runner.py` (+1 more)
- async def settle_gateway_runtime(runner, budget=None, reason=None):
  - `gateway/run_runtime.py@-819 +887,4` UNMARKED (2 lines): await end_idle_acp_sessions(authority, deadline)
  - `gateway/run_runtime.py@-822,3 +893,2` UNMARKED (1 lines): logger.warning("ACP sessions of %s not ended at shutdown',

### `gateway/run_shutdown.py`

- class GatewayShutdownMixin(GatewaySessionEndMixin, GatewayHomeShutdownNoticeMixi
  - `gateway/run_shutdown.py@-1070 +1365,14` UNMARKED (2 lines): from gateway.run_shutdown_targets import reserve_shutdown_note_target
  - `gateway/run_shutdown.py@-1090,0 +1397,7` UNMARKED (1 lines): notified.add(dedup_key) # a loop-muted home chat hears nothing, active task or not
  - `gateway/run_shutdown.py@-1121,3 +1436,52` UNMARKED (17 lines): # A home chat the active-session pass already told is owed "Gateway online" like one this
  - `gateway/run_shutdown.py@-1121,3 +1436,52#moved` moved: `gateway/cron_store_notices.py`, `gateway/handoff_target.py`, `gateway/kanban_watchers_notifier.py`, `gateway/platforms/webhook_ingress.py` (+22 more)
  - `gateway/run_shutdown.py@-1558,0 +2003,2` UNMARKED (2 lines): # Delegations are waited on under their own budget; the restart wait splits them out.
  - `gateway/run_shutdown.py@-1597,0 +2045,3` UNMARKED (3 lines): # Delegations with the parent session each reports back to (restart_when_idle). Its proces
  - `gateway/run_shutdown.py@-1600 +2050,13` UNMARKED (10 lines): for unit in background_units(self):
  - `gateway/run_shutdown.py@-1604,3 +2066` UNMARKED (1 lines): """Serve normally until in-flight work finishes or its wait budget runs out.
  - `gateway/run_shutdown.py@-1608,4 +2068,120` UNMARKED (90 lines): Restart when idle, bounded: admission stays open for the whole wait (''request_restart''
  - `gateway/run_shutdown.py@-1608,4 +2068,120#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/auxiliary_client.py`, `agent/bedrock_adapter.py` (+547 more)
  - `gateway/run_shutdown.py@-1754,5 +2381,23` UNMARKED (1 lines): with _log_suppressed(logging.DEBUG, "Failed to settle home online notice: %s"):
  - `gateway/run_shutdown.py@-1853 +2584,26` UNMARKED (3 lines): """Spool owned inbound slots before entering cancellable shutdown cleanup."""
  - `gateway/run_shutdown.py@-1908,7 +2787,17` UNMARKED (3 lines): for session_key, state in dict(self.__dict__.get("_sessions") or {}).items():
  - `gateway/run_shutdown.py@-2044,5 +2931,25` UNMARKED (2 lines): if (self._restart_requested and self._restart_command_source is None
  - `gateway/run_shutdown.py@-217 +234,5` UNMARKED (5 lines): # Delegations count as busy in gateway_state.json/status and restart waits.
  - `gateway/run_shutdown.py@-299,0 +322,19` UNMARKED (7 lines): """Live detached delegation units, including queued/finalizing work but not terminal histo
- from gateway.run_shutdown_home_notice import (
  - `gateway/run_shutdown.py@-34,0 +40` UNMARKED (1 lines): HOME_NOTICE_SENT,

### `gateway/run_startup.py`

- class GatewayStartupMixin:
  - `gateway/run_startup.py@-1620,0 +1902,4` UNMARKED (1 lines): "_handoff_watcher", "_async_delegation_watcher", "_loop_wakeup_watcher", "_session_control
  - `gateway/run_startup.py@-555,7 +685,4#moved` moved: `gateway/run_restart_recovery.py`, `gateway/run_restored_delivery.py`
  - `gateway/run_startup.py@-563,49 +690,11` UNMARKED (5 lines): candidates = ResumeCandidates([
  - `gateway/run_startup.py@-563,49 +690,11#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)
  - `gateway/run_startup.py@-613,36 +702,19` UNMARKED (13 lines): logger.warning("Failed to enumerate resume-pending sessions: %s", exc)
  - `gateway/run_startup.py@-613,36 +702,19#moved` moved: `agent/context_compressor.py`, `agent/conversation_compression.py`, `agent/gemini_native_adapter.py`, `agent/reasoning_effort.py` (+56 more)
  - `gateway/run_startup.py@-650,21 +722,82` UNMARKED (3 lines): candidates.breaker_tripped = _rlg.check_and_record(
  - `gateway/run_startup.py@-650,21 +722,82#moved` moved: `agent/conversation_compression.py`, `agent/verification_evidence.py`, `apps/desktop/electron/favicon.ts`, `apps/desktop/electron/git-binary-candidates.ts` (+16 more)

### `gateway/run_topics.py`

- class GatewayTopicThreadsMixin:
  - `gateway/run_topics.py@-150 +154,21` UNMARKED (16 lines): "/topic — enable multi-session DM mode (one bot, many parallel chats)\n"
  - `gateway/run_topics.py@-150 +154,21#moved` moved: `agent/coding_context.py`, `agent/curator.py`, `agent/prompt_builder.py`, `agent/trace_upload.py` (+37 more)
- from gateway.session import SessionSource
  - `gateway/run_topics.py@-22,0 +23` UNMARKED (1 lines): from hermes_state_telegram import TELEGRAM_TOPIC_ICON_HISTORY_LIMIT
- _TOPIC_SWITCH_BIND_LOCK = threading.Lock()
  - `gateway/run_topics.py@-35,0 +37,3` UNMARKED (2 lines): # /title waits for the icon pick before replying; bound it so a slow aux model cannot stal
- class GatewayTopicThreadsMixin:
  - `gateway/run_topics.py@-373 +397,3` UNMARKED (3 lines): async def _transfer_telegram_root_dm(
  - `gateway/run_topics.py@-386,0 +413` UNMARKED (1 lines): title = t("gateway.topic.default_title")
  - `gateway/run_topics.py@-388 +415,9` UNMARKED (6 lines): from agent.title_generator import derive_title
  - `gateway/run_topics.py@-630,0 +666,127` UNMARKED (72 lines): async def _rename_telegram_topic_explicit(
  - `gateway/run_topics.py@-630,0 +666,127#moved` moved: `acp_adapter/model_catalog.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/agent_runtime_helpers.py` (+388 more)
  - `gateway/run_topics.py@-652 +814,9` UNMARKED (4 lines): def _schedule_telegram_topic_title_rename(
  - `gateway/run_topics.py@-652 +814,9#moved` moved: `agent/auxiliary_unavailable.py`, `agent/background_review.py`, `agent/bounded_response.py`, `agent/chat_completion_helpers_cleanup.py` (+398 more)
  - `gateway/run_topics.py@-658 +828,3` UNMARKED (2 lines): lambda copied: self._rename_telegram_topic_for_session_title(
  - `gateway/run_topics.py@-661,0 +834,23` UNMARKED (15 lines): def _telegram_topic_icon_context(self, source: SessionSource) -> Optional[dict]:
  - `gateway/run_topics.py@-661,0 +834,23#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9734 more)
  - `gateway/run_topics.py@-669 +864,145` UNMARKED (95 lines): def _telegram_platform_extra(self, source: SessionSource) -> dict:
  - `gateway/run_topics.py@-669 +864,145#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_unavailable.py`, `agent/background_review.py`, `agent/bounded_response.py` (+576 more)
  - `gateway/run_topics.py@-690 +1029#moved` moved: `gateway/run_notifications_ownership.py`
  - `gateway/run_topics.py@-697 +1036` UNMARKED (1 lines): if not binding or str(binding.get("session_id") or "") != str(session_id):
  - `gateway/run_topics.py@-704,0 +1044,22` UNMARKED (13 lines): icon_custom_emoji_id, icon_state_to_record, icon_history_to_record, icon_state_owner = (
  - `gateway/run_topics.py@-704,0 +1044,22#moved` moved: `cron/scheduler_session_cleanup.py`
  - `gateway/run_topics.py@-708 +1069,6` UNMARKED (3 lines): kwargs = {"chat_id": str(source.chat_id), "thread_id": str(source.thread_id), "name": topi
  - `gateway/run_topics.py@-708 +1069,6#moved` moved: `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/topic_creation.py`
  - `gateway/run_topics.py@-713,0 +1080,3` UNMARKED (1 lines): kwargs = {"name": topic_name}
  - `gateway/run_topics.py@-713,0 +1080,3#moved` moved: `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/topic_creation.py`
  - `gateway/run_topics.py@-715 +1084` UNMARKED (1 lines): await edit_forum_topic(chat_id=int(source.chat_id), message_thread_id=int(source.thread_id
  - `gateway/run_topics.py@-717 +1086,2` UNMARKED (2 lines): await edit_forum_topic(chat_id=source.chat_id, message_thread_id=source.thread_id, **kwarg

### `gateway/run_turn_hygiene.py`

- class GatewayTurnHygieneMixin:
  - `gateway/run_turn_hygiene.py@-114 +223` UNMARKED (1 lines): hs.model, _hyg_runtime = await self._resolve_session_agent_runtime_async(
  - `gateway/run_turn_hygiene.py@-120,0 +230` UNMARKED (1 lines): hs.api_mode = _hyg_runtime.get("api_mode") or ""
  - `gateway/run_turn_hygiene.py@-162,0 +273` UNMARKED (1 lines): api_mode=hs.api_mode,
  - `gateway/run_turn_hygiene.py@-178,4 +289,6` UNMARKED (4 lines): def _read_anchor():
  - `gateway/run_turn_hygiene.py@-178,4 +289,6#moved` moved: `agent/activity_tracking.py`, `agent/context_compressor.py`, `hermes_cli/cli_process_notifications.py`, `run_agent.py`
  - `gateway/run_turn_hygiene.py@-198,13 +311,16` UNMARKED (8 lines): def _read_cooldown():
  - `gateway/run_turn_hygiene.py@-198,13 +311,16#moved` moved: `agent/activity_tracking.py`, `agent/context_compressor.py`, `hermes_cli/cli_process_notifications.py`, `run_agent.py`
  - `gateway/run_turn_hygiene.py@-314 +430,2` UNMARKED (2 lines): async def _hmwa_hygiene_stamp(agent, desc, provenance_name, debug_label):
  - `gateway/run_turn_hygiene.py@-317 +434,4` UNMARKED (2 lines): _stamp_hygiene_compression_provenance,
  - `gateway/run_turn_hygiene.py@-327 +447` UNMARKED (1 lines): except (OSError, RuntimeError, ValueError) as _werr:
  - `gateway/run_turn_hygiene.py@-338 +458` UNMARKED (1 lines): await asyncio.to_thread(_record_hygiene_cooldown, self, session_id, _hyg_cooldown, reason)
  - `gateway/run_turn_hygiene.py@-341,3 +461,7` UNMARKED (6 lines): """''except HygieneTurnHoldExceeded'' body: keep or cancel the worker's commit admission
  - `gateway/run_turn_hygiene.py@-361,0 +486` UNMARKED (1 lines): _meta = attempt.meta
  - `gateway/run_turn_hygiene.py@-363,0 +489` UNMARKED (1 lines): _exc = None
  - `gateway/run_turn_hygiene.py@-372,0 +499,5` UNMARKED (4 lines): _comp = getattr(_agent, "context_compressor", None)
  - `gateway/run_turn_hygiene.py@-379,4 +510,21` UNMARKED (16 lines): # The prompt-token count on the entry measured the pre-compaction transcript:
  - `gateway/run_turn_hygiene.py@-386,2 +534,2` UNMARKED (2 lines): _gw._hmwa_hygiene_schedule_cooldown(
  - `gateway/run_turn_hygiene.py@-389,0 +538` UNMARKED (1 lines): failure_label="hygiene turn-hold retry spacing failed",
- logger = logging.getLogger("gateway.run")
  - `gateway/run_turn_hygiene.py@-39,0 +40,36` UNMARKED (1 lines): _HYGIENE_PENDING_COOLDOWN_WAIT_S = 10.0
  - `gateway/run_turn_hygiene.py@-39,0 +40,36#moved` moved: `agent/agent_runtime_helpers.py`, `agent/codex_runtime.py`, `agent/compression_facade.py`, `agent/context_references.py` (+28 more)
- class GatewayTurnHygieneMixin:
  - `gateway/run_turn_hygiene.py@-396,0 +546` UNMARKED (1 lines): _notify_user = False
  - `gateway/run_turn_hygiene.py@-402,2 +552,2` UNMARKED (1 lines): _record_hygiene_cooldown, self, session_entry.session_id, _HYGIENE_TURNHOLD_RETRY_SECONDS,
  - `gateway/run_turn_hygiene.py@-408 +558,2` UNMARKED (2 lines): _notify_user = True
  - `gateway/run_turn_hygiene.py@-417,3 +568,5` UNMARKED (2 lines): if _notify_user:
  - `gateway/run_turn_hygiene.py@-42,0 +79,71` UNMARKED (44 lines): async def _hmwa_hygiene_off_loop(func, *args, failure_label):
  - `gateway/run_turn_hygiene.py@-42,0 +79,71#moved` moved: `agent/compact_context.py`, `agent/plugin_llm.py`, `apps/desktop/src/app/chat/hooks/use-composer-actions.ts`, `apps/desktop/src/lib/image-resize.ts` (+63 more)
  - `gateway/run_turn_hygiene.py@-446 +599` UNMARKED (1 lines): await self._hmwa_hygiene_stamp(
  - `gateway/run_turn_hygiene.py@-490 +643,3` UNMARKED (1 lines): def _record() -> None:
  - `gateway/run_turn_hygiene.py@-490 +643,3#moved` moved: `gateway/slash_commands_session.py`
  - `gateway/run_turn_hygiene.py@-492 +647` UNMARKED (1 lines): self, session_id,
  - `gateway/run_turn_hygiene.py@-496,2 +651,4` UNMARKED (3 lines): # The unwinding turn cannot await; the streak read and cooldown write are state.db I/O.
  - `gateway/run_turn_hygiene.py@-55,0 +163,2` UNMARKED (1 lines): _default_route = resolve_default_route(data)
  - `gateway/run_turn_hygiene.py@-55,0 +163,2#moved` moved: `acp_adapter/session.py`, `gateway/run.py`, `gateway/run_config_loaders.py`, `gateway/run_inbound.py` (+7 more)
  - `gateway/run_turn_hygiene.py@-58 +167` UNMARKED (1 lines): hs.model = _default_route.model
  - `gateway/run_turn_hygiene.py@-60 +169` UNMARKED (1 lines): hs.model = _default_route.model or hs.model
  - `gateway/run_turn_hygiene.py@-621 +778` UNMARKED (1 lines): await self._hmwa_hygiene_stamp(
  - `gateway/run_turn_hygiene.py@-65,2 +174,2` UNMARKED (2 lines): hs.provider = _default_route.provider or None
  - `gateway/run_turn_hygiene.py@-788 +955` UNMARKED (1 lines): _hyg_model, _hyg_runtime = await self._resolve_session_agent_runtime_async(

### `gateway/run_turn_prepare.py`

- def _session_override_snapshot(override: dict) -> dict:
  - `gateway/run_turn_prepare.py@-168 +179,30` UNMARKED (17 lines): def resolve_stored_override(override: Optional[dict], model: str):
  - `gateway/run_turn_prepare.py@-168 +179,30#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2560 more)
- def _resolve_channel_runtime(runner, source, model, runtime_kwargs) -> tuple[str
  - `gateway/run_turn_prepare.py@-171,2 +211,6` UNMARKED (6 lines): # Keep the helper compatible with older internal callers that passed the runner
  - `gateway/run_turn_prepare.py@-174 +218#moved` moved: `gateway/session_info_route.py`
  - `gateway/run_turn_prepare.py@-189,0 +234,141` UNMARKED (71 lines): def _resolve_attended_runtime(attended_route, model, runtime_kwargs, notice):
  - `gateway/run_turn_prepare.py@-189,0 +234,141#moved` moved: `acp_adapter/auth.py`, `acp_adapter/session.py`, `agent/account_usage.py`, `agent/agent_init_compression_notice.py` (+120 more)
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-193 +378` UNMARKED (1 lines): user_config: Optional[dict] = None, fallback: bool = True, assignment=None, record_notice:
  - `gateway/run_turn_prepare.py@-197,10 +382,5` UNMARKED (4 lines): Priority (highest first): launch policy → session ''/model'' → ''channel_overrides'' → glo
  - `gateway/run_turn_prepare.py@-197,10 +382,5#moved` moved: `gateway/run_config_loaders.py`, `gateway/run_inbound.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_attended_turn_runner.py`
  - `gateway/run_turn_prepare.py@-217 +392,4` UNMARKED (3 lines): return _resolve_policy_agent_runtime(
  - `gateway/run_turn_prepare.py@-217 +392,4#moved` moved: `gateway/session_llm_oneshot.py`, `gateway/session_tui_side.py`, `tests/gateway/test_config_policy_recovery.py`, `tests/gateway/test_config_secret_scope.py` (+7 more)
  - `gateway/run_turn_prepare.py@-287,21 +398,19` UNMARKED (11 lines): self._last_attended_decision = None
  - `gateway/run_turn_prepare.py@-287,21 +398,19#moved` moved: `gateway/run_turn/__init__.py`
  - `gateway/run_turn_prepare.py@-312,3 +421` UNMARKED (1 lines): ''request_overrides'' are deep-merged OVER the per-provider ones so both reach the model."
  - `gateway/run_turn_prepare.py@-324,0 +432,2` UNMARKED (2 lines): if runtime_kwargs.get("model_pinned") is True:
  - `gateway/run_turn_prepare.py@-328,0 +438` UNMARKED (1 lines): "base_request_overrides": base_request_overrides,
  - `gateway/run_turn_prepare.py@-361,0 +472` UNMARKED (1 lines): runtime = {k: getattr(agent, k, None) for k in ("provider", "base_url", "api_mode")}
  - `gateway/run_turn_prepare.py@-363,7 +474,2` UNMARKED (2 lines): runtime["provider"] = session_provider_identity(
  - `gateway/run_turn_prepare.py@-390 +496,4` UNMARKED (4 lines): # Off a fallback, the route is the session"s own: a stale fallback marker must not undo it
  - `gateway/run_turn_prepare.py@-400,0 +510,2#moved` moved: `evals/gateway_status_render/iteration_ceiling_ab.py`, `gateway/run_busy.py`, `gateway/run_checkins.py`, `gateway/run_plugin_rewire.py` (+39 more)
  - `gateway/run_turn_prepare.py@-405,4 +516` UNMARKED (1 lines): recovered = (await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
  - `gateway/run_turn_prepare.py@-432 +540,2` UNMARKED (2 lines): if session_entry is None or not pinned_session_id or not await GatewayTurnMixin._wake_pin_
- _TIER_FROM_RUNNER = object()
  - `gateway/run_turn_prepare.py@-44 +45` UNMARKED (1 lines): def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, session_key=None, reco
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-466,0 +560,2` UNMARKED (1 lines): if await asyncio.to_thread(self._claimed_turn_route_was_stopped, event, session_entry.sess
  - `gateway/run_turn_prepare.py@-469,0 +565,22` UNMARKED (11 lines): if strict_session and not await GatewayTurnMixin._wake_pin_matches(
  - `gateway/run_turn_prepare.py@-469,0 +565,22#moved` moved: `gateway/run_busy.py`, `gateway/run_notifications_ownership.py`, `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py` (+5 more)
  - `gateway/run_turn_prepare.py@-472,0 +590,2` UNMARKED (2 lines): from gateway.meeting_lane import apply_reasoning_default
  - `gateway/run_turn_prepare.py@-474,0 +594,19` UNMARKED (8 lines): def _claimed_turn_route_was_stopped(self, event, resolved_session_id):
  - `gateway/run_turn_prepare.py@-474,0 +594,19#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+2050 more)
  - `gateway/run_turn_prepare.py@-563 +701` UNMARKED (1 lines): await self._clear_conversation_scope_async(session_key, reason="auto_reset")
  - `gateway/run_turn_prepare.py@-568 +706,12` UNMARKED (4 lines): attended_new_session = getattr(
  - `gateway/run_turn_prepare.py@-568 +706,12#moved` moved: `gateway/run_config_loaders.py`, `gateway/run_inbound.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_attended_turn_runner.py`
  - `gateway/run_turn_prepare.py@-573,0 +723,2` UNMARKED (1 lines): self._session_state(session_key).conversation.attended_new_session = True
  - `gateway/run_turn_prepare.py@-573,0 +723,2#moved` moved: `gateway/run_turn/__init__.py`
- def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, record_notic
  - `gateway/run_turn_prepare.py@-59,0 +61` UNMARKED (1 lines): notice = None
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-649,2 +800,4` UNMARKED (4 lines): a resend notice for callers without an inbound event). Inbound messages retain their FIFO
  - `gateway/run_turn_prepare.py@-660,2 +828,2` UNMARKED (1 lines): # The cleanup finally starts later; cancellation and spool failures restore identity too.
- def _resolve_policy_agent_runtime(runner, policy, *, fallback=True, record_notic
  - `gateway/run_turn_prepare.py@-68,4 +68,15` UNMARKED (12 lines): model = fallback_entry["model']
  - `gateway/run_turn_prepare.py@-68,4 +68,15#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_busy.py`, `gateway/run_notifications.py`, `gateway/run_notifications_ownership.py` (+14 more)
- class GatewayTurnPrepareMixin:
  - `gateway/run_turn_prepare.py@-707,5 +870` UNMARKED (1 lines): home_env = home_env or (os.getenv(env_key) or "").strip()
  - `gateway/run_turn_prepare.py@-773,0 +933,2` UNMARKED (2 lines): # Deferred hygiene adoptions seen before the agent ran; persist compares it to drop a stal
  - `gateway/run_turn_prepare.py@-779,2 +940` UNMARKED (1 lines): running (history unreadable); ''None'' drops the turn (inbound text rejected)."""
  - `gateway/run_turn_prepare.py@-784 +944` UNMARKED (1 lines): restore_gateway_yolo(session_key, session_entry.yolo)
  - `gateway/run_turn_prepare.py@-787,0 +948,3` UNMARKED (2 lines): from gateway.session_context import set_turn_origin
  - `gateway/run_turn_prepare.py@-787,0 +948,3#moved` moved: `gateway/reply_latency.py`, `gateway/run_background_admission.py`, `gateway/run_busy.py`, `gateway/run_goals.py` (+7 more)
  - `gateway/run_turn_prepare.py@-840,2 +995,13` UNMARKED (9 lines): return t("gateway.errors.history_unavailable"), _session_env_tokens
  - `gateway/run_turn_prepare.py@-862,0 +1029,9` UNMARKED (3 lines): # Resolve once after preparation so expiry behaves like /fast off and its notice rides thi
  - `gateway/run_turn_prepare.py@-862,0 +1029,9#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_busy.py`, `gateway/run_notifications.py`, `gateway/run_notifications_ownership.py` (+13 more)
  - `gateway/run_turn_prepare.py@-884,0 +1060` UNMARKED (1 lines): hygiene_adoption_epoch=self._hygiene_deferred_adoption_epoch(session_key),

### `gateway/session.py`

- class SessionStore(
  - `gateway/session.py@-1173 +1378,3` UNMARKED (1 lines): self, session_key: str, override: Optional[dict[str, Any]], *, scope: Optional[str] = None
  - `gateway/session.py@-1173 +1378,3#moved` moved: `hermes_cli/kanban_db.py`
  - `gateway/session.py@-1177,0 +1385` UNMARKED (1 lines): resolved_scope = scope if scope in ("session", "temporary", "once") else "session"
  - `gateway/session.py@-1181 +1389,8` UNMARKED (4 lines): if entry is None or (
  - `gateway/session.py@-1181 +1389,8#moved` moved: `gateway/slash_commands_incognito.py`
  - `gateway/session.py@-1187 +1402,4` UNMARKED (3 lines): data[session_key] = replace(
  - `gateway/session.py@-1187 +1402,4#moved` moved: `agent/conversation_loop.py`, `tests/gateway/test_local_viewer_events_turn.py`, `tests/monitoring/test_cron_health_export.py`
  - `gateway/session.py@-1189,0 +1408` UNMARKED (1 lines): entry.model_override_scope = resolved_scope if cleaned else "session"
  - `gateway/session.py@-1191,3 +1410,2` UNMARKED (2 lines): def set_session_yolo(self, session_key: str, enabled: bool) -> bool:
  - `gateway/session.py@-1205,0 +1424,103` UNMARKED (53 lines): def get_model_override_scope(self, session_key: str) -> str:
  - `gateway/session.py@-1205,0 +1424,103#moved` moved: `agent/agent_init.py`, `agent/agent_init_compression_notice.py`, `agent/agent_init_ollama.py`, `agent/agent_runtime_helpers.py` (+466 more)
  - `gateway/session.py@-1207,0 +1529,6` UNMARKED (3 lines): if self.incognito_enabled(session_key):
  - `gateway/session.py@-1207,0 +1529,6#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+62 more)
  - `gateway/session.py@-1296,0 +1624,13` UNMARKED (4 lines): def unbind_session(self, session_key: str, expected_session_id: str) -> bool:
  - `gateway/session.py@-1355 +1695` UNMARKED (1 lines): preserve_prompt_pin: bool = True, preserve_attended_pin: bool = True,
  - `gateway/session.py@-1364 +1704,2` UNMARKED (2 lines): starts a different conversation on the same routing key. Attended pins also follow
  - `gateway/session.py@-1371,4 +1712,4` UNMARKED (2 lines): logger.info("Session switch for %s refused: route moved from %s to %s after the caller's s
  - `gateway/session.py@-1378 +1719` UNMARKED (1 lines): if self._db_for_key(session_key):
  - `gateway/session.py@-1380,21 +1721,16` UNMARKED (10 lines): yolo=old_entry.yolo, reasoning_override=old_entry.reasoning_override,
- from typing import Dict, List, Optional, Any
  - `gateway/session.py@-14,0 +16,2` UNMARKED (1 lines): from hermes_loop_guard import LoopGuardedLock
- class SessionStore(
  - `gateway/session.py@-1403,4 +1739,2` UNMARKED (2 lines): self._promote_session_reset(session_key, old_entry.session_id, "session_switch",
  - `gateway/session.py@-1408,6 +1742,2` UNMARKED (2 lines): self._reopen_session_row(session_key, target_session_id,
  - `gateway/session.py@-1417,2 +1747` UNMARKED (1 lines): transport_profile=new_entry.transport_profile)
- def build_session_context(
  - `gateway/session.py@-1478 +1807,2` UNMARKED (2 lines): context.created_at = getattr(session_entry, "created_at", None)
- from gateway.session_identity import transport_profile_of
  - `gateway/session.py@-18,0 +22` UNMARKED (1 lines): from gateway.session_attended import SessionAttendedMixin
- logger = logging.getLogger(__name__)
  - `gateway/session.py@-27,0 +31` UNMARKED (1 lines): _route_versions = count()
- def sanitize_model_override(override: Optional[dict[str, Any]]) -> Optional[dict
  - `gateway/session.py@-491,0 +496,4` UNMARKED (3 lines): deadline = model_pick_deadline(override)
  - `gateway/session.py@-491,0 +496,4#moved` moved: `gateway/run_agent_cache.py`, `gateway/run_session_readback.py`, `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_attended.py`
  - `gateway/session.py@-494,0 +503,9` UNMARKED (4 lines): def sanitize_reasoning_override(override: Optional[dict[str, Any]]) -> Optional[dict[str,
  - `gateway/session.py@-494,0 +503,9#moved` moved: `agent/auxiliary_client.py`, `agent/moa_loop.py`, `agent/voice_turn_route.py`, `cron/jobs.py` (+30 more)
- class SessionEntry:
  - `gateway/session.py@-538,0 +556,13` UNMARKED (13 lines): resume_marker_token: Optional[str] = None
  - `gateway/session.py@-544 +574,5` UNMARKED (5 lines): active_turn_human: bool = True
  - `gateway/session.py@-546,0 +581,2` UNMARKED (2 lines): # Session-scoped /reasoning pick, persisted like model_override so a restart keeps the eff
  - `gateway/session.py@-555,3 +591,2` UNMARKED (2 lines): # keeps it; cleared with it at every conversation boundary (''_clear_session_boundary_secu
  - `gateway/session.py@-558,0 +594,26` UNMARKED (21 lines): # When a resumed successor is interrupted before its predecessor preview can be re-keyed,
  - `gateway/session.py@-565 +626,5` UNMARKED (5 lines): "expiry_finalized", "suspended", "resume_pending", "resume_reason", "resume_marker_token",
  - `gateway/session.py@-572,0 +638,3` UNMARKED (2 lines): if self.incognito:
  - `gateway/session.py@-572,0 +638,3#moved` moved: `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py`, `gateway/session_automation.py` (+75 more)
  - `gateway/session.py@-575 +643` UNMARKED (1 lines): "created_at": self.created_at.isoformat(), "updated_at": _local_iso(self.updated_at),
  - `gateway/session.py@-581 +649` UNMARKED (1 lines): # Preserve legacy readers" boolean view while the tri-state value is authoritative.
  - `gateway/session.py@-582,0 +651,10` UNMARKED (7 lines): if self.restart_notes:
  - `gateway/session.py@-585,0 +664,15` UNMARKED (13 lines): if self.last_user_activity_at is not None:
  - `gateway/session.py@-589,0 +683,6` UNMARKED (6 lines): if self.reasoning_override:
  - `gateway/session.py@-626,3 +725,25` UNMARKED (19 lines): # Legacy rows stored bare yolo=False for both "off" and "never toggled"; only True
  - `gateway/session.py@-626,3 +725,25#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/retry/action.yml`, `.github/known-flakes.json`, `.prettierrc` (+1974 more)
  - `gateway/session.py@-629,0 +751,7` UNMARKED (7 lines): owner_pid = data.get("active_turn_owner_pid")
  - `gateway/session.py@-633 +761` UNMARKED (1 lines): updated_at=_local_naive(datetime.fromisoformat(data["updated_at"])), origin=origin,
  - `gateway/session.py@-635,0 +764` UNMARKED (1 lines): restart_note_marked_at=_parse_iso(data.get("restart_note_marked_at")),
  - `gateway/session.py@-637,0 +767` UNMARKED (1 lines): active_turn_owner_pid=owner_pid, active_turn_owner_slot=owner_slot,
  - `gateway/session.py@-638,0 +769,11` UNMARKED (11 lines): reasoning_override=sanitize_reasoning_override(data.get("reasoning_override")),
- class AsyncSessionStore:
  - `gateway/session.py@-825,0 +973,3` UNMARKED (1 lines): from gateway.session_incognito import SessionIncognitoMixin
- class SessionStore(
  - `gateway/session.py@-827,2 +977,2` UNMARKED (2 lines): SessionIncognitoMixin, SessionPersistenceMixin, SessionRecoveryMixin, SessionLifecycleMixi
  - `gateway/session.py@-840 +990,3` UNMARKED (3 lines): # Guards _entries / _loaded, and is held across the state.db write of a turn marker or
  - `gateway/session.py@-846,0 +999,3` UNMARKED (3 lines): # Routing rows this process last persisted (key -> entry_json, None if unknown); full save
  - `gateway/session.py@-868,13 +1023,3` UNMARKED (3 lines): # Construction only initializes the cache and makes no state.db probe. The expensive routi
- import threading
  - `gateway/session.py@-9,0 +10#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/permissions.py`, `agent/i18n_layers.py`, `tests/cron/test_missed_execution_retention.py` (+1 more)
- class SessionStore(
  - `gateway/session.py@-904 +1049,3` UNMARKED (3 lines): # Do not probe state.db during construction: another Hermes process may hold its writer lo
  - `gateway/session.py@-929,0 +1077,55` UNMARKED (30 lines): def override_session_keys(self) -> set[str]:
  - `gateway/session.py@-929,0 +1077,55#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2060 more)
  - `gateway/session.py@-931,2 +1133` UNMARKED (1 lines): """Whether any session has ever been created. SQLite is the source of truth."""
  - `gateway/session.py@-997,0 +1199,4` UNMARKED (4 lines): if observed is not None and observed.incognito and not force_new:

### `gateway/session_acp_lifecycle.py`

- async def reopen_if_reattached(authority, session_id, ended):
  - `gateway/session_acp_lifecycle.py@-67,2 +69,4` UNMARKED (3 lines): def _end_idle_acp_sessions(authority, stopped):
  - `gateway/session_acp_lifecycle.py@-67,2 +69,4#moved` moved: `acp_adapter/entry.py`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py` (+691 more)
- A later ``session/load`` + prompt reopens the row (``session_local_recovery.reop
  - `gateway/session_acp_lifecycle.py@-7,0 +8,2#moved` moved: `agent/account_usage_nous.py`, `agent/message_sanitization.py`, `agent/relay_llm.py`, `agent/runtime_session_child.py` (+113 more)
- def end_idle_acp_sessions(authority):
  - `gateway/session_acp_lifecycle.py@-72 +76,23` UNMARKED (9 lines): if stopped.is_set():
  - `gateway/session_acp_lifecycle.py@-72 +76,23#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+5425 more)
- import logging
  - `gateway/session_acp_lifecycle.py@-8,0 +11` UNMARKED (1 lines): from threading import Event

### `gateway/session_api_turn.py`

- def admit_api_steps(adapter, **kwargs):
  - `gateway/session_api_turn.py@-174,0 +216,5` UNMARKED (5 lines): browser_control = _request_browser_control()
- def _admit_api_payload(authority, adapter, sid, request_id, payload, settings, d
  - `gateway/session_api_turn.py@-227 +273` UNMARKED (1 lines): yield partial(check_api_turn, authority, ref, payload)
- def prepare_api_execution(authority, ref, payload):
  - `gateway/session_api_turn.py@-450 +496,2` UNMARKED (2 lines): "resume_unanswered': bool(data and data.get("resume_unanswered')),
- def check_api_turn(authority, ref, payload):
  - `gateway/session_api_turn.py@-49,2 +49,4` UNMARKED (3 lines): if live is None or live.source.platform == Platform.API_SERVER:
  - `gateway/session_api_turn.py@-51,0 +54,3` UNMARKED (1 lines): raise RuntimeStoreError("runtime_draining')
- def api_runtime_locked():
  - `gateway/session_api_turn.py@-555,0 +603,14` UNMARKED (9 lines): def settle_api_runtime_lock(agent, result, captured):
  - `gateway/session_api_turn.py@-555,0 +603,14#moved` moved: `agent/lsp/install.py`, `agent/secret_sources/bitwarden.py`, `agent/secret_sources/onepassword.py`, `evals/desktop_bug_campaign/persistence_live.py` (+75 more)
- def prepare_api_runtime(model, runtime_kwargs):
  - `gateway/session_api_turn.py@-569,0 +631,4` UNMARKED (4 lines): if (options.get("requested_model') or route or options.get("session_model')
- def check_api_turn(authority, ref, payload):
  - `gateway/session_api_turn.py@-65 +68,2` UNMARKED (2 lines): or ("run_owner_scope' in data and not _valid_owner_scope(data["run_owner_scope']))
- def _valid_owner_scope(value):
  - `gateway/session_api_turn.py@-77,0 +82,33` UNMARKED (20 lines): def _valid_browser_control(value):
  - `gateway/session_api_turn.py@-77,0 +82,33#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2523 more)
- def api_session_vars():
  - `gateway/session_api_turn.py@-80 +117,2` UNMARKED (2 lines): #98619 history-delivery provenance and the rebound browser-controller identity. ''{}''
  - `gateway/session_api_turn.py@-84 +122,4` UNMARKED (4 lines): bound = current.get("browser_control') or {}

### `gateway/session_authority.py`

- class SessionAuthority:
  - `gateway/session_authority.py@-1094,0 +1151,9` UNMARKED (1 lines): self._require_admission_open(owned_work=True)
  - `gateway/session_authority.py@-1094,0 +1151,9#moved` moved: `hermes_state_local.py`, `hermes_state_local_lineage.py`, `hermes_state_runtime.py`
  - `gateway/session_authority.py@-1128,2 +1194,6` UNMARKED (2 lines): current = get_session_admission(self.db, admission_id=first["admission_id'])
  - `gateway/session_authority.py@-1171 +1241,5` UNMARKED (1 lines): self._require_admission_open(owned_work=True)
  - `gateway/session_authority.py@-1177,2 +1251,3` UNMARKED (3 lines): session_id=ref.session_id, _guard=self._admission_gate(owned_work=True),
  - `gateway/session_authority.py@-1202 +1277,3` UNMARKED (2 lines): if ((self.runner._draining and not handover_runs_followups(self.runner))
  - `gateway/session_authority.py@-1202 +1277,3#moved` moved: `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/session_authority.py@-1258,2 +1335,4` UNMARKED (2 lines): head, _passing = await claimable_head(self, [row for row in list_session_admissions(
- def backfill_compression_created_sources(db):
  - `gateway/session_authority.py@-1467 +1559` UNMARKED (1 lines): async def initialize_session_authority(runner, *, profile_id, instance_id, db=None, regist
- async def initialize_session_authority(runner, *, profile_id, instance_id, db=No
  - `gateway/session_authority.py@-1475,2 +1572,47` UNMARKED (10 lines): # The runner"s store failed to open (the live-DB guard refusing a probe, a locked or corru
  - `gateway/session_authority.py@-1475,2 +1572,47#moved` moved: `acp_adapter/entry.py`, `acp_adapter/gateway_server.py`, `acp_adapter/session.py`, `agent/agent_init.py` (+805 more)
  - `gateway/session_authority.py@-1477,0 +1620` UNMARKED (1 lines): db = authority.db
  - `gateway/session_authority.py@-1479 +1622,7` UNMARKED (2 lines): if epoch is not None and await asyncio.to_thread(_stored_epoch, db) != epoch:
  - `gateway/session_authority.py@-1490,2 +1639,2` UNMARKED (1 lines): await asyncio.to_thread(release_superseded_bot_chat_at_boot, db, authority.runner.session_
  - `gateway/session_authority.py@-1495,0 +1645,11` UNMARKED (3 lines): return db_key
  - `gateway/session_authority.py@-1495,0 +1645,11#moved` moved: `cron/detached_authority.py`, `cron/executions.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py` (+38 more)
  - `gateway/session_authority.py@-1507 +1662` UNMARKED (1 lines): epochs[db_key] = authority.epoch
- class SessionAuthority:
  - `gateway/session_authority.py@-247 +247` UNMARKED (1 lines): def _require_admission_open(self, *, queue_only=False, owned_work=False):
  - `gateway/session_authority.py@-255 +255,3` UNMARKED (2 lines): if not ((queue_only and restart_wait_open(self.runner))
  - `gateway/session_authority.py@-255 +255,3#moved` moved: `gateway/run_turn/__init__.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/session_authority.py@-258 +260` UNMARKED (1 lines): def _admission_gate(self, authorize=None, *, queue_only=False, owned_work=False):
  - `gateway/session_authority.py@-262 +264` UNMARKED (1 lines): self._require_admission_open(queue_only=queue_only, owned_work=owned_work)
  - `gateway/session_authority.py@-527 +536,2` UNMARKED (1 lines): async def admit_automation(self, adapter, event, identity, replaces=None, fold=None, *, au
  - `gateway/session_authority.py@-529 +539,3` UNMARKED (2 lines): return await admit_automation(self, adapter, event, identity, replaces, fold,
  - `gateway/session_authority.py@-730 +779` UNMARKED (1 lines): "voice_turn', "display_kind', "title_preview', "turn_author', "turn_origin', "api_text', "

### `gateway/session_automation.py`

- async def _admit_folded_completion(authority, adapter, event, evt, identity):
  - `gateway/session_automation.py@-115 +142` UNMARKED (1 lines): fold=waiting["_skip'], owned_work=True)
  - `gateway/session_automation.py@-120 +147` UNMARKED (1 lines): return await authority.admit_automation(adapter, event, identity, owned_work=True)
- async def prepare_heartbeat(runner, event):
  - `gateway/session_automation.py@-188,0 +216#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+1 more)
  - `gateway/session_automation.py@-190 +218` UNMARKED (1 lines): entry = await run_store_call(route_entry, authority, str(event.get("session_key') or "'))
  - `gateway/session_automation.py@-193 +221,2` UNMARKED (2 lines): owner = await asyncio.to_thread(_route_owner, authority, entry)
- def producer_identity(runner, event):
  - `gateway/session_automation.py@-23 +24,2` UNMARKED (2 lines): raise RuntimeStoreError("invalid_params', field="producer_identity',
- def _fold_queued(admission_id, session_id, replaced, skip=()):
  - `gateway/session_automation.py@-254 +283,4` UNMARKED (4 lines): from gateway.session_automation_retry import refusal_held
- async def withdraw_process_ticks(runner, event):
  - `gateway/session_automation.py@-274,0 +307#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+1 more)
  - `gateway/session_automation.py@-276 +309` UNMARKED (1 lines): entry = await run_store_call(route_entry, authority, str(event.get("session_key') or "'))
  - `gateway/session_automation.py@-282,2 +315,2` UNMARKED (2 lines): owner = await asyncio.to_thread(_route_owner, authority, entry)
- def _owner(runner, event, authority):
  - `gateway/session_automation.py@-386,2 +419,17` UNMARKED (15 lines): # A route not yet restored is not proof its work is stale. Only a known
  - `gateway/session_automation.py@-397,0 +446,13` UNMARKED (7 lines): def automation_checkin_metadata(metadata):
  - `gateway/session_automation.py@-397,0 +446,13#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+157 more)
- def automation_display_metadata(metadata):
  - `gateway/session_automation.py@-420,0 +482,13` UNMARKED (10 lines): def automation_retry_metadata(descriptor):
- _AUTOMATION_METADATA = frozenset({'gateway_session_key', 'gateway_session_id', '
  - `gateway/session_automation.py@-432 +506` UNMARKED (1 lines): "delegation_retry_notice', "checkin', "completion_wakes_parent', "completion_resumes_paren
- def admission_display(payload):
  - `gateway/session_automation.py@-449,0 +524,15` UNMARKED (9 lines): _UNCOMPARED_FIELDS = frozenset({"message_id', "chat_topic'})
- def wake_route_envelope(authority, entry, requester_user_id=None):
  - `gateway/session_automation.py@-545 +634` UNMARKED (1 lines): def snapshot_automation(authority, adapter, event, identity, captured=None):
- def snapshot_automation(authority, adapter, event, identity):
  - `gateway/session_automation.py@-547 +636,3` UNMARKED (2 lines): private_goal = is_synthetic(event) and event.metadata.get("goal_continuation') is True
  - `gateway/session_automation.py@-547 +636,3#moved` moved: `gateway/session_refusals.py`, `gateway/shutdown_flush.py`, `tests/gateway/test_goal_wait_durable_admission.py`
  - `gateway/session_automation.py@-549,3 +640,8` UNMARKED (7 lines): or event.media_urls or event.prompt_response or event.source.platform == Platform.API_SERV
  - `gateway/session_automation.py@-549,3 +640,8#moved` moved: `gateway/shutdown_goal_wakes.py`
  - `gateway/session_automation.py@-553,2 +649,3` UNMARKED (3 lines): | automation_checkin_metadata(event.metadata)
  - `gateway/session_automation.py@-557 +654,2` UNMARKED (2 lines): return snapshot_local_automation(authority, adapter, event, identity, entry,
  - `gateway/session_automation.py@-559,13 +657,22` UNMARKED (16 lines): owner = captured[2] if captured is not None else authority.logical_owner(entry.session_id)
  - `gateway/session_automation.py@-559,13 +657,22#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3467 more)
  - `gateway/session_automation.py@-587 +694,2` UNMARKED (2 lines): "automation': {"identity': identity, "owner': owner},
  - `gateway/session_automation.py@-588,0 +697,4` UNMARKED (3 lines): envelope["synthetic'] = synthetic
  - `gateway/session_automation.py@-588,0 +697,4#moved` moved: `tui_gateway/agent_factory.py`
  - `gateway/session_automation.py@-597 +709` UNMARKED (1 lines): def _check_automation_route(runner, payload, session_id, available_source, adapter, entry)
- def check_automation_route(runner, payload, session_id, available_source, adapte
  - `gateway/session_automation.py@-614 +725` UNMARKED (1 lines): def _local_automation_owner(authority, adapter, event, entry, *, ref=None):
- def _local_automation_owner(authority, adapter, event, entry):
  - `gateway/session_automation.py@-624,2 +735,3#moved` moved: `agent/vault_store.py`, `gateway/platforms/api_server_bot_chat.py`, `gateway/session_bot.py`, `gateway/session_policy.py` (+4 more)
  - `gateway/session_automation.py@-633,2 +745,26` UNMARKED (8 lines): """Synchronous compatibility API for worker-only callers."""
  - `gateway/session_automation.py@-633,2 +745,26#moved` moved: `gateway/run_inbound.py`, `gateway/session_controls.py`, `gateway/session_detached.py`, `gateway/session_local_recovery.py` (+5 more)
- def snapshot_local_automation(authority, adapter, event, identity, entry):
  - `gateway/session_automation.py@-638,0 +775,2` UNMARKED (2 lines): descriptor.update(automation_checkin_metadata(event.metadata))
  - `gateway/session_automation.py@-652,0 +786,5` UNMARKED (4 lines): if synthetic := project_synthetic(event):
  - `gateway/session_automation.py@-652,0 +786,5#moved` moved: `gateway/shutdown_goal_wakes.py`
- def restore_local_automation(authority, ref, row):
  - `gateway/session_automation.py@-690,0 +829,2` UNMARKED (2 lines): reply_expected=descriptor.get("reply_expected'),
  - `gateway/session_automation.py@-694,0 +835,2` UNMARKED (2 lines): event.metadata.update(automation_checkin_metadata(descriptor))
  - `gateway/session_automation.py@-698,0 +841,3` UNMARKED (3 lines): if "synthetic' in descriptor:
  - `gateway/session_automation.py@-702,17 +847,6` UNMARKED (3 lines): async def admit_automation(authority, adapter, event, identity, replaces=None, fold=None,
- async def admit_automation(authority, adapter, event, identity, replaces=None, f
  - `gateway/session_automation.py@-721,13 +856,48` UNMARKED (32 lines): authority._require_admission_open(owned_work=owned_work)
  - `gateway/session_automation.py@-721,13 +856,48#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3468 more)
- def route_owner_lock(runner, session_id):
  - `gateway/session_automation.py@-749,0 +920,25` UNMARKED (14 lines): async def _cancel_reset_pinned_wake(authority, row, new_session_id):
  - `gateway/session_automation.py@-749,0 +920,25#moved` moved: `gateway/platforms/api_server_bot_chat.py`, `gateway/run_goal_wait.py`, `gateway/session_authority.py`, `gateway/session_bot.py` (+15 more)
- async def move_route_wakes(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-781,0 +977,2` UNMARKED (1 lines): if await _cancel_reset_pinned_wake(authority, row, entry.session_id):
- async def _reset_and_retarget_local(authority, runner, session_key, old_session_
  - `gateway/session_automation.py@-826,0 +1024,6` UNMARKED (3 lines): rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=owner.ses
- async def reset_and_rehome_messages(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-849 +1052,2` UNMARKED (2 lines): profile_id = getattr(authority, "profile_id', "default')
- async def admit_process_notice(runner, authority, adapter, event, evt):
  - `gateway/session_automation.py@-88,0 +92,21` UNMARKED (9 lines): # A trusted process notice recovers a stored origin, not a new sender assertion.
  - `gateway/session_automation.py@-88,0 +92,21#moved` moved: `gateway/cron_review.py`, `gateway/platforms/base.py`, `gateway/platforms/webhook_ingress.py`, `gateway/plugin_messages.py` (+27 more)
- import logging
  - `gateway/session_automation.py@-9,0 +10#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+4247 more)
- async def reset_and_rehome_messages(runner, session_key, old_session_id):
  - `gateway/session_automation.py@-918,44 +1122,5` UNMARKED (4 lines): async def admit_route_wake(runner, adapter, source, text, producer, metadata=None, *, even
  - `gateway/session_automation.py@-918,44 +1122,5#moved` moved: `gateway/run_goal_wait.py`, `tests/gateway/test_goal_wait_durable_admission.py`
- async def admit_process_notice(runner, authority, adapter, event, evt):
  - `gateway/session_automation.py@-94 +118` UNMARKED (1 lines): return await authority.admit_automation(adapter, event, identity, replaces=evt["_replaces'
  - `gateway/session_automation.py@-96 +120` UNMARKED (1 lines): return await authority.admit_automation(adapter, event, identity, owned_work=True)
  - `gateway/session_automation.py@-98 +122,4` UNMARKED (2 lines): # Serialize replay lookup with folding so a retry cannot fold its own committed batch.
  - `gateway/session_automation.py@-98 +122,4#moved` moved: `cron/scheduler_delivery.py`, `gateway/platforms/webhook.py`, `gateway/run_goal_wait.py`, `gateway/session_api.py` (+14 more)

### `gateway/session_controls.py`

- class AuthorityConnection:
  - `gateway/session_controls.py@-638,0 +639,11#moved` moved: `hermes_state_local.py`, `hermes_state_mutation_chat_pick.py`, `hermes_state_runtime.py`, `tests/gateway/test_goal_wait_durable_admission.py`
  - `gateway/session_controls.py@-673 +684,2` UNMARKED (1 lines): resolve_local_target, self.authority, self.actor,

### `gateway/session_cron.py`

- CRON_SESSION_READ_ONLY_SURFACE = {'layer': 'gateway', 'code': 'cron_session_read
  - `gateway/session_cron.py@-157,2 +166,3` UNMARKED (3 lines): # never-reached model; a store-unavailable refusal preserves repeat budget). They cross th
- async def operation(authority, name, params, actor=None):
  - `gateway/session_cron.py@-186,0 +194,2` UNMARKED (1 lines): await _restore_local_session_for_cron(authority, sid, admission_gate=False)
  - `gateway/session_cron.py@-208 +216` UNMARKED (1 lines): ref = await _restore_local_session_for_cron(authority, ref["session_id'], admission_gate=T
- _owners = {}
  - `gateway/session_cron.py@-21,0 +22,9` UNMARKED (6 lines): async def _restore_local_session_for_cron(authority, sid, *, admission_gate):
- async def operation(authority, name, params, actor=None):
  - `gateway/session_cron.py@-217,0 +226,5` UNMARKED (4 lines): # Cron admissions are local sessions. Restore a cold local session through the async
  - `gateway/session_cron.py@-217,0 +226,5#moved` moved: `gateway/session_ancillary.py`, `gateway/session_complete_path.py`, `gateway/session_connections.py`, `gateway/session_ingress.py` (+3 more)
  - `gateway/session_cron.py@-228 +241` UNMARKED (1 lines): if row["status'] in ("started', "unknown'):
  - `gateway/session_cron.py@-231,0 +245,2` UNMARKED (1 lines): await asyncio.to_thread(cancel_detached, row)
  - `gateway/session_cron.py@-231,0 +245,2#moved` moved: `cron/scheduler_control.py`
  - `gateway/session_cron.py@-232,0 +248,9` UNMARKED (7 lines): from cron.detached_authority import recover_detached
  - `gateway/session_cron.py@-232,0 +248,9#moved` moved: `hermes_state_local.py`, `hermes_state_mutation_chat_pick.py`, `hermes_state_runtime.py`, `tests/cron/test_canonical_detached_survival.py` (+1 more)
  - `gateway/session_cron.py@-240,0 +265` UNMARKED (1 lines): "generation': row["generation'], "profile_id': authority.profile_id,
- async def execute(authority, ref, row, policy):
  - `gateway/session_cron.py@-314,4 +339,10` UNMARKED (7 lines): from cron.detached_authority import detached_assignment, observe_detached
  - `gateway/session_cron.py@-314,4 +339,10#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3466 more)
- async def _admit_local_delivery(authority, chat_id, text, metadata):
  - `gateway/session_cron.py@-54 +63` UNMARKED (1 lines): await _restore_local_session_for_cron(authority, chat_id, admission_gate=True)

### `gateway/session_detached.py`

- async def follow_detached_pin(runner, evt):
  - `gateway/session_detached.py@-239 +253` UNMARKED (1 lines): row = await asyncio.to_thread(authority.db.get_session, target)

### `gateway/session_envelope.py`

- def _validate_native(runner, event, provenance=None, fresh_roles=False):
  - `gateway/session_envelope.py@-149,0 +233,15` UNMARKED (10 lines): if is_synthetic(event):
  - `gateway/session_envelope.py@-149,0 +233,15#moved` moved: `gateway/run_goals.py`, `gateway/run_pending_recovery.py`, `gateway/session_refusals.py`, `gateway/session_synthetic.py` (+4 more)
- async def prepare_native(runner, event):
  - `gateway/session_envelope.py@-231 +329` UNMARKED (1 lines): return await _snapshot_native_async(runner, frozen, provenance, fresh_roles, encoded_sourc
  - `gateway/session_envelope.py@-234 +332` UNMARKED (1 lines): def _native_payload(runner, event, provenance, fresh_roles=False, encoded_source=None, med
- def _snapshot_native(runner, event, provenance, fresh_roles=False, encoded_sourc
  - `gateway/session_envelope.py@-240,0 +339,9` UNMARKED (8 lines): if (received_at := getattr(event, "_attended_activity_received_at', None)) is not None:
  - `gateway/session_envelope.py@-259 +366,4` UNMARKED (3 lines): if synthetic := project_synthetic(event):
  - `gateway/session_envelope.py@-259 +366,4#moved` moved: `gateway/shutdown_goal_wakes.py`
  - `gateway/session_envelope.py@-260,0 +371,2` UNMARKED (2 lines): if hasattr(event, "_native_absorbed_synthetic'):
  - `gateway/session_envelope.py@-272 +384,4` UNMARKED (2 lines): return {"text': event.text, "native_text_v1': envelope}
  - `gateway/session_envelope.py@-275,11 +390,16` UNMARKED (9 lines): legacy["native_text_v1'].pop("attended_activity', None)
  - `gateway/session_envelope.py@-275,11 +390,16#moved` moved: `agent/account_usage.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/credential_pool.py` (+117 more)
  - `gateway/session_envelope.py@-288,0 +409,20` UNMARKED (12 lines): """Build a sync snapshot; sync callers may acquire the SessionStore lock."""
- _CONTEXT_FIELDS = ('auto_skill', 'channel_prompt', 'channel_context', 'reply_exp
  - `gateway/session_envelope.py@-30,2 +30` UNMARKED (1 lines): # Closed metadata contracts for ordinary adapter inputs. Unknown keys refuse admission.
- def restore_native_source(payload, runner=None):
  - `gateway/session_envelope.py@-301 +441,15` UNMARKED (13 lines): provenance = dict(envelope["provenance'])
  - `gateway/session_envelope.py@-301 +441,15#moved` moved: `gateway/authz_mixin.py`
- def restore_native(payload, runner=None):
  - `gateway/session_envelope.py@-316,0 +471,4` UNMARKED (4 lines): if activity := envelope.get("attended_activity'):
  - `gateway/session_envelope.py@-318,0 +477,2` UNMARKED (1 lines): if recovery_id := envelope.get("recovery_id'):
  - `gateway/session_envelope.py@-318,0 +477,2#moved` moved: `gateway/run_pending_recovery.py`, `tests/gateway/test_shutdown_followup_restart.py`
  - `gateway/session_envelope.py@-327,0 +488,10` UNMARKED (10 lines): from gateway.session_automation import automation_checkin_metadata
- async def check_native_route(runner, payload, session_id, available_source, adap
  - `gateway/session_envelope.py@-334,2 +504,2` UNMARKED (2 lines): from gateway.session_automation import check_automation_route_async
  - `gateway/session_envelope.py@-347 +517,2` UNMARKED (1 lines): entry = await run_store_lookup(store, "lookup_by_session_key', route)
  - `gateway/session_envelope.py@-347 +517,2#moved` moved: `gateway/run_agent_cache.py`, `gateway/turn_session_titles.py`
- _WHATSAPP_NATIVE_MAX_BYTES = 16 * 1024
  - `gateway/session_envelope.py@-36,0 +36` UNMARKED (1 lines): _TELEGRAM_RICH_FIELDS = ("block_count', "block_types', "source', "truncated')
- _MEDIA_FIELDS = ('media_urls', 'media_types', 'media_text_inlined', 'media_filen
  - `gateway/session_envelope.py@-40,2 +40,2` UNMARKED (2 lines): def _whatsapp_metadata(metadata):
- def _native_metadata(event):
  - `gateway/session_envelope.py@-64,0 +59,53` UNMARKED (30 lines): def _telegram_metadata(metadata):
  - `gateway/session_envelope.py@-64,0 +59,53#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9665 more)
- def _adapter_event(event, **changes):
  - `gateway/session_envelope.py@-69 +116,35` UNMARKED (34 lines): projected = replace(event, **changes) if changes else event
- def _refuse(event, reason, clause):
  - `gateway/session_envelope.py@-93 +174,3` UNMARKED (3 lines): from gateway.session_refusals import internal_input

### `gateway/session_ingress.py`

- def row_turn_author(policy, row):
  - `gateway/session_ingress.py@-296,0 +419,45` UNMARKED (2 lines): from gateway.session_results import validate_unexecuted_claim

### `gateway/session_ingress_context.py`

- (module)
  - `gateway/session_ingress_context.py@-1,0 +2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2202 more)
- from hermes_state_runtime import RuntimeStoreError
  - `gateway/session_ingress_context.py@-12,0 +15,2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+929 more)
- def _runtime_profile(runner, source, profile, owner_name, homes, relay):
  - `gateway/session_ingress_context.py@-132 +139` UNMARKED (1 lines): def _registry_candidates(runner, platform):
- def _binding(runner, source, profile):
  - `gateway/session_ingress_context.py@-133,0 +141,44` UNMARKED (28 lines): return [(owner, mapping.get(platform)) for owner, mapping in registries]
  - `gateway/session_ingress_context.py@-133,0 +141,44#moved` moved: `acp_adapter/server.py`, `agent/auxiliary_client.py`, `agent/deadline.py`, `gateway/control_socket.py` (+159 more)
  - `gateway/session_ingress_context.py@-137,2 +188,3` UNMARKED (3 lines): candidates = _registry_candidates(runner, platform)
  - `gateway/session_ingress_context.py@-141 +193` UNMARKED (1 lines): home = getattr(runner, "_native_transport_homes', {}).get(lookup_profile)
  - `gateway/session_ingress_context.py@-189,0 +242` UNMARKED (1 lines): lookup_profile = None if profile == "default' else profile
  - `gateway/session_ingress_context.py@-191 +244` UNMARKED (1 lines): or f"adapter:{source.platform.value}:{lookup_profile or ""}')
- def capture_provenance(runner, event):
  - `gateway/session_ingress_context.py@-214,0 +268,15` UNMARKED (11 lines): # The callback has already authenticated this event and _binding has resolved the
  - `gateway/session_ingress_context.py@-214,0 +268,15#moved` moved: `agent/memory_caller_scope.py`, `cron/bot_chat_legacy.py`, `cron/scheduler_control.py`, `cron/scheduler_delivery.py` (+29 more)
- def restore_provenance(runner, source, provenance):
  - `gateway/session_ingress_context.py@-226,0 +295,5` UNMARKED (5 lines): # Before multiplexing, primary native envelopes had no transport_profile field. The
  - `gateway/session_ingress_context.py@-228 +301,18` UNMARKED (14 lines): stored_profile = provenance.get("transport_profile')
  - `gateway/session_ingress_context.py@-228 +301,18#moved` moved: `gateway/session_automation_legacy.py`, `gateway/session_worker_discovery.py`
- import json
  - `gateway/session_ingress_context.py@-9,0 +11#moved` moved: `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py`, `acp_adapter/entry.py` (+2018 more)
- def carry_callback(event, rewritten):
  - `gateway/session_ingress_context.py@-95,0 +100,3` UNMARKED (3 lines): from gateway.session_synthetic import is_synthetic, mark_synthetic

### `gateway/session_ingress_media.py`

- def ingress_fingerprint(payload, raw=None):
  - `gateway/session_ingress_media.py@-192,0 +195,2` UNMARKED (2 lines): envelope.pop("attended_activity', None)
- from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError
  - `gateway/session_ingress_media.py@-23,0 +24,2` UNMARKED (1 lines): from hermes_state_common import HELD_MEDIA_WHERE
  - `gateway/session_ingress_media.py@-23,0 +24,2#moved` moved: `hermes_state_media.py`
- def _held_media_paths(conn):
  - `gateway/session_ingress_media.py@-434,2 +438` UNMARKED (1 lines): FROM session_admissions WHERE """ + HELD_MEDIA_WHERE).fetchall()
  - `gateway/session_ingress_media.py@-437,0 +441#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _held_file_identities(paths, root):
  - `gateway/session_ingress_media.py@-459,0 +464#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def release_admission_media(db, admission_id):
  - `gateway/session_ingress_media.py@-493,2 +498,3` UNMARKED (3 lines): released = release_unheld_media(db, admission_media_references(row["payload']), admitted=T
  - `gateway/session_ingress_media.py@-497,0 +504,43` UNMARKED (1 lines): await asyncio.to_thread(release_admission_media, authority.db, admission_id)
- def collect_unheld_api_images(db):
  - `gateway/session_ingress_media.py@-556,4 +605,33` UNMARKED (28 lines): def _mention_scope(conn, references):
  - `gateway/session_ingress_media.py@-556,4 +605,33#moved` moved: `agent/lsp/deferred.py`, `hermes_cli/plugins_incognito.py`, `hermes_cli/shared_session_attach.py`, `hermes_cli/update_host_obligation.py` (+6 more)
- def _history_mentions(conn, references, after_id=0):
  - `gateway/session_ingress_media.py@-564,0 +643#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
  - `gateway/session_ingress_media.py@-571,2 +650,13` UNMARKED (8 lines): query = "WITH digests(value) AS MATERIALIZED (SELECT value FROM json_each(?)) SELECT conte
  - `gateway/session_ingress_media.py@-571,2 +650,13#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/context_compressor.py` (+108 more)
  - `gateway/session_ingress_media.py@-574 +664,2` UNMARKED (1 lines): for (content,) in conn.execute(query, params):
  - `gateway/session_ingress_media.py@-574 +664,2#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _collectable(references, root):
  - `gateway/session_ingress_media.py@-581,2 +672,7` UNMARKED (3 lines): collected = []
  - `gateway/session_ingress_media.py@-581,2 +672,7#moved` moved: `hermes_cli/mcp_catalog.py`, `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py` (+3 more)
  - `gateway/session_ingress_media.py@-585 +681` UNMARKED (1 lines): def release_unheld_media(db, references, *, retain_history=True, admitted=False):
- def release_unheld_media(db, references, *, retain_history=True):
  - `gateway/session_ingress_media.py@-591 +687,4` UNMARKED (4 lines): lock) or from a copied row (branch/compress insert, caught by the range). ''admitted'': th
  - `gateway/session_ingress_media.py@-598 +697#moved` moved: `hermes_state_media.py`
  - `gateway/session_ingress_media.py@-602 +701,2` UNMARKED (2 lines): scope = {"sessions': _mention_scope(conn, references)} if admitted else {}
  - `gateway/session_ingress_media.py@-617,0 +718#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`

### `gateway/session_local_migration.py`

- def legacy_launch_params(row):
  - `gateway/session_local_migration.py@-85,0 +86,2` UNMARKED (1 lines): row = fallback_restored_row(row) # a fallback"s model is not the session's route (#152333)
  - `gateway/session_local_migration.py@-85,0 +86,2#moved` moved: `gateway/platforms/api_server.py`

### `gateway/session_local_recovery.py`

- def _stray_route_target(authority, receipt, session_id):
  - `gateway/session_local_recovery.py@-173,3 +178` UNMARKED (1 lines): def _read_local_session(authority, sid):
- def restore_local_session(authority, sid):
  - `gateway/session_local_recovery.py@-196,0 +198,5` UNMARKED (1 lines): return receipt, source, policy, entry
  - `gateway/session_local_recovery.py@-202,3 +211,17` UNMARKED (9 lines): receipt, source, policy, entry = prepared
  - `gateway/session_local_recovery.py@-202,3 +211,17#moved` moved: `gateway/run_goal_wait.py`, `gateway/run_inbound.py`, `gateway/run_pending_recovery.py`, `gateway/session_ancillary.py` (+34 more)
  - `gateway/session_local_recovery.py@-241 +248` UNMARKED (1 lines): elif (current.session_id not in receipt.get("lineage', [source.chat_id])
- def _route_already_restored(store, route, target, lineage):
  - `gateway/session_local_recovery.py@-257,0 +261,53` UNMARKED (13 lines): def _publish_local_session(authority, sid, adapter, prepared, live):
  - `gateway/session_local_recovery.py@-257,0 +261,53#moved` moved: `gateway/session_api.py`
- def restore_completion_route(runner, session_key):
  - `gateway/session_local_recovery.py@-440,0 +504,12` UNMARKED (5 lines): with contextlib.suppress(RuntimeStoreError):
- def release_handed_off_receipts(db, *, epoch):
  - `gateway/session_local_recovery.py@-453 +528,4` UNMARKED (3 lines): now holds: any other refused receipt stays as it is, logged when it is touched.
  - `gateway/session_local_recovery.py@-457,0 +536,9` UNMARKED (4 lines): def handed_off(conn, owners=None):
  - `gateway/session_local_recovery.py@-457,0 +536,9#moved` moved: `agent/context_compressor.py`, `agent/gemini_native_adapter.py`, `agent/reasoning_effort.py`, `agent/vault_login_classifier.py` (+77 more)
  - `gateway/session_local_recovery.py@-460,3 +547` UNMARKED (1 lines): return hand_off_conversations(conn, handed_off(conn, candidates))
- def _refused_check(exc):
  - `gateway/session_local_recovery.py@-64,0 +66,4` UNMARKED (1 lines): class _LiveSessionChanged(RuntimeStoreError):

### `gateway/session_persistence.py`

- import threading
  - `gateway/session_persistence.py@-10,0 +11#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2575 more)
- class SessionPersistenceMixin:
  - `gateway/session_persistence.py@-112 +119,2` UNMARKED (2 lines): return self._db_handle_cache.get(
  - `gateway/session_persistence.py@-152 +160,3` UNMARKED (1 lines): if _is_live_system_guard(exc):
  - `gateway/session_persistence.py@-212 +222,3` UNMARKED (1 lines): if _is_live_system_guard(exc):
  - `gateway/session_persistence.py@-224 +236,3` UNMARKED (1 lines): if _is_live_system_guard(exc):
- _SESSIONS_JSON_README = (
  - `gateway/session_persistence.py@-34,0 +36,6` UNMARKED (4 lines): # A full routing save writes only its diff against the rows this process last persisted; t
- class SessionPersistenceMixin:
  - `gateway/session_persistence.py@-358,0 +373` UNMARKED (1 lines): self._load_incognito_locked()
  - `gateway/session_persistence.py@-467,0 +483,2` UNMARKED (1 lines): if _is_live_system_guard(exc):
  - `gateway/session_persistence.py@-600 +617,2` UNMARKED (2 lines): retired = self._write_routing_index_locked(
  - `gateway/session_persistence.py@-625,0 +644,38` UNMARKED (31 lines): def _write_routing_index_locked(self, replacer, encoded: dict[str, str]) -> set[str]:
  - `gateway/session_persistence.py@-625,0 +644,38#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/auxiliary_client.py` (+293 more)
  - `gateway/session_persistence.py@-650 +706,3` UNMARKED (2 lines): if (entry is None or getattr(entry, "incognito", False)
  - `gateway/session_persistence.py@-650 +706,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)
  - `gateway/session_persistence.py@-666,0 +725,5` UNMARKED (4 lines): baseline = getattr(self, "_routing_baseline", None)
  - `gateway/session_persistence.py@-666,0 +725,5#moved` moved: `tools/skill_manager_tool.py`
  - `gateway/session_persistence.py@-90 +97` UNMARKED (1 lines): def _open_session_db_for_active_scope(self, db_path: Optional[Path] = None, *, wait_for_op

### `gateway/session_prune.py`

- async def prune(connection, ref, params):
  - `gateway/session_prune.py@-37,0 +42,11` UNMARKED (9 lines): from hermes_state_prune_budget import validate_max_seconds
  - `gateway/session_prune.py@-37,0 +42,11#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)
  - `gateway/session_prune.py@-40 +55,6` UNMARKED (6 lines): from hermes_cli.sessions_owner import PRUNE_RPC_TIMEOUT
  - `gateway/session_prune.py@-44 +64,11` UNMARKED (8 lines): async def _apply(authority, filters, never_active, within_window=False, max_seconds=None):
  - `gateway/session_prune.py@-44 +64,11#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)
- async def _apply(authority, filters, never_active):
  - `gateway/session_prune.py@-51,3 +81,7` UNMARKED (6 lines): from hermes_state_prune_pacing import prune_pass
  - `gateway/session_prune.py@-58 +92` UNMARKED (1 lines): deleted_ids=deleted_ids, within_deletion_window=within_window, max_seconds=max_seconds, **
- the store under a live gateway would be a second writer, so it never does.
  - `gateway/session_prune.py@-6,0 +7,4` UNMARKED (3 lines): Deletes run only inside ''maintenance.deletion_window'' unless the operator passes ''now''

### `gateway/session_queued_controls.py`

- async def replace_queued(connection, ref, params):
  - `gateway/session_queued_controls.py@-238,2 +238,2` UNMARKED (1 lines): release_admission_media_later(authority, old_id) # queued: the edit never waits on its sca
  - `gateway/session_queued_controls.py@-238,2 +238,2#moved` moved: `gateway/session_authority.py`

### `gateway/session_results.py`

- from hermes_state_terminal import RESULT_PREFIX as _RESULT_PREFIX, load_result
  - `gateway/session_results.py@-9,0 +10,25` UNMARKED (3 lines): def validate_unexecuted_claim(db, epoch, row):
  - `gateway/session_results.py@-9,0 +10,25#moved` moved: `cron/detached_authority.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py`, `gateway/session_pause_announce.py` (+47 more)

### `gateway/session_retry_admission.py`

- def unbind(token) -> None:
  - `gateway/session_retry_admission.py@-58,0 +59,8` UNMARKED (1 lines): raise RuntimeStoreError("runtime_draining')

### `gateway/session_settlement_recovery.py`

- def commit_and_publish(authority, live, ref, row, response, outcome, captured, s
  - `gateway/session_settlement_recovery.py@-180,0 +224` UNMARKED (1 lines): Terminal media cleanup is not part of it: the delivery owner queues it after delivery.
- async def settle_with_retry(authority, live, ref, row, response, outcome, captur
  - `gateway/session_settlement_recovery.py@-215,0 +260,2` UNMARKED (2 lines): from gateway.checkin_delivery import fence_local_settlement
- async def settle_and_deliver(authority, live, ref, row, response, outcome):
  - `gateway/session_settlement_recovery.py@-322,0 +369,3` UNMARKED (2 lines): from gateway.run_checkins import record_local_settlement
  - `gateway/session_settlement_recovery.py@-322,0 +369,3#moved` moved: `gateway/run_watchers.py`
- def release_media_after_delivery(authority, row):
  - `gateway/session_settlement_recovery.py@-348,19 +397,3` UNMARKED (2 lines): """Queue cleanup after delivery through the authority's single off-loop collector."""
  - `gateway/session_settlement_recovery.py@-348,19 +397,3#moved` moved: `gateway/session_authority.py`
- async def recover_failed_settlement(authority, row):
  - `gateway/session_settlement_recovery.py@-53,0 +54,53` UNMARKED (3 lines): from hermes_state_runtime import requeue_unstarted_claim

### `gateway/session_transcript.py`

- class SessionTranscriptMixin:
  - `gateway/session_transcript.py@-164,0 +173,6` UNMARKED (2 lines): private = self._private_history(session_id)
  - `gateway/session_transcript.py@-164,0 +173,6#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
  - `gateway/session_transcript.py@-450,0 +465,4` UNMARKED (3 lines): pending = self._lazy("_pending_outbound_platform_messages", dict)
  - `gateway/session_transcript.py@-450,0 +465,4#moved` moved: `gateway/run_startup.py`
  - `gateway/session_transcript.py@-451,0 +470` UNMARKED (1 lines): pending.pop(key, None) # Consume only after the exact row was successfully written.
  - `gateway/session_transcript.py@-508 +527` UNMARKED (1 lines): with self._get_transcript_drain_lock(), self._transcript_retry_lock:
  - `gateway/session_transcript.py@-510,0 +530,32` UNMARKED (14 lines): pending = self._lazy("_pending_outbound_platform_messages", dict)
  - `gateway/session_transcript.py@-510,0 +530,32#moved` moved: `acp_adapter/model_catalog.py`, `acp_adapter/session.py`, `agent/acp_client.py`, `agent/agent_init.py` (+264 more)
  - `gateway/session_transcript.py@-526,0 +578,11` UNMARKED (4 lines): """Whether a user row with exact content is persisted (False without a DB)."""
  - `gateway/session_transcript.py@-526,0 +578,11#moved` moved: `agent/prompt_refresh.py`, `hermes_cli/browser_connect.py`, `hermes_state_messages.py`
  - `gateway/session_transcript.py@-539,0 +602,16` UNMARKED (9 lines): def replace_failed_turn_boundary(self, session_id: str, row_id: int, content: str) -> bool
  - `gateway/session_transcript.py@-548,0 +627,5` UNMARKED (2 lines): private = self._private_history(session_id)
  - `gateway/session_transcript.py@-548,0 +627,5#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
  - `gateway/session_transcript.py@-631,0 +715,7` UNMARKED (4 lines): private = self._private_history(session_id)
  - `gateway/session_transcript.py@-631,0 +715,7#moved` moved: `agent/auxiliary_health.py`, `agent/incognito_agent.py`, `agent/tool_call_batches.py`, `gateway/platforms/webhook_delivery.py` (+76 more)
- def transcript_append_kwargs(session_id: str, message: dict[str, Any], *, fallba
  - `gateway/session_transcript.py@-90,0 +91,8` UNMARKED (7 lines): # Identity the caller already stamped. Omitting it makes append_message mint a
  - `gateway/session_transcript.py@-90,0 +91,8#moved` moved: `gateway/outbound_reply.py`, `hermes_state_coverage.py`, `session_rules/turns.py`

### `gateway/session_unattended_unknown.py`

- logger = logging.getLogger(__name__)
  - `gateway/session_unattended_unknown.py@-22,0 +25,32` UNMARKED (20 lines): def _trusted_synthetic_descriptor(synthetic) -> bool:
  - `gateway/session_unattended_unknown.py@-22,0 +25,32#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py` (+12709 more)
- def is_unattended(row) -> bool:
  - `gateway/session_unattended_unknown.py@-29,0 +62,8` UNMARKED (6 lines): # These producers never absorb native ingress merges. Legacy snapshots omitted
  - `gateway/session_unattended_unknown.py@-29,0 +62,8#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/gateway_server.py`, `acp_adapter/permissions.py` (+2056 more)
  - `gateway/session_unattended_unknown.py@-34 +74,74` UNMARKED (48 lines): or "automation' in native or "webhook_delivery' in native
  - `gateway/session_unattended_unknown.py@-34 +74,74#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+5983 more)
- def settle_recovered_unattended(db, epoch) -> int:
  - `gateway/session_unattended_unknown.py@-52,0 +165,6` UNMARKED (3 lines): from cron.detached_authority import recover_detached, defer_detached
  - `gateway/session_unattended_unknown.py@-53,0 +172` UNMARKED (1 lines): notice = interrupted_notice(row)
  - `gateway/session_unattended_unknown.py@-56 +175` UNMARKED (1 lines): _terminal_write=lambda conn, lost, notice=notice: close_discarded_turn(db, conn, lost, not
- a cron run (``CRON_PRINCIPAL_ID``), a process completion or heartbeat (``automat
  - `gateway/session_unattended_unknown.py@-7,2 +7,4` UNMARKED (4 lines): (''webhook_delivery''), a Kanban card attempt (''kanban_card_v1'', set by the dispatcher),
- async def settle_unattended_unknown(authority, ref) -> bool:
  - `gateway/session_unattended_unknown.py@-77,0 +196,6` UNMARKED (3 lines): from cron.detached_authority import recover_detached, defer_detached
  - `gateway/session_unattended_unknown.py@-80 +204` UNMARKED (1 lines): notice=interrupted_notice(row))

### `gateway/session_worker.py`

- def _claim(connection, ref, params):
  - `gateway/session_worker.py@-25,0 +27,8` UNMARKED (1 lines): """The same live producer claim at RPC admission and private worker discovery."""
- (module)
  - `gateway/session_worker.py@-3,2 +3,2` UNMARKED (2 lines): Explicit local compute and ledger-fenced canonical cron workers register here. A delegated
- def _verify(connection, ref, params, claim):
  - `gateway/session_worker.py@-50,0 +59,6` UNMARKED (1 lines): from hermes_state_worker_compression import worker_retry_target
  - `gateway/session_worker.py@-62,0 +77,2` UNMARKED (1 lines): if live_only and row["status'] == "terminal':
  - `gateway/session_worker.py@-62,0 +77,2#moved` moved: `cron/detached_authority.py`, `gateway/session_teammate_waits.py`, `hermes_state_runtime.py`
- async def worker_request(connection, ref, params, *, operation):
  - `gateway/session_worker.py@-84,0 +100,7` UNMARKED (5 lines): if params["kind'] == "cron':
  - `gateway/session_worker.py@-84,0 +100,7#moved` moved: `cron/detached_authority.py`, `gateway/session_teammate_waits.py`, `hermes_state_runtime.py`

### `gateway/session_worker_reservation.py`

- def reserve_admission_worker(authority, *, admission_id, process, principal_id,
  - `gateway/session_worker_reservation.py@-110 +110,3` UNMARKED (2 lines): return execute_owned_write(authority.db, write, (),
  - `gateway/session_worker_reservation.py@-110 +110,3#moved` moved: `hermes_state_runtime.py`

### `gateway/shutdown_flush.py`

- logger = logging.getLogger(__name__)
  - `gateway/shutdown_flush.py@-27,0 +30` UNMARKED (1 lines): outcome_logger = logging.getLogger("gateway.run")
- def _remove_replayed(path: Path, session_id: str) -> None:
  - `gateway/shutdown_flush.py@-278,0 +382,12` UNMARKED (4 lines): def quarantine_pending_spool(path: Path, why: str) -> bool:
- def _order_flush_files(paths) -> list[Path]:
  - `gateway/shutdown_flush.py@-317 +444,13` UNMARKED (1 lines): def recover_pending_to_db(session_db=None, *, session_resolver=None, deferred_followup=Non
- def recover_pending_to_db(session_db=None, *, session_resolver=None) -> int:
  - `gateway/shutdown_flush.py@-320 +459,2` UNMARKED (1 lines): session_db, session_resolver=session_resolver, deferred_followup=deferred_followup)[0]
  - `gateway/shutdown_flush.py@-323 +463` UNMARKED (1 lines): def recover_pending_spool(session_db=None, *, session_resolver=None, deferred_followup=Non
- def recover_pending_spool(session_db=None, *, session_resolver=None) -> tuple[in
  - `gateway/shutdown_flush.py@-331,3 +471,4` UNMARKED (1 lines): gateways); ''None'' falls back to ''session_db''. ''deferred_followup'' may claim a resolv
  - `gateway/shutdown_flush.py@-338,0 +480,7` UNMARKED (2 lines): if own_db and session_resolver is not None and not needs_ambient_db:
  - `gateway/shutdown_flush.py@-359,3 +509,8` UNMARKED (1 lines): result = _recover_one_payload(session_db, path, payload,
  - `gateway/shutdown_flush.py@-368 +524` UNMARKED (1 lines): if own_db and session_db is not None: # shutdown cancellation/interrupt must not strand an
- def _recover_one_payload(session_db, path: Path, payload: dict[str, Any], *,
  - `gateway/shutdown_flush.py@-439 +695` UNMARKED (1 lines): timestamp=payload.get("ts", int(time.time())), **row_display)
- All hooks write atomic JSON payloads under ``<hermes_home>/pending_messages/``:
  - `gateway/shutdown_flush.py@-7,2 +7,3` UNMARKED (3 lines): ''recover_pending_to_db'' (after ''runner.start()''; replays via ''SessionDB.append_messag

### `gateway/slash_commands.py`

- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1011 +1097` UNMARKED (1 lines): return ContextNotice(f"{description}\n" + t("gateway.verbose.saved_suffix", platform=platf
  - `gateway/slash_commands.py@-1047 +1133` UNMARKED (1 lines): return EphemeralReply(ContextNotice(t("gateway.busy.set", mode=arg, behavior=t(_BUSY_MODE_
  - `gateway/slash_commands.py@-1093 +1179` UNMARKED (1 lines): return ContextNotice(t("gateway.footer.saved", state=_state(new_state), example=example))
- def _restart_notify_payload(event: MessageEvent) -> dict:
  - `gateway/slash_commands.py@-128,0 +134,27` UNMARKED (15 lines): def _systemd_scope_wrap_if_supervised(argv: list) -> tuple[list, dict | None]:
  - `gateway/slash_commands.py@-128,0 +134,27#moved` moved: `hermes_cli/gateway.py`, `hermes_cli/tools_config_cua.py`, `optional-skills/software-development/ast-grep/scripts/ast_grep_helper.py`, `skills/productivity/google-workspace/scripts/google_api.py` (+6 more)
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-131,5 +163,2` UNMARKED (2 lines): ''--gateway'' enables file-based IPC and the process-exit marker lets the watcher distingu
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1374,0 +1460,2` UNMARKED (1 lines): return t("gateway.restart.in_progress")
  - `gateway/slash_commands.py@-1374,0 +1460,2#moved` moved: `gateway/run_busy.py`, `gateway/run_turn.py`, `gateway/run_turn/run_turn_execution.py`
  - `gateway/slash_commands.py@-1396,5 +1483,4` UNMARKED (2 lines): hermes_cmd = _resolve_update_hermes_bin(_hermes_home, project_root=project_root)
  - `gateway/slash_commands.py@-1396,5 +1483,4#moved` moved: `gateway/update_launcher.py`
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-141,2 +170,2` UNMARKED (2 lines): [sys.executable, "-c", _WINDOWS_UPDATE_HELPER, str(output_path), str(exit_code_path.parent
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-1411 +1494` UNMARKED (1 lines): "timestamp": datetime.now(UTC).isoformat(), "authority": opener_authority(event)} # who an
  - `gateway/slash_commands.py@-1416,4 +1499,4` UNMARKED (4 lines): # The detached wrapper reports completion only through ''.update_process_exit_code'', so t
  - `gateway/slash_commands.py@-1421 +1504,2` UNMARKED (2 lines): result = launch_native_update(home=_hermes_home, hermes_cmd=hermes_cmd, pending=pending,
  - `gateway/slash_commands.py@-1425,0 +1508,2` UNMARKED (2 lines): if not result["started"]:
- def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
  - `gateway/slash_commands.py@-146,0 +176,2` UNMARKED (2 lines): process_exit = exit_code_path.parent / ".update_process_exit_code"
  - `gateway/slash_commands.py@-152,3 +183,4` UNMARKED (4 lines): # Write then rename: a '>' redirect creates the marker empty before printf fills it, and
  - `gateway/slash_commands.py@-158,0 +191` UNMARKED (1 lines): scoped_argv, scoped_env = _systemd_scope_wrap_if_supervised(argv)
  - `gateway/slash_commands.py@-160 +193,6` UNMARKED (5 lines): if scoped_env is not None:
- def _home_thread_from_source(source) -> Optional[str]:
  - `gateway/slash_commands.py@-175,0 +214,3` UNMARKED (1 lines): from gateway.slash_commands_incognito import GatewayIncognitoCommandsMixin
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-177 +218` UNMARKED (1 lines): GatewayLoginCommandsMixin, GatewayAttendCommandsMixin,
  - `gateway/slash_commands.py@-178,0 +220` UNMARKED (1 lines): GatewayIncognitoCommandsMixin,
- from gateway.platforms.event import MessageEvent
  - `gateway/slash_commands.py@-26 +28,2#moved` moved: `gateway/relay/prompt_resolvers.py`, `gateway/run_inbound.py`
- from gateway.session_transcript import TranscriptReadError
  - `gateway/slash_commands.py@-27,0 +31` UNMARKED (1 lines): from gateway.slash_commands_attend import GatewayAttendCommandsMixin
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-280 +322,2` UNMARKED (1 lines): resume_typing_after_prompt(adapter, self._session_key_for_source(source), source.chat_id,
  - `gateway/slash_commands.py@-280 +322,2#moved` moved: `gateway/run_inbound.py`
  - `gateway/slash_commands.py@-442,0 +486,19` UNMARKED (9 lines): async def _source_for_key(key: str) -> SessionSource:
  - `gateway/slash_commands.py@-442,0 +486,19#moved` moved: `agent/acp_client.py`, `agent/auxiliary_managed_local.py`, `agent/auxiliary_plugin_oauth.py`, `agent/client_lifecycle.py` (+167 more)
  - `gateway/slash_commands.py@-443,0 +506` UNMARKED (1 lines): stop_source = await _source_for_key(key)
  - `gateway/slash_commands.py@-445 +508` UNMARKED (1 lines): key, stop_source, interrupt_reason=_INTERRUPT_REASON_STOP,
  - `gateway/slash_commands.py@-483,0 +547,3` UNMARKED (2 lines): # Between turns, /stop still means stop: hold the completions it produces and pause the go
  - `gateway/slash_commands.py@-483,0 +547,3#moved` moved: `gateway/run_agent_cache.py`
  - `gateway/slash_commands.py@-485,2 +551,4` UNMARKED (4 lines): interrupted = interrupt_for_session(session_key=session_key, reason="stop_command",
  - `gateway/slash_commands.py@-607 +675,7` UNMARKED (7 lines): # Test/embedded callers may provide explicit environment-cleared restart context. Do not
- from __future__ import annotations
  - `gateway/slash_commands.py@-7,0 +8,2#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- with open(output_path, "wb") as f:
  - `gateway/slash_commands.py@-77,2 +81,3` UNMARKED (3 lines): # Publish atomically: readers poll for existence, so the marker must never be visible empt
- with open(exit_code_path + ".tmp", "w", encoding="utf-8") as f:
  - `gateway/slash_commands.py@-80 +85` UNMARKED (1 lines): os.replace(temporary, exit_code_path)
- class GatewaySlashCommandsMixin(
  - `gateway/slash_commands.py@-848,0 +923,9` UNMARKED (7 lines): async def _background_reply_route(self, event: MessageEvent):
  - `gateway/slash_commands.py@-848,0 +923,9#moved` moved: `gateway/slash_commands_model.py`, `gateway/slash_commands_session.py`
  - `gateway/slash_commands.py@-854,0 +938` UNMARKED (1 lines): source, reply_anchor = await self._background_reply_route(event)
  - `gateway/slash_commands.py@-857 +941,2` UNMARKED (2 lines): prompt, source, task_id,
  - `gateway/slash_commands.py@-860 +945` UNMARKED (1 lines): return ContextNotice(t("gateway.background.started", preview=_preview(prompt), task_id=tas
  - `gateway/slash_commands.py@-878 +963` UNMARKED (1 lines): model, rt = await self._resolve_session_agent_runtime_async(source=source)
  - `gateway/slash_commands.py@-896 +981,2` UNMARKED (2 lines): reply_source, reply_anchor = await self._background_reply_route(event)

### `gateway/slash_commands_session.py`

- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1019 +1082,48` UNMARKED (30 lines): async def _edit_telegram_topic_metadata(self, event: MessageEvent, raw_args: str) -> str:
  - `gateway/slash_commands_session.py@-1019 +1082,48#moved` moved: `agent/display.py`, `agent/markdown_tables.py`, `agent/message_sanitization.py`, `agent/redact.py` (+103 more)
- def _manual_compression_reply_lines(summary: dict, compressor, focus_topic) -> l
  - `gateway/slash_commands_session.py@-105 +107` UNMARKED (1 lines): def _compress_preview_reply(history, partial: bool, keep_last, focus_topic, agg_note: str,
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1112,0 +1223` UNMARKED (1 lines): topic_lane = await asyncio.to_thread(self._is_telegram_topic_lane, source)
  - `gateway/slash_commands_session.py@-1114 +1225,5` UNMARKED (5 lines): if topic_lane and hasattr(self._session_db, "set_session_title_in_lineage"):
  - `gateway/slash_commands_session.py@-1115,0 +1231,2` UNMARKED (1 lines): stored_title = sanitized
  - `gateway/slash_commands_session.py@-1118 +1235,11` UNMARKED (9 lines): reply = t("gateway.title.set_to", title=stored_title)
  - `gateway/slash_commands_session.py@-1118 +1235,11#moved` moved: `hermes_cli/web_routers/files.py`
- def _compress_preview_reply(history, partial: bool, keep_last, focus_topic, agg_
  - `gateway/slash_commands_session.py@-113 +115` UNMARKED (1 lines): estimate_request_tokens_rough(pv_msgs), level=level)
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-1216 +1343` UNMARKED (1 lines): return ContextNotice(t("gateway.resume.already_on", name=name))
  - `gateway/slash_commands_session.py@-1221,13 +1348,17` UNMARKED (5 lines): session_key, target_id, preserve_prompt_pin=False, preserve_attended_pin=False,
  - `gateway/slash_commands_session.py@-1221,13 +1348,17#moved` moved: `gateway/run_agent_cache.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-1263,2 +1383,2` UNMARKED (2 lines): return ContextNotice(t("gateway.resume.matrix_cross_room_success", title=title,
  - `gateway/slash_commands_session.py@-1266 +1386` UNMARKED (1 lines): return ContextNotice(t("gateway.resume.resumed_no_count", title=title) + left_notice)
  - `gateway/slash_commands_session.py@-1268,2 +1388,2` UNMARKED (2 lines): return ContextNotice(t("gateway.resume.resumed_one", title=title, count=msg_count) + left_
  - `gateway/slash_commands_session.py@-1419 +1539,3` UNMARKED (1 lines): dest_key, new_session_id, preserve_attended_pin=False,
  - `gateway/slash_commands_session.py@-1428 +1550,2` UNMARKED (2 lines): self._clear_session_boundary_security_state(dest_key, persist_yolo=False)
  - `gateway/slash_commands_session.py@-294,0 +297,20` UNMARKED (12 lines): old_session_id = getattr(old_entry, "session_id", None) if old_entry else None
  - `gateway/slash_commands_session.py@-294,0 +297,20#moved` moved: `agent/background_review.py`, `agent/codex_responses_adapter.py`, `agent/conversation_compression.py`, `agent/conversation_compression_checkpoint.py` (+69 more)
  - `gateway/slash_commands_session.py@-295,0 +318,3` UNMARKED (3 lines): getattr(self, "_incognito_gateway_snapshots", {}).pop(session_key, None)
  - `gateway/slash_commands_session.py@-299,8 +324,9` UNMARKED (3 lines): await self._clear_conversation_scope_locked_async(session_key, reason="session_reset")
  - `gateway/slash_commands_session.py@-299,8 +324,9#moved` moved: `gateway/run_agent_cache.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-307,0 +334,21` UNMARKED (16 lines): was_private = is_incognito()
  - `gateway/slash_commands_session.py@-307,0 +334,21#moved` moved: `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/credential_pool_errors.py`, `agent/memory_manager.py` (+53 more)
  - `gateway/slash_commands_session.py@-346,0 +389` UNMARKED (1 lines): paused_count = await paused_followers(self, _old_sid) if _old_sid else 0
  - `gateway/slash_commands_session.py@-550 +593` UNMARKED (1 lines): history_before_user_originated_turn, split_user_originated_turn,
  - `gateway/slash_commands_session.py@-551,0 +595#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`
  - `gateway/slash_commands_session.py@-782,0 +827#moved` moved: `gateway/pairing.py`, `gateway/run_shutdown.py`, `gateway/slash_commands_incognito.py`, `gateway/slash_commands_model.py` (+8 more)
  - `gateway/slash_commands_session.py@-784,0 +830#moved` moved: `gateway/run_turn.py`, `gateway/run_turn_mcp.py`, `gateway/slash_commands_incognito.py`, `gateway/slash_commands_model.py`
  - `gateway/slash_commands_session.py@-786,2 +832,2` UNMARKED (1 lines): return await self._handle_compress_command_inner(event, profile_home=profile_home)
  - `gateway/slash_commands_session.py@-786,2 +832,2#moved` moved: `gateway/run_adapters.py`, `gateway/slash_commands_model.py`, `tests/gateway/test_slash_command_profile_scope.py`, `tests/gateway/test_status_command.py`
- from __future__ import annotations
  - `gateway/slash_commands_session.py@-8,0 +9,2#moved` moved: `agent/agent_init_compression_notice.py`, `agent/agent_runtime_helpers.py`, `agent/background_review.py`, `agent/chat_completion_helpers.py` (+31 more)
- class GatewaySessionCommandsMixin:
  - `gateway/slash_commands_session.py@-812 +858` UNMARKED (1 lines): async def _handle_compress_command_inner(self, event: MessageEvent, *, profile_home=None)
  - `gateway/slash_commands_session.py@-830 +876,2` UNMARKED (2 lines): return _compress_preview_reply(history, request.partial, request.keep_last, request.focus_
  - `gateway/slash_commands_session.py@-832 +879,3` UNMARKED (2 lines): return await self._run_manual_compression(
  - `gateway/slash_commands_session.py@-837 +886,3` UNMARKED (2 lines): async def _run_manual_compression(
  - `gateway/slash_commands_session.py@-837 +886,3#moved` moved: `acp_adapter/content.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/agent_init_compression_notice.py` (+135 more)
  - `gateway/slash_commands_session.py@-847 +898` UNMARKED (1 lines): model, runtime_kwargs = await self._resolve_session_agent_runtime_async(source=source, ses
  - `gateway/slash_commands_session.py@-862,3 +913,8` UNMARKED (6 lines): # Match the live turn"s reasoning settings, including persisted picks and
  - `gateway/slash_commands_session.py@-934 +990,2` UNMARKED (2 lines): archive; an unchanged id without in-place means rotation FAILED. Incognito
  - `gateway/slash_commands_session.py@-936 +993,4` UNMARKED (4 lines): if session_entry.incognito:
  - `gateway/slash_commands_session.py@-970,0 +1031,3` UNMARKED (3 lines): command, _, command_args = args.partition(" ")

### `hermes_cli/cli_model_switch_mixin.py`

- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-1005,0 +1038,3` UNMARKED (1 lines): _cprint("  ✗ /model --all is only available in the gateway.")
  - `hermes_cli/cli_model_switch_mixin.py@-1005,0 +1038,3#moved` moved: `gateway/slash_commands_model.py`
- def _print_switch_summary(cli, result, old_model, *, one_turn: bool, strict_cont
  - `hermes_cli/cli_model_switch_mixin.py@-176 +182` UNMARKED (1 lines): api_key=result.api_key or cli.api_key or "", model_info=mi, api_mode=result.api_mode or ""
- def _apply_reasoning_after_switch(cli, effort: str, *, persist_global: bool, one
  - `hermes_cli/cli_model_switch_mixin.py@-332,0 +339` UNMARKED (1 lines): cli._reasoning_override = None if persist_global else parsed
  - `hermes_cli/cli_model_switch_mixin.py@-339,0 +347,2` UNMARKED (2 lines): cli.agent.reasoning_override = cli._reasoning_override
- def _resolve_cli_reasoning(cli) -> None:
  - `hermes_cli/cli_model_switch_mixin.py@-49 +49,2` UNMARKED (2 lines): # Config-derived, so no explicit pick survives it (delegation must not treat an equal valu
- def stored_session_route(session_meta, *, current_model, current_provider):
  - `hermes_cli/cli_model_switch_mixin.py@-57,0 +59,2` UNMARKED (1 lines): session_meta = fallback_restored_row(session_meta)
  - `hermes_cli/cli_model_switch_mixin.py@-57,0 +59,2#moved` moved: `gateway/platforms/api_server.py`
- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-727,0 +737,2` UNMARKED (2 lines): "reasoning_override": copy.deepcopy(getattr(agent, "reasoning_override", None)),
- def stored_session_route(session_meta, *, current_model, current_provider):
  - `hermes_cli/cli_model_switch_mixin.py@-73,0 +77,3#moved` moved: `agent/agent_init_memory_provider.py`, `agent/delegation_backends.py`, `agent/memory_manager.py`, `agent/terminal_env_registry.py` (+26 more)
- class CLIModelSwitchMixin:
  - `hermes_cli/cli_model_switch_mixin.py@-742,0 +754,2` UNMARKED (2 lines): if "cli_reasoning_override" in snapshot:
  - `hermes_cli/cli_model_switch_mixin.py@-745,6 +758,13` UNMARKED (6 lines): def _restore_reasoning() -> None:
  - `hermes_cli/cli_model_switch_mixin.py@-745,6 +758,13#moved` moved: `agent/route_binding.py`, `tests/agent/test_switch_model_reasoning_override.py`, `tui_gateway/model_switch.py`
  - `hermes_cli/cli_model_switch_mixin.py@-757,0 +778` UNMARKED (1 lines): _restore_reasoning()
  - `hermes_cli/cli_model_switch_mixin.py@-769,0 +791` UNMARKED (1 lines): _restore_reasoning()
  - `hermes_cli/cli_model_switch_mixin.py@-878,0 +899,12` UNMARKED (9 lines): if not getattr(self, "_model_switch_one_turn", False):
  - `hermes_cli/cli_model_switch_mixin.py@-878,0 +899,12#moved` moved: `agent/agent_init_fallback.py`, `gateway/run_config_loaders.py`, `hermes_cli/cli_agent_setup_mixin.py`, `hermes_cli/runtime_provider.py` (+2 more)

### `hermes_cli/cli_session_mixin.py`

- def _reset_model_to_config_default(cli, silent: bool) -> None:
  - `hermes_cli/cli_session_mixin.py@-68,4 +68,9` UNMARKED (3 lines): logger.debug("Config default is unavailable during /new", exc_info=True)
  - `hermes_cli/cli_session_mixin.py@-68,4 +68,9#moved` moved: `acp_adapter/session.py`, `gateway/run.py`, `gateway/run_config_loaders.py`, `gateway/run_inbound.py` (+9 more)
- class CLISessionMixin:
  - `hermes_cli/cli_session_mixin.py@-782,2 +792,2#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`

### `hermes_cli/config_migrations.py`

- from hermes_cli.config_migrations_browser import migrate_to_52
  - `hermes_cli/config_migrations.py@-19,0 +20` UNMARKED (1 lines): from hermes_cli.config_migrations_model_aliases import migrate_to_54
- MIGRATIONS: tuple[tuple[int, Callable[[dict[str, Any], bool], None]], ...] = (
  - `hermes_cli/config_migrations.py@-928,0 +930` UNMARKED (1 lines): (54, migrate_to_54),
  - `hermes_cli/config_migrations.py@-940 +942` UNMARKED (1 lines): LEGACY_KEY_STEPS = frozenset({12, 14, 16, 17, 29, 33, 38, 39, 42, 43, 46, 50, 51, 52, 54})

### `hermes_cli/gateway.py`

- import logging
  - `hermes_cli/gateway.py@-11,0 +12#moved` moved: `agent/account_usage_nous.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_adapter.py`, `agent/auxiliary_health.py` (+149 more)
- def _reaper_candidate_is_supervisor_owned(pid: int) -> bool:
  - `hermes_cli/gateway.py@-1707,0 +1789,19` UNMARKED (9 lines): def _reaper_candidate_matches_home(pid: int) -> bool:
  - `hermes_cli/gateway.py@-1707,0 +1789,19#moved` moved: `CONTRIBUTING.md`, `agent/battery.py`, `agent/deadline.py`, `agent/managed_worker.py` (+254 more)
- def _get_service_pids(all_profiles: bool = False) -> set:
  - `hermes_cli/gateway.py@-174,0 +177` UNMARKED (1 lines): pids |= _gateway_descendants_of(pid)
- def _reap_unsupervised_gateway_orphans(
  - `hermes_cli/gateway.py@-1749 +1849,3` UNMARKED (3 lines): # Cmdlines of ordinary 'gateway run' children do not name their HERMES_HOME.
  - `hermes_cli/gateway.py@-1751 +1853,3` UNMARKED (3 lines): p for p in find_gateway_pids(exclude_pids=own)
- def _get_service_pids(all_profiles: bool = False) -> set:
  - `hermes_cli/gateway.py@-196,0 +200` UNMARKED (1 lines): pids |= _gateway_descendants_of(pid)
  - `hermes_cli/gateway.py@-209,0 +214` UNMARKED (1 lines): pids |= _gateway_descendants_of(pid)
  - `hermes_cli/gateway.py@-217,0 +223,33` UNMARKED (10 lines): def _gateway_descendants_of(pid: int) -> set:
  - `hermes_cli/gateway.py@-217,0 +223,33#moved` moved: `agent/agent_init.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py`, `agent/context_compressor.py` (+234 more)
- def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
  - `hermes_cli/gateway.py@-247,0 +286,8` UNMARKED (6 lines): return bool(_ancestor_chain_to(target_pid))
  - `hermes_cli/gateway.py@-251,0 +298#moved` moved: `hermes_cli/dashboard_procs.py`
  - `hermes_cli/gateway.py@-255 +302` UNMARKED (1 lines): return [pid, *reversed(chain[1:])] if chain else [pid]
  - `hermes_cli/gateway.py@-256,0 +304` UNMARKED (1 lines): chain.append(pid)
- def get_systemd_linger_status(username: str | None = None) -> tuple[bool | None,
  - `hermes_cli/gateway.py@-2771,9 +2875,5` UNMARKED (5 lines): def _launchd_account_home() -> Path:
- def get_launchd_plist_path() -> Path:
  - `hermes_cli/gateway.py@-2781 +2881,2` UNMARKED (1 lines): return Path(pwd.getpwuid(os.getuid()).pw_dir) # windows-footgun: ok — POSIX launchd (macOS
  - `hermes_cli/gateway.py@-2785,2 +2886,8` UNMARKED (1 lines): return _launchd_account_home() / "Library" / "LaunchAgents" / f"{name}.plist"
  - `hermes_cli/gateway.py@-2785,2 +2886,8#moved` moved: `hermes_constants.py`
- def get_python_path() -> str:
  - `hermes_cli/gateway.py@-2846,0 +2954,51` UNMARKED (35 lines): """Resolve the service interpreter; effective config is needed before a current pointer ex
  - `hermes_cli/gateway.py@-2846,0 +2954,51#moved` moved: `agent/auxiliary_health.py`, `agent/auxiliary_task_config.py`, `agent/checkpoints/manager.py`, `agent/relay_runtime.py` (+83 more)
- def generate_systemd_unit(system: bool = False, run_as_user: str | None = None)
  - `hermes_cli/gateway.py@-3109 +3267,3` UNMARKED (3 lines): # An unbounded drain budget resolves to math.inf; systemd spells that "infinity" and rejec
- def _agent_timeout_setting(env_var: str, key: str, parse) -> float:
  - `hermes_cli/gateway.py@-3431,0 +3592,12` UNMARKED (5 lines): def _gateway_timeout_setting(env_var: str, key: str, parse) -> float:
- def _get_restart_exit_wait_budget() -> float:
  - `hermes_cli/gateway.py@-3447 +3619,4` UNMARKED (3 lines): delegation_timeout=_gateway_timeout_setting(
- from hermes_cli.gateway_launchd import (
  - `hermes_cli/gateway.py@-3829,0 +4004` UNMARKED (1 lines): get_launchd_slot_labels,
- from gateway.restart import (
  - `hermes_cli/gateway.py@-44,0 +46` UNMARKED (1 lines): parse_restart_delegation_timeout,
- def _respawn_storm_backoff() -> None:
  - `hermes_cli/gateway.py@-4456,0 +4632,17` UNMARKED (9 lines): async def _acknowledge_release_when_running(*, poll_seconds: float = 1.0) -> None:
  - `hermes_cli/gateway.py@-4456,0 +4632,17#moved` moved: `agent/file_safety.py`, `agent/lsp/workspace.py`, `agent/redact_commands.py`, `cron/lifecycle_guard.py` (+26 more)
- def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False, fo
  - `hermes_cli/gateway.py@-4458 +4650` UNMARKED (1 lines): idle_exit: bool = False, client_owned: bool = False):
  - `hermes_cli/gateway.py@-4488 +4680` UNMARKED (1 lines): if not client_owned and supports_systemd_services():
  - `hermes_cli/gateway.py@-4497,0 +4690,2` UNMARKED (2 lines): from gateway.shutdown_watchdog import sweep_stale_pid_heartbeats
  - `hermes_cli/gateway.py@-4521,5 +4714,6` UNMARKED (3 lines): async def _run_with_release_ack() -> bool:
  - `hermes_cli/gateway.py@-4521,5 +4714,6#moved` moved: `gateway/run_restart_recovery.py`, `tests/cron/test_ambiguous_admission.py`, `tests/gateway/status_report/test_gateway_wiring.py`, `tests/gateway/status_report/test_reporter_lock.py` (+5 more)
  - `hermes_cli/gateway.py@-4527,23 +4721,7` UNMARKED (5 lines): from hermes_cli.gateway_process_exit import make_gateway_process_exit_hook
  - `hermes_cli/gateway.py@-4527,23 +4721,7#moved` moved: `acp_adapter/auth.py`, `acp_adapter/events.py`, `acp_adapter/model_catalog.py`, `acp_adapter/session.py` (+482 more)
- def _cmd_run(args):
  - `hermes_cli/gateway.py@-4950,0 +5129,3` UNMARKED (3 lines): # The direct/launchd ''gateway run'' path must refuse withdrawn handover leftovers too:
  - `hermes_cli/gateway.py@-4953,5 +5134,2` UNMARKED (2 lines): from hermes_cli.gateway_runtime_cli import run_options
- def _install_systemd_from_cli(args, *, force: bool, system: bool, run_as_user) -
  - `hermes_cli/gateway.py@-5071,25 +5249,2` UNMARKED (2 lines): from hermes_cli.gateway_install_cli import install_systemd
- def _cmd_install(args):
  - `hermes_cli/gateway.py@-5101,0 +5257,3` UNMARKED (3 lines): # Installing/bootstrapping the legacy service beside a live withdrawn generation job would
- def probe_gateway_loop_liveness(
  - `hermes_cli/gateway.py@-511 +559,2` UNMARKED (2 lines): def _escalate_wedged_gateway(pid: int, *, term_grace: float = 5.0, kill_wait: float = 5.0,
- def _cmd_install(args):
  - `hermes_cli/gateway.py@-5112,0 +5271,2` UNMARKED (2 lines): from hermes_cli.gateway_install_cli import launchd_options
  - `hermes_cli/gateway.py@-5119 +5279` UNMARKED (1 lines): force_unit_path=getattr(args, "force_unit_path", False), **options)
- def _escalate_wedged_gateway(pid: int, *, term_grace: float = 5.0, kill_wait: fl
  - `hermes_cli/gateway.py@-518,7 +567,27` UNMARKED (15 lines): def replaced() -> bool:
  - `hermes_cli/gateway.py@-518,7 +567,27#moved` moved: `agent/auxiliary_client.py`, `agent/credential_pool_selection.py`, `agent/onboarding_record.py`, `agent/runtime_scope.py` (+70 more)
- def _print_unfolded_gateway_note(owner) -> None:
  - `hermes_cli/gateway.py@-5184,0 +5345,22` UNMARKED (15 lines): def _refuse_forward_only_leftovers(*, retry_on_inspection_error: bool = False) -> None:
  - `hermes_cli/gateway.py@-5184,0 +5345,22#moved` moved: `agent/lsp/install.py`, `agent/secret_sources/bitwarden.py`, `agent/secret_sources/onepassword.py`, `evals/desktop_bug_campaign/persistence_live.py` (+84 more)
- def _cmd_start(args):
  - `hermes_cli/gateway.py@-5185,0 +5368` UNMARKED (1 lines): _refuse_forward_only_leftovers()
  - `hermes_cli/gateway.py@-5191,0 +5375,5` UNMARKED (4 lines): if sys.platform == "darwin" and not start_all:
  - `hermes_cli/gateway.py@-5191,0 +5375,5#moved` moved: `hermes_cli/gateway_launchd.py`
- def _cmd_stop(args):
  - `hermes_cli/gateway.py@-5251,0 +5440,3` UNMARKED (1 lines): set_intent(get_hermes_home(), stopped=True)
  - `hermes_cli/gateway.py@-5251,0 +5440,3#moved` moved: `hermes_cli/_early_recovery.py`, `hermes_cli/auth_device_flow.py`, `hermes_cli/cli_tui_runtime_mixin.py`, `hermes_cli/clipboard.py` (+37 more)
- def _escalate_wedged_gateway(pid: int, *, term_grace: float = 5.0, kill_wait: fl
  - `hermes_cli/gateway.py@-527 +596,12` UNMARKED (6 lines): terminate_pid(pid, force=False, expected_start_time=guarded_start_time())
  - `hermes_cli/gateway.py@-527 +596,12#moved` moved: `gateway/run_shutdown.py`, `hermes_cli/gateway_guardian.py`, `pm/filesystem.py`, `tools/browser_supervisor_capture.py` (+2 more)
  - `hermes_cli/gateway.py@-531 +611,2` UNMARKED (2 lines): gone = _wait_for_pid_exit(pid, remaining(kill_wait))
- def _cmd_restart(args):
  - `hermes_cli/gateway.py@-5353,0 +5545` UNMARKED (1 lines): _refuse_forward_only_leftovers()
  - `hermes_cli/gateway.py@-5367,0 +5560,3` UNMARKED (2 lines): if sys.platform == "darwin" and not restart_all:
  - `hermes_cli/gateway.py@-5367,0 +5560,3#moved` moved: `hermes_cli/gateway_launchd.py`
- def _cmd_list(args):
  - `hermes_cli/gateway.py@-5550,0 +5747,5` UNMARKED (3 lines): def _cmd_update(args) -> int:
- def _cmd_migrate(args):
  - `hermes_cli/gateway.py@-5565,0 +5767,5` UNMARKED (3 lines): def _cmd_guardian(args):
- _GATEWAY_SUBCOMMANDS = {
  - `hermes_cli/gateway.py@-5569 +5775` UNMARKED (1 lines): "status": _cmd_status, "list": _cmd_list, "update": _cmd_update, "guardian": _cmd_guardian
- def _gateway_command_inner(args):
  - `hermes_cli/gateway.py@-5576 +5782,21` UNMARKED (14 lines): def _pm_runtime_venv_dir(project_root: Path | None = None) -> Path | None:
  - `hermes_cli/gateway.py@-5576 +5782,21#moved` moved: `agent/lsp/cli.py`, `apps/desktop/electron/fixtures/source-backend.py`, `hermes_cli/_secrets_common.py`, `hermes_cli/connectors_cmd.py` (+24 more)

### `hermes_cli/gateway_client.py`

- async def connect_gateway(endpoint=None, subject=None, *, max_size=8 * 1024 * 10
  - `hermes_cli/gateway_client.py@-361 +397,4` UNMARKED (1 lines): result = await asyncio.to_thread(ensure_gateway_runtime, home, **kwargs)

### `hermes_cli/gateway_runtime.py`

- def ensure_gateway_runtime(profile_home: str | Path, *, timeout: float | None =
  - `hermes_cli/gateway_runtime.py@-473 +512,2` UNMARKED (1 lines): observed = _observe_replacing_stale(home, deadline, replaced, **own_child)

### `hermes_cli/gateway_session_store.py`

- class GatewaySessionStore:
  - `hermes_cli/gateway_session_store.py@-308,0 +314,8` UNMARKED (2 lines): row = conn.execute("SELECT runtime_revision FROM sessions WHERE id = ?", (session_id,)).fe
  - `hermes_cli/gateway_session_store.py@-308,0 +314,8#moved` moved: `cron/detached_authority.py`, `gateway/session_automation_retry.py`, `gateway/session_epoch_watch.py`, `gateway/session_pause_announce.py` (+34 more)

### `hermes_cli/goals.py`

- def draft_contract(objective: str, *, timeout: Optional[float] = None) -> Option
  - `hermes_cli/goals.py@-1077,0 +2421,307` UNMARKED (6 lines): info = db.get_session(sender[1]) or {}
  - `hermes_cli/goals.py@-1077,0 +2421,307#moved` moved: `agent/prompt_cache_clock.py`, `hermes_cli/goals_authority.py`
- class GoalManager:
  - `hermes_cli/goals.py@-1152,0 +2825,99` UNMARKED (5 lines): if db is None or not hasattr(db, "_execute_write"):
  - `hermes_cli/goals.py@-1152,0 +2825,99#moved` moved: `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py`, `persistence/sqlite/conversation/_store/legacy_refusal.py`
- _JUDGE_RESPONSE_SNIPPET_CHARS = 4000
  - `hermes_cli/goals.py@-40,7 +434,39` UNMARKED (1 lines): # Passed to the SessionDB finders so exclusions apply before their row limits.
- def load_goal(session_id: str) -> Optional[GoalState]:
  - `hermes_cli/goals.py@-654,0 +1348,53` UNMARKED (7 lines): cursor.execute("UPDATE state_meta SET value = ? WHERE key = ?",
  - `hermes_cli/goals.py@-654,0 +1348,53#moved` moved: `gateway/run_goal_wait.py`, `hermes_cli/goals_evaluation.py`, `hermes_cli/loops_snapshot.py`
- def save_goal(session_id: str, state: GoalState) -> None:
  - `hermes_cli/goals.py@-666 +1415,5` UNMARKED (1 lines): db.set_meta(_meta_key(session_id), state.to_json())
  - `hermes_cli/goals.py@-666 +1415,5#moved` moved: `hermes_cli/goals_evaluation.py`
  - `hermes_cli/goals.py@-670,0 +1426,30` UNMARKED (3 lines): if row is None or not row[0]:
  - `hermes_cli/goals.py@-670,0 +1426,30#moved` moved: `gateway/shutdown_goal_wakes.py`, `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py` (+1 more)
- def _session_waiting(session_id: str) -> bool:
  - `hermes_cli/goals.py@-749,0 +1557,138` UNMARKED (2 lines): rows = db.list_meta_prefix("goal:")
- def _render_background_block(background_processes: Optional[list[dict[str, Any]]
  - `hermes_cli/goals.py@-877,0 +1868,330` UNMARKED (20 lines): rows = db.get_messages(session_id, limit=_EVIDENCE_SCAN_ROWS + 1, latest=True)

### `hermes_cli/loops.py`

- def load_loop(session_id: str) -> Optional[LoopState]:
  - `hermes_cli/loops.py@-282,2 +491,8` UNMARKED (1 lines): raw = _db_op("get_meta", lambda: db.get_meta(_meta_key(session_id)))
  - `hermes_cli/loops.py@-282,2 +491,8#moved` moved: `gateway/run_goal_wait.py`, `hermes_cli/goals_evaluation.py`, `hermes_cli/loops_snapshot.py`
- def save_loop(session_id: str, state: LoopState) -> None:
  - `hermes_cli/loops.py@-295,2 +541,42` UNMARKED (3 lines): stored = _parse_state(row[0]) if row and row[0] else None
  - `hermes_cli/loops.py@-295,2 +541,42#moved` moved: `gateway/telegram_retire_work.py`, `hermes_cli/lifecycle_wakes.py`, `hermes_cli/session_controls.py`, `persistence/sqlite/conversation/_store/legacy_refusal.py`
- def migrate_loop_to_session(old_session_id: str, new_session_id: str, *, reason:
  - `hermes_cli/loops.py@-348 +634,20` UNMARKED (3 lines): or conn.execute("SELECT value FROM state_meta WHERE key = ?",

### `hermes_cli/session_recovery.py`

- def _init_delivery_ledger_schema(conn: sqlite3.Connection) -> None:
  - `hermes_cli/session_recovery.py@-37,0 +38,23` UNMARKED (2 lines): def _init_async_delegation_events_schema(conn: sqlite3.Connection) -> None:
  - `hermes_cli/session_recovery.py@-37,0 +38,23#moved` moved: `apps/bootstrap-installer/src-tauri/build.rs`, `apps/bootstrap-installer/src-tauri/src/bootstrap.rs`, `apps/bootstrap-installer/src-tauri/src/install_script.rs`, `apps/bootstrap-installer/src-tauri/src/lib.rs` (+234 more)
- _AUXILIARY_TABLE_SCHEMAS: dict[str, Callable[[sqlite3.Connection], None]] = {
  - `hermes_cli/session_recovery.py@-43,0 +67` UNMARKED (1 lines): "async_delegation_events": _init_async_delegation_events_schema,

### `hermes_cli/sessions_cmd.py`

- def _cmd_optimize(db, args):
  - `hermes_cli/sessions_cmd.py@-1001,0 +1035,24` UNMARKED (16 lines): def _cmd_optimize_next_start(args):
  - `hermes_cli/sessions_cmd.py@-1001,0 +1035,24#moved` moved: `gateway/run.py`, `hermes_cli/console_engine.py`, `hermes_cli/kanban_transfer.py`, `hermes_cli/web_routers/profiles.py` (+9 more)
- def _cmd_repair_prompts(db, args):
  - `hermes_cli/sessions_cmd.py@-1205,0 +1263` UNMARKED (1 lines): snapshots = {}
  - `hermes_cli/sessions_cmd.py@-1213,0 +1272` UNMARKED (1 lines): snapshots[row["id"]] = (row["system_prompt"], row.get("tool_names"))
  - `hermes_cli/sessions_cmd.py@-1238,0 +1298` UNMARKED (1 lines): snapshots[row["id"]] = (row["system_prompt"], row.get("tool_names"))
  - `hermes_cli/sessions_cmd.py@-1257,0 +1318#moved` moved: `hermes_cli/worktree_archive.py`, `tests/gateway/test_multiplex_primary_token_scope.py`, `tests/persistence_sqlite/housekeeping/test_delete_session_write_guards.py`, `tests/test_hermes_dir_acl.py`
  - `hermes_cli/sessions_cmd.py@-1264,0 +1326` UNMARKED (1 lines): "skipped": skipped,
  - `hermes_cli/sessions_cmd.py@-1300,2 +1362,7` UNMARKED (4 lines): prompt, pin = snapshots[finding["id"]]
  - `hermes_cli/sessions_cmd.py@-1305,0 +1373,2` UNMARKED (2 lines): for row in skipped:
- def cmd_sessions(args, sessions_parser=None):
  - `hermes_cli/sessions_cmd.py@-1413,0 +1483,3` UNMARKED (3 lines): if action == "optimize" and (getattr(args, "at_next_start", False)
  - `hermes_cli/sessions_cmd.py@-1423,0 +1496,5` UNMARKED (4 lines): if not getattr(args, "dry_run", False) and not getattr(args, "now", False):
- _NEVER_ACTIVE_DEFAULT_DAYS = 30.0
  - `hermes_cli/sessions_cmd.py@-697,0 +698,9` UNMARKED (6 lines): def _now_param(args):
  - `hermes_cli/sessions_cmd.py@-697,0 +698,9#moved` moved: `apps/desktop/src/api/client.ts`, `apps/desktop/src/plugins/hermes-bots/canonical-group-workspace.test.tsx`, `apps/desktop/src/store/session-request-router.ts`, `apps/shared/src/node/gateway-client.test.ts` (+15 more)
- def _prune_never_active_keyed(db, args):
  - `hermes_cli/sessions_cmd.py@-739 +748` UNMARKED (1 lines): result = owner_prune(owner, never_active_days=days, **_now_param(args))
- def _note_pinned_skipped(db, filters, action):
  - `hermes_cli/sessions_cmd.py@-769,0 +779,12` UNMARKED (6 lines): def _prune_runtime_refusal(args):
  - `hermes_cli/sessions_cmd.py@-769,0 +779,12#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_media.py`, `agent/acp_client.py`, `agent/auxiliary_client.py` (+186 more)
- def _cmd_prune_or_archive(db, args, action):
  - `hermes_cli/sessions_cmd.py@-771,0 +793,3` UNMARKED (2 lines): if prune and (refusal := _prune_runtime_refusal(args)):
  - `hermes_cli/sessions_cmd.py@-846 +870,2` UNMARKED (2 lines): pruned = owner_prune(args.owner_endpoint, filters=filters, **_now_param(args))
  - `hermes_cli/sessions_cmd.py@-850 +875,9` UNMARKED (6 lines): from hermes_state_prune_pacing import DeletionWindowClosedError

### `hermes_cli/web_routers/models.py`

- def get_model_info(profile: Optional[str] = None):
  - `hermes_cli/web_routers/models.py@-109 +108,3` UNMARKED (3 lines): auto_ctx = _bounded_context_length_probe(
- _MODEL_INFO_PROBE_BUDGET_S = 5.0
  - `hermes_cli/web_routers/models.py@-64 +64` UNMARKED (1 lines): def _bounded_context_length_probe(model: str, base_url: str, provider: str, *, api_mode: s
- def _bounded_context_length_probe(model: str, base_url: str, provider: str) -> i
  - `hermes_cli/web_routers/models.py@-77,2 +77` UNMARKED (1 lines): config_context_length=None, api_mode=api_mode).result(timeout=_MODEL_INFO_PROBE_BUDGET_S)

### `hermes_cli/web_routers/sessions.py`

- def _prune_sessions(body: SessionPrune):
  - `hermes_cli/web_routers/sessions.py@-116,3 +116,7` UNMARKED (5 lines): from hermes_state_prune_pacing import DeletionWindowClosedError, PruneInProgressError
  - `hermes_cli/web_routers/sessions.py@-116,3 +116,7#moved` moved: `hermes_cli/web_routers/files.py`, `hermes_cli/web_routers/ops.py`, `hermes_cli/web_server_config.py`, `hermes_cli/web_server_sessions.py` (+1 more)
- async def prune_sessions_endpoint(body: SessionPrune):
  - `hermes_cli/web_routers/sessions.py@-988,0 +993,3` UNMARKED (2 lines): from hermes_cli.maintenance_window import deletion_window_refusal
  - `hermes_cli/web_routers/sessions.py@-988,0 +993,3#moved` moved: `hermes_cli/web_routers/ops.py`

### `hermes_state.py`

- class SessionDB(
  - `hermes_state.py@-1024,0 +1141,12` UNMARKED (9 lines): *, session_id: Optional[str] = None, input_rows: Optional[int] = None,
  - `hermes_state.py@-1024,0 +1141,12#moved` moved: `hermes_state_transactions.py`, `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1030,0 +1159,3` UNMARKED (3 lines): # Metrics and privacy share the same session identity. Transcript transactions
  - `hermes_state.py@-1033,0 +1165,3` UNMARKED (2 lines): from hermes_state_prune_budget import current_budget, prune_sql, write_patience
  - `hermes_state.py@-1033,0 +1165,3#moved` moved: `hermes_state_deletion.py`, `hermes_state_prune_budget.py`, `hermes_state_transactions.py`
  - `hermes_state.py@-1041,0 +1176` UNMARKED (1 lines): from hermes_state_incognito import private_write, write_admission
  - `hermes_state.py@-1042,0 +1178,2` UNMARKED (2 lines): if private_write(session_ids):
  - `hermes_state.py@-1044,10 +1181,12` UNMARKED (12 lines): # NOTE: the AUTHORITATIVE replaced/generation probe runs INSIDE the lock below.
  - `hermes_state.py@-1056,2 +1195,10` UNMARKED (7 lines): acquisition_started = time.monotonic()
  - `hermes_state.py@-1056,2 +1195,10#moved` moved: `cron/scheduler_tick.py`, `gateway/run_busy.py`, `gateway/run_goal_wait.py`, `hermes_cli/quiet_single_query.py` (+12 more)
  - `hermes_state.py@-1060,2 +1207,3` UNMARKED (1 lines): conn = cast(sqlite3.Connection, self._conn) # reopen succeeded under the writer guard
  - `hermes_state.py@-1060,2 +1207,3#moved` moved: `cron/delivery_queue.py`, `cron/executions.py`, `gateway/hosted_room_work_storage.py`, `gateway/hosted_rooms_common.py` (+31 more)
  - `hermes_state.py@-1063,10 +1211,4` UNMARKED (4 lines): result = settle_write_transaction(conn, fn, clock=time, logger=logger,
  - `hermes_state.py@-1074,4 +1216,2#moved` moved: `agent/conversation_compression_publication.py`, `persistence/sqlite/conversation/_errors.py`
  - `hermes_state.py@-1092,0 +1233,4` UNMARKED (2 lines): # Local mutex patience is a distinct contention outcome. Preserve its
  - `hermes_state.py@-1092,0 +1233,4#moved` moved: `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1093,0 +1238#moved` moved: `agent/conversation_compression_publication.py`
  - `hermes_state.py@-1123,16 +1268,4` UNMARKED (2 lines): if isinstance(exc, sqlite3.DatabaseError) and triage_write_corruption(
  - `hermes_state.py@-1123,16 +1268,4#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+3750 more)
  - `hermes_state.py@-1142,0 +1276` UNMARKED (1 lines): session_ids: Any = (),
  - `hermes_state.py@-1147 +1281` UNMARKED (1 lines): self._execute_write(_do, patience_s=patience_s, session_ids=(session_ids, params,))
  - `hermes_state.py@-1149 +1283,2` UNMARKED (2 lines): def _write_rowcount(self, sql: str, params: Any = (), *, patience_s: Optional[float] = Non
  - `hermes_state.py@-1157 +1292` UNMARKED (1 lines): return self._execute_write(_do, patience_s=patience_s, session_ids=(session_ids, params,),
  - `hermes_state.py@-1167 +1302` UNMARKED (1 lines): def _read_retrying_ioerr(self, fn: Callable[[sqlite3.Connection], T], *, read_context=None
  - `hermes_state.py@-1169,0 +1305,2` UNMARKED (1 lines): ''read_context'' selects an independent snapshot reader when writer fallback is deferred.
  - `hermes_state.py@-1179,2 +1316,5` UNMARKED (3 lines): with (read_context or self._read_ctx)() as conn:
  - `hermes_state.py@-1179,2 +1316,5#moved` moved: `hermes_state_messages.py`, `hermes_state_transactions.py`, `persistence/sqlite/_holder.py`, `persistence/sqlite/conversation/_store/messages.py` (+4 more)
  - `hermes_state.py@-1194 +1334,2` UNMARKED (2 lines): OR IGNORE); application_id is written only while 0 so racers converge. Skip the checkpoint
  - `hermes_state.py@-1210 +1351,2` UNMARKED (2 lines): generation_needs_checkpoint = not (row and row[0])
  - `hermes_state.py@-1225,0 +1368` UNMARKED (1 lines): generation_needs_checkpoint = True
  - `hermes_state.py@-1227,4 +1370,9` UNMARKED (5 lines): if generation_needs_checkpoint or _read_sqlite_application_id(self.db_path) != current:
  - `hermes_state.py@-1227,4 +1370,9#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/tools.py`, `agent/agent_init.py` (+1891 more)
  - `hermes_state.py@-1337,0 +1486,5` UNMARKED (3 lines): self._raise_if_db_generation_flagged()
  - `hermes_state.py@-1337,0 +1486,5#moved` moved: `persistence/sqlite/_holder.py`
  - `hermes_state.py@-1342 +1495,24` UNMARKED (13 lines): def _db_generation_probe_clean(self) -> bool:
  - `hermes_state.py@-1342 +1495,24#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+2727 more)
  - `hermes_state.py@-1502,0 +1679,6` UNMARKED (5 lines): It runs on a dedicated connection WITHOUT ''self._lock''. A PASSIVE checkpoint needs no pa
  - `hermes_state.py@-1507,6 +1689,4` UNMARKED (2 lines): with self._maintenance_conn(blocking=False) as conn:
  - `hermes_state.py@-1507,6 +1689,4#moved` moved: `hermes_cli/gateway_agent.py`, `hermes_state_compaction_prepare.py`, `hermes_state_display_fold.py`, `hermes_state_search.py` (+8 more)
  - `hermes_state.py@-1517,0 +1698,31` UNMARKED (19 lines): def _maintenance_conn(self, *, blocking: bool = True) -> Iterator[Optional[sqlite3.Connect
  - `hermes_state.py@-1517,0 +1698,31#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3197 more)
  - `hermes_state.py@-1561 +1772,3` UNMARKED (3 lines): with self._checkpoint_lock, self._lock: # an in-flight periodic checkpoint finishes first
  - `hermes_state.py@-1587,0 +1801,4` UNMARKED (4 lines): if checkpoint_conn is not None and not retire_without_close:
  - `hermes_state.py@-1590,0 +1808,2` UNMARKED (2 lines): if checkpoint_conn is not None:
  - `hermes_state.py@-1597,0 +1817,2` UNMARKED (2 lines): elif checkpoint_conn is not None:
  - `hermes_state.py@-1669,0 +1891,3` UNMARKED (1 lines): if private_write((key, value)):
  - `hermes_state.py@-1669,0 +1891,3#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3000 more)
  - `hermes_state.py@-1700 +1924#moved` moved: `hermes_state_gateway.py`, `hermes_state_telegram.py`
- from contextlib import contextmanager, suppress
  - `hermes_state.py@-25,0 +26` UNMARKED (1 lines): from hermes_state_publication import PublicationConnection, state_lock, measure_write, rai
- def _secure_state_db_files(db_path: Path, *, create_main: bool = False) -> None:
  - `hermes_state.py@-315 +322,5#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2110 more)
- def _foreign_state_db_holders(db_path: Path) -> list[tuple[int, str]]:
  - `hermes_state.py@-475,0 +487,24` UNMARKED (13 lines): def _storage_write_in_progress():
  - `hermes_state.py@-475,0 +487,24#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_health.py`, `agent/auxiliary_task_config.py`, `agent/background_review.py` (+1128 more)
- class SessionDB(
  - `hermes_state.py@-481 +516` UNMARKED (1 lines): SessionPromptRefreshMixin, SessionStoredReadsMixin, SessionMissedInboundMixin,
  - `hermes_state.py@-515,0 +551,2` UNMARKED (2 lines): # Warn with callback and pid when one transaction holds the WAL write lock unusually long.
  - `hermes_state.py@-539 +576` UNMARKED (1 lines): def _delete_prompt_if_unreferenced(conn, prompt_hash: str) -> None:
  - `hermes_state.py@-541,3 +578,4` UNMARKED (4 lines): "DELETE FROM system_prompts WHERE hash=? AND NOT EXISTS ("
  - `hermes_state.py@-545,0 +584,41` UNMARKED (21 lines): def _replace_prompt_reference(self, conn, session_id: str, value: Optional[str], *, tool_n
  - `hermes_state.py@-545,0 +584,41#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3894 more)
  - `hermes_state.py@-596,0 +676` UNMARKED (1 lines): check_loop_thread("SessionDB open")
  - `hermes_state.py@-612 +692` UNMARKED (1 lines): self._lock = LoopGuardedLock("SessionDB writer lock")
  - `hermes_state.py@-618 +698` UNMARKED (1 lines): # DATABASE PATH; ordinary reads degrade to the writer lock, searches queue separately.
  - `hermes_state.py@-621 +701` UNMARKED (1 lines): # cannot get a permit must degrade (ordinary reads) or use bounded search admission — bloc
- from hermes_state_lockowners import log_write_lock_holders
  - `hermes_state.py@-63 +64,2` UNMARKED (2 lines): from hermes_loop_guard import LoopGuardedLock, check as check_loop_thread
- class SessionDB(
  - `hermes_state.py@-646,0 +727,5` UNMARKED (5 lines): # Periodic checkpoints run on their own connection, outside self._lock (see _try_wal_check
  - `hermes_state.py@-649,0 +735` UNMARKED (1 lines): self._fts_automerge_disabled = False
- from hermes_state_dbfile import (
  - `hermes_state.py@-68 +70,2` UNMARKED (2 lines): _watched_sqlite_sidecar_paths, has_invalid_sqlite_header_preopen, has_valid_sqlite_header_
- from hermes_state_search import SessionSearchMixin
  - `hermes_state.py@-87,0 +91,4` UNMARKED (4 lines): from hermes_state_stored_reads import SessionStoredReadsMixin
- class SessionDB(
  - `hermes_state.py@-873 +967` UNMARKED (1 lines): def _get_read_conn(self, *, independent: bool = False) -> Optional[sqlite3.Connection]:
  - `hermes_state.py@-875,5 +969,5` UNMARKED (5 lines): return it to self._read_pool). Ordinary reads retain their legacy non-WAL
  - `hermes_state.py@-884 +978,3` UNMARKED (3 lines): if independent and self._read_conns_closed:
  - `hermes_state.py@-888 +984,4` UNMARKED (2 lines): if independent:
  - `hermes_state.py@-888 +984,4#moved` moved: `persistence/sqlite/_holder.py`, `tests/hermes_cli/test_refresh_singleflight.py`
  - `hermes_state.py@-901 +1000` UNMARKED (1 lines): if independent or not isinstance(exc, sqlite3.Error):
  - `hermes_state.py@-926 +1025` UNMARKED (1 lines): def _checkout_read_conn(self, *, independent: bool = False) -> Optional[sqlite3.Connection
  - `hermes_state.py@-929 +1028` UNMARKED (1 lines): if not independent and (not self._wal_active or self.read_only):
  - `hermes_state.py@-930,0 +1030,2` UNMARKED (2 lines): if independent and self._read_conns_closed:
  - `hermes_state.py@-934 +1035,11` UNMARKED (2 lines): return self._get_read_conn(independent=True) if independent else self._get_read_conn()
  - `hermes_state.py@-934 +1035,11#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py` (+4273 more)
  - `hermes_state.py@-937 +1048` UNMARKED (1 lines): def _read_ctx(self, *, reopen: bool = True, resident_only: bool = False, pooled_only: bool
  - `hermes_state.py@-941 +1052,2` UNMARKED (2 lines): degradation: slower beats EMFILE, which the supervisor cannot see. A search
  - `hermes_state.py@-943,3 +1055,18` UNMARKED (5 lines): writer. Pinned snapshots and search operations retain their own connection.
  - `hermes_state.py@-943,3 +1055,18#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+3015 more)
  - `hermes_state.py@-955,12 +1082` UNMARKED (1 lines): self._return_read_conn(conn)
  - `hermes_state.py@-971 +1087#moved` moved: `hermes_state_display_fold.py`

### `hermes_state_common.py`

- from hermes_cli.timefmt import EPOCH_MAX, EPOCH_MIN
  - `hermes_state_common.py@-15 +15,2` UNMARKED (2 lines): from hermes_state_keys import CRON_PRINCIPAL_ID, POLICY_PREFIX

### `hermes_state_errors.py` → `persistence/sqlite/_state/errors.py`

- PERSISTENCE_ERROR_CAUSES = (
  - `hermes_state_errors.py@-101 +116` UNMARKED (1 lines): "replaced", "deleted_wal", "disk", "session_row_missing", "owner_restart", "unknown",
- class SessionCompressionInProgressError(CompressionSessionBusyError):
  - `hermes_state_errors.py@-168,0 +184,4` UNMARKED (2 lines): class CompressionLeaseLostError(SessionCompressionInProgressError):
- _MALFORMED_DB_MARKERS = (*_MALFORMED_SCHEMA_MARKERS, "database disk image is mal
  - `hermes_state_errors.py@-18,0 +19,15` UNMARKED (8 lines): def is_fts_query_syntax_error(exc: BaseException) -> bool:
  - `hermes_state_errors.py@-18,0 +19,15#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/gateway_server.py`, `acp_adapter/permissions.py` (+2057 more)
- class SessionActiveWriteGuardError(RuntimeError):
  - `hermes_state_errors.py@-199,0 +219,6` UNMARKED (3 lines): class GatewayOwnerRestartError(RuntimeError):
- _PERSISTENCE_CAUSE_BY_TYPE = (
  - `hermes_state_errors.py@-269,0 +295` UNMARKED (1 lines): (GatewayOwnerRestartError, "owner_restart"),
- _PERSISTENCE_CAUSE_BY_PHRASE = (
  - `hermes_state_errors.py@-279,0 +306` UNMARKED (1 lines): (("owner_restart", "owner_unavailable", "incomplete_control_response"), "owner_restart"),
- def classify_persistence_error(exc_or_str) -> str:
  - `hermes_state_errors.py@-302 +329,2` UNMARKED (2 lines): live agent (FK rejection; the flush recreates it); "owner_restart" = gateway owner

### `hermes_state_gateway.py`

- class SessionGatewayMixin:
  - `hermes_state_gateway.py@-1030 +1188` UNMARKED (1 lines): rowcount = conn.execute(
  - `hermes_state_gateway.py@-1034 +1192,4` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(session_id, platform), skip_result=0) > 0
  - `hermes_state_gateway.py@-1034 +1192,4#moved` moved: `persistence/sqlite/_holder.py`

### `hermes_state_identity.py` → `persistence/sqlite/conversation/_store/identity.py`

- def _stable_tool_key(row: Any) -> Optional[tuple[Any, ...]]:
  - `hermes_state_identity.py@-111,0 +172,18` UNMARKED (11 lines): # hashlib releases the GIL for inputs of 2,048 bytes or more; display_identity stays below
  - `hermes_state_identity.py@-111,0 +172,18#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py`, `acp_adapter/entry.py`, `acp_adapter/events.py` (+9396 more)
- from agent.message_sanitization import coalesce_tool_call_id
  - `hermes_state_identity.py@-12 +14,59` UNMARKED (34 lines): _MessageRow = TypeVar("_MessageRow", dict[str, Any], sqlite3.Row)
  - `hermes_state_identity.py@-12 +14,59#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+10066 more)
- from __future__ import annotations
  - `hermes_state_identity.py@-6,0 +7#moved` moved: `acp_adapter/gateway_media.py`, `agent/account_usage_cache.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py` (+508 more)
- import json
  - `hermes_state_identity.py@-8 +9,2#moved` moved: `agent/context_compressor.py`, `agent/context_compressor_durable.py`, `agent/conversation_compression_publication.py`, `agent/kanban_turn_recovery.py` (+805 more)

### `hermes_state_mutation_compress.py`

- def compress_in_transaction(db, conn, session_id, payload, prepared):
  - `hermes_state_mutation_compress.py@-21,0 +22,3` UNMARKED (1 lines): from hermes_state_local import _title_follows_head
  - `hermes_state_mutation_compress.py@-21,0 +22,3#moved` moved: `persistence/sqlite/authority/_mutation_publish.py`

### `hermes_state_profile_repair.py` → `persistence/sqlite/admin/profile_repair.py`

- class SessionProfileRepairMixin:
  - `hermes_state_profile_repair.py@-183 +183` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(ids,), skip_result=0)
  - `hermes_state_profile_repair.py@-206 +206` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(ids,), skip_result=0)
  - `hermes_state_profile_repair.py@-269 +269` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(payload,), skip_result="')
  - `hermes_state_profile_repair.py@-291 +291#moved` moved: `hermes_state_sessions.py`
  - `hermes_state_profile_repair.py@-316 +316` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(ids,), skip_result=0)
  - `hermes_state_profile_repair.py@-327 +327` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(rows,), skip_result=0)
  - `hermes_state_profile_repair.py@-340 +340` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(rows,), skip_result=0)
  - `hermes_state_profile_repair.py@-378 +378` UNMARKED (1 lines): return self._execute_write(_do, session_ids=(rows,), skip_result={})

### `hermes_state_telegram.py`

- class SessionTelegramTopicsMixin:
  - `hermes_state_telegram.py@-307 +564` UNMARKED (1 lines): only_if_unbound: bool = False, expected_session_id: Optional[str] = None, move_session: bo

### `hermes_state_usage.py`

- class SessionUsageMixin:
  - `hermes_state_usage.py@-316,6 +327,6` UNMARKED (1 lines): ''session_model_usage'' under that task, but never become the session"s recorded route.

### `hermes_state_worker_compression.py` → `persistence/sqlite/workers/_compression.py`

- def worker_history(db, conn, sid, payload):
  - `hermes_state_worker_compression.py@-201,0 +202,4` UNMARKED (2 lines): elif payload["repair_alternation'] and not payload["include_inactive'] and not payload["in
  - `hermes_state_worker_compression.py@-201,0 +202,4#moved` moved: `agent/runtime_session_compression.py`, `gateway/session_incognito_boundary.py`, `hermes_state_compression.py`, `hermes_state_messages.py`
  - `hermes_state_worker_compression.py@-204 +208,2` UNMARKED (2 lines): include_row_ids=payload["include_row_ids'],
  - `hermes_state_worker_compression.py@-210 +215` UNMARKED (1 lines): def archive_on_connection(db, conn, session_id: str, compacted_messages: list[dict[str, An
- def archive_on_connection(db, conn, session_id: str, compacted_messages: list[di
  - `hermes_state_worker_compression.py@-216 +221#moved` moved: `hermes_state_messages.py`
- def worker_archive(db, conn, sid, payload):
  - `hermes_state_worker_compression.py@-256 +261` UNMARKED (1 lines): optional=("carried_messages', "covered_ids', "unresolved_held', "held_messages', "held_for
  - `hermes_state_worker_compression.py@-257,0 +263,3` UNMARKED (2 lines): held = payload.get("held_messages')
  - `hermes_state_worker_compression.py@-257,0 +263,3#moved` moved: `gateway/platforms/base_session_command.py`, `gateway/session_bot.py`, `gateway/session_ingress.py`, `gateway/session_policy.py` (+4 more)
  - `hermes_state_worker_compression.py@-284,3 +292,8` UNMARKED (5 lines): value = archive_on_connection(db, conn, sid, payload["messages'], model_config_patch=patch
  - `hermes_state_worker_compression.py@-284,3 +292,8#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6803 more)

### `hermes_state_worker_lifecycle.py` → `persistence/sqlite/workers/_lifecycle.py`

- def worker_next_title(db, conn, session_id, payload):
  - `hermes_state_worker_lifecycle.py@-110,0 +116,10` UNMARKED (6 lines): def worker_title_collision(db, conn, session_id, payload):
  - `hermes_state_worker_lifecycle.py@-110,0 +116,10#moved` moved: `gateway/session_handoff.py`, `gateway/session_teammate_waits.py`, `gateway/session_worker_discovery.py`, `hermes_state_local.py` (+5 more)
- WORKER_LIFECYCLE_HANDLERS = {
  - `hermes_state_worker_lifecycle.py@-196,0 +212` UNMARKED (1 lines): "session.title_collision': worker_title_collision,
- def insert_session_row_in_transaction(
  - `hermes_state_worker_lifecycle.py@-225,2 +241,2` UNMARKED (2 lines): if any(conn.execute("SELECT 1 FROM state_meta WHERE key=?', (RETIRED_PREFIX + sid,)).fetch
  - `hermes_state_worker_lifecycle.py@-292 +308` UNMARKED (1 lines): self._delete_prompt_if_unreferenced(conn, system_prompt_hash)
- def worker_title(db, conn, session_id, payload):
  - `hermes_state_worker_lifecycle.py@-64,2 +64,6` UNMARKED (6 lines): # ''reject_collision'' is sent only as True, and only for automatic sources.
  - `hermes_state_worker_lifecycle.py@-69 +73,2` UNMARKED (2 lines): conn, session_id, payload["title'], source=payload["source'],

### `maintenance/adopt-worker-indexed-lookup.md`

- (module)
  - `maintenance/adopt-worker-indexed-lookup.md@-0,0 +1,57` UNMARKED (1 lines): Contract tests: 'tests/hermes_state/test_adoption_indexed_lookup.py',

### `maintenance/compaction-publication.md`

- (module)
  - `maintenance/compaction-publication.md@-0,0 +1,103` UNMARKED (2 lines): Regression coverage belongs in 'tests/hermes_state/test_publication_wait_budget.py'

### `maintenance/compaction-write-amplification.md`

- (module)
  - `maintenance/compaction-write-amplification.md@-0,0 +1,146` UNMARKED (4 lines): 'tests/hermes_state/test_archive_compact_off_writer_lock.py' covers metadata-key

### `maintenance/cron-restart-survival.md`

- (module)
  - `maintenance/cron-restart-survival.md@-0,0 +1,277` UNMARKED (1 lines): Contract tests: 'tests/cron/test_detached_child_restart.py', 'tests/hermes_state/test_runt

### `maintenance/desktop-idle-cpu.md`

- (module)
  - `maintenance/desktop-idle-cpu.md@-0,0 +1,65` UNMARKED (4 lines): 'tests/hermes_state/test_session_signature_triggers.py',

### `maintenance/fts-writer-automerge.md`

- (module)
  - `maintenance/fts-writer-automerge.md@-0,0 +1,23` UNMARKED (1 lines): - **Contract tests:** 'tests/hermes_state/test_fts_automerge.py'.

### `maintenance/gateway-loop-statedb-guard.md`

- (module)
  - `maintenance/gateway-loop-statedb-guard.md@-0,0 +1,341` UNMARKED (8 lines): 'tests/hermes_state/test_active_conversation_replay.py'. On fork base

### `maintenance/hygiene-deferred-adoption.md`

- (module)
  - `maintenance/hygiene-deferred-adoption.md@-0,0 +1,79` UNMARKED (1 lines): Contract tests: 'tests/gateway/test_session_hygiene_turnhold_adoption.py', 'tests/hermes_s

### `maintenance/incognito.md`

- (module)
  - `maintenance/incognito.md@-0,0 +1,176` UNMARKED (3 lines): 'tests/hermes_state/test_incognito_off_regression.py' runs only where git

### `maintenance/nightly-regression-0926b.md`

- (module)
  - `maintenance/nightly-regression-0926b.md@-0,0 +1,76` UNMARKED (3 lines): The absent-path guard alone does not cover an absent-then-recreated database between 'os.a

### `maintenance/preflight-input-archive.md`

- (module)
  - `maintenance/preflight-input-archive.md@-0,0 +1,23` UNMARKED (2 lines): Run 'tests/e2e/core/compaction/', 'tests/agent/test_preflight_compression_timeout_fail_clo

### `maintenance/provider-reported-cost.md`

- (module)
  - `maintenance/provider-reported-cost.md@-0,0 +1,56` UNMARKED (1 lines): Contract tests: 'tests/hermes_state/test_provider_reported_cost.py', 'tests/agent/test_usa

### `maintenance/release-defects.md`

- (module)
  - `maintenance/release-defects.md@-0,0 +1,1461` UNMARKED (2 lines): - Two native invariant functions in 'tests/hermes_state/test_state_db_holders.py' reproduc

### `maintenance/restart-recovery-bounded.md`

- (module)
  - `maintenance/restart-recovery-bounded.md@-0,0 +1,44` UNMARKED (1 lines): Contract tests: 'tests/hermes_state/test_restart_recovery_bounded.py',

### `maintenance/runtime-epoch-owner-fence.md`

- (module)
  - `maintenance/runtime-epoch-owner-fence.md@-0,0 +1,28` UNMARKED (2 lines): 'tests/hermes_state/test_direct_probe_isolation.py' runs the probe shape in a child with a

### `maintenance/runtime-epoch-ownership.md`

- (module)
  - `maintenance/runtime-epoch-ownership.md@-0,0 +1,59` UNMARKED (2 lines): The ownership regression is 'tests/hermes_state/test_runtime_epoch_ownership.py'.

### `maintenance/session-admission-adoption-index.md`

- (module)
  - `maintenance/session-admission-adoption-index.md@-0,0 +1,8` UNMARKED (1 lines): - **Contract tests:** 'tests/hermes_state/test_worker_admission_index.py', 'tests/hermes_s

### `maintenance/session-search-isolation.md`

- (module)
  - `maintenance/session-search-isolation.md@-0,0 +1,47` UNMARKED (2 lines): 'tests/hermes_state/test_search_isolation.py' exercises error classification, saturated ad

### `maintenance/slack-context-notices.md`

- (module)
  - `maintenance/slack-context-notices.md@-0,0 +1,132` UNMARKED (2 lines): ('tests/hermes_state/test_storage_import_boundary.py').

### `maintenance/state-db-holder-scan.md`

- (module)
  - `maintenance/state-db-holder-scan.md@-0,0 +1,40` UNMARKED (1 lines): Contract tests: 'tests/hermes_state/test_darwin_scan_bounds.py', 'tests/hermes_state/test_

### `maintenance/state-db-prune-lock-batching.md`

- (module)
  - `maintenance/state-db-prune-lock-batching.md@-0,0 +1,152` UNMARKED (3 lines): Contract tests: 'tests/hermes_state/test_compaction_prepare_snapshot.py', 'tests/hermes_st

### `maintenance/state-write-lock-hold-indexes.md`

- (module)
  - `maintenance/state-write-lock-hold-indexes.md@-0,0 +1,66` UNMARKED (2 lines): 'tests/hermes_state/test_lock_hold_indexes.py'.

### `maintenance/state-write-stall-attribution.md`

- (module)
  - `maintenance/state-write-stall-attribution.md@-0,0 +1,126` UNMARKED (5 lines): Contract tests: 'tests/hermes_state/test_commit_tail_attribution.py',

### `maintenance/telegram-topics.md`

- (module)
  - `maintenance/telegram-topics.md@-0,0 +1,210` UNMARKED (2 lines): 'tests/hermes_state/test_telegram_icon_migration.py' injects failures at both

### `model_tools.py`

- def handle_function_call(
  - `model_tools.py@-1002,2 +1117,4` UNMARKED (4 lines): result = _emit(result, duration_ms=duration_ms)
  - `model_tools.py@-1006,2 +1123,6` UNMARKED (6 lines): from agent.browser_output_egress import scrub_browser_result
  - `model_tools.py@-1009 +1130` UNMARKED (1 lines): status="error", error_type=type(e).__name__, error_message=detail)
- import time
  - `model_tools.py@-18,0 +19#moved` moved: `acp_adapter/events.py`, `acp_adapter/gateway_server.py`, `acp_adapter/session.py`, `acp_adapter/tools.py` (+342 more)
- def _discord_rewriter(schema_fn_name: str):
  - `model_tools.py@-386,0 +380,11` UNMARKED (7 lines): def _rewrite_browser_navigate(td: dict[str, Any], available: set) -> Optional[dict[str, An
- def _rewrite_browser_exec(td: dict[str, Any], available: set) -> Optional[dict[s
  - `model_tools.py@-399,13 +403,8` UNMARKED (4 lines): if "terminal" not in available:
  - `model_tools.py@-399,13 +403,8#moved` moved: `tests/hermes_cli/test_cli_custom_provider_vision.py`, `tests/tools/test_vision_native_fast_path.py`, `tools/browser_tool.py`
- def _rewrite_browser_vault(td: dict[str, Any], available: set) -> Optional[dict[
  - `model_tools.py@-445,2 +444` UNMARKED (1 lines): """Name the input tool actually available on this surface."""
  - `model_tools.py@-452,0 +451,2` UNMARKED (2 lines): elif "browser_type" in available:
  - `model_tools.py@-459,3 +459,18` UNMARKED (16 lines): _VAULT_NO_PASSWORD_NOTE = (
  - `model_tools.py@-459,3 +459,18#moved` moved: `evals/prefix_rediet/worker.py`
- _DYNAMIC_SCHEMA_REWRITERS = {
  - `model_tools.py@-490,0 +506` UNMARKED (1 lines): "browser_navigate": _rewrite_browser_navigate,
  - `model_tools.py@-492 +508,2` UNMARKED (2 lines): "browser_exec": _compose_rewriters(_rewrite_browser_exec, _rewrite_input_tool_for_vault),
- def _resolve_active_context_length() -> int:
  - `model_tools.py@-601,0 +619` UNMARKED (1 lines): api_mode = str(model_cfg.get("api_mode") or "")
  - `model_tools.py@-609,0 +628` UNMARKED (1 lines): api_mode = str(rt.get("api_mode") or api_mode)
  - `model_tools.py@-621 +640,2` UNMARKED (2 lines): config_context_length=config_ctx, provider=provider,
  - `model_tools.py@-632 +652` UNMARKED (1 lines): _AGENT_LOOP_TOOLS = {"todo_list", "memory", "session_search", "delegate_task", "compact_co
- def _dispatch_bridge_tool(function_name: str, function_args: dict[str, Any],
  - `model_tools.py@-760,0 +781,21` UNMARKED (19 lines): if underlying_name == ts.LOCAL_BATCH_SENTINEL:
  - `model_tools.py@-778,0 +820,12` UNMARKED (6 lines): def _prefix_batch_error(payload: str, where: str) -> str:
  - `model_tools.py@-778,0 +820,12#moved` moved: `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py`, `agent/api_request_hooks.py` (+509 more)
- def _elapsed_ms(start: float) -> int:
  - `model_tools.py@-905,0 +959,49` UNMARKED (28 lines): def _dispatch_local_batch(calls, ids, *, user_task, enabled_tools, middleware_trace,
  - `model_tools.py@-905,0 +959,49#moved` moved: `acp_adapter/session.py`, `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py` (+512 more)
- def handle_function_call(
  - `model_tools.py@-925,0 +1028,4` UNMARKED (1 lines): refusal = tool_refusal(function_name, function_args)
  - `model_tools.py@-925,0 +1028,4#moved` moved: `agent/title_link_context.py`, `agent/turn_recovery.py`, `cron/lifecycle_guard.py`, `gateway/session_tui_ops.py` (+24 more)
  - `model_tools.py@-937 +1043,3` UNMARKED (3 lines): """Scrub browser output before hooks and return the exact scrubbed value."""
  - `model_tools.py@-949,0 +1058` UNMARKED (1 lines): from tools.tool_search import LOCAL_BATCH_SENTINEL
  - `model_tools.py@-950,0 +1060,6` UNMARKED (2 lines): if underlying[0] == LOCAL_BATCH_SENTINEL:

### `plugins/platforms/telegram/adapter.py`

- class TelegramAdapter(TelegramContactMixin, TelegramHeldInboundMixin, TelegramRe
  - `plugins/platforms/telegram/adapter.py@-1149,2 +1647,9` UNMARKED (7 lines): """Check the normalized payload against the countable rich text limit.
  - `plugins/platforms/telegram/adapter.py@-1207 +1714,3` UNMARKED (1 lines): shapes, under the cap, async-capable bot)."""
  - `plugins/platforms/telegram/adapter.py@-1374,11 +1874,14` UNMARKED (3 lines): message_id = msg.get("message_id")
  - `plugins/platforms/telegram/adapter.py@-1374,11 +1874,14#moved` moved: `agent/auxiliary_client.py`, `agent/context_compressor.py`, `agent/conversation_loop.py`, `agent/vision_message_prep.py` (+1 more)
- from plugins.platforms.telegram.telegram_audio_meta import (
  - `plugins/platforms/telegram/adapter.py@-180,2 +212,4` UNMARKED (4 lines): _coerce_duration_seconds, _probe_video_geometry, _probe_voice_duration_seconds,
- from plugins.platforms.telegram.telegram_media_group import (
  - `plugins/platforms/telegram/adapter.py@-191 +225,4` UNMARKED (2 lines): add_item_note, flush_media_group_event, inline_item_text, queue_media_group_event, trackin
- class TelegramAdapter(TelegramContactMixin, TelegramHeldInboundMixin, TelegramRe
  - `plugins/platforms/telegram/adapter.py@-3326,4 +4408,6` UNMARKED (1 lines): if (metadata or {}).get("notify") or (metadata or {}).get("job_id"):
  - `plugins/platforms/telegram/adapter.py@-3407,2 +4555,22` UNMARKED (1 lines): # Rich skips and permanent refusals fall back, never successes or transient failures.
  - `plugins/platforms/telegram/adapter.py@-3414,2 +4582,6` UNMARKED (2 lines): chunks = ([self.format_message(chunk) for chunk in copy_chunks] if copy_chunks is not None
  - `plugins/platforms/telegram/adapter.py@-3417 +4589#moved` moved: `gateway/outbound/send.py`, `plugins/platforms/telegram/rich_parts.py`
  - `plugins/platforms/telegram/adapter.py@-3426,3 +4598,3` UNMARKED (2 lines): """Send chunks in order, extending ''delivered'' (including pre-seeded ids on resume).
  - `plugins/platforms/telegram/adapter.py@-3429,0 +4602,2` UNMARKED (1 lines): used_thread_fallback = False
  - `plugins/platforms/telegram/adapter.py@-3669,2 +4894` UNMARKED (1 lines): reply anchor once on "reply message not found'. Returns the sent message, a flood refusal,
  - `plugins/platforms/telegram/adapter.py@-3700,3 +4934` UNMARKED (1 lines): if use_markdown and ("parse" in str(send_err).lower() or "markdown" in str(send_err).lower
  - `plugins/platforms/telegram/adapter.py@-3710,3 +4942,73` UNMARKED (29 lines): the consumer keeps editing the newest message. A failed continuation reports partial deliv
  - `plugins/platforms/telegram/adapter.py@-3710,3 +4942,73#moved` moved: `apps/desktop/electron/main.ts`, `gateway/platforms/helpers.py`, `gateway/stream_consumer.py`, `hermes_cli/web_routers/models.py` (+13 more)
  - `plugins/platforms/telegram/adapter.py@-4311,17 +5745,12` UNMARKED (2 lines): # One auth gate for every chat-id picker: strangers in a shared group must not drive the o
  - `plugins/platforms/telegram/adapter.py@-4875,3 +6364,5` UNMARKED (1 lines): if delivered and ambiguous:
  - `plugins/platforms/telegram/adapter.py@-5660 +7483` UNMARKED (1 lines): """Cache media from the message this turn replies to, if any."""
  - `plugins/platforms/telegram/adapter.py@-5877,2 +7710,2` UNMARKED (1 lines): event._lobby_split_owner = self._root_lobby_key(msg) # type: ignore[attr-defined]
  - `plugins/platforms/telegram/adapter.py@-6110,2 +8038,2` UNMARKED (2 lines): if ext in _TELEGRAM_IMAGE_EXTENSIONS or doc_mime.startswith("image/"):
  - `plugins/platforms/telegram/adapter.py@-6134 +8061` UNMARKED (1 lines): _file_obj, video_bytes = await _fetch_telegram_file(doc, "video document", local_root(self
  - `plugins/platforms/telegram/adapter.py@-6141 +8068` UNMARKED (1 lines): _file_obj, raw_bytes = await _fetch_telegram_file(doc, "document", local_root(self))
  - `plugins/platforms/telegram/adapter.py@-6168 +8095,2` UNMARKED (1 lines): @tracking_album_items
  - `plugins/platforms/telegram/adapter.py@-6199 +8130,2` UNMARKED (2 lines): file_obj, image_bytes = await _fetch_telegram_file(
  - `plugins/platforms/telegram/adapter.py@-6249 +8181` UNMARKED (1 lines): _file_obj, image_bytes = await _fetch_telegram_file(sticker, "sticker", local_root(self))

### `pm/install_states.py`

- def _install_lock(state: Path):
  - `pm/install_states.py@-101,2 +122` UNMARKED (1 lines): """Hold the legacy per-state lock without waiting, alongside the stable fork locks."""
- def _remove_contents(state: Path) -> bool:
  - `pm/install_states.py@-148,0 +166,3` UNMARKED (1 lines): _KEPT_UNTIL_LAST = frozenset({"inputs", ".install.lock"})
- def collect_orphan_install_states(installs: Path) -> list[Path]:
  - `pm/install_states.py@-161,13 +181,15` UNMARKED (2 lines): if emptied: # still under the locks: only the record and legacy lock remain

### `pm/runtime.py`

- def prepare_runtime(uv: Path, python: Path, root: Path, *, offline: bool = False
  - `pm/runtime.py@-144,31 +150,35` UNMARKED (1 lines): with (root / ".prepare.lock").open("a+b") as lock:
  - `pm/runtime.py@-144,31 +150,35#moved` moved: `pm/receipt.py`, `tests/pm/test_runtime_gc.py`
- def collect_runtime_generations(root: Path) -> list[Path]:
  - `pm/runtime.py@-200,15 +211,18` UNMARKED (2 lines): with (root / ".prepare.lock").open("a+b") as lock:

### `tests/acp_adapter/test_gateway_catalog_reset_owner.py`

- (module)
  - `tests/acp_adapter/test_gateway_catalog_reset_owner.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_reset_child_maps_to_its_owner_while_legacy_reset_histories_stay_indepen
  - `tests/acp_adapter/test_gateway_catalog_reset_owner.py@-31 +32#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)

### `tests/acp_adapter/test_gateway_editor_mcp.py`

- def _peers_running(pid):
  - `tests/acp_adapter/test_gateway_editor_mcp.py@-217,2 +217,2` UNMARKED (2 lines): return [child for child in process_children(pid)

### `tests/acp_adapter/test_gateway_retry_command.py`

- async def _editor(tmp_path, monkeypatch, wire_class=SlashWire):
  - `tests/acp_adapter/test_gateway_retry_command.py@-77 +80,2` UNMARKED (1 lines): yield SimpleNamespace(agent=agent, db=authority.db, sid=sid, wire=wire, updates=updates, e

### `tests/agent/lsp/test_lsp_owned_server.py`

- def test_a_live_owner_keeps_its_server_tree(tmp_path):
  - `tests/agent/lsp/test_lsp_owned_server.py@-171 +171,8` UNMARKED (6 lines): supervisor = []

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
  - `tests/agent/test_runtime_scope.py@-641 +642` UNMARKED (1 lines): with closing(SessionDB(tmp_path / "state.db")) as db, exclusive_maintenance([tmp_path]):

### `tests/agent/test_runtime_session_store.py`

- def test_outbox_journal_survives_a_cross_device_rename(tmp_path, monkeypatch):
  - `tests/agent/test_runtime_session_store.py@-110 +112` UNMARKED (1 lines): assert json.loads(journal.read_text(encoding="utf-8-sig'))["scope'] == {"session_id': "s'}
- def test_worker_store_persists_and_restores_the_tools_prefix_pin(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-124 +126,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_outbox_freezes_failed_payload_and_reopens_exclusively(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-19 +19` UNMARKED (1 lines): disk = json.loads(store.path.read_text(encoding="utf-8-sig'))
  - `tests/agent/test_runtime_session_store.py@-37 +37` UNMARKED (1 lines): assert json.loads(restored.path.read_text(encoding="utf-8-sig'))["pending'] == []
- def test_outbox_capacity_failure_does_not_advance_or_discard(tmp_path):
  - `tests/agent/test_runtime_session_store.py@-60,0 +61,2#moved` moved: `agent/managed_worker.py`, `tests/agent/test_worker_mandatory_capacity.py`, `tests/agent/test_worker_outbox_io_failure.py`, `tests/gateway/fixtures/agent_persistence_worker.py`

### `tests/agent/test_runtime_session_store_surface_parity.py`

- from hermes_state import SessionDB
  - `tests/agent/test_runtime_session_store_surface_parity.py@-15 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_tail_reads_answer_like_the_owner(tmp_path):
  - `tests/agent/test_runtime_session_store_surface_parity.py@-56,0 +58,2` UNMARKED (2 lines): db.set_session_title("s', "Straße')
  - `tests/agent/test_runtime_session_store_surface_parity.py@-62,0 +66,7` UNMARKED (5 lines): for title in ("STRASSE', "Another Subject', "No Match', "'):
  - `tests/agent/test_runtime_session_store_surface_parity.py@-62,0 +66,7#moved` moved: `agent/prompt_cache_clock.py`, `agent/runtime_session_compression.py`, `agent/runtime_session_journal.py`, `agent/runtime_session_privacy.py` (+10 more)

### `tests/agent/test_session_prompt_refresh.py`

- def _agent(srv, home, session_id):
  - `tests/agent/test_session_prompt_refresh.py@-69 +78,2` UNMARKED (1 lines): session_db=db, session_id=session_id, quiet_mode=True, platform="whatsapp",

### `tests/agent/test_title_generator.py`

- from agent.title_generator import (
  - `tests/agent/test_title_generator.py@-14,0 +15` UNMARKED (1 lines): is_junk_title,
- class TestGenerateTitle:
  - `tests/agent/test_title_generator.py@-415,0 +417,22` UNMARKED (21 lines): @pytest.mark.parametrize("title, junk", [
  - `tests/agent/test_title_generator.py@-417,0 +441,5` UNMARKED (5 lines): def test_abstain_payload_is_an_empty_title():
  - `tests/agent/test_title_generator.py@-419,0 +448,14` UNMARKED (8 lines): ("{"title": "Q3 Billing Dispute", "abstain": false}', "Q3 Billing Dispute"),
  - `tests/agent/test_title_generator.py@-419,0 +448,14#moved` moved: `tests/agent/test_auxiliary_client.py`, `tests/agent/test_compaction_archive_once.py`, `tests/agent/test_compaction_offload.py`, `tests/agent/test_compression_attempt_lifecycle.py` (+22 more)
  - `tests/agent/test_title_generator.py@-421,0 +464,18` UNMARKED (11 lines): @pytest.mark.parametrize("result", ["abstain", "loose_abstain", "failure", "truncated"])
  - `tests/agent/test_title_generator.py@-421,0 +464,18#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3497 more)
  - `tests/agent/test_title_generator.py@-423,0 +484,55` UNMARKED (32 lines): @pytest.mark.parametrize("existing, generated", [
  - `tests/agent/test_title_generator.py@-423,0 +484,55#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3477 more)
- class TestAutoTitleSession:
  - `tests/agent/test_title_generator.py@-455,0 +571,49` UNMARKED (32 lines): def test_abstain_keeps_placeholder_and_reply_titles_next(self, tmp_path):
  - `tests/agent/test_title_generator.py@-455,0 +571,49#moved` moved: `tests/agent/test_compress_context_progress_timeout.py`, `tests/agent/test_fallback_session_reload.py`, `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_partial_stream_finish_reason.py` (+20 more)
  - `tests/agent/test_title_generator.py@-458,0 +623` UNMARKED (1 lines): db.has_auto_title_collision.return_value = False
  - `tests/agent/test_title_generator.py@-469 +634` UNMARKED (1 lines): "sess-1", "Readable Session", source="llm", reject_collision=True
- class TestMaybeAutoTitle:
  - `tests/agent/test_title_generator.py@-516,0 +682,51` UNMARKED (23 lines): def test_skips_if_not_first_exchange(self):
  - `tests/agent/test_title_generator.py@-516,0 +682,51#moved` moved: `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/account_usage_nous.py`, `agent/acp_client.py` (+2593 more)
  - `tests/agent/test_title_generator.py@-582,0 +799,51` UNMARKED (37 lines): "main_provider, providers, deferred",
  - `tests/agent/test_title_generator.py@-816,2 +1083,2` UNMARKED (2 lines): def test_a_placeholder_stops_model_requests_after_the_third_turn(self, tmp_path):
  - `tests/agent/test_title_generator.py@-822 +1089,2` UNMARKED (2 lines): with patch("agent.title_generator.generate_title", return_value=None) as generate, \
  - `tests/agent/test_title_generator.py@-827,3 +1095,4` UNMARKED (3 lines): assert db.get_session_title_source("sess-1") == "derived"
  - `tests/agent/test_title_generator.py@-827,3 +1095,4#moved` moved: `tests/agent/test_compaction_events.py`, `tests/agent/test_task_title_style.py`, `tests/agent/test_title_completion_retry.py`
  - `tests/agent/test_title_generator.py@-831 +1100,3` UNMARKED (2 lines): assert db.get_session_title("sess-1") == "Q4 Invoice Reconciliation"
  - `tests/agent/test_title_generator.py@-831 +1100,3#moved` moved: `tests/agent/test_compaction_events.py`, `tests/agent/test_task_title_style.py`, `tests/agent/test_title_completion_retry.py`
- class TestAutoTitleDuplicateHandling:
  - `tests/agent/test_title_generator.py@-869 +1140,2` UNMARKED (2 lines): with patch("agent.title_generator.generate_title", return_value=None), \
  - `tests/agent/test_title_generator.py@-873 +1145` UNMARKED (1 lines): def test_collision_at_persistence_never_notifies_unsuffixed_title(self):
  - `tests/agent/test_title_generator.py@-875,0 +1148` UNMARKED (1 lines): db.has_auto_title_collision.return_value = False
  - `tests/agent/test_title_generator.py@-883,0 +1157,4` UNMARKED (2 lines): def title_callback(title, source, *, display_title=None):
  - `tests/agent/test_title_generator.py@-888 +1165` UNMARKED (1 lines): title_callback=title_callback,
  - `tests/agent/test_title_generator.py@-895,2 +1172,4` UNMARKED (2 lines): # The concurrent winner was not in the earlier snapshot. Show only the persisted alias.
  - `tests/agent/test_title_generator.py@-895,2 +1172,4#moved` moved: `tests/cron/test_scheduler.py`, `tests/gateway/test_display_admission.py`, `tests/gateway/test_multiplex_mcp_discovery.py`, `tests/gateway/test_reset_cleanup_profile_scope.py` (+4 more)
- class TestModelSwitchMarkerNotTitleable:
  - `tests/agent/test_title_generator.py@-997,0 +1277,117` UNMARKED (90 lines): class TestForkTitleContracts:
  - `tests/agent/test_title_generator.py@-997,0 +1277,117#moved` moved: `apps/desktop/src/components/assistant-ui/thread/timeline-position.ts`, `evals/completion_backlog_probe.py`, `evals/cron_error_diagnostics.py`, `evals/goal_command_parity.py` (+895 more)

### `tests/agent/test_turn_facade_lease.py`

- def test_interrupt_before_any_wait_does_not_blame_another_process():
  - `tests/agent/test_turn_facade_lease.py@-141,0 +142,12` UNMARKED (6 lines): def test_message_less_system_interrupt_keeps_its_source():
  - `tests/agent/test_turn_facade_lease.py@-141,0 +142,12#moved` moved: `hermes_cli/immutable_releases.py`, `tests/agent/test_413_compression.py`, `tests/agent/test_codex_app_server_persist.py`, `tests/agent/test_compaction_redaction_boundaries.py` (+112 more)
- def test_lock_tolerance_never_outlives_the_committed_row_expiry(tmp_path, monkey
  - `tests/agent/test_turn_facade_lease.py@-239,3 +251,16` UNMARKED (8 lines): # Near and past expiry with the lock held throughout: a busy database is not a successor,
  - `tests/agent/test_turn_facade_lease.py@-248,4 +273,31` UNMARKED (16 lines): assert [msg for _, msg in events] == ["Session turn lease lost; stopping to protect the tr
  - `tests/agent/test_turn_facade_lease.py@-248,4 +273,31#moved` moved: `agent/conversation_compression.py`, `apps/desktop/e2e/core/harness.ts`, `apps/desktop/e2e/core/interrupted-reply.spec.ts`, `apps/desktop/e2e/core/lineage-compaction-prompt.spec.ts` (+428 more)

### `tests/agent/test_vault_backend_browser.py`

- def test_fill_is_password_blind_and_redaction_registered(real_profile):
  - `tests/agent/test_vault_backend_browser.py@-115 +115,2` UNMARKED (1 lines): patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://example.com"),

### `tests/agent/test_vault_backends.py`

- def test_unlock_uses_vendor_passwordenv_contract_then_fill_routes_by_prefix(fake
  - `tests/agent/test_vault_backends.py@-120 +120` UNMARKED (1 lines): "origin": "https://example.com", "remembered": False}
- if argv[:2] == ["item", "get"]:
  - `tests/agent/test_vault_backends.py@-250,0 +251,7` UNMARKED (7 lines): if "--format" in argv: # metadata read, or the exact authorized item"s revealed JSON
- def test_onepassword_service_account_reads_name_the_items_own_vault(fake_op_serv
  - `tests/agent/test_vault_backends.py@-271 +278,4` UNMARKED (2 lines): 'op item get' never named a vault. Each read now carries the vault its fresh metadata veri
  - `tests/agent/test_vault_backends.py@-271 +278,4#moved` moved: `agent/vault_backends/onepassword.py`
  - `tests/agent/test_vault_backends.py@-273 +283` UNMARKED (1 lines): fill = new_backend() # a fill: get_meta proves handle + origin, then the read consumes tha
  - `tests/agent/test_vault_backends.py@-276 +286` UNMARKED (1 lines): assert new_backend().resolve_otp("op:item-a") == "123456" # the display listing is reused,
  - `tests/agent/test_vault_backends.py@-278,2 +288,4` UNMARKED (3 lines): assert calls == [["item", "list"], ["item", "get", "item-b", "--vault", "vault-b", "--form
  - `tests/agent/test_vault_backends.py@-278,2 +288,4#moved` moved: `tests/agent/test_vault_onepassword_subprocess.py`
- def test_onepassword_service_account_refusals_name_the_real_cause(fake_op_servic
  - `tests/agent/test_vault_backends.py@-284,0 +297,3` UNMARKED (1 lines): from agent.vault_backends.onepassword import invalidate_listing_cache
  - `tests/agent/test_vault_backends.py@-284,0 +297,3#moved` moved: `agent/vault_backends/onepassword.py`
  - `tests/agent/test_vault_backends.py@-286 +301` UNMARKED (1 lines): with pytest.raises(ValueError, match="Invalid 1Password item handle"):

### `tests/agent/test_worker_outbox_adoption.py`

- from hermes_state import SessionDB
  - `tests/agent/test_worker_outbox_adoption.py@-7,2 +7,2` UNMARKED (1 lines): from hermes_state_runtime import adopt_worker_execution, mutate_worker_execution, register
  - `tests/agent/test_worker_outbox_adoption.py@-7,2 +7,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/conftest.py`

- def _close_leaked_session_dbs():
  - `tests/conftest.py@-552,6 +667` UNMARKED (1 lines): registry = getattr(sys.modules.get("hermes_state_guard"), "_test_instance_registry", ())
  - `tests/conftest.py@-568,0 +679,20` UNMARKED (1 lines): close_shared = getattr(sys.modules.get("hermes_state_registry"), "close_all_under", None)

### `tests/cron/test_cron_recovery.py`

- from hermes_state import SessionDB
  - `tests/cron/test_cron_recovery.py@-12 +12,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/cron/test_scheduler.py`

- class TestParallelTick:
  - `tests/cron/test_scheduler.py@-1794,0 +1795,16` UNMARKED (10 lines): @pytest.mark.parametrize("configured, expected", [
  - `tests/cron/test_scheduler.py@-1794,0 +1795,16#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)
- class TestBuildJobPromptExtraPrompt:
  - `tests/cron/test_scheduler.py@-2618,0 +2635,50` UNMARKED (4 lines): db.set_session_title.side_effect = [ValueError("in use"), True]
  - `tests/cron/test_scheduler.py@-2618,0 +2635,50#moved` moved: `tests/agent/test_bot_chat_toolset_refresh.py`, `tests/agent/test_cross_process_turn_lease.py`, `tests/agent/test_system_prompt_restore.py`, `tests/agent/test_title_generator.py` (+20 more)
- def _run_owned_job(job, tmp_path, db=None):
  - `tests/cron/test_scheduler.py@-75,0 +62,14` UNMARKED (1 lines): run_job,

### `tests/cron/test_sessiondb_init_hang.py`

- (module)
  - `tests/cron/test_sessiondb_init_hang.py@-1 +1` UNMARKED (1 lines): """Regression test for a hung SessionDB() init permanently wedging a cron job.
- def test_owner_store_failure_never_runs_unpersisted_agent(owner_run, failure):
  - `tests/cron/test_sessiondb_init_hang.py@-109,0 +217,2` UNMARKED (1 lines): def test_guard_is_released_and_job_refires_after_sessiondb_hang(self, tmp_path, monkeypatc
  - `tests/cron/test_sessiondb_init_hang.py@-109,0 +217,2#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_cron_incidents.py`, `tests/cron/test_cron_incidents_resolve.py`, `tests/cron/test_cron_kanban_env_isolation.py` (+32 more)
  - `tests/cron/test_sessiondb_init_hang.py@-111,4 +220,4` UNMARKED (1 lines): monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
  - `tests/cron/test_sessiondb_init_hang.py@-111,4 +220,4#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_fire_claim_lost_after_delivery.py`, `tests/cron/test_inflight_stale_guard.py`, `tests/cron/test_parallel_pool.py` (+5 more)
- def test_guard_is_released_and_job_refires_after_sessiondb_hang(owner_run, monke
  - `tests/cron/test_sessiondb_init_hang.py@-116,4 +225,11` UNMARKED (4 lines): "id": "guard-sessiondb-hang",
  - `tests/cron/test_sessiondb_init_hang.py@-116,4 +225,11#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9666 more)
  - `tests/cron/test_sessiondb_init_hang.py@-121,5 +237,2` UNMARKED (1 lines): return _run_owned_job(fired, tmp_path, owner_db, **kwargs)
  - `tests/cron/test_sessiondb_init_hang.py@-132,8 +241,5` UNMARKED (1 lines): patch("hermes_state_registry.acquire", return_value=owner_db), \
  - `tests/cron/test_sessiondb_init_hang.py@-132,8 +241,5#moved` moved: `tests/cron/test_cleanup_timeout.py`, `tests/cron/test_cron_incidents.py`, `tests/cron/test_cron_mcp_toolset_empty_block.py`, `tests/cron/test_cron_pinned_job_fallback.py` (+7 more)
  - `tests/cron/test_sessiondb_init_hang.py@-142,3 +248,75` UNMARKED (30 lines): return_value=_RUNTIME,
  - `tests/cron/test_sessiondb_init_hang.py@-142,3 +248,75#moved` moved: `apps/desktop/e2e/core/lineage-compaction-prompt.spec.ts`, `cron/scheduler_session_store.py`, `gateway/outbox.py`, `hermes_cli/goals.py` (+234 more)
  - `tests/cron/test_sessiondb_init_hang.py@-145,0 +324` UNMARKED (1 lines): assert late_db_holder == []
  - `tests/cron/test_sessiondb_init_hang.py@-147,6 +326,12` UNMARKED (9 lines): lambda: returned.call_count == 3 and registry.stats()["total_refcounts"] == refs_before,
  - `tests/cron/test_sessiondb_init_hang.py@-147,6 +326,12#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10183 more)
- import pytest
  - `tests/cron/test_sessiondb_init_hang.py@-15,2 +30,12` UNMARKED (3 lines): # Hold the real class: patching cron.scheduler.concurrent.futures.ThreadPoolExecutor
  - `tests/cron/test_sessiondb_init_hang.py@-15,2 +30,12#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9676 more)
- def test_guard_is_released_and_job_refires_after_sessiondb_hang(owner_run, monke
  - `tests/cron/test_sessiondb_init_hang.py@-155 +340` UNMARKED (1 lines): registry.release(owner_db)
  - `tests/cron/test_sessiondb_init_hang.py@-158,9 +343,62` UNMARKED (26 lines): # Gated runs must release the store acquired before their acknowledgement
  - `tests/cron/test_sessiondb_init_hang.py@-158,9 +343,62#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9715 more)
- from gateway import session_cron
  - `tests/cron/test_sessiondb_init_hang.py@-19,19 +44,4` UNMARKED (2 lines): def _run_owned_job(job, home, db, *, execution_id=None, **kwargs):
- (module)
  - `tests/cron/test_sessiondb_init_hang.py@-3,2 +3,18` UNMARKED (16 lines): Real-world incident: a cron job"s ''SessionDB()'' construction inside
- def owner_run(tmp_path, monkeypatch):
  - `tests/cron/test_sessiondb_init_hang.py@-39,14 +49,3` UNMARKED (2 lines): db.db_path = home / "state.db"
  - `tests/cron/test_sessiondb_init_hang.py@-39,14 +49,3#moved` moved: `tests/cron/test_script_context_propagation.py`
- def test_sessiondb_init_preserves_multiplex_profile_context(owner_run, tmp_path)
  - `tests/cron/test_sessiondb_init_hang.py@-54,2 +53#moved` moved: `gateway/session_cron.py`
  - `tests/cron/test_sessiondb_init_hang.py@-63,6 +58,122` UNMARKED (45 lines): def _session_db_executor(timeouts: list, *, instant_timeout: bool = True):
  - `tests/cron/test_sessiondb_init_hang.py@-63,6 +58,122#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10473 more)
- def test_run_job_does_not_hang_when_sessiondb_init_wedges(owner_run, tmp_path, m
  - `tests/cron/test_sessiondb_init_hang.py@-70,3 +181,6` UNMARKED (3 lines): yaml.safe_dump({"cron": {"session_db_timeout_seconds": 0.2}})
  - `tests/cron/test_sessiondb_init_hang.py@-70,3 +181,6#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10046 more)
  - `tests/cron/test_sessiondb_init_hang.py@-74,4 +188,17` UNMARKED (4 lines): patch("hermes_state_registry.acquire") as acquire_store, \
  - `tests/cron/test_sessiondb_init_hang.py@-74,4 +188,17#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2081 more)
  - `tests/cron/test_sessiondb_init_hang.py@-79,16 +206,5` UNMARKED (2 lines): success, _output, _final_response, error = _run_owned_job(job, tmp_path, acquire_store.ret
  - `tests/cron/test_sessiondb_init_hang.py@-79,16 +206,5#moved` moved: `tests/cron/test_cron_empty_payload.py`, `tests/cron/test_cron_kanban_env_isolation.py`, `tests/cron/test_cron_mcp_toolset_empty_block.py`, `tests/cron/test_cron_pinned_job_fallback.py` (+12 more)
  - `tests/cron/test_sessiondb_init_hang.py@-97,12 +213,3` UNMARKED (3 lines): class TestDispatchGuardReleasedAfterHang:

### `tests/fakes/cron_owner.py`

- from hermes_state import SessionDB
  - `tests/fakes/cron_owner.py@-15 +15#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/conftest.py`

- import ast
  - `tests/gateway/conftest.py@-34,0 +35#moved` moved: `optional-skills/blockchain/hyperliquid/scripts/hyperliquid_client.py`, `optional-skills/research/osint-investigation/scripts/timing_analysis.py`, `plugins/platforms/discord/adapter.py`, `plugins/platforms/discord/recovery.py` (+6 more)
- import sys
  - `tests/gateway/conftest.py@-36,0 +38#moved` moved: `agent/incognito_agent.py`, `agent/memory_provider.py`, `agent/title_generator.py`, `gateway/incognito.py` (+20 more)
- import pytest
  - `tests/gateway/conftest.py@-42,0 +45,31` UNMARKED (15 lines): def fixed_daily_quota_wall(monkeypatch):
  - `tests/gateway/conftest.py@-42,0 +45,31#moved` moved: `acp_adapter/entry.py`, `agent/anthropic_message_convert.py`, `agent/legacy_cli.py`, `agent/managed_worker.py` (+41 more)
- def owner(tmp_path):
  - `tests/gateway/conftest.py@-698 +731#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def hosted_owner(tmp_path, monkeypatch):
  - `tests/gateway/conftest.py@-757 +790` UNMARKED (1 lines): authority = asyncio.run_coroutine_threadsafe(initialize_session_authority(runner, profile_
  - `tests/gateway/conftest.py@-759 +792` UNMARKED (1 lines): principal = SessionActor("durable-room-owner', authority.profile_id, frozenset({"session:c

### `tests/gateway/fixtures/control_boundaries.py`

- async def exercise_control_boundaries(tmp_path, monkeypatch):
  - `tests/gateway/fixtures/control_boundaries.py@-15,2 +15,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_inp
  - `tests/gateway/fixtures/control_boundaries.py@-15,2 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/fixtures/control_boundaries.py@-45 +45,5` UNMARKED (2 lines): ctx = TurnContext(source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat'),
  - `tests/gateway/fixtures/control_boundaries.py@-45 +45,5#moved` moved: `cron/scheduler_delivery.py`, `cron/scheduler_delivery_sessions.py`, `evals/delivery_flood_wire.py`, `evals/gateway_status_render/iteration_ceiling_ab.py` (+737 more)

### `tests/gateway/fixtures/replay_gap_owner.py`

- class Owner:
  - `tests/gateway/fixtures/replay_gap_owner.py@-29 +28#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/fixtures/replay_gap_owner.py@-30,0 +30` UNMARKED (1 lines): self.blocked, self.reattached = threading.Event(), threading.Event()
  - `tests/gateway/fixtures/replay_gap_owner.py@-43,0 +44,2` UNMARKED (2 lines): if not self.running.is_set():
  - `tests/gateway/fixtures/replay_gap_owner.py@-63 +65,4` UNMARKED (3 lines): result = await getattr(self.conn, verb)(self.ref, {} if verb == "resume" else params) if v
  - `tests/gateway/fixtures/replay_gap_owner.py@-63 +65,4#moved` moved: `agent/acp_client.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py`, `agent/auxiliary_client.py` (+793 more)
  - `tests/gateway/fixtures/replay_gap_owner.py@-69,0 +74,5` UNMARKED (3 lines): # A drain can capture a whole batch before blocking. Park its writer first so the
  - `tests/gateway/fixtures/replay_gap_owner.py@-73,3 +82` UNMARKED (1 lines): assert self.reattached.wait(5), "viewer did not re-attach after the gap"

### `tests/gateway/test_admission_results.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_admission_results.py@-5,4 +5,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_inp
  - `tests/gateway/test_admission_results.py@-5,4 +5,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_adversarial_responses_pending.py`

- async def test_unknown_retry_recovers_admission_after_pointer_commit_loss(api, o
  - `tests/gateway/test_adversarial_responses_pending.py@-58 +58,2` UNMARKED (1 lines): from hermes_state_runtime import recover_session_inputs
  - `tests/gateway/test_adversarial_responses_pending.py@-58 +58,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_adversarial_settlement_review.py`

- from gateway.session_contract import Submission
  - `tests/gateway/test_adversarial_settlement_review.py@-9 +9,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, get_session_admission

### `tests/gateway/test_api_admission_off_loop.py`

- async def test_admission_waits_for_the_writer_off_the_owner_loop(api, owner, mon
  - `tests/gateway/test_api_admission_off_loop.py@-42,2 +70` UNMARKED (1 lines): holder = sqlite3.connect(owner.db.db_path, timeout=0, isolation_level=None)

### `tests/gateway/test_api_authority_store.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_api_authority_store.py@-12 +12#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_api_media_profile_scope.py`

- async def test_restart_recovery_keeps_every_profiles_held_images_and_collects_ea
  - `tests/gateway/test_api_media_profile_scope.py@-38 +38,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_
  - `tests/gateway/test_api_media_profile_scope.py@-38 +38,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_approval_projection.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_approval_projection.py@-16 +16#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_busy_preempt_parity.py`

- async def test_busy_voice_note_interrupts_at_once_in_interrupt_mode(tmp_path, mo
  - `tests/gateway/test_authority_busy_preempt_parity.py@-107,0 +109,356` UNMARKED (7 lines): assert [(row["request_id'], row["status']) for row in rows] == [("h1', "queued')]
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
  - `tests/gateway/test_authority_pause_recovery.py@-11,2 +11,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admis
  - `tests/gateway/test_authority_pause_recovery.py@-11,2 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_authority_principal.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_authority_principal.py@-9 +9#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_mailbox_async.py`

- async def test_adopted_result_during_unknown_projection_is_not_lost(bot, monkeyp
  - `tests/gateway/test_bot_mailbox_async.py@-117,2 +117,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, recover_session_inputs, register_wor
  - `tests/gateway/test_bot_mailbox_async.py@-117,2 +117,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_corruption.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_bot_receipt_corruption.py@-11,5 +11,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
  - `tests/gateway/test_bot_receipt_corruption.py@-11,5 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_recovery_logs.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_bot_receipt_recovery_logs.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_bot_receipt_tombstone_expiry.py`

- re-read at every owner start would otherwise abort the gateway on every boot."""
  - `tests/gateway/test_bot_receipt_tombstone_expiry.py@-3,0 +4#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _receipt_then_deleted(tmp_path, *, status, days_ago, **fields):
  - `tests/gateway/test_bot_receipt_tombstone_expiry.py@-33 +34#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`

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
  - `tests/gateway/test_claim_reopen_off_loop.py@-38 +39` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))

### `tests/gateway/test_control_socket.py`

- def test_stop_removes_socket_and_pointer(home: Path):
  - `tests/gateway/test_control_socket.py@-201 +212,2` UNMARKED (2 lines): # The private fallback may differ from the predictable candidate.
- def _run(coro):
  - `tests/gateway/test_control_socket.py@-28,0 +30,8` UNMARKED (3 lines): def _socket_tmpdir(short_sock_dir, monkeypatch):
  - `tests/gateway/test_control_socket.py@-28,0 +30,8#moved` moved: `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `tests/_fixtures/live_system_guard.py`, `tests/_fixtures/reply_latency.py`, `tests/acp_adapter/conftest.py` (+923 more)
- def test_home_refusing_sockets_listens_on_private_fallback_both_clients_reach(sh
  - `tests/gateway/test_control_socket.py@-305 +317,2` UNMARKED (2 lines): assert server._bind_path == target
  - `tests/gateway/test_control_socket.py@-314 +327` UNMARKED (1 lines): assert not Path(attempts[-1]).is_relative_to(home) # the in-home attempt, if made, was ref
- def test_collect_fleet_versions_falls_back_to_state_file(tmp_path: Path, monkeyp
  - `tests/gateway/test_control_socket.py@-446,0 +460#moved` moved: `tests/hermes_cli/test_fleet_matrix_down_state.py`
- def _serve(home: Path, handlers=None):
  - `tests/gateway/test_control_socket.py@-45,21 +54,7` UNMARKED (2 lines): def test_short_home_binds_in_home(short_sock_dir):
- def test_runtime_inventory_prefers_socket_supervisor(tmp_path: Path, monkeypatch
  - `tests/gateway/test_control_socket.py@-556,0 +571,34` UNMARKED (16 lines): def test_both_argument_protocols_survive_real_socket_roundtrip(home):
  - `tests/gateway/test_control_socket.py@-556,0 +571,34#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+5385 more)
- def test_long_home_uses_pointer_fallback(tmp_path: Path):
  - `tests/gateway/test_control_socket.py@-76,0 +72,16` UNMARKED (4 lines): def test_long_home_fallback_server_starts_and_answers(tmp_path: Path, monkeypatch):
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
  - `tests/gateway/test_delivery_ledger.py@-126,0 +136,3` UNMARKED (1 lines): } <= columns
  - `tests/gateway/test_delivery_ledger.py@-126,0 +136,3#moved` moved: `tests/agent/test_compression_concurrent_fork.py`, `tests/agent/test_incognito.py`, `tests/gateway/test_base_topic_sessions.py`, `tests/gateway/test_run_progress_topics.py` (+13 more)
- class TestSweep:
  - `tests/gateway/test_delivery_ledger.py@-147,0 +160,11` UNMARKED (8 lines): def test_claim_preserves_resume_owner(self):
- import time
  - `tests/gateway/test_delivery_ledger.py@-15,0 +16#moved` moved: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_unavailable.py`, `agent/billing_usage.py` (+220 more)
- class TestSweep:
  - `tests/gateway/test_delivery_ledger.py@-151,0 +175,18` UNMARKED (5 lines): def test_zero_attempt_attempting_row_is_ambiguous_until_owner_dies(self):
- class TestRuntimeFailedSweep:
  - `tests/gateway/test_delivery_ledger.py@-296,0 +338,42` UNMARKED (26 lines): def test_runtime_sweep_never_claims_past_an_outstanding_earlier_row(self, monkeypatch):
  - `tests/gateway/test_delivery_ledger.py@-296,0 +338,42#moved` moved: `tests/agent/test_subagent_lifecycle.py`, `tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py`, `tests/gateway/test_reaction_outbound_identity.py`, `tests/gateway/test_telegram_send_path_recovery_sweep.py` (+2 more)
- class TestGatewayRedeliverySweep:
  - `tests/gateway/test_delivery_ledger.py@-413,0 +497,128` UNMARKED (84 lines): async def test_one_bots_refusal_never_holds_another_bots_rows(self):
  - `tests/gateway/test_delivery_ledger.py@-413,0 +497,128#moved` moved: `acp_adapter/commands.py`, `agent/acp_client.py`, `agent/acp_openai_bridge.py`, `agent/agent_init.py` (+2701 more)
- def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
  - `tests/gateway/test_delivery_ledger.py@-44,0 +46,8` UNMARKED (5 lines): def test_recovery_markers_keep_only_the_duplicate_hint():
- class TestGatewayRedeliverySweep:
  - `tests/gateway/test_delivery_ledger.py@-557 +768` UNMARKED (1 lines): dl.RECOVERED_MARKER
- class TestOwnerAlivePidProbe:
  - `tests/gateway/test_delivery_ledger.py@-810,0 +1022,315` UNMARKED (154 lines): class TestFloodDeadlineExactness:
  - `tests/gateway/test_delivery_ledger.py@-810,0 +1022,315#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+10095 more)
- def _orphan(oid):
  - `tests/gateway/test_delivery_ledger.py@-92 +101` UNMARKED (1 lines): "UPDATE delivery_obligations SET owner_pid=2147483647, "

### `tests/gateway/test_delivery_ledger_fd_leak.py`

- class _TrackingConnection:
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-19 +19` UNMARKED (1 lines): """Close instrumentation mixed into the requested real connection factory.
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-21,3 +21,2` UNMARKED (2 lines): Keep sqlite_safe_read"s tracked factory/close semantics intact; a delegating
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-31,15 +26,2` UNMARKED (1 lines): self._closed_ids.append(id(self))
- def _track_connections(monkeypatch):
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-57,0 +40,2` UNMARKED (2 lines): factory = kwargs.pop("factory", sqlite3.Connection)
  - `tests/gateway/test_delivery_ledger_fd_leak.py@-58,0 +43` UNMARKED (1 lines): conn._closed_ids = closed

### `tests/gateway/test_headless_completion_io.py`

- (module)
  - `tests/gateway/test_headless_completion_io.py@-0,0 +1,207` UNMARKED (2 lines): for module, name in [(automation, "_owner'), (recovery, "_read_local_session'),

### `tests/gateway/test_heartbeat_inflight_owner_scan.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_heartbeat_inflight_owner_scan.py@-12 +12,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_synthetic_provenance_retry.py`

### `tests/gateway/test_hosted_document_custody.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_document_custody.py@-7 +7,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, cancel_session_input

### `tests/gateway/test_hosted_room_attempt_home_moves.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_room_attempt_home_moves.py@-20,3 +20,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, list_session_admissions, settle_sess
  - `tests/gateway/test_hosted_room_attempt_home_moves.py@-20,3 +20,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_room_unserved_member_stop.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_hosted_room_unserved_member_stop.py@-15 +15#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_rooms.py`

- def test_room_log_pages_are_bounded_by_serialized_event_bytes(tmp_path, monkeypa
  - `tests/gateway/test_hosted_rooms.py@-738,5 +736,10` UNMARKED (1 lines): rooms.read_events(db, room_id="room-1", since_seq=index, limit=1)

### `tests/gateway/test_hosted_rooms_gateway_import.py`

- def test_gateway_startup_takes_over_the_standalone_rooms(tmp_path, monkeypatch):
  - `tests/gateway/test_hosted_rooms_gateway_import.py@-109 +109#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hosted_stop_executor_recovery.py`

- def test_recovered_stop_does_not_discard_a_different_canonical_attempt(mux, monk
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-168 +168,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, list_session_admissions, recover_ses
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-168 +168,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cross_profile_stopping_recovery(mux, admission_state):
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-59,3 +59,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, list_session_admissions, recover_ses
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-59,3 +59,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_recovered_group_loads_and_answers_the_next_message_without_replaying_ol
  - `tests/gateway/test_hosted_stop_executor_recovery.py@-97 +96,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py`

- def test_live_agent_compaction_still_rebuilds():
  - `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py@-61,0 +64,29` UNMARKED (19 lines): @pytest.mark.parametrize("seed", [None, ""])
  - `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py@-61,0 +64,29#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+10286 more)

### `tests/gateway/test_kanban_finite_admission.py`

- def _lose_across_restart(authority, sid):
  - `tests/gateway/test_kanban_finite_admission.py@-215,2 +215,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, list_session_admissions, recover_ses
  - `tests/gateway/test_kanban_finite_admission.py@-215,2 +215,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_legacy_desktop_source.py`

- async def test_editing_ones_own_branch_leaves_another_logins_parent_alone(tmp_pa
  - `tests/gateway/test_legacy_desktop_source.py@-130 +130#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_renaming_ones_own_branch_leaves_another_logins_parent_title_alone
  - `tests/gateway/test_legacy_desktop_source.py@-175 +175#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_receipt_untracked_compression.py`

- def _unrecovered_owner(tmp_path, instance_id):
  - `tests/gateway/test_local_receipt_untracked_compression.py@-66 +66#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_session_recovery.py`

- def test_messaging_session_history_follows_the_compression_tip(tmp_path, monkeyp
  - `tests/gateway/test_local_session_recovery.py@-83 +83#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_local_viewer_events_submit.py`

- def test_a_classic_cli_receipt_carries_the_run_conversation_keys(classic):
  - `tests/gateway/test_local_viewer_events_submit.py@-207 +219,2` UNMARKED (2 lines): assert delivered["final_response"] == ""
- async def test_cli_turn_fields_are_admitted_and_bound_to_their_own_turn(tmp_path
  - `tests/gateway/test_local_viewer_events_submit.py@-22,0 +23#moved` moved: `gateway/reply_latency.py`, `gateway/run_background_admission.py`, `gateway/run_busy.py`, `gateway/run_goals.py` (+5 more)
  - `tests/gateway/test_local_viewer_events_submit.py@-27 +28,2` UNMARKED (2 lines): seen.append((event.text, cli_turn(event.message_id), cli_turn("another-event'),
  - `tests/gateway/test_local_viewer_events_submit.py@-35 +37` UNMARKED (1 lines): ("full', {"turn_author': author, "turn_origin': "goal_loop', "api_text': "[voice] hi', "cl
  - `tests/gateway/test_local_viewer_events_submit.py@-55 +57` UNMARKED (1 lines): "turn_author': author, "turn_origin': "goal_loop', "api_text': "[voice] hi', "clarify_batc
  - `tests/gateway/test_local_viewer_events_submit.py@-59 +61` UNMARKED (1 lines): assert seen[0] == ("full', rows["full']["payload']["cli_turn_v1'], {}, author, "goal_loop'
  - `tests/gateway/test_local_viewer_events_submit.py@-63,0 +66,10` UNMARKED (5 lines): @pytest.mark.parametrize(("origin", "expected"), [
  - `tests/gateway/test_local_viewer_events_submit.py@-63,0 +66,10#moved` moved: `agent/checkpoints/manager.py`, `agent/conversation_loop.py`, `agent/credential_sources.py`, `agent/curator.py` (+1960 more)

### `tests/gateway/test_lost_worker_receipt_compaction.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_lost_worker_receipt_compaction.py@-13,4 +13,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, mutate_worker_e
  - `tests/gateway/test_lost_worker_receipt_compaction.py@-13,4 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_managed_worker_drain.py`

- async def test_reserved_worker_persists_while_runtime_drains(tmp_path):
  - `tests/gateway/test_managed_worker_drain.py@-16 +16,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, RuntimeStoreErr

### `tests/gateway/test_managed_worker_fencing.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_fencing.py@-13 +13,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/gateway/test_managed_worker_finished_exit.py`

- def test_finished_worker_keeps_its_result_on_slow_or_nonzero_exit(tmp_path, monk
  - `tests/gateway/test_managed_worker_finished_exit.py@-44 +44,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_hard_stop_frame_fence.py`

- def test_worker_frames_read_no_ledger_per_frame(tmp_path, monkeypatch):
  - `tests/gateway/test_managed_worker_hard_stop_frame_fence.py@-91 +95,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_launch.py`

- from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
  - `tests/gateway/test_managed_worker_launch.py@-16 +16,17` UNMARKED (10 lines): @pytest.mark.parametrize("error_name', ["NoSuchProcess', "ZombieProcess'])
  - `tests/gateway/test_managed_worker_launch.py@-16 +16,17#moved` moved: `.github/workflows/prompt-surface-diff.yml`, `tests/gateway/test_managed_worker_stop_before_hello.py`, `tests/tui_gateway/test_survivor_notifications.py`
- def test_ordinary_owner_launches_tool_worker_and_detach_does_not_cancel(tmp_path
  - `tests/gateway/test_managed_worker_launch.py@-251,2 +267` UNMARKED (1 lines): await async_wait_until(lambda: not process_alive(background.pid),

### `tests/gateway/test_managed_worker_reservation_off_loop.py`

- def test_reservation_and_loss_writes_never_block_the_loop(tmp_path, monkeypatch,
  - `tests/gateway/test_managed_worker_reservation_off_loop.py@-47 +47,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_stop_before_hello.py`

- def test_stop_acknowledged_during_spawn_never_bootstraps_the_worker(tmp_path, mo
  - `tests/gateway/test_managed_worker_stop_before_hello.py@-82 +81,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_managed_worker_tool_events.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_tool_events.py@-11 +11,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/gateway/test_managed_worker_trampoline.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_managed_worker_trampoline.py@-23 +23,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/gateway/test_managed_worker_unknown_status.py`

- def _authority_with_a_lost_worker(tmp_path, monkeypatch):
  - `tests/gateway/test_managed_worker_unknown_status.py@-31 +31,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)

### `tests/gateway/test_model_confirmation_clients.py`

- class _Owner:
  - `tests/gateway/test_model_confirmation_clients.py@-41 +42` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(self.db, instance_id="owner'))
- ask, re-send once with the token on yes, and leave the session untouched on no.
  - `tests/gateway/test_model_confirmation_clients.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_mutation_prepare_off_loop.py`

- (module)
  - `tests/gateway/test_mutation_prepare_off_loop.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_model_prepare_waiting_on_a_contended_writer_keeps_the_loop_live(t
  - `tests/gateway/test_mutation_prepare_off_loop.py@-37 +38` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))

### `tests/gateway/test_native_fifo_context_exhaustion.py`

- (module)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-1,4 +1,4` UNMARKED (3 lines): """Compression exhaustion retains native FIFO ownership and dedupe, without replaying the
- async def test_exhaustion_reset_is_ordered_with_redelivery_and_cleanup(native, m
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-107,68 +111,7` UNMARKED (6 lines): original = admitted[0][1].ref
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-107,68 +111,7#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
- async def test_no_scheduler_starts_the_fresh_fifo_before_reset_cleanup_finishes(
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-182,5 +123,4` UNMARKED (2 lines): await _exhaust(runner, entry, session_key, source)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-188 +128,33` UNMARKED (13 lines): _completed()
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-188 +128,33#moved` moved: `agent/monitoring/emitter.py`, `evals/heartbeat_idle_wire.py`, `evals/postmortem/review_probes/deadline_probe.py`, `hermes_cli/win_pty_bridge.py` (+278 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-193,17 +165,11` UNMARKED (6 lines): authority._schedule(original)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-213,4 +179` UNMARKED (1 lines): async def test_admission_reserved_before_exhaustion_stays_on_the_original_fifo(native, mon
- from plugins.platforms.discord.adapter import DiscordAdapter
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-22 +23` UNMARKED (1 lines): _REAL_SCHEDULE = SessionAuthority._schedule
- async def test_admission_reserved_on_the_old_route_commits_on_the_fresh_session(
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-222,2 +184,3` UNMARKED (2 lines): original = admitted[0][1].ref
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-222,2 +184,3#moved` moved: `hermes_cli/status.py`, `tests/acp_adapter/test_gateway_catalog_offline.py`, `tests/agent/test_model_metadata.py`, `tests/agent/test_preflight_compression_pool_saturation.py` (+8 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-227 +190,2` UNMARKED (2 lines): held.append(answer)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-233,7 +197#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-246,5 +203` UNMARKED (1 lines): await _exhaust(runner, entry, session_key, source)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-252 +205` UNMARKED (1 lines): _completed()
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-256,2 +209,6` UNMARKED (3 lines): await asyncio.wait_for(reserved.wait(), 10)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-261,4 +217,5` UNMARKED (5 lines): assert retry.ref.session_id == late_d.ref.session_id == original.session_id
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-268,3 +225` UNMARKED (1 lines): async def test_exhaustion_keeps_the_adapter_queue_on_its_original_session(native):
- async def test_exhaustion_reset_keeps_the_adapter_queue_for_the_fresh_session(na
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-273,2 +228` UNMARKED (1 lines): session_key = native.authority.sessions[receipt.ref.session_id].route
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-280 +234` UNMARKED (1 lines): _, retained = await runner._hmwa_compression_exhaustion_reset(
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-282 +236` UNMARKED (1 lines): assert retained is entry
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-284,0 +239` UNMARKED (1 lines): assert not native.authority.deferred_resets
- async def native(monkeypatch):
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-44,0 +46,2#moved` moved: `tests/gateway/test_attended_session_opening.py`, `tests/gateway/test_compression_exhaustion_keeps_context.py`, `tests/gateway/test_internal_event_pin_wiring.py`, `tests/gateway/test_loop_thread_statedb_sweep.py` (+1 more)
- async def _admit(native, text, event=None):
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-54,2 +57` UNMARKED (1 lines): async def _exhaust(runner, entry, session_key, source):
- async def test_exhaustion_reset_runs_queued_native_messages_on_the_fresh_session
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-56,0 +59,20` UNMARKED (8 lines): slot = execution_result.get()
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-56,0 +59,20#moved` moved: `tests/agent/test_codex_stream_supersession.py`, `tests/agent/test_moa_aggregator_cost_slot.py`, `tests/gateway/test_compression_exhaustion_keeps_context.py`
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-59,2 +81,2` UNMARKED (1 lines): original = admitted[0][1].ref
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-59,2 +81,2#moved` moved: `tests/acp_adapter/test_gateway_retry_command.py`, `tests/acp_adapter/test_gateway_submit_retry.py`, `tests/acp_adapter/test_gateway_unacked_slash.py`, `tests/acp_adapter/test_retained_identity_retirement.py` (+48 more)
- from types import SimpleNamespace
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-6,0 +7#moved` moved: `agent/client_lifecycle.py`, `agent/turn_api_call.py`, `tests/agent/lsp/test_client_e2e.py`, `tests/agent/lsp/test_mock_pid_publication.py` (+159 more)
- async def test_exhaustion_reset_runs_queued_native_messages_on_the_fresh_session
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-67,6 +88,2` UNMARKED (2 lines): await _exhaust(runner, entry, session_key, source)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-74 +91` UNMARKED (1 lines): _completed()
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-78,14 +95,7` UNMARKED (7 lines): await authority._drain(original)
  - `tests/gateway/test_native_fifo_context_exhaustion.py@-99,5 +108` UNMARKED (1 lines): async def test_exhaustion_keeps_owner_and_scoped_state_during_redelivery(native, monkeypat

### `tests/gateway/test_native_media_budget.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_budget.py@-9 +9,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_headless_completion_io.py`, `tests/gateway/test_process_notice_committed_source.py`, `tests/gateway/test_synthetic_native_admission.py` (+2 more)

### `tests/gateway/test_native_media_history_retention.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_history_retention.py@-11 +12,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_

### `tests/gateway/test_native_media_ownership.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_media_ownership.py@-9 +9,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, cancel_session_input

### `tests/gateway/test_native_operator_provenance.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_operator_provenance.py@-14,8 +14,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_inp
  - `tests/gateway/test_native_operator_provenance.py@-14,8 +14,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_native_recovery_viewer_queue.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_native_recovery_viewer_queue.py@-14,7 +14,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, list_session_ad
  - `tests/gateway/test_native_recovery_viewer_queue.py@-14,7 +14,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_offloop_write_retirement_join.py`

- def _owner(tmp_path, monkeypatch):
  - `tests/gateway/test_offloop_write_retirement_join.py@-36 +37` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- takes the writer (another process holding state.db) until after the join or the
  - `tests/gateway/test_offloop_write_retirement_join.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_owner_loop_claim_read.py`

- async def test_claim_pending_read_keeps_the_owner_loop_ticking(tmp_path, monkeyp
  - `tests/gateway/test_owner_loop_claim_read.py@-25,0 +26#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/test_owner_loop_claim_read.py@-29 +30` UNMARKED (1 lines): db=db, epoch=begin_runtime_epoch(db, instance_id="t'))

### `tests/gateway/test_owner_loop_writes_class.py`

- async def _first_read_adoption(authority, owner, tmp_path, db):
  - `tests/gateway/test_owner_loop_writes_class.py@-153 +154` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(store._db, instance_id="cold'))
- def _owner(tmp_path, monkeypatch, handle):
  - `tests/gateway/test_owner_loop_writes_class.py@-33 +34` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- stall itself is shown by test_mutation_prepare_off_loop / test_claim_reopen_off_
  - `tests/gateway/test_owner_loop_writes_class.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_owner_loop_writes_cron_create.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_owner_loop_writes_cron_create.py@-15 +15,2#moved` moved: `gateway/run_checkins.py`, `gateway/session_pause_announce.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/agent/test_incognito_main_sinks.py` (+72 more)

### `tests/gateway/test_plugin_gateway_session_parity.py`

- async def test_keyed_local_injection_stays_inside_the_plugins_own_profile(tmp_pa
  - `tests/gateway/test_plugin_gateway_session_parity.py@-76 +77#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_profile_retire_stops_turns.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_profile_retire_stops_turns.py@-17 +17,2#moved` moved: `gateway/run_checkins.py`, `gateway/session_pause_announce.py`, `tests/acp_adapter/test_gateway_retry_command.py`, `tests/agent/test_incognito_main_sinks.py` (+72 more)

### `tests/gateway/test_prompt_attachments.py`

- (module)
  - `tests/gateway/test_prompt_attachments.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_discarding_a_lost_image_turn_releases_its_committed_bytes(tmp_pat
  - `tests/gateway/test_prompt_attachments.py@-156 +161` UNMARKED (1 lines): authority.epoch = _begin_owned_runtime_epoch(authority.db, instance_id="restarted')
  - `tests/gateway/test_prompt_attachments.py@-159,0 +165,2` UNMARKED (1 lines): await media_releases_idle(authority) # Discard queues the cleanup rather than awaiting its
  - `tests/gateway/test_prompt_attachments.py@-159,0 +165,2#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`
- async def test_staged_attachment_reaches_message_event_media(tmp_path, monkeypat
  - `tests/gateway/test_prompt_attachments.py@-70 +72,4` UNMARKED (2 lines): # Retained bytes are exact-retry evidence by digest only; settlement releases them, queued
  - `tests/gateway/test_prompt_attachments.py@-70 +72,4#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_prompt_queued_controls.py`

- from gateway.session_authority import LiveSession, SessionAuthority
  - `tests/gateway/test_prompt_queued_controls.py@-11,4 +11,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, find_session_admission, list_session
  - `tests/gateway/test_prompt_queued_controls.py@-11,4 +11,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_prompt_queued_transfer.py`

- from gateway.session_contract import SessionRef
  - `tests/gateway/test_prompt_queued_transfer.py@-15,4 +15,2` UNMARKED (1 lines): from hermes_state_runtime import claim_session_input, find_session_admission, get_session_
  - `tests/gateway/test_prompt_queued_transfer.py@-15,4 +15,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_reset_tip_unknown_barrier.py`

- def _unknown_after_reset(tmp_path, runner):
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-24 +25` UNMARKED (1 lines): epoch = _begin_owned_runtime_epoch(db, instance_id="crashed')
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-29 +30#moved` moved: `tests/hermes_state/test_worker_lineage_exclusion.py`
- free and silently re-run a turn whose canonical admission is ``unknown``."""
  - `tests/gateway/test_reset_tip_unknown_barrier.py@-4,0 +5#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_restart_interrupt_notice.py`

- async def test_user_stop_is_not_relabelled_when_shutdown_starts_during_settlemen
  - `tests/gateway/test_restart_interrupt_notice.py@-100 +100,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
  - `tests/gateway/test_restart_interrupt_notice.py@-100 +100,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_managed_worker_turn_settles_with_the_cause_of_the_stop_that_reached_it(
  - `tests/gateway/test_restart_interrupt_notice.py@-184 +185,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+55 more)
- def _settle_on_real_store(tmp_path, cause, principal='person', payload=None):
  - `tests/gateway/test_restart_interrupt_notice.py@-339 +342,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
  - `tests/gateway/test_restart_interrupt_notice.py@-339 +342,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def _core_closed_turn(tmp_path, monkeypatch, *, principal='person', earlier_fail
  - `tests/gateway/test_restart_interrupt_notice.py@-415 +419,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
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
  - `tests/gateway/test_resume_latest_local_cli.py@-35 +36` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- the gateway had created.
  - `tests/gateway/test_resume_latest_local_cli.py@-8,0 +9#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_retained_response_chain_after_delete.py`

- async def test_a_deleted_chain_continues_on_a_new_generation(api, owner, path, c
  - `tests/gateway/test_retained_response_chain_after_delete.py@-26 +26#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

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
  - `tests/gateway/test_runner_startup_failures.py@-12 +14` UNMARKED (1 lines): def _host_attach_gate_is_not_under_test(monkeypatch, tmp_path):
- async def test_start_gateway_verbosity_imports_redacting_formatter(monkeypatch,
  - `tests/gateway/test_runner_startup_failures.py@-128,2 +133,2` UNMARKED (2 lines): await asyncio.to_thread(self.session_store.close_all_db_handles)
  - `tests/gateway/test_runner_startup_failures.py@-145,0 +151,46` UNMARKED (16 lines): @pytest.mark.parametrize(("replace", "expected_replace", "starts_runner"), [
  - `tests/gateway/test_runner_startup_failures.py@-145,0 +151,46#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+4363 more)
- def _host_attach_gate_is_not_under_test(monkeypatch):
  - `tests/gateway/test_runner_startup_failures.py@-18,0 +21,3` UNMARKED (2 lines): # A runner process pins its launch home at module import. These tests simulate separate
  - `tests/gateway/test_runner_startup_failures.py@-18,0 +21,3#moved` moved: `tests/gateway/test_auth_fallback.py`, `tests/gateway/test_busy_steer_origin.py`, `tests/gateway/test_busy_steer_sender_label.py`, `tests/gateway/test_busy_steer_subagent_fanout.py` (+10 more)
- async def test_start_gateway_replace_writes_takeover_marker_before_sigterm(
  - `tests/gateway/test_runner_startup_failures.py@-275,2 +326,2` UNMARKED (2 lines): await asyncio.to_thread(self.session_store.close_all_db_handles)
- async def test_start_gateway_propagates_fatal_config_exit_code(monkeypatch, tmp_
  - `tests/gateway/test_runner_startup_failures.py@-493,0 +545,3` UNMARKED (1 lines): session_db = await asyncio.to_thread(SessionDB, tmp_path / "state.db")
  - `tests/gateway/test_runner_startup_failures.py@-493,0 +545,3#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/session_incognito_boundary.py`, `gateway/telegram_topic_sessions.py`, `hermes_cli/config_migrations_alias_routing.py` (+185 more)
  - `tests/gateway/test_runner_startup_failures.py@-500 +553#moved` moved: `cron/scheduler_bookkeeping.py`, `hermes_cli/gateway_agent.py`, `tests/agent/test_background_review_usage.py`, `tests/hermes_cli/test_handoff_slow_dispatch_timeout.py`
  - `tests/gateway/test_runner_startup_failures.py@-513,2 +566,2` UNMARKED (2 lines): await asyncio.to_thread(self.session_store.close_all_db_handles)
- async def test_token_lock_plus_retryable_peer_stays_alive(monkeypatch, tmp_path)
  - `tests/gateway/test_runner_startup_failures.py@-640,0 +694,203` UNMARKED (94 lines): class _MissingCredentialAdapter(BasePlatformAdapter):
  - `tests/gateway/test_runner_startup_failures.py@-640,0 +694,203#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3740 more)

### `tests/gateway/test_session_authority_cancel_settlement.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_authority_cancel_settlement.py@-10 +10,2#moved` moved: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_headless_completion_io.py`, `tests/gateway/test_process_notice_committed_source.py`, `tests/gateway/test_synthetic_native_admission.py` (+2 more)

### `tests/gateway/test_session_bot_state.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_bot_state.py@-13 +13,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/gateway/test_session_create_tier_cwd.py`

- def _owner(home, monkeypatch, config):
  - `tests/gateway/test_session_create_tier_cwd.py@-31 +32` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- NAMED profile's own ``terminal.cwd``). The owner freezes both into the LocalSess
  - `tests/gateway/test_session_create_tier_cwd.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_cron.py`

- async def test_owner_execution_binds_the_scheduler_cron_identity_inside_run_job(
  - `tests/gateway/test_session_cron.py@-104 +277,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
  - `tests/gateway/test_session_cron.py@-104 +277,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
  - `tests/gateway/test_session_cron.py@-15 +54,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+80 more)
- def test_draining_owner_still_answers_status_and_cancel_of_admitted_cron_work(tm
  - `tests/gateway/test_session_cron.py@-150,2 +324,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, claim_session_input, settle_session_in
  - `tests/gateway/test_session_cron.py@-150,2 +324,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
  - `tests/gateway/test_session_cron.py@-46,0 +87,132` UNMARKED (57 lines): async def test_cron_delivery_retries_when_another_restore_publishes_live_session(tmp_path,
  - `tests/gateway/test_session_cron.py@-46,0 +87,132#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+11745 more)
- async def test_cron_cancel_between_claim_and_execution_registration_stops_the_jo
  - `tests/gateway/test_session_cron.py@-57 +229,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input
  - `tests/gateway/test_session_cron.py@-57 +229,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- import pytest
  - `tests/gateway/test_session_cron.py@-7,0 +10,37` UNMARKED (20 lines): async def test_cron_status_restores_before_authorize(monkeypatch):
  - `tests/gateway/test_session_cron.py@-7,0 +10,37#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+14878 more)

### `tests/gateway/test_session_db_recovery.py`

- def test_recovered_db_rows_survive_fallback_structural_save(monkeypatch, tmp_pat
  - `tests/gateway/test_session_db_recovery.py@-333,0 +334,3` UNMARKED (3 lines): # SessionDB opens are lazy; route the store"s production routing-home lookup to this

### `tests/gateway/test_session_events_overflow.py`

- async def test_overflowed_subscriber_is_detached_and_told_to_resume(tmp_path):
  - `tests/gateway/test_session_events_overflow.py@-33 +33#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_follow_profile_config.py`

- def _owner(home):
  - `tests/gateway/test_session_follow_profile_config.py@-21,0 +21#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway/session_time_persistence_controls.py`, `evals/gateway_completion/delivery_lifecycle.py`, `evals/gateway_failure_ownership/probe.py` (+664 more)
  - `tests/gateway/test_session_follow_profile_config.py@-28,3 +28,9` UNMARKED (3 lines): # Keep real session-key derivation and scoped runtime publication without starting a gatew
  - `tests/gateway/test_session_follow_profile_config.py@-28,3 +28,9#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway/session_time_persistence_controls.py`, `evals/heartbeat_idle_wire.py`, `evals/token_accounting/display_provenance.py` (+403 more)
  - `tests/gateway/test_session_follow_profile_config.py@-33 +39` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
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
  - `tests/gateway/test_session_handoff_owner.py@-122 +123` UNMARKED (1 lines): authority.epoch = begin_runtime_epoch(authority.db, instance_id="restarted')

### `tests/gateway/test_session_hosted_controls.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_hosted_controls.py@-8 +8,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+27 more)

### `tests/gateway/test_session_hosted_rpc.py`

- def test_unknown_info_is_inactive_with_hosted_not_canonical_generation(hosted_ow
  - `tests/gateway/test_session_hosted_rpc.py@-213 +214,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_discard_requires_exact_owned_unknown_tuple_without_replay(hosted_owner,
  - `tests/gateway/test_session_hosted_rpc.py@-233 +235,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, list_session_admissions, claim_session
  - `tests/gateway/test_session_hosted_rpc.py@-233 +235,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def test_local_hosted_member_revoked_during_preparation_is_not_admitted(hosted_o
  - `tests/gateway/test_session_hosted_rpc.py@-26,2 +26,2` UNMARKED (1 lines): # The authority identity is synthetic, while its profile home is the temporary DB parent.
  - `tests/gateway/test_session_hosted_rpc.py@-26,2 +26,2#moved` moved: `tests/gateway/test_hosted_room_member_binding.py`, `tests/gateway/test_hosted_room_retired_admission.py`
- def test_room_binding_exact_retry_terminal_history_and_unknown(hosted_owner):
  - `tests/gateway/test_session_hosted_rpc.py@-96 +96,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, list_session_admissions, claim_session
  - `tests/gateway/test_session_hosted_rpc.py@-96 +96,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_hosted_service.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_session_hosted_service.py@-7 +7,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+80 more)

### `tests/gateway/test_session_hosted_transport.py`

- class _SourceTask:
  - `tests/gateway/test_session_hosted_transport.py@-104 +104#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_hygiene.py`

- async def test_hygiene_fence_cancel_records_cooldown_without_abort_flag(
  - `tests/gateway/test_session_hygiene.py@-1041 +1047` UNMARKED (1 lines): async def test_hygiene_lock_in_flight_defers_turn_without_sibling_compressor(
- async def test_hygiene_skips_when_compression_already_in_flight(
  - `tests/gateway/test_session_hygiene.py@-1044 +1050,2` UNMARKED (2 lines): """A held compression lock neither spawns a sibling hygiene compressor nor
  - `tests/gateway/test_session_hygiene.py@-1071,2 +1078,7` UNMARKED (2 lines): # The lock starts after admission, and the transport drains the retained input.
  - `tests/gateway/test_session_hygiene.py@-1071,2 +1078,7#moved` moved: `tests/gateway/test_busy_relay_silence.py`, `tests/gateway/test_busy_wake_admission.py`, `tests/gateway/test_compression_deferred_retry.py`, `tests/gateway/test_compression_retry_lifecycle.py` (+10 more)
  - `tests/gateway/test_session_hygiene.py@-1074 +1086,4` UNMARKED (1 lines): assert runner._session_has_compression_in_flight.await_count == 3
  - `tests/gateway/test_session_hygiene.py@-1074 +1086,4#moved` moved: `tests/gateway/test_attended_busy_activity.py`, `tests/gateway/test_checkin_retry_disarm.py`, `tests/gateway/test_clarify_context_when_interim_off.py`, `tests/gateway/test_compression_deferred_retry.py` (+23 more)
- async def test_session_hygiene_preserves_transcript_when_no_rotation(monkeypatch
  - `tests/gateway/test_session_hygiene.py@-122,0 +123#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
  - `tests/gateway/test_session_hygiene.py@-156 +157` UNMARKED (1 lines): monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1000000001")
- async def test_session_hygiene_preserves_transcript_when_in_place_configured_but
  - `tests/gateway/test_session_hygiene.py@-248,0 +250#moved` moved: `tests/gateway/test_compress_command.py`, `tests/gateway/test_compress_focus.py`, `tests/gateway/test_compress_plugin_engine.py`, `tests/gateway/test_silence_reply_composed.py` (+1 more)
  - `tests/gateway/test_session_hygiene.py@-282 +284` UNMARKED (1 lines): monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1000000001")
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
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-152,0 +154,17` UNMARKED (7 lines): async def _drain_retained_tasks(runner, attribute, timeout):
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-152,0 +154,17#moved` moved: `agent/auxiliary_client.py`, `agent/chat_completion_helpers.py`, `agent/command_token_source.py`, `agent/gemini_native_adapter.py` (+330 more)
- async def _drain_deferred(runner, timeout=10.0):
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-154,5 +172,7` UNMARKED (5 lines): await _drain_retained_tasks(runner, "_deferred_agent_cleanup_tasks", timeout)
- async def test_turn_hold_keeps_admission_and_adopts_watermark_fenced_summary(
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-172,0 +193` UNMARKED (1 lines): worker_gave_up = threading.Event()
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-207 +228,2` UNMARKED (2 lines): if time.monotonic() - _spin_started > 120:
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-234,3 +256,3` UNMARKED (3 lines): # Generous outer bound only, kept under the 60s hygiene timeout so that path cannot pass f
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-238 +260,3` UNMARKED (3 lines): # #90845/#92318 invariant intact: the turn is released at the budget, i.e. it completed
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-241,0 +265,3` UNMARKED (2 lines): assert not release_worker.is_set() and not committed.is_set() and not worker_gave_up.is_se
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-267,0 +294` UNMARKED (1 lines): await _drain_background(runner)
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-273 +300` UNMARKED (1 lines): # The summary was adopted: the turn-hold is internal lifecycle, so the user sees nothing.
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-275,3 +302,4` UNMARKED (4 lines): assert sent == [], f"adopted turn-hold must not notify the user, got: {sent}"
- async def test_turn_hold_kept_admission_arms_flat_retry_only_when_nothing_commit
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-358,0 +387,71` UNMARKED (17 lines): # Nothing was adopted, but nothing failed either: a superseded/no-op attempt stays silent.
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-358,0 +387,71#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `agent/background_review.py`, `agent/checkpoints/undo_hint.py` (+205 more)
- async def test_turn_hold_without_watermark_fence_still_cancels(
  - `tests/gateway/test_session_hygiene_turnhold_adoption.py@-431,0 +531,62` UNMARKED (37 lines): # Cancelled: nothing will be adopted, so the deferral notice is the user"s only signal.
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
  - `tests/gateway/test_session_mutation_branch_boundary.py@-31 +32` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- transcript inside the receipt transaction and part of the retry identity.
  - `tests/gateway/test_session_mutation_branch_boundary.py@-7,0 +8#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_mutation_compress_preview.py`

- async def test_canonical_compress_runs_the_live_agents_pre_compress_memory_hook(
  - `tests/gateway/test_session_mutation_compress_preview.py@-108 +119` UNMARKED (1 lines): authority = SessionAuthority(runner, profile_id=str(db.db_path.parent), instance_id="owner
  - `tests/gateway/test_session_mutation_compress_preview.py@-121,0 +133` UNMARKED (1 lines): assert hook_homes == [db.db_path.parent], "post-compaction discovery escaped the isolated
- async def test_input_is_admitted_while_compress_summarizes_but_not_claimed_until
  - `tests/gateway/test_session_mutation_compress_preview.py@-142 +154#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-154 +166` UNMARKED (1 lines): authority = SessionAuthority(runner, profile_id=str(db.db_path.parent), instance_id="owner
- async def test_a_compress_that_removes_nothing_reports_mains_no_changes_without_
  - `tests/gateway/test_session_mutation_compress_preview.py@-198 +210#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-204 +216` UNMARKED (1 lines): authority = SessionAuthority(runner, profile_id=str(db.db_path.parent), instance_id="owner
- async def test_no_op_compress_without_a_resident_agent_estimates_the_stored_prom
  - `tests/gateway/test_session_mutation_compress_preview.py@-241 +253#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-247 +259` UNMARKED (1 lines): authority = SessionAuthority(runner, profile_id=str(db.db_path.parent), instance_id="owner
- async def test_preview_mutation_reports_without_summarizing_or_writing(tmp_path,
  - `tests/gateway/test_session_mutation_compress_preview.py@-38 +39#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_session_mutation_compress_preview.py@-45 +46` UNMARKED (1 lines): authority = SessionAuthority(runner, profile_id=str(db.db_path.parent), instance_id="owner
- surface uses must govern the payload, and a preview must not write a receipt, a
  - `tests/gateway/test_session_mutation_compress_preview.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- async def test_canonical_compress_runs_the_live_agents_pre_compress_memory_hook(
  - `tests/gateway/test_session_mutation_compress_preview.py@-90,2 +91,12` UNMARKED (6 lines): hooked, summarized, hook_homes = [], [], []
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
  - `tests/gateway/test_session_mutation_model.py@-281 +285` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- async def test_guarded_model_target_commits_only_with_its_confirmation_token(tmp
  - `tests/gateway/test_session_mutation_model.py@-655 +659` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- async def test_large_context_guard_reads_the_persisted_anchor_with_no_resident_a
  - `tests/gateway/test_session_mutation_model.py@-721 +725` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))

### `tests/gateway/test_session_mutation_model_busy.py`

- (module)
  - `tests/gateway/test_session_mutation_model_busy.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _owner(tmp_path, monkeypatch, handle):
  - `tests/gateway/test_session_mutation_model_busy.py@-31 +32` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))

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
  - `tests/gateway/test_session_policy_verbs.py@-32 +36` UNMARKED (1 lines): epoch=_begin_owned_runtime_epoch(db, instance_id="owner'))
- LocalSessionPolicy, so each is a revision-fenced ``session.mutate`` that rewrite
  - `tests/gateway/test_session_policy_verbs.py@-5,0 +6#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/gateway/test_session_shell.py`

- async def test_shell_exec_refusal_names_a_sandboxed_terminal_backend(tmp_path, m
  - `tests/gateway/test_session_shell.py@-50 +50#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_session_store_lock_p1.py`

- (module)
  - `tests/gateway/test_session_store_lock_p1.py@-0,0 +1,383` UNMARKED (1 lines): monkeypatch.setattr(recovery, "_read_local_session", read_local)

### `tests/gateway/test_settlement_lock_liveness.py`

- async def test_submit_claim_and_discard_behind_a_held_writer_leave_the_loop_free
  - `tests/gateway/test_settlement_lock_liveness.py@-160 +160,2` UNMARKED (1 lines): from hermes_state_runtime import get_session_admission, recover_session_inputs
  - `tests/gateway/test_settlement_lock_liveness.py@-160 +160,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
  - `tests/gateway/test_settlement_lock_liveness.py@-180,0 +182,3` UNMARKED (1 lines): # Discard queues the terminal media cleanup as its own tracked task, never awaited inline.
  - `tests/gateway/test_settlement_lock_liveness.py@-180,0 +182,3#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_slack_replay_display_name.py`

- def _rows(authority):
  - `tests/gateway/test_slack_replay_display_name.py@-60,2 +62` UNMARKED (1 lines): return list_session_admissions(authority.db, session_id=session_id, pending_only=False)

### `tests/gateway/test_stop_claim_race_class.py`

- def _cron(hosted_owner, tmp_path, monkeypatch):
  - `tests/gateway/test_stop_claim_race_class.py@-43 +43,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)

### `tests/gateway/test_telegram_send_path_recovery_sweep.py`

- async def test_stopping_a_profile_mid_recovery_leaves_no_reply_stranded(tmp_path
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-147,0 +148` UNMARKED (1 lines): release_operation = asyncio.Event()
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-161 +162,2` UNMARKED (1 lines): await release_operation.wait()
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-161 +162,2#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/edit_approval.py`, `acp_adapter/elicitation.py` (+2019 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-166 +168` UNMARKED (1 lines): await release_operation.wait() # cancellation must settle this same in-flight send
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-172 +174#moved` moved: `evals/delivery_flood_wire.py`, `evals/gateway_completion/delivery_lifecycle.py`, `evals/heartbeat_idle_wire.py`, `tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py` (+36 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-174 +176,2` UNMARKED (1 lines): _store=None, clear_resume_pending=_clear, get_resume_pending_marker=AsyncMock(return_value
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-174 +176,2#moved` moved: `tests/gateway/test_attended_busy_activity.py`, `tests/gateway/test_auto_skill_title_input.py`, `tests/gateway/test_handoff_secondary_profile_adapter.py`, `tests/gateway/test_model_reset_all.py` (+5 more)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-177,0 +181,2` UNMARKED (1 lines): runner._profile_adapters = {"work": {Platform.TELEGRAM: adapter}}
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-177,0 +181,2#moved` moved: `tests/gateway/test_autonomous_wake_pacing.py`
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-182,0 +188` UNMARKED (1 lines): release_operation.set()
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-184 +190` UNMARKED (1 lines): assert len(sent) == (1 if stop_during == "send" else 0)
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-187 +193,4` UNMARKED (3 lines): assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM, profile=
  - `tests/gateway/test_telegram_send_path_recovery_sweep.py@-187 +193,4#moved` moved: `tests/agent/test_gemini_long_decode.py`

### `tests/gateway/test_telegram_topic_profile_isolation.py`

- def test_legacy_rows_migrate_only_to_default(tmp_path: Path):
  - `tests/gateway/test_telegram_topic_profile_isolation.py@-62 +62#moved` moved: `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/core/test_telegram_icon_migration.py`

### `tests/gateway/test_turn_result_retention.py`

- from gateway.session_results import admission_result, finish_result
  - `tests/gateway/test_turn_result_retention.py@-29,2 +29,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_inp
  - `tests/gateway/test_turn_result_retention.py@-29,2 +29,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_unknown_receipt_boundaries.py`

- from gateway.session_results import admission_result, finish_result
  - `tests/gateway/test_unknown_receipt_boundaries.py@-12,8 +12,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_inp
  - `tests/gateway/test_unknown_receipt_boundaries.py@-12,8 +12,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_discard_closure_failure_keeps_unknown_fence_and_retry_is_exact(ow
  - `tests/gateway/test_unknown_receipt_boundaries.py@-61,0 +56,2` UNMARKED (1 lines): await media_releases_idle(owner) # Discard queues the cleanup rather than awaiting its sca
  - `tests/gateway/test_unknown_receipt_boundaries.py@-61,0 +56,2#moved` moved: `tests/gateway/test_adversarial_settlement_review.py`, `tests/gateway/test_fifo_media_settlement_no_stall.py`, `tests/gateway/test_native_media_budget.py`

### `tests/gateway/test_unknown_resolution_physical_worker.py`

- (module)
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-1,0 +2#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def test_resolve_unknown_retires_the_physical_worker_row(tmp_path):
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-50 +51#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
  - `tests/gateway/test_unknown_resolution_physical_worker.py@-68 +69#moved` moved: `tests/hermes_state/test_worker_lineage_exclusion.py`

### `tests/gateway/test_unmanaged_idle_exit.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_unmanaged_idle_exit.py@-19 +19,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, cancel_session_input
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

### `tests/gateway/test_worker_admission_reservation.py`

- async def test_input_queued_behind_a_live_worker_is_released_then_runs_when_it_f
  - `tests/gateway/test_worker_admission_reservation.py@-112 +114,2` UNMARKED (2 lines): source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat', user_id="human')
  - `tests/gateway/test_worker_admission_reservation.py@-129 +132` UNMARKED (1 lines): event = MessageEvent(text="hi', source=source)
- from hermes_state import SessionDB
  - `tests/gateway/test_worker_admission_reservation.py@-13,2 +13,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, register_worker
  - `tests/gateway/test_worker_admission_reservation.py@-13,2 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- async def test_input_queued_behind_a_live_worker_is_released_then_runs_when_it_f
  - `tests/gateway/test_worker_admission_reservation.py@-97,0 +98,2#moved` moved: `cron/scheduler_delivery_sessions.py`, `evals/delivery_flood_wire.py`, `evals/gateway_failure_ownership/probe.py`, `evals/heartbeat_idle_wire.py` (+606 more)

### `tests/gateway/test_worker_delegation_bridge.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_delegation_bridge.py@-18 +18,2#moved` moved: `tests/hermes_state/test_runtime_worker_operations.py`

### `tests/gateway/test_worker_delegation_receipts.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_delegation_receipts.py@-13,2 +13,2` UNMARKED (1 lines): from hermes_state_runtime import RuntimeStoreError, register_worker_execution, adopt_worke
  - `tests/gateway/test_worker_delegation_receipts.py@-13,2 +13,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/gateway/test_worker_register_restriction.py`

- async def test_worker_register_requires_the_owners_launched_worker(tmp_path, mon
  - `tests/gateway/test_worker_register_restriction.py@-22 +22,2#moved` moved: `agent/runtime_session_privacy.py`, `cron/detached_authority.py`, `gateway/admission_canary.py`, `gateway/run_goal_wait.py` (+27 more)

### `tests/gateway/test_worker_reservation_automation_principal.py`

- from hermes_state import SessionDB
  - `tests/gateway/test_worker_reservation_automation_principal.py@-11 +11,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/hermes_cli/serve_handler_ledger.json`

- (module)
  - `tests/hermes_cli/serve_handler_ledger.json@-10 +10` UNMARKED (1 lines): "undecided": 183
  - `tests/hermes_cli/serve_handler_ledger.json@-401,0 +402,4` UNMARKED (4 lines): {"current_path": "legacy fallback; standalone: legacy server.dispatch", "decision": "undec

### `tests/hermes_cli/test_backup_stability.py`

- def test_failed_automatic_backup_preserves_previous_archive(tmp_path, monkeypatc
  - `tests/hermes_cli/test_backup_stability.py@-141,0 +142,89` UNMARKED (48 lines): #: Cap below the database so it is skipped for SIZE — a standing property of the file,
  - `tests/hermes_cli/test_backup_stability.py@-141,0 +142,89#moved` moved: `agent/acp_client.py`, `agent/curator.py`, `agent/redact.py`, `apps/desktop/e2e/update/seed.py` (+476 more)

### `tests/hermes_cli/test_cli_title_follows_compression.py`

- (module)
  - `tests/hermes_cli/test_cli_title_follows_compression.py@-0,0 +1,44` UNMARKED (12 lines): def _rotated(tmp_path):
  - `tests/hermes_cli/test_cli_title_follows_compression.py@-0,0 +1,44#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+6923 more)

### `tests/hermes_cli/test_config_retired_key_migrations.py`

- _NOT_TRIGGERED_HERE = {
  - `tests/hermes_cli/test_config_retired_key_migrations.py@-147,0 +148,2` UNMARKED (1 lines): 54: "model alias migration only runs when presets or retired delegation routes exist",

### `tests/hermes_cli/test_foreign_sessions.py`

- def gateway(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_foreign_sessions.py@-125 +125#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/hermes_cli/test_gateway_session_store.py`

- def test_rename_refusal_prints_mains_title_in_use_text(store, tmp_path, monkeypa
  - `tests/hermes_cli/test_gateway_session_store.py@-289,0 +290,28` UNMARKED (6 lines): writer = SessionDB(db_path=tmp_path / "state.db")
  - `tests/hermes_cli/test_gateway_session_store.py@-289,0 +290,28#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/control_socket.py`, `gateway/session_incognito_boundary.py`, `gateway/shutdown_watchdog.py` (+215 more)

### `tests/hermes_cli/test_goal_evidence_regressions.py`

- (module)
  - `tests/hermes_cli/test_goal_evidence_regressions.py@-0,0 +1,184` UNMARKED (12 lines): rows = db.find_tool_results_for_call(sid, "python -c "print(42)"', since=mgr.state.created
  - `tests/hermes_cli/test_goal_evidence_regressions.py@-0,0 +1,184#moved` moved: `tests/hermes_cli/test_goal_adaptive.py`, `tests/hermes_cli/test_goal_capability_evidence.py`, `tests/hermes_cli/test_goal_judge_evidence.py`, `tests/hermes_cli/test_goal_round3_contract.py`

### `tests/hermes_cli/test_runtime_maintenance_exclusion.py`

- def test_recovery_output_exemption_excludes_canonical_activation(tmp_path):
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-179 +180#moved` moved: `evals/hosted_attachment_transfer/bench.py`, `gateway/run_bootstrap.py`, `gateway/run_runtime.py`, `gateway/status.py` (+54 more)
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-182 +183,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+54 more)
  - `tests/hermes_cli/test_runtime_maintenance_exclusion.py@-188 +190#moved` moved: `tests/gateway/test_runtime_epoch_watch.py`, `tests/persistence_sqlite/core/test_runtime_epoch_foreign_owner.py`

### `tests/hermes_cli/test_session_controls.py`

- (module)
  - `tests/hermes_cli/test_session_controls.py@-0,0 +1,1534` UNMARKED (55 lines): db.append_message(sid, "user", text, display_kind=display_kind)
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
  - `tests/hermes_cli/test_sessions_delete.py@-129,0 +131,2` UNMARKED (1 lines): # These cases test filter translation, not the wall-clock deletion window.
  - `tests/hermes_cli/test_sessions_delete.py@-129,0 +131,2#moved` moved: `tests/cron/conftest.py`, `tests/gateway/test_housekeeping_profile_scope.py`, `tests/hermes_cli/test_sessions_failure_exit_codes.py`
- def test_sessions_delete_with_unfinished_turn_points_to_discard(tmp_path, monkey
  - `tests/hermes_cli/test_sessions_delete.py@-54 +55` UNMARKED (1 lines): epoch = _begin_owned_runtime_epoch(db, instance_id="a')
  - `tests/hermes_cli/test_sessions_delete.py@-58 +59` UNMARKED (1 lines): rt.recover_session_inputs(db, epoch=_begin_owned_runtime_epoch(db, instance_id="b'))

### `tests/hermes_cli/test_sessions_held_store_gate.py`

- def state_db(monkeypatch, tmp_path):
  - `tests/hermes_cli/test_sessions_held_store_gate.py@-37,0 +38,2` UNMARKED (1 lines): # The holder gate, not the wall-clock deletion window (tests/hermes_cli/test_sessions_prun
  - `tests/hermes_cli/test_sessions_held_store_gate.py@-37,0 +38,2#moved` moved: `tests/cron/conftest.py`, `tests/gateway/test_housekeeping_profile_scope.py`, `tests/hermes_cli/test_sessions_failure_exit_codes.py`

### `tests/hermes_cli/test_sessions_prune_via_owner.py`

- def test_prune_under_the_live_owner_is_applied_by_the_owner_not_refused(owned_st
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-105 +108` UNMARKED (1 lines): include_archived=False, include_pinned=False, now=True, max_seconds=2.0,
- async def test_owner_prune_is_local_operator_only(owned_store, grant):
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-137,0 +141,29` UNMARKED (14 lines): async def test_disconnected_prune_client_cannot_leave_unbounded_owner_work(owned_store, mo
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-137,0 +141,29#moved` moved: `acp_adapter/provenance.py`, `acp_adapter/server.py`, `acp_adapter/tools.py`, `agent/account_usage.py` (+1428 more)
- def owned_store(monkeypatch, tmp_path):
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-52 +54#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+12 more)
- as the gateway does; the owner end is the production ``AuthorityConnection`` dis
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-8,0 +9,2#moved` moved: `acp_adapter/edit_approval.py`, `acp_adapter/entry.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2226 more)
- def test_prune_under_the_live_owner_is_applied_by_the_owner_not_refused(owned_st
  - `tests/hermes_cli/test_sessions_prune_via_owner.py@-90,0 +93` UNMARKED (1 lines): assert params["max_seconds"] == 2.0

### `tests/hermes_cli/test_sessions_repair_prompts.py`

- def test_unpinned_row_is_unverifiable_and_apply_json_clears_only_verified(db, mo
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-63,0 +64,9` UNMARKED (8 lines): malformed = db.create_session("malformed", "telegram", system_prompt=DEGRADED)
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-63,0 +64,9#moved` moved: `tests/hermes_cli/test_gateway_migrate_backends.py`, `tests/tools/test_computer_use_nodes.py`
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-69 +78` UNMARKED (1 lines): assert {row["id"] for row in payload["unverifiable"]} == {"old-unpinned", "malformed"}
  - `tests/hermes_cli/test_sessions_repair_prompts.py@-73,0 +83` UNMARKED (1 lines): assert db.get_session(malformed) == before[malformed]

### `tests/hermes_cli/test_update_channel_release_hold.py`

- def test_an_explicit_branch_to_a_pre_runtime_commit_is_still_refused(source, mon
  - `tests/hermes_cli/test_update_channel_release_hold.py@-136 +137` UNMARKED (1 lines): update_cmd._cmd_update_impl(source.parser.parse_args(["update", "--branch", "release", "--
- downgrade on every attempt. An explicit ``--branch`` to a pre-runtime commit is
  - `tests/hermes_cli/test_update_channel_release_hold.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def _ledgered(home):
  - `tests/hermes_cli/test_update_channel_release_hold.py@-82 +83#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`

### `tests/hermes_cli/test_update_check_exit_reaps_git.py`

- _CHILD = textwrap.dedent('''
  - `tests/hermes_cli/test_update_check_exit_reaps_git.py@-49,2 +61,2` UNMARKED (2 lines): # Exit as /exit does: normally once git and its helper are both running; in the
  - `tests/hermes_cli/test_update_check_exit_reaps_git.py@-52 +64,3` UNMARKED (1 lines): if (len(target.read_text().split()) if target.exists() else 0) >= (1 if len(sys.argv) > 4

### `tests/hermes_cli/test_update_downgrade_guard.py`

- def _ledgered_store(home):
  - `tests/hermes_cli/test_update_downgrade_guard.py@-45 +46#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`
- names the pre-update snapshot that predates the cutover.
  - `tests/hermes_cli/test_update_downgrade_guard.py@-6,0 +7#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/hermes_cli/test_update_finish.py`

- def completion(tmp_path, monkeypatch):
  - `tests/hermes_cli/test_update_finish.py@-47 +47` UNMARKED (1 lines): for name in ("hermes_cli", "hermes_platform", "hermes_agent", "pm", "agent", "gateway", "t

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
  - `tests/hermes_state/test_append_messages_batch.py@-136,5 +139,2` UNMARKED (1 lines): return real_insert(self_db, conn, session_id, messages)

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
  - `tests/hermes_state/test_display_fold_one_step.py@-0,0 +1,102` UNMARKED (1 lines): values = [None, "", "é \x00\x1f\"\\ 😀 text", "x" * 5000, b"\x00\xff" * 20, b"", 0, -1, 2**
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
  - `tests/hermes_state/test_fts_runtime_rebuild.py@-481 +476,2` UNMARKED (1 lines): db.search_messages("大别山项目")

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
  - `tests/hermes_state/test_runtime_worker_children.py@-134 +136` UNMARKED (1 lines): authority = SimpleNamespace(db=db, epoch=_begin_owned_runtime_epoch(db, instance_id="owner
- caps them, scopes their writes to their own row, and retires them with their par
  - `tests/hermes_state/test_runtime_worker_children.py@-2,0 +3#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)
- def ledger(tmp_path):
  - `tests/hermes_state/test_runtime_worker_children.py@-35 +36#moved` moved: `tests/hermes_state/test_runtime_admission.py`, `tests/hermes_state/test_runtime_adoption.py`, `tests/hermes_state/test_runtime_mutations.py`, `tests/hermes_state/test_runtime_recovery.py` (+2 more)
- def test_owner_restart_retires_in_flight_children_instead_of_recovering_them(led
  - `tests/hermes_state/test_runtime_worker_children.py@-80 +81` UNMARKED (1 lines): replacement = _begin_owned_runtime_epoch(db, instance_id="restart')
  - `tests/hermes_state/test_runtime_worker_children.py@-87,2 +88,3` UNMARKED (3 lines): for submitted_epoch in (epoch, replacement):

### `tests/hermes_state/test_runtime_worker_compaction.py` → `tests/persistence_sqlite/workers/test_runtime_worker_compaction.py`

- def test_micro_compaction_live_dicts_persist_through_the_worker_like_direct(work
  - `tests/hermes_state/test_runtime_worker_compaction.py@-95 +95` UNMARKED (1 lines): types.SimpleNamespace(_session_db=sdb, _session_id=sid), compacted) is None

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
  - `tests/hermes_state/test_session_db_read_conn_pool.py@-224 +227,3` UNMARKED (1 lines): db.search_messages("graphiti", limit=5)

### `tests/hermes_state/test_session_delete_batches.py`

- (module)
  - `tests/hermes_state/test_session_delete_batches.py@-0,0 +1,378#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+12482 more)

### `tests/hermes_state/test_session_signature_triggers.py`

- (module)
  - `tests/hermes_state/test_session_signature_triggers.py@-0,0 +1,132#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+15633 more)

### `tests/hermes_state/test_session_workspace_binding.py` → `tests/persistence_sqlite/conversation/test_session_workspace_binding.py`

- (module)
  - `tests/hermes_state/test_session_workspace_binding.py@-1 +1` UNMARKED (1 lines): """Session <-> workspace grouping key (hermes_state_sessions_workspace.workspace_key).

### `tests/hermes_state/test_sidecar_permissions.py`

- (module)
  - `tests/hermes_state/test_sidecar_permissions.py@-0,0 +1,80#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py` (+11929 more)

### `tests/hermes_state/test_stale_held_generation_guard.py`

- (module)
  - `tests/hermes_state/test_stale_held_generation_guard.py@-0,0 +1,155#moved` moved: `.github/known-flakes.json`, `.github/workflows/prompt-surface-diff.yml`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md` (+17339 more)

### `tests/hermes_state/test_state_db_malformed_repair.py` → `tests/persistence_sqlite/core/test_state_db_malformed_repair.py`

- from hermes_state_repair import repair_state_db_schema
  - `tests/hermes_state/test_state_db_malformed_repair.py@-31,0 +32,3#moved` moved: `tests/persistence_sqlite/core/test_state_db_fts_segment_collision_probe.py`
  - `tests/hermes_state/test_state_db_malformed_repair.py@-505 +508,3` UNMARKED (2 lines): from tests._fixtures.repair_capacity import ample_repair_capacity
- def _release_header_probe_fds() -> None:
  - `tests/hermes_state/test_state_db_malformed_repair.py@-519,0 +525,35` UNMARKED (15 lines): def test_repair_budget_probe_holds_cross_process_lock(tmp_path, monkeypatch):
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
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-479 +482,5` UNMARKED (5 lines): for _ in range(hermes_state_repair._MAX_PERSISTENT_REPAIR_ATTEMPTS - 1):
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-481,5 +488,8` UNMARKED (5 lines): def lock_after_final_failure(path):
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-481,5 +488,8#moved` moved: `cron/jobs.py`, `hermes_cli/kanban_db_connect.py`, `hermes_state_common.py`, `persistence/sqlite/_state/common.py` (+7 more)
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-488 +498` UNMARKED (1 lines): hermes_state_repair, "_cross_process_repair_lock", lock_after_final_failure
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-501 +510` UNMARKED (1 lines): report = repair_state_db_schema(db, backup=True)
  - `tests/hermes_state/test_state_db_repair_non_destructive.py@-506,0 +515,2` UNMARKED (1 lines): assert db.read_bytes() == original_bytes
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
  - `tests/hermes_state/test_wal_checkpoint_strategy.py@-225,0 +276,35` UNMARKED (3 lines): class TestFtsMergeOffTheWriterLock:
  - `tests/hermes_state/test_wal_checkpoint_strategy.py@-225,0 +276,35#moved` moved: `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/core/test_fts_automerge.py`

### `tests/hermes_state/test_worker_admission_index.py`

- (module)
  - `tests/hermes_state/test_worker_admission_index.py@-0,0 +1,117#moved` moved: `.github/known-flakes.json`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py` (+16040 more)

### `tests/hermes_state/test_worker_message_archive.py`

- (module)
  - `tests/hermes_state/test_worker_message_archive.py@-0,0 +1,105#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/catalog.py` (+15763 more)

### `tests/hermes_state/test_write_lock_patience.py` → `tests/persistence_sqlite/core/test_write_lock_patience.py`

- from hermes_state import SessionDB
  - `tests/hermes_state/test_write_lock_patience.py@-30,2 +30,3` UNMARKED (3 lines): def _hold_write_lock(db_path, hold_s, started_evt, release_evt=None):
- def _hold_write_lock(db_path, hold_s, started_evt):
  - `tests/hermes_state/test_write_lock_patience.py@-36 +37,4` UNMARKED (3 lines): if release_evt is None:
  - `tests/hermes_state/test_write_lock_patience.py@-36 +37,4#moved` moved: `CONTRIBUTING.md`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/entry.py` (+3466 more)
- class TestTranscriptWritePatience:
  - `tests/hermes_state/test_write_lock_patience.py@-89,0 +94,3` UNMARKED (2 lines): # Held until the write attempt returns: a fixed hold could expire before a
  - `tests/hermes_state/test_write_lock_patience.py@-89,0 +94,3#moved` moved: `tests/agent/test_account_usage_fetch.py`, `tests/agent/test_authorization_gate.py`, `tests/agent/test_auxiliary_explicit_cancellation.py`, `tests/agent/test_bedrock_no_sdk_retry.py` (+125 more)
  - `tests/hermes_state/test_write_lock_patience.py@-91 +98` UNMARKED (1 lines): target=_hold_write_lock, args=(db.db_path, 60.0, started, release)
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
  - `tests/pm/test_lease_reselection.py@-62,3 +53,3` UNMARKED (2 lines): assert not any((generation / ".leases").exists() and any((generation / ".leases").iterdir(

### `tests/scripts/test_release_artifacts.py`

- def test_candidate_smoke_survives_real_promotion_and_renderer(tmp_path, r2_serve
  - `tests/scripts/test_release_artifacts.py@-438 +445` UNMARKED (1 lines): def test_candidate_smoke_admission_fails_before_publication(tmp_path, r2_server, staged_ca

### `tests/scripts/test_termux_build_driver.py`

- def test_child_failure_stops_sequence_even_with_stale_product(build_fixture, sta
  - `tests/scripts/test_termux_build_driver.py@-161 +160` UNMARKED (1 lines): def test_release_identity_runs_the_shared_sequence(build_fixture, identity):

### `tests/test_session_spillover_cleanup.py`

- def test_canonical_delete_retires_the_spill_for_collection(_home):
  - `tests/test_session_spillover_cleanup.py@-169 +170#moved` moved: `tests/hermes_state/test_retired_fence_outlives_horizon.py`, `tests/persistence_sqlite/core/test_session_delete_batches.py`
- Two halves under test:
  - `tests/test_session_spillover_cleanup.py@-9,0 +10#moved` moved: `tests/hermes_state/test_delete_branched_root_leftovers.py`, `tests/hermes_state/test_import_deleted_session.py`, `tests/hermes_state/test_ledger_delete_consistency.py`, `tests/hermes_state/test_reset_lineage_delete_queued_work.py` (+20 more)

### `tests/test_tests_tree_layout.py`

- TESTS_ROOT = REPO_ROOT / "tests"
  - `tests/test_tests_tree_layout.py@-30,0 +31,2` UNMARKED (2 lines): # "drills": live cross-process Telegram handover harnesses, which intentionally
- _NON_MIRROR_DIRS = {
  - `tests/test_tests_tree_layout.py@-32 +34` UNMARKED (1 lines): "compat", "installation", "_fixtures", "drills",

### `tests/tools/test_async_delegation_fd_leak.py`

- def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
  - `tests/tools/test_async_delegation_fd_leak.py@-125 +109,5` UNMARKED (3 lines): factory = kwargs.pop("factory", sqlite3.Connection)
  - `tests/tools/test_async_delegation_fd_leak.py@-125 +109,5#moved` moved: `tests/hermes_cli/test_backup_preflight.py`, `tests/hermes_cli/test_doctor_wal_checkpoint_guard.py`, `tests/persistence_sqlite/admin/test_backup_parity.py`, `tests/tools/test_async_delegation.py`
- def test_post_open_hardening_failure_closes_connection(monkeypatch, tmp_path):
  - `tests/tools/test_async_delegation_fd_leak.py@-154 +141,7` UNMARKED (5 lines): # Track through the requested factory, as _track_connections does: a delegating proxy is
  - `tests/tools/test_async_delegation_fd_leak.py@-154 +141,7#moved` moved: `tests/hermes_cli/test_backup_preflight.py`, `tests/hermes_cli/test_doctor_wal_checkpoint_guard.py`, `tests/persistence_sqlite/admin/test_backup_parity.py`, `tests/tools/test_async_delegation.py`
- class _TrackingConnection:
  - `tests/tools/test_async_delegation_fd_leak.py@-19 +19` UNMARKED (1 lines): """Close instrumentation mixed into the requested real connection factory.
  - `tests/tools/test_async_delegation_fd_leak.py@-21,3 +21,2` UNMARKED (2 lines): Keep sqlite_safe_read"s tracked factory/close semantics intact; a delegating
  - `tests/tools/test_async_delegation_fd_leak.py@-31,15 +26,2` UNMARKED (1 lines): self._closed_ids.append(id(self))
  - `tests/tools/test_async_delegation_fd_leak.py@-31,15 +26,2#moved` moved: `agent/runtime_session_child.py`, `hermes_logging.py`, `tests/cron/test_cron_persistence_handle.py`, `tests/gateway/test_delivery_ledger_fd_leak.py` (+3 more)
- def _track_connections(monkeypatch):
  - `tests/tools/test_async_delegation_fd_leak.py@-57,0 +40,2` UNMARKED (2 lines): factory = kwargs.pop("factory", sqlite3.Connection)
  - `tests/tools/test_async_delegation_fd_leak.py@-58,0 +43` UNMARKED (1 lines): conn._closed_ids = closed

### `tests/tools/test_bot_mode_dm_outcomes.py`

- from hermes_state import SessionDB
  - `tests/tools/test_bot_mode_dm_outcomes.py@-9 +9,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_

### `tests/tools/test_browser_vault.py`

- class TestClassifier:
  - `tests/tools/test_browser_vault.py@-173 +191` UNMARKED (1 lines): res = classify_login_control(_ctrl(autocomplete=token, type="password" if token == "curren
- class TestBrowserVaultTools:
  - `tests/tools/test_browser_vault.py@-266,2 +268,2` UNMARKED (2 lines): patch("tools.browser_use_cli.is_browser_use_cli_mode", return_value=False):
  - `tests/tools/test_browser_vault.py@-269 +271` UNMARKED (1 lines): with patch("tools.browser_tool_install.check_browser_requirements", return_value=False):
  - `tests/tools/test_browser_vault.py@-271,3 +273,3` UNMARKED (3 lines): # Browser Use mode: check_browser_requirements() is False by design, the vault must still
- sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
  - `tests/tools/test_browser_vault.py@-28,0 +28#moved` moved: `agent/vault_backend_registry.py`, `agent/vault_backends/base.py`, `tests/agent/test_vault_protected_fields.py`, `tests/tools/test_browser_protected_date_pixels.py` (+10 more)
- class TestBrowserVaultTools:
  - `tests/tools/test_browser_vault.py@-318 +421` UNMARKED (1 lines): class _ManagerBackend(LocalLoginBackend):
  - `tests/tools/test_browser_vault.py@-366 +466` UNMARKED (1 lines): class _ManagerBackend(LocalLoginBackend):
  - `tests/tools/test_browser_vault.py@-424,0 +525` UNMARKED (1 lines): "remembered": False,
  - `tests/tools/test_browser_vault.py@-488,3 +589,3` UNMARKED (3 lines): # No supervisor registered and the local daemon exposes no CDP endpoint → the fill refuses
  - `tests/tools/test_browser_vault.py@-493,2 +594,2` UNMARKED (2 lines): patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg, \
  - `tests/tools/test_browser_vault.py@-500,2 +601,2` UNMARKED (2 lines): assert all(call.args[1] == "get" for call in run_cmd.call_args_list), run_cmd.call_args_li
  - `tests/tools/test_browser_vault.py@-549 +650,2` UNMARKED (1 lines): def test_payment_fill_requires_confirmation_then_fills_card_fields(self, store, partial):
  - `tests/tools/test_browser_vault.py@-549 +650,2#moved` moved: `tests/persistence_sqlite/core/test_state_db_holders.py`, `tests/tui_gateway/test_hosted_room_frozen_publication.py`
  - `tests/tools/test_browser_vault.py@-573 +675,3` UNMARKED (3 lines): return {"success": True, "result": json.dumps({"filled": 3, "fieldResults": [
  - `tests/tools/test_browser_vault.py@-586,0 +691` UNMARKED (1 lines): patch("tools.approval_context.get_current_session_key", return_value="new-session"), \
  - `tests/tools/test_browser_vault.py@-590 +695,3` UNMARKED (3 lines): assert out["success"] is (not partial) and out["partial"] is partial
  - `tests/tools/test_browser_vault.py@-592 +699#moved` moved: `tests/tools/test_browser_vault_manager_card.py`
- class TestTwoFactor:
  - `tests/tools/test_browser_vault.py@-825 +960` UNMARKED (1 lines): def test_no_code_field_describes_supported_passkey_and_approval_routes(self):
  - `tests/tools/test_browser_vault.py@-832,0 +968,6` UNMARKED (6 lines): assert "1Password passkey" in out["error"]

### `tests/tools/test_browser_vault_preview.py`

- class PreviewWindow:
  - `tests/tools/test_browser_vault_preview.py@-41 +41` UNMARKED (1 lines): elif "element.setAttribute("data-hermes-vault-slot", stamp)' in expression:

### `tests/tools/test_cron_run_notice_usage.py`

- def owner(tmp_path, monkeypatch):
  - `tests/tools/test_cron_run_notice_usage.py@-18,0 +21,5#moved` moved: `evals/completion_backlog_probe.py`, `tests/agent/test_review_engine.py`, `tests/gateway/test_agents_command_delegations.py`, `tests/gateway/test_async_delegation_session_binding.py` (+56 more)
  - `tests/tools/test_cron_run_notice_usage.py@-20,2 +27,11#moved` moved: `gateway/platforms/webhook_ingress.py`, `pm/downloader.py`, `tests/agent/test_compression_worker_isolation.py`, `tests/agent/test_review_engine.py` (+69 more)
  - `tests/tools/test_cron_run_notice_usage.py@-24 +40` UNMARKED (1 lines): def _completion(job, run_one_job, *, execution_id=None):
- def _completion(job, run_one_job):
  - `tests/tools/test_cron_run_notice_usage.py@-30,0 +47,8` UNMARKED (5 lines): def run_and_settle(claimed, **kwargs):
  - `tests/tools/test_cron_run_notice_usage.py@-30,0 +47,8#moved` moved: `hermes_cli/skills_hub.py`, `tests/cron/test_execution_ledger.py`, `tests/cron/test_script_admission.py`, `tests/gateway/test_windows_proactor_accept.py`
  - `tests/tools/test_cron_run_notice_usage.py@-32,2 +56,6` UNMARKED (5 lines): stack.enter_context(patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid
  - `tests/tools/test_cron_run_notice_usage.py@-32,2 +56,6#moved` moved: `agent/shell_hooks.py`, `hermes_cli/auth.py`, `hermes_cli/data_cleanup_holders.py`, `hermes_cli/skills_hub.py` (+24 more)
- async def test_notice_carries_the_run_sessions_model_and_api_calls(owner):
  - `tests/tools/test_cron_run_notice_usage.py@-56 +84` UNMARKED (1 lines): assert claimed["execution_id"] == fire
- model pin (None for a job on the default model). The run's own session in state.
  - `tests/tools/test_cron_run_notice_usage.py@-6,0 +7,2#moved` moved: `gateway/run_turn_runner.py`, `hermes_cli/gateway_session_store.py`, `hermes_cli/sessions_cmd.py`, `hermes_cli/skills_hub.py` (+35 more)
- async def test_notice_carries_the_run_sessions_model_and_api_calls(owner):
  - `tests/tools/test_cron_run_notice_usage.py@-60 +88` UNMARKED (1 lines): evt = _completion(job, run_one_job, execution_id=fire)

### `tests/tools/test_delegate.py`

- import json
  - `tests/tools/test_delegate.py@-12,0 +13#moved` moved: `acp_adapter/content.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+3476 more)
- class TestDelegateTask(unittest.TestCase):
  - `tests/tools/test_delegate.py@-324 +325` UNMARKED (1 lines): os.path.realpath(child_db.db_path), os.path.realpath(parent_db.db_path)

### `tests/tools/test_react_tool.py`

- (module)
  - `tests/tools/test_react_tool.py@-0,0 +1,22` UNMARKED (4 lines): db.latest_message_row_id.return_value = 42
  - `tests/tools/test_react_tool.py@-0,0 +1,22#moved` moved: `tests/agent/test_bot_chat_toolset_refresh.py`, `tests/agent/test_cross_process_turn_lease.py`, `tests/agent/test_system_prompt_restore.py`, `tests/agent/test_title_generator.py` (+17 more)

### `tests/tools/test_session_search.py`

- class TestDiscoveryTemporalNarrowing:
  - `tests/tools/test_session_search.py@-1158,0 +1426,13` UNMARKED (7 lines): @pytest.mark.parametrize("after,before", [("not-a-date", None), (None, "not-a-date")])
  - `tests/tools/test_session_search.py@-1158,0 +1426,13#moved` moved: `tests/persistence_sqlite/core/test_search_isolation.py`, `tests/persistence_sqlite/core/test_search_isolation_review.py`, `tests/persistence_sqlite/core/test_search_query_budget.py`
- class TestBrowseShape:
  - `tests/tools/test_session_search.py@-118 +294` UNMARKED (1 lines): monkeypatch.setattr("tools.session_search_tool._open_current_db", lambda: db)
  - `tests/tools/test_session_search.py@-128,0 +305,17` UNMARKED (5 lines): def test_failed_release_keeps_the_finished_result(self, monkeypatch):
  - `tests/tools/test_session_search.py@-128,0 +305,17#moved` moved: `acp_adapter/content.py`, `acp_adapter/model_catalog.py`, `acp_adapter/tools.py`, `agent/account_usage.py` (+1606 more)
- from hermes_state import SessionDB
  - `tests/tools/test_session_search.py@-17,0 +18,2` UNMARKED (1 lines): from hermes_state_common import LEGACY_FTS_SQL, _FTS_TRIGGERS
  - `tests/tools/test_session_search.py@-17,0 +18,2#moved` moved: `tests/hermes_state/test_hermes_state.py`
- class TestDiscoverySort:
  - `tests/tools/test_session_search.py@-295,6 +488,21` UNMARKED (8 lines): def test_sort_newest_orders_equal_relevance_by_recency(self, db):
  - `tests/tools/test_session_search.py@-295,6 +488,21#moved` moved: `agent/activity_tracking.py`, `gateway/session_snapshot.py`, `hermes_state_messages.py`, `persistence/sqlite/conversation/_store/messages.py` (+78 more)
  - `tests/tools/test_session_search.py@-303,4 +511,35` UNMARKED (17 lines): for session_id, age in (("s_oldest", 30000), ("s_middle", 15000), ("s_newest", 1000)):
  - `tests/tools/test_session_search.py@-303,4 +511,35#moved` moved: `tests/cron/test_review_subject.py`, `tests/hermes_cli/test_goal_external_wait_backoff_real_path.py`, `tests/hermes_state/test_hermes_state.py`, `tests/persistence_sqlite/authority/test_mutate.py` (+33 more)
- class TestSessionLink:
  - `tests/tools/test_session_search.py@-499,0 +739,28` UNMARKED (20 lines): def test_non_desktop_results_keep_titles_without_internal_links(self, db):
- def _seed_modpack_sessions(db):
  - `tests/tools/test_session_search.py@-65 +67` UNMARKED (1 lines): # Retrieval boundary evals (synthetic versions of the private misses)
  - `tests/tools/test_session_search.py@-68,0 +71,174` UNMARKED (118 lines): def _seed_boundary_message(db, content, *, tool_calls=None):
  - `tests/tools/test_session_search.py@-68,0 +71,174#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/acp_client.py`, `agent/api_error_summary.py` (+614 more)

### `tests/tui_gateway/test_adversarial_sidecar_availability.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_adversarial_sidecar_availability.py@-8 +8#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_hosted_room_attachment_routes.py`

- _LEASE_TTL_SECONDS = 30.0
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-116 +116` UNMARKED (1 lines): def _legacy_runtime(db: Path, rpc: HostedRoomServerRPC, **kwargs) -> HostedRoomRuntime:
- def _legacy_runtime(db: Path, rpc: HostedRoomServerRPC) -> HostedRoomRuntime:
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-119 +119` UNMARKED (1 lines): lease_ttl_seconds=_LEASE_TTL_SECONDS, poll_interval_seconds=0.01, **kwargs)
- def test_legacy_route_still_runs_a_turn_without_a_file(db: Path):
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-184 +184,3` UNMARKED (2 lines): # Lease expiry is not the fault under test.
  - `tests/tui_gateway/test_hosted_room_attachment_routes.py@-184 +184,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/auxiliary_health.py`, `agent/auxiliary_unavailable.py` (+217 more)

### `tests/tui_gateway/test_hosted_room_prepare_fence.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_hosted_room_prepare_fence.py@-11 +11#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-18,4 +18,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, list_session_ad
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-18,4 +18,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)
- def _legacy(monkeypatch, db):
  - `tests/tui_gateway/test_legacy_submit_forwards_to_owner.py@-56 +54` UNMARKED (1 lines): monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **kw: turns.append(a[1]))

### `tests/tui_gateway/test_legacy_turn_canonical_barrier.py`

- from tui_gateway import server
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-12,0 +12,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_diagnostic_turn_end_to_end.py`, `tests/tui_gateway/test_failed_turn_retention.py` (+9 more)
- durable ledger: an ``unknown`` admission is never replayed, queued input runs in
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-3,0 +4#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_legacy_submit_refuses_while_canonical_ledger_holds_work(monkeypatch, tm
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-31,2 +32,2` UNMARKED (1 lines): monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **kw: turns.append(a[1]))
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-31,2 +32,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_failed_turn_retention.py`, `tests/tui_gateway/test_goal_command.py`, `tests/tui_gateway/test_hosted_room_turn_slot_release.py` (+4 more)
- from hermes_state import SessionDB
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-7,4 +8,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input, list_session_ad
  - `tests/tui_gateway/test_legacy_turn_canonical_barrier.py@-7,4 +8,2#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_notification_refusal_backoff.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_notification_refusal_backoff.py@-11 +11,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/tui_gateway/test_reap_respects_canonical_ledger.py`

- from hermes_state import SessionDB
  - `tests/tui_gateway/test_reap_respects_canonical_ledger.py@-13 +13,2` UNMARKED (1 lines): from hermes_state_runtime import admit_session_input, claim_session_input

### `tests/tui_gateway/test_serve_exit_flush.py`

- def test_shutdown_mid_tool_kills_the_command_and_keeps_its_result(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-208,2 +210,2` UNMARKED (2 lines): sleeper = next((p for p in process_children(os.getpid())
- import pytest
  - `tests/tui_gateway/test_serve_exit_flush.py@-23,0 +24,2` UNMARKED (1 lines): from tests.waiting import kill_if_alive, process_children, process_cmdline, process_name
- def test_shutdown_mid_tool_kills_the_command_and_keeps_its_result(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-232,2 +234` UNMARKED (1 lines): kill_if_alive(sleeper)
- def test_sigterm_grace_hard_exit_kills_a_sigterm_ignoring_command(monkeypatch):
  - `tests/tui_gateway/test_serve_exit_flush.py@-257,2 +258,2` UNMARKED (2 lines): sleeper = next((p for p in process_children(os.getpid())
  - `tests/tui_gateway/test_serve_exit_flush.py@-271,2 +272` UNMARKED (1 lines): kill_if_alive(sleeper)

### `tests/tui_gateway/test_tui_gateway_server.py`

- (module)
  - `tests/tui_gateway/test_tui_gateway_server.py@-0,0 +1#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_config_set_model_requires_confirmation_for_expensive_model(monkeypatch)
  - `tests/tui_gateway/test_tui_gateway_server.py@-10153 +10167,2` UNMARKED (2 lines): @pytest.mark.parametrize("slot", ["keep", "off"])
- def test_config_set_model_global_persists(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-10181 +10196,4` UNMARKED (1 lines): f"model:\n  default: old/model\n  model_slots:\n    fast: "{slot}"\n',
  - `tests/tui_gateway/test_tui_gateway_server.py@-10181 +10196,4#moved` moved: `tests/agent/test_env_loader_secret_sources.py`, `tests/agent/test_relay_runtime_plugins.py`, `tests/agent/test_skill_utils.py`, `tests/gateway/test_auth_fallback.py` (+13 more)
  - `tests/tui_gateway/test_tui_gateway_server.py@-10201 +10219,2` UNMARKED (2 lines): assert saved_values["api_mode"] == "anthropic_messages"
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
  - `tests/tui_gateway/test_tui_gateway_server.py@-14112,0 +14132` UNMARKED (1 lines): monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)
- def test_run_prompt_submit_never_ticks_after_message_complete(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-1417 +1410#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_run_prompt_submit_registers_turn_thread_for_interrupt(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14388 +14408` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_interrupt_before_agent_ready_prevents_late_turn_start(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14457 +14477` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_cancelled_turn_before_agent_ready_emits_error_event(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14527 +14547` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_session_not_running_before_agent_ready_emits_error_event(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14599 +14619` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_slow_agent_build_delivers_prompt_instead_of_timing_out(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14685 +14705` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_slow_agent_build_emits_keyed_progress_notice(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14758 +14778` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
- def test_agent_build_failure_surfaces_error_and_drops_turn(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-14832 +14852` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _FakeThread)
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
  - `tests/tui_gateway/test_tui_gateway_server.py@-18986 +19006` UNMARKED (1 lines): assert server._ws_session_is_detached(server._sessions["b"])
- def test_close_sessions_for_transport_skips_session_rebound_before_claim(
  - `tests/tui_gateway/test_tui_gateway_server.py@-19029 +19049` UNMARKED (1 lines): server._attach_session_transport(session, new_transport)
  - `tests/tui_gateway/test_tui_gateway_server.py@-19046 +19066` UNMARKED (1 lines): assert server._session_transport_contains(session, new_transport)
- def test_prompt_submit_passes_persist_user_message_to_agent(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-20677 +20636#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_personality_marker_does_not_shift_truncate_ordinal(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-21074 +21033#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncation_archives_instead_of_deleting(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-21170 +21129#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_row_id_resolves_via_db_when_memory_lacks_stamps(monkeypat
  - `tests/tui_gateway/test_tui_gateway_server.py@-21339,0 +21299` UNMARKED (1 lines): monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)
- def test_prompt_submit_row_id_real_sessiondb_resolve_without_memory_stamps(
  - `tests/tui_gateway/test_tui_gateway_server.py@-21365 +21325` UNMARKED (1 lines): monkeypatch, tmp_path, _submit_truncation_only
  - `tests/tui_gateway/test_tui_gateway_server.py@-21406 +21364` UNMARKED (1 lines): resp = _submit_truncation_only(
- def test_prompt_submit_row_id_misaligned_memory_role_shift_targets_real_turn(
  - `tests/tui_gateway/test_tui_gateway_server.py@-21562 +21520` UNMARKED (1 lines): monkeypatch, tmp_path, _submit_truncation_only
  - `tests/tui_gateway/test_tui_gateway_server.py@-21601 +21557` UNMARKED (1 lines): resp = _submit_truncation_only(
- def test_load_cfg_raw_sees_replacement_with_pinned_mtime_and_size(monkeypatch, t
  - `tests/tui_gateway/test_tui_gateway_server.py@-22235,2 +22191,5` UNMARKED (3 lines): same-size atomic replacement that keeps the old mtime.
  - `tests/tui_gateway/test_tui_gateway_server.py@-22248,5 +22207#moved` moved: `tests/gateway/test_profile_serve_signature.py`, `tests/tools/test_model_tools.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-22253,0 +22209,3#moved` moved: `tests/tools/test_model_tools.py`
- def test_load_enabled_toolsets_filters_invalid_tui_env(monkeypatch, capsys):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2415 +2408#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_accepts_plugin_env_after_discovery(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2438 +2431` UNMARKED (1 lines): discover_plugins=lambda *a, **k: discovered.update({"ready": True})
- def test_load_enabled_toolsets_rejects_disabled_mcp_env(monkeypatch, capsys):
  - `tests/tui_gateway/test_tui_gateway_server.py@-2510 +2503#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_falls_back_when_tui_env_invalid(monkeypatch, caps
  - `tests/tui_gateway/test_tui_gateway_server.py@-2545 +2538#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_load_enabled_toolsets_warns_when_config_fallback_fails(monkeypatch, cap
  - `tests/tui_gateway/test_tui_gateway_server.py@-2569 +2562#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def _dispatch_sync(req: dict, transport=None) -> dict | None:
  - `tests/tui_gateway/test_tui_gateway_server.py@-26,9 +27` UNMARKED (1 lines): """Drive pooled RPC business logic inline while preserving transport context."""
- def test_load_enabled_toolsets_reports_disabled_mcp_separately(monkeypatch, caps
  - `tests/tui_gateway/test_tui_gateway_server.py@-2632 +2625#moved` moved: `tests/hermes_cli/test_dashboard_web_dist_validation.py`, `tests/hermes_cli/test_mcp_startup.py`
- def test_prompt_submit_fails_open_inline_when_compute_host_dispatch_breaks(monke
  - `tests/tui_gateway/test_tui_gateway_server.py@-398 +391#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_ws_orphan_reap_spares_turn_reattached_within_grace(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5142 +5135#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-5144 +5137` UNMARKED (1 lines): server._attach_session_transport(session, _LiveTransport())
- def test_close_transport_detaches_when_no_viewers_remain(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5599,2 +5592` UNMARKED (1 lines): """The last viewer closing parks the mailbox and schedules the grace reap."""
  - `tests/tui_gateway/test_tui_gateway_server.py@-5616 +5608#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
- def test_close_transport_skips_dead_remaining_viewers(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5653 +5645#moved` moved: `tests/tui_gateway/test_multi_client_fanout.py`, `tests/tui_gateway/test_session_event_publication.py`
- def test_live_session_payload_registers_transport_as_viewer():
  - `tests/tui_gateway/test_tui_gateway_server.py@-5670 +5662` UNMARKED (1 lines): assert server._session_transport_contains(session, t)
- def test_claim_or_reuse_live_winner_cancels_pending_reap(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5779 +5771` UNMARKED (1 lines): assert server._session_transport_contains(winner, transport)
  - `tests/tui_gateway/test_tui_gateway_server.py@-5788 +5780,2` UNMARKED (2 lines): @pytest.mark.parametrize("parked", ["legacy_sentinel", "empty_mailbox"])
- def test_superseded_runtime_finalized_without_reclaimed_broadcast(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-5790 +5783` UNMARKED (1 lines): is parked, the old record is finalized quietly with end_reason
  - `tests/tui_gateway/test_tui_gateway_server.py@-5792 +5785,2` UNMARKED (2 lines): reap Timer is cancelled (storm killer, part 2). Live sessions park as an
  - `tests/tui_gateway/test_tui_gateway_server.py@-5821,0 +5816,2` UNMARKED (2 lines): parked_transport = (server._detached_ws_transport if parked == "legacy_sentinel"
  - `tests/tui_gateway/test_tui_gateway_server.py@-5825 +5821` UNMARKED (1 lines): transport=parked_transport,
- def test_lazy_unpersisted_resume_rebinds_transport_and_cancels_reap(monkeypatch)
  - `tests/tui_gateway/test_tui_gateway_server.py@-5921 +5917` UNMARKED (1 lines): assert server._session_transport_contains(session, live_transport)
- def test_prompt_submit_refuses_unconfirmed_nonempty_truncation(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6669 +6665,18` UNMARKED (9 lines): def _submit_truncation_only(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6669 +6665,18#moved` moved: `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py`, `skills/productivity/microsoft-office/tests/test_xlsx_skill.py`, `tests/AGENTS.md` (+2221 more)
- def test_prompt_submit_truncates_by_message_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6670,0 +6684,2` UNMARKED (2 lines): # This test ends at truncation admission, not model execution.
- def test_prompt_submit_golden_transcript_matches_flag_off_and_on(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-669 +662#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_prompt_submit_truncates_by_message_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6700 +6709` UNMARKED (1 lines): resp = _submit_truncation_only(
  - `tests/tui_gateway/test_tui_gateway_server.py@-6715,0 +6725,4` UNMARKED (1 lines): worker = server._sessions["msg-id-trunc-sid"].get("_run_thread")
  - `tests/tui_gateway/test_tui_gateway_server.py@-6715,0 +6725,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-6719 +6732,3` UNMARKED (2 lines): def test_prompt_submit_truncation_falls_back_to_sid_when_session_key_null(
- def test_prompt_submit_truncation_falls_back_to_sid_when_session_key_null(monkey
  - `tests/tui_gateway/test_tui_gateway_server.py@-6758 +6768` UNMARKED (1 lines): resp = _submit_truncation_only(
- def test_prompt_submit_refuses_ordinal_only_when_durable_history_is_unstamped(mo
  - `tests/tui_gateway/test_tui_gateway_server.py@-6926 +6936` UNMARKED (1 lines): def test_prompt_submit_truncates_by_row_id(monkeypatch, _submit_truncation_only):
- def test_prompt_submit_truncates_by_row_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6927,0 +6938,2` UNMARKED (2 lines): # This test ends at truncation admission, not model execution.
  - `tests/tui_gateway/test_tui_gateway_server.py@-6956 +6964` UNMARKED (1 lines): resp = _submit_truncation_only(
  - `tests/tui_gateway/test_tui_gateway_server.py@-6974,0 +6983,4` UNMARKED (1 lines): worker = server._sessions["row-id-trunc-sid"].get("_run_thread")
  - `tests/tui_gateway/test_tui_gateway_server.py@-6974,0 +6983,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
  - `tests/tui_gateway/test_tui_gateway_server.py@-6978 +6990` UNMARKED (1 lines): def test_prompt_submit_truncates_by_string_row_id(monkeypatch, _submit_truncation_only):
- def test_prompt_submit_truncates_by_string_row_id(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-6979,0 +6992,2` UNMARKED (2 lines): # This test ends at truncation admission, not model execution.
  - `tests/tui_gateway/test_tui_gateway_server.py@-7005 +7018` UNMARKED (1 lines): resp = _submit_truncation_only(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7019,0 +7033,4` UNMARKED (1 lines): worker = server._sessions["str-row-id-trunc-sid"].get("_run_thread")
  - `tests/tui_gateway/test_tui_gateway_server.py@-7019,0 +7033,4#moved` moved: `tests/agent/test_cron_inline_api_call.py`
- def test_prompt_submit_row_id_not_found(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7107 +7124,3` UNMARKED (2 lines): def test_prompt_submit_resolves_row_id_absorbed_into_marker_merge(
- def test_prompt_submit_resolves_row_id_absorbed_into_marker_merge(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7170 +7185` UNMARKED (1 lines): resp = _submit_truncation_only(
- def _mk_merge_fake_db(verbatim, repaired, replaced):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7226 +7241,3` UNMARKED (2 lines): def test_prompt_submit_resolves_row_id_swallowed_by_marker_merge(
- def test_prompt_submit_resolves_row_id_swallowed_by_marker_merge(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-7283 +7297` UNMARKED (1 lines): resp = _submit_truncation_only(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7313 +7327,3` UNMARKED (2 lines): def test_prompt_submit_resolves_row_id_swallowed_by_plain_user_merge(
- def test_prompt_submit_resolves_row_id_swallowed_by_plain_user_merge(monkeypatch
  - `tests/tui_gateway/test_tui_gateway_server.py@-7360 +7373` UNMARKED (1 lines): resp = _submit_truncation_only(
- def test_notification_poller_live_loop_requeues_foreign_completion_for_owner(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7520,0 +7534` UNMARKED (1 lines): monkeypatch.setattr(process_registry, "restore_completions", lambda: None)
- def _configure_immediate_prompt_run(
  - `tests/tui_gateway/test_tui_gateway_server.py@-7791 +7805#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_thread
  - `tests/tui_gateway/test_tui_gateway_server.py@-8040 +8054` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, _recording_thread)
- def test_session_create_drops_pending_title_on_valueerror(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server.py@-8711 +8725#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)

### `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py`

- def test_prompt_submit_empty_truncation_allowed_with_confirm(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-156 +161#moved` moved: `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_free_tier_task_done.py`, `tests/tui_gateway/test_lazy_session_regressions.py` (+5 more)
- def test_unconfirmed_deep_truncation_is_warn_only_this_release(monkeypatch, capl
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-223 +228` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, lambda *a, **k: types.SimpleNamespace(start=lamb
- anchor can still name the wrong, stale row)."""
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-4,0 +5#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_composite_carrier_rewind.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_ephemeral_profile_override.py` (+20 more)
- def test_prompt_submit_refuses_deep_truncation_without_confirm(monkeypatch):
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-41 +46` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, lambda *a, **k: types.SimpleNamespace(start=lamb
- import types
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-7,0 +9,2#moved` moved: `evals/kanban_pr_acceptance_live.py`, `evals/postmortem/review_probes/finalizer_schedule_probe.py`, `evals/postmortem/tests/test_postmortem_harness.py`, `optional-skills/creative/comfyui/tests/conftest.py` (+7709 more)
- from tui_gateway import server
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-9,0 +13,2#moved` moved: `tests/tui_gateway/test_auto_continue.py`, `tests/tui_gateway/test_desktop_session_row_git_meta.py`, `tests/tui_gateway/test_diagnostic_turn_end_to_end.py`, `tests/tui_gateway/test_failed_turn_retention.py` (+9 more)
- def test_deep_truncate_counts_every_durable_row_a_merged_carrier_holds(monkeypat
  - `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py@-93 +98` UNMARKED (1 lines): patch_server_threads(monkeypatch, server, lambda *a, **k: types.SimpleNamespace(start=lamb

### `tests/tui_gateway/test_tui_gateway_ws.py`

- def test_legacy_fallback_never_writes_an_authority_owned_session(monkeypatch, tm
  - `tests/tui_gateway/test_tui_gateway_ws.py@-551 +551#moved` moved: `tests/agent/test_incognito_main_sinks.py`, `tests/agent/test_prompt_cache_clock_persistence.py`, `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_owner_restart.py` (+52 more)

### `tests/tui_gateway/test_vault_preview_fill.py`

- def window(monkeypatch):
  - `tests/tui_gateway/test_vault_preview_fill.py@-54 +54` UNMARKED (1 lines): elif "element.setAttribute("data-hermes-vault-slot", stamp)' in expr:

### `tools/async_delegation.py`

- (module)
  - `tools/async_delegation.py@-1,0 +2` UNMARKED (1 lines): # health: allow FILE_LINES -- route persistence and lineage notice policy belong in this e
- def _dispatch_admitted(
  - `tools/async_delegation.py@-1002,4 +808,6` UNMARKED (6 lines): **_capture_routing_origin(), "status": "queued", "_lifecycle_state": "new", "_durable_stat
  - `tools/async_delegation.py@-1007 +815,2` UNMARKED (2 lines): **({"route_chain": [tuple(route) for route in route_chain]} if route_chain else {}),
  - `tools/async_delegation.py@-1012,3 +819,7` UNMARKED (6 lines): "_worker_context_runner": propagate_context_to_thread(lambda worker: worker()), "_progress
  - `tools/async_delegation.py@-1016,25 +827,7` UNMARKED (6 lines): if any(selector in _refused_by for selector in session_selectors):
  - `tools/async_delegation.py@-1016,25 +827,7#moved` moved: `agent/provider_registry.py`, `cron/executions.py`, `gateway/cron_review.py`, `gateway/platforms/base.py` (+43 more)
- _STALL_GRACE_SECONDS = 120.0
  - `tools/async_delegation.py@-104,0 +167,4` UNMARKED (4 lines): # An admitted record without a Future is a pre-submit recovery window, not live
- def _dispatch_admitted(
  - `tools/async_delegation.py@-1042,31 +835,26` UNMARKED (24 lines): durable = _durable_state(delegation_id)
  - `tools/async_delegation.py@-1074,6 +862,77` UNMARKED (57 lines): _PENDING_ADMISSION_SLOTS.add(slot)
  - `tools/async_delegation.py@-1074,6 +862,77#moved` moved: `agent/rate_limit_credits.py`, `gateway/platforms/webhook_ingress.py`, `hermes_cli/commands_platforms.py`, `hermes_cli/plugins_cmd_inspect.py` (+4 more)
- def dispatch_async_delegation(
  - `tools/async_delegation.py@-1086 +945,3` UNMARKED (3 lines): max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFA
  - `tools/async_delegation.py@-1099,0 +961,2` UNMARKED (2 lines): max_queued_delegations=max_queued_delegations, cancel_fn=cancel_fn,
- from __future__ import annotations
  - `tools/async_delegation.py@-11,0 +13#moved` moved: `agent/auxiliary_client.py`, `agent/auxiliary_stream_watchdog.py`, `agent/context_compressor_summary.py`, `agent/context_default_record.py` (+120 more)
- _monitor_stop = threading.Event()
  - `tools/async_delegation.py@-110 +176` UNMARKED (1 lines): _LIVE_STATES = {"queued", "admitted", "running", "stalling", "finalizing"}
- _ACTIVE_STATES = ("running", "stalling")
  - `tools/async_delegation.py@-111,0 +178,8` UNMARKED (8 lines): _FINALIZABLE_STATES = {"queued", "admitted", "running", "stalling"}
- def dispatch_async_delegation_batch(
  - `tools/async_delegation.py@-1114,2 +977,2` UNMARKED (2 lines): max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFA
  - `tools/async_delegation.py@-1117 +980,4` UNMARKED (3 lines): task_contexts: Optional[dict[str, str]] = None,
  - `tools/async_delegation.py@-1135 +1001,3` UNMARKED (3 lines): max_queued_delegations=max_queued_delegations, cancel_fn=cancel_fn,
- _ACTIVE_STATES = ("running", "stalling")
  - `tools/async_delegation.py@-114 +188,7` UNMARKED (5 lines): # Session keys do not retain the originating Slack reply thread (a DM session
  - `tools/async_delegation.py@-114 +188,7#moved` moved: `gateway/run_notifications_sources.py`
- def dispatch_async_delegation_batch(
  - `tools/async_delegation.py@-1147,9 +1015,4` UNMARKED (4 lines): # Everything that decides which parent a completion re-enters. A replacement must match al
- def _keep_finished_children(record: dict[str, Any], result: Any, status: str) ->
  - `tools/async_delegation.py@-1158,6 +1021,10` UNMARKED (8 lines): def supersede_delegation(delegation_id: str, replacement_id: str, reason: str = "") -> boo
- def _finalize(delegation_id: str, result: Any, status: str) -> None:
  - `tools/async_delegation.py@-1165,15 +1032,53` UNMARKED (38 lines): record, replacement = _records.get(delegation_id), _records.get(replacement_id)
  - `tools/async_delegation.py@-1181,2 +1086,5` UNMARKED (4 lines): live = _records.get(delegation_id)
  - `tools/async_delegation.py@-1181,2 +1086,5#moved` moved: `gateway/relay/egress.py`, `gateway/run_goals.py`, `gateway/run_housekeeping_disk.py`, `gateway/run_turn_runner_notices.py` (+22 more)
  - `tools/async_delegation.py@-1185,2 +1093,3` UNMARKED (3 lines): live.pop("_context", None)
  - `tools/async_delegation.py@-1187,0 +1097` UNMARKED (1 lines): # A terminal event does not free a submitted slot; only release_slot does.
- def completion_event(record: dict[str, Any], result: dict[str, Any], status: str
  - `tools/async_delegation.py@-1195,2 +1105` UNMARKED (1 lines): # Its runner unwound on the stall monitor"s stop inside the grace window: read as interrup
  - `tools/async_delegation.py@-1204,2 +1113,2` UNMARKED (2 lines): result = {**result, "results": [{**r, "status": "waiting_to_resume"} if r in waiting else
  - `tools/async_delegation.py@-1210,0 +1120` UNMARKED (1 lines): **({"task_indexes": list(record["task_indexes"])} if record.get("task_indexes") is not Non
- _STALL_FIELD_MAP = (("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
  - `tools/async_delegation.py@-123,686 +203,9` UNMARKED (9 lines): # In-memory marker on a terminal event whose producer could not prove which durable identi
- def completion_event(record: dict[str, Any], result: dict[str, Any], status: str
  - `tools/async_delegation.py@-1230,0 +1151` UNMARKED (1 lines): **({"cut_reason": cut_reason} if cut_reason else {}),
  - `tools/async_delegation.py@-1232 +1153` UNMARKED (1 lines): **{k: record[k] for k in _ROUTING_KEYS if record.get(k) or (k == "thread_id" and k in reco
- def _push_completion_event(record: dict[str, Any], result: dict[str, Any], statu
  - `tools/async_delegation.py@-1253,6 +1174,8` UNMARKED (6 lines): if silent and not record.get("superseded_by"):
  - `tools/async_delegation.py@-1261,0 +1185,15` UNMARKED (12 lines): if record.get("superseded_by"):
  - `tools/async_delegation.py@-1263,4 +1201,99` UNMARKED (65 lines): persist_evt = dict(evt)
  - `tools/async_delegation.py@-1263,4 +1201,99#moved` moved: `agent/agent_runtime_helpers.py`, `agent/checkpoints/manager.py`, `agent/checkpoints/manager_profile_rename.py`, `agent/checkpoints/pruning.py` (+100 more)
- def push_task_failure_notice(delegation_id: str, entry: dict[str, Any], *, n_tas
  - `tools/async_delegation.py@-1277,2 +1310,4` UNMARKED (4 lines): queue only (never persisted), so the parent"s copy honours ''security.redact_secrets: fals
  - `tools/async_delegation.py@-1282,0 +1318,2` UNMARKED (1 lines): if will_retry:
  - `tools/async_delegation.py@-1299,0 +1338,5` UNMARKED (2 lines): _persist_outbox_event(evt, None, event_kind="task_failure")
- import contextvars
  - `tools/async_delegation.py@-13,0 +16#moved` moved: `acp_adapter/gateway_media.py`, `agent/account_usage_cache.py`, `agent/anthropic_credentials.py`, `agent/anthropic_thinking_replay.py` (+508 more)
- def _sweep_stale_locked(now: float):
  - `tools/async_delegation.py@-1327,0 +1371,71` UNMARKED (52 lines): if status == "running" and record.get("_future") is None:
  - `tools/async_delegation.py@-1327,0 +1371,71#moved` moved: `agent/context_compressor_window.py`, `agent/message_sanitization.py`, `cron/delivery_queue.py`, `cron/executions.py` (+14 more)
  - `tools/async_delegation.py@-1332,0 +1447,6` UNMARKED (4 lines): if status == "running" and not record.get("_started"):
  - `tools/async_delegation.py@-1351,0 +1472` UNMARKED (1 lines): _transition_memory_locked(record, "stalling", expected="running")
  - `tools/async_delegation.py@-1353 +1474` UNMARKED (1 lines): _interrupted_at=now, _stall_quiet_seconds=round(quiet_for, 2),
  - `tools/async_delegation.py@-1359 +1480` UNMARKED (1 lines): def _call_interrupt(fn, msg: str, *args, reason: str | None = None) -> bool:
- def _call_interrupt(fn, msg: str, *args) -> bool:
  - `tools/async_delegation.py@-1364 +1485,18` UNMARKED (8 lines): parameters = inspect.signature(fn).parameters.values()
  - `tools/async_delegation.py@-1364 +1485,18#moved` moved: `acp_adapter/tools.py`, `agent/agent_init.py`, `agent/agent_init_ollama.py`, `agent/api_request_hooks.py` (+435 more)
- def _stale_monitor_loop() -> None:
  - `tools/async_delegation.py@-1376,0 +1515,3` UNMARKED (3 lines): # Retirement prepare may reopen without a Future callback. Preserve
  - `tools/async_delegation.py@-1386 +1527,6` UNMARKED (5 lines): _call_interrupt(
  - `tools/async_delegation.py@-1389,2 +1535,4` UNMARKED (2 lines): ctx = (_records.get(delegation_id) or {}).get("_context")
  - `tools/async_delegation.py@-1389,2 +1535,4#moved` moved: `acp_adapter/server.py`, `agent/aux_accounting.py`, `agent/azure_identity_adapter.py`, `agent/deadline.py` (+20 more)
  - `tools/async_delegation.py@-1392 +1540,18` UNMARKED (13 lines): # Make the exit decision atomic with _ensure_stale_monitor(). A
- def list_async_delegations() -> list[dict[str, Any]]:
  - `tools/async_delegation.py@-1463 +1628` UNMARKED (1 lines): item = {k: v for k, v in r.items() if k not in {"interrupt_fn", "progress_fn", "cancel_fn"
  - `tools/async_delegation.py@-1492,2 +1657,5` UNMARKED (4 lines): def _interrupt_records(targets: list[dict[str, Any]], caller: str, reason: str, msg: str,
  - `tools/async_delegation.py@-1492,2 +1657,5#moved` moved: `agent/agent_runtime_helpers.py`, `agent/checkpoints/manager.py`, `agent/skill_utils.py`, `apps/desktop/electron/watch-storm-breaker.ts` (+36 more)
- def _interrupt_records(targets: list[dict[str, Any]], caller: str, reason: str,
  - `tools/async_delegation.py@-1495,6 +1663,81` UNMARKED (63 lines): queued_snapshot = None
  - `tools/async_delegation.py@-1495,6 +1663,81#moved` moved: `acp_adapter/gateway_server.py`, `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/tools.py` (+2495 more)
  - `tools/async_delegation.py@-1506,5 +1749,22` UNMARKED (13 lines): def _cancel_pending_retries_safely(caller: str, **selectors: Any) -> set:
- def interrupt_all(reason: str = "shutdown", *, resumable: bool = False) -> int:
  - `tools/async_delegation.py@-1515,2 +1775,16` UNMARKED (13 lines): targets = [r for r in _records.values() if r.get("status") in _INTERRUPTIBLE_STATES]
- def interrupt_for_session(
  - `tools/async_delegation.py@-1522,4 +1796` UNMARKED (1 lines): """Signal interruptible async delegations owned by ONE ending session."""
  - `tools/async_delegation.py@-1530 +1801` UNMARKED (1 lines): except Exception: # the interrupt itself must still run
  - `tools/async_delegation.py@-1532,14 +1803,7` UNMARKED (6 lines): targets = _session_records(_INTERRUPTIBLE_STATES, session_key, origin_ui_session_id, paren
- def refused_for_session(token: Any) -> bool:
  - `tools/async_delegation.py@-1549 +1813` UNMARKED (1 lines): """Return whether a session token has been fenced against new dispatches."""
- def _reset_for_tests() -> None:
  - `tools/async_delegation.py@-1583,0 +1833,2` UNMARKED (2 lines): _PENDING_QUEUE.clear()
  - `tools/async_delegation.py@-1589,0 +1841` UNMARKED (1 lines): _unresolved.clear()
  - `tools/async_delegation.py@-1592,0 +1845` UNMARKED (1 lines): # Defined below (imported from the ledger submodule); resolved at call time.
  - `tools/async_delegation.py@-1594,0 +1848,26` UNMARKED (20 lines): from tools.async_delegation_ledger import (
- import uuid
  - `tools/async_delegation.py@-20,0 +24#moved` moved: `acp_adapter/events.py`, `agent/acp_client.py`, `agent/agent_init.py`, `agent/tool_guardrails.py` (+35 more)
- from tools.daemon_pool import DaemonThreadPoolExecutor
  - `tools/async_delegation.py@-26,0 +31,3` UNMARKED (2 lines): from tools.async_delegation_completion import (
- _executor_max_workers: int = 0
  - `tools/async_delegation.py@-41 +48,4` UNMARKED (4 lines): # One re-entrant lock owns every in-memory lifecycle transition. A re-entrant
- _FENCED_FIELDS = ("session_key", "parent_session_id")
  - `tools/async_delegation.py@-49,0 +60,27` UNMARKED (22 lines): # Durable and in-memory lifecycle states. ''finalizing'' is an in-memory claim
  - `tools/async_delegation.py@-49,0 +60,27#moved` moved: `.github/actions/check-clean-worktree/action.yml`, `.github/actions/setup-pm/prune/index.mjs`, `.github/known-flakes.json`, `.prettierrc` (+9664 more)
- _DEFAULT_MAX_ASYNC_CHILDREN = 3
  - `tools/async_delegation.py@-50,0 +88,5` UNMARKED (5 lines): # Low-level callers retain the historical reject-at-capacity contract. The
- _MAX_RETAINED_COMPLETED = 50
  - `tools/async_delegation.py@-52,0 +95,4` UNMARKED (4 lines): # Restart-cut/retry rows are protected from the ordinary terminal-history cap, but
- _MAX_DURABLE_PENDING = 1000
  - `tools/async_delegation.py@-54,0 +101,21` UNMARKED (19 lines): # Rows delegation_resume would still accept for an explicit resume (owner present,
- def _get_executor(max_workers: int) -> ThreadPoolExecutor:
  - `tools/async_delegation.py@-884,0 +233,21` UNMARKED (9 lines): def open_state_db(path: Path, db_label: str) -> sqlite3.Connection:
  - `tools/async_delegation.py@-884,0 +233,21#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+1144 more)
- def active_count(*, home: Optional[Path] = None) -> int:
  - `tools/async_delegation.py@-887,0 +257,2` UNMARKED (2 lines): # Writers hold the registry lock across state.db commits. The gateway loop only needs a
  - `tools/async_delegation.py@-888,0 +260,6` UNMARKED (3 lines): return sum(1 for r in list(_records.values()) if r.get("status") in _LIVE_STATES
  - `tools/async_delegation.py@-890,2 +267,5` UNMARKED (3 lines): {"delegation_id": r.get("delegation_id"), "status": r.get("status"), "dispatched_at": r.ge
  - `tools/async_delegation.py@-890,2 +267,5#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/server.py` (+3394 more)
- def active_task_count() -> int:
  - `tools/async_delegation.py@-903,0 +284,6` UNMARKED (3 lines): def _session_selectors(session_key: str, origin_ui_session_id: str, parent_session_id: Opt
- def _session_records(statuses, session_key: str, origin_ui_session_id: str, pare
  - `tools/async_delegation.py@-909,3 +295` UNMARKED (1 lines): selectors = _session_selectors(session_key, origin_ui_session_id, parent_session_id)
  - `tools/async_delegation.py@-918,0 +303,28` UNMARKED (7 lines): def refuse_for_session(token: Any, *, session_key: str = "", parent_session_ids=(), origin
- def _prune_completed_locked() -> None:
  - `tools/async_delegation.py@-931,2 +343,5` UNMARKED (4 lines): ''_finalize''"s missing-record path and silently drop a real result. A terminal record who
- def _dispatch(**kwargs) -> dict[str, Any]:
  - `tools/async_delegation.py@-972 +387` UNMARKED (1 lines): return {"status": "rejected", "accepted": False, "error": "backend is retiring; reconnect
  - `tools/async_delegation.py@-975,0 +391,385` UNMARKED (247 lines): def _queued_count_locked() -> int:
  - `tools/async_delegation.py@-975,0 +391,385#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/gateway_server.py` (+5549 more)
- def _dispatch_admitted(
  - `tools/async_delegation.py@-982,0 +783` UNMARKED (1 lines): task_contexts: Optional[dict[str, str]] = None,
  - `tools/async_delegation.py@-983,0 +785,5` UNMARKED (4 lines): child_sessions: Optional[dict[str, str]] = None,
  - `tools/async_delegation.py@-985,6 +791,6` UNMARKED (4 lines): """Register and submit one async unit without ever running its runner inline.

### `tools/browser_vault_tool.py`

- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-10 +11` UNMARKED (1 lines): - ''browser_vault_fill'' → server-side fill of the CURRENT page from a vault
- def _ensure_supervisor(task_id: str):
  - `tools/browser_vault_tool.py@-108,3 +210,6` UNMARKED (5 lines): """The supervisor for ''task_id'', attaching one on demand for a LOCAL built-in browser se
  - `tools/browser_vault_tool.py@-116,2 +221,3#moved` moved: `agent/browser_output_egress.py`, `tools/browser_tool_vision.py`, `tools/browser_use_cli.py`
  - `tools/browser_vault_tool.py@-119 +225,2` UNMARKED (2 lines): res = _run_browser_command(_last_session_key(task_id), "get", ["cdp-url"])
- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-12 +13,2` UNMARKED (2 lines): the user confirms), address fields for address items, or one configured
- def _ensure_supervisor(task_id: str):
  - `tools/browser_vault_tool.py@-122 +229#moved` moved: `tools/browser_tool_cdp.py`, `tools/browser_use_cli.py`
  - `tools/browser_vault_tool.py@-124 +231` UNMARKED (1 lines): return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(cdp
  - `tools/browser_vault_tool.py@-126,2 +233,2` UNMARKED (1 lines): logger.debug("vault fill: supervisor attach to local session failed (%s)", exc)
- def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
  - `tools/browser_vault_tool.py@-132 +239` UNMARKED (1 lines): """Evaluate a SECRET-BEARING JS expression over CDP or a protected page transport.
  - `tools/browser_vault_tool.py@-140,0 +248,30` UNMARKED (19 lines): from tools.browser_camofox import _get_session, _post, get_camofox_url, _clear_stale_tab,
  - `tools/browser_vault_tool.py@-140,0 +248,30#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/tools.py`, `agent/agent_init.py` (+1962 more)
  - `tools/browser_vault_tool.py@-155,2 +292,4` UNMARKED (4 lines): "CDP WebSocket). The fallback eval path would place the "
- def _deep_probe(selector: str) -> str:
  - `tools/browser_vault_tool.py@-218,5 +350,9` UNMARKED (8 lines): _TAB_PROBES = {kind: build_form_probe_js(selector) for kind, selector in {
  - `tools/browser_vault_tool.py@-218,5 +350,9#moved` moved: `scripts/releases/lanes.py`
- def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[str]:
  - `tools/browser_vault_tool.py@-228,3 +364,4` UNMARKED (3 lines): login page). Returns the origin when a tab was focused, else None (caller falls back to th
  - `tools/browser_vault_tool.py@-228,3 +364,4#moved` moved: `hermes_cli/tools_config_providers.py`, `tests/tools/test_browser_profile_live_cdp.py`
  - `tools/browser_vault_tool.py@-239,25 +376,10` UNMARKED (5 lines): # Adopted from upstream PR 128194: the requested origin is only a filter,
- def _bound_page_origin(task_id: str, meta: Any, probe_kind: str) -> str | dict[s
  - `tools/browser_vault_tool.py@-270,2 +392,15` UNMARKED (13 lines): def _browser_account_refusal(backend, task_id: str) -> Optional[str]:
- def browser_vault_list() -> str:
  - `tools/browser_vault_tool.py@-272,0 +408,43` UNMARKED (28 lines): def _remember_login(task_id: str) -> bool:
  - `tools/browser_vault_tool.py@-272,0 +408,43#moved` moved: `agent/credential_pool.py`, `agent/credential_pool_admin.py`, `agent/credential_pool_plugin.py`, `agent/credential_pool_selection.py` (+139 more)
  - `tools/browser_vault_tool.py@-274 +452` UNMARKED (1 lines): agent knows to call browser_vault_fill (which prompts the user to unlock) or tell the user
  - `tools/browser_vault_tool.py@-277,0 +456,9` UNMARKED (4 lines): if kind is not None and kind not in ("login", "payment", "address", "protected_field"):
  - `tools/browser_vault_tool.py@-277,0 +456,9#moved` moved: `agent/vault_backends/browser.py`, `cron/blueprint_catalog.py`, `hermes_cli/desktop_plugin_halves.py`, `hermes_cli/plugin_host_wire.py` (+4 more)
  - `tools/browser_vault_tool.py@-283 +470` UNMARKED (1 lines): "unlock": "browser_vault_unlock" if can_prompt_here() else "unavailable_in_this_session"})
  - `tools/browser_vault_tool.py@-286 +473,14` UNMARKED (12 lines): metas = apply_origin_aliases(backend.list_items())
  - `tools/browser_vault_tool.py@-286 +473,14#moved` moved: `agent/vault_backends/base.py`, `tests/agent/test_vault_backends.py`, `tests/agent/test_vault_connect.py`, `tests/agent/test_vault_onepassword_accounts.py` (+6 more)
  - `tools/browser_vault_tool.py@-289,0 +490,10` UNMARKED (10 lines): metas = [meta for meta in metas
  - `tools/browser_vault_tool.py@-292 +502,3` UNMARKED (3 lines): "origin": meta.origin,
  - `tools/browser_vault_tool.py@-295,2 +507` UNMARKED (1 lines): entry.update(_list_two_factor_metadata(backend, meta, origin, len(selected_logins) == 1))
  - `tools/browser_vault_tool.py@-299,0 +511,5` UNMARKED (4 lines): if meta.field_token:
  - `tools/browser_vault_tool.py@-299,0 +511,5#moved` moved: `gateway/run_config_loaders.py`
  - `tools/browser_vault_tool.py@-303,2 +519,13` UNMARKED (13 lines): out["hint"] = ("No matching vault items for this filter. Try another kind or origin." if k
- import logging
  - `tools/browser_vault_tool.py@-34,0 +34,2#moved` moved: `acp_adapter/server.py`, `acp_adapter/session.py`, `agent/account_usage_cache.py`, `agent/acp_client.py` (+2392 more)
- def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) ->
  - `tools/browser_vault_tool.py@-347,0 +575` UNMARKED (1 lines): browser_key = _browser_key(effective_task_id)
  - `tools/browser_vault_tool.py@-350,2 +578,2` UNMARKED (2 lines): _focus_bound_origin(browser_key, "", "login")
- from typing import Any, Dict, Optional
  - `tools/browser_vault_tool.py@-37 +38,3` UNMARKED (1 lines): from agent.vault_login_classifier import build_form_probe_js
- (module)
  - `tools/browser_vault_tool.py@-4,2 +4,3` UNMARKED (3 lines): Vault actions are available alongside a built-in browser, Browser Use, or a
- logger = logging.getLogger(__name__)
  - `tools/browser_vault_tool.py@-40,0 +44,64` UNMARKED (41 lines): # A refused/unanswered prompt blocks another card prompt for this approval session and ori
  - `tools/browser_vault_tool.py@-40,0 +44,64#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/commands.py` (+4277 more)
- def _saved_login(handle: str) -> tuple[Any, Any]:
  - `tools/browser_vault_tool.py@-400 +610` UNMARKED (1 lines): def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None, remember: bo
- def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None) ->
  - `tools/browser_vault_tool.py@-402,3 +612,4` UNMARKED (4 lines): authenticator seed, the code is minted server-side and nobody is asked. Otherwise the call
  - `tools/browser_vault_tool.py@-405,0 +617,2#moved` moved: `tests/agent/test_vault_onepassword_accounts.py`, `tools/browser_vault_context.py`
  - `tools/browser_vault_tool.py@-407,0 +621#moved` moved: `tools/browser_vault_context.py`
  - `tools/browser_vault_tool.py@-410,9 +624,28` UNMARKED (15 lines): if backend is not None and (refusal := _browser_account_refusal(backend, effective_task_id
  - `tools/browser_vault_tool.py@-410,9 +624,28#moved` moved: `tests/gateway/test_streaming_tts_consumer.py`, `tests/scripts/desktop_update/test_desktop_update_windows_child_stage.py`, `tests/scripts/desktop_update/test_desktop_update_windows_log_exit_flush.py`, `tests/scripts/desktop_update/test_desktop_update_windows_marker.py` (+5 more)
  - `tools/browser_vault_tool.py@-420,4 +653,33` UNMARKED (22 lines): _focus_bound_origin(browser_key, "", "otp")
  - `tools/browser_vault_tool.py@-420,4 +653,33#moved` moved: `tools/browser_vault_aliases.py`
  - `tools/browser_vault_tool.py@-427 +689` UNMARKED (1 lines): inspect = _eval_js(browser_key, build_inspection_js(nonce))
  - `tools/browser_vault_tool.py@-434,2 +696,6` UNMARKED (6 lines): "error": ("No one-time-code field on the current page. If the site wants a passkey, use a
  - `tools/browser_vault_tool.py@-436,0 +703,7` UNMARKED (6 lines): # Inspection and alias consent can race navigation. Refuse before minting
  - `tools/browser_vault_tool.py@-438,0 +712,4` UNMARKED (4 lines): if raw_meta is not None and (refusal := _alias_fill_revalidation_refusal(raw_meta, origin)
  - `tools/browser_vault_tool.py@-440 +717,3` UNMARKED (3 lines): result = _eval_js_secret(browser_key, build_fill_js(
  - `tools/browser_vault_tool.py@-450 +729,3` UNMARKED (3 lines): remembered = bool(parsed.get("remembered")) if isinstance(parsed, dict) else False
  - `tools/browser_vault_tool.py@-453 +734,2#moved` moved: `agent/vault_backends/unlock.py`, `hermes_cli/gateway.py`, `hermes_cli/gateway_setup_wizard.py`, `hermes_cli/web_routers/files.py` (+2 more)
- def _check_vault_available() -> bool:
  - `tools/browser_vault_tool.py@-47,3 +114,3` UNMARKED (3 lines): """Schema-gate: the vault tools ride with the browser. An empty vault still needs
  - `tools/browser_vault_tool.py@-51,3 +118,5` UNMARKED (5 lines): from tools.browser_tool_install import check_browser_requirements
- def _identifier_fill(classified: list, password_fill: list[dict[str, Any]], iden
  - `tools/browser_vault_tool.py@-532,4 +764,23` UNMARKED (15 lines): def _fill_metadata(backend, handle):
  - `tools/browser_vault_tool.py@-532,4 +764,23#moved` moved: `tests/tools/test_vault_origin_aliases.py`, `tools/browser_vault_context.py`
- def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, eff
  - `tools/browser_vault_tool.py@-545,0 +797,2` UNMARKED (2 lines): classify_protected_field_control,
  - `tools/browser_vault_tool.py@-547,2 +800,3` UNMARKED (1 lines): from agent.vault_backends.base import FILL_METADATA_NOT_APPLICABLE, FILL_METADATA_TTL_SECO
  - `tools/browser_vault_tool.py@-550,12 +804,12` UNMARKED (5 lines): # Pin ONE browser for the whole fill: the privacy check, inspection, write and redaction m
  - `tools/browser_vault_tool.py@-576,8 +818` UNMARKED (1 lines): raw_meta, saved_origins, meta = _fill_metadata(backend, handle)
  - `tools/browser_vault_tool.py@-586,32 +821,2` UNMARKED (1 lines): "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
- def _check_vault_available() -> bool:
  - `tools/browser_vault_tool.py@-59,0 +130,9` UNMARKED (6 lines): def _browser_key(task_id: str) -> str:
  - `tools/browser_vault_tool.py@-59,0 +130,9#moved` moved: `agent/browser_output_egress.py`, `tools/browser_tool_vision.py`
- def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, eff
  - `tools/browser_vault_tool.py@-623,4 +827,3` UNMARKED (1 lines): f"No vault item with handle {handle!r}. Use browser_vault_list. "
  - `tools/browser_vault_tool.py@-632,8 +834,315` UNMARKED (207 lines): def _discard_fill_metadata() -> None:
  - `tools/browser_vault_tool.py@-632,8 +834,315#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `agent/auxiliary_client.py`, `agent/context_compressor.py` (+171 more)
  - `tools/browser_vault_tool.py@-642 +1151,16` UNMARKED (12 lines): def _payment_fill_refusal(decision: str, retry_key: tuple[str, str, str]) -> str:
  - `tools/browser_vault_tool.py@-642 +1151,16#moved` moved: `evals/prefix_rediet/worker.py`, `tools/browser_vault_aliases.py`
- def _confirm_payment_fill(label: str, origin: str) -> bool:
  - `tools/browser_vault_tool.py@-645,0 +1170,35` UNMARKED (17 lines): surface="vault-payment", title="Confirm payment card fill?")
  - `tools/browser_vault_tool.py@-645,0 +1170,35#moved` moved: `agent/agent_runtime_helpers.py`, `hermes_cli/web_server_memory.py`, `scripts/ci/cache_report.py`, `tests/cron/test_scheduler_provider.py` (+3 more)
  - `tools/browser_vault_tool.py@-647,0 +1207,7` UNMARKED (4 lines): warning = alias_domain_warning(origin, saved_origins)
  - `tools/browser_vault_tool.py@-647,0 +1207,7#moved` moved: `cli.py`, `hermes_cli/doctor_platform.py`, `hermes_cli/gateway_chat.py`, `hermes_cli/gateway_session_store.py` (+6 more)
  - `tools/browser_vault_tool.py@-649,4 +1215,3` UNMARKED (2 lines): f"Fill login '{label}' on exact origin {origin} (alias)?",
  - `tools/browser_vault_tool.py@-649,4 +1215,3#moved` moved: `apps/desktop/e2e/core/oracle.ts`, `apps/desktop/src/app/artifacts/index.tsx`, `apps/desktop/src/app/chat/composer/status-stack/goal-indicator.test.tsx`, `apps/desktop/src/app/chat/hooks/use-composer-actions.ts` (+9 more)
  - `tools/browser_vault_tool.py@-658,0 +1224,14` UNMARKED (5 lines): BROWSER_VAULT_LIST_SCHEMA = {
  - `tools/browser_vault_tool.py@-658,0 +1224,14#moved` moved: `tests/fixtures/cua_driver_0_9_tools_list.json`, `tests/fixtures/prompt_surface/tools/browser_back.json`, `tests/fixtures/prompt_surface/tools/browser_cdp.json`, `tests/fixtures/prompt_surface/tools/browser_close.json` (+14 more)
- BROWSER_VAULT_SCHEMA = {
  - `tools/browser_vault_tool.py@-662,2 +1241,2` UNMARKED (2 lines): "Use saved credentials on the current browser page without revealing secrets. "
  - `tools/browser_vault_tool.py@-669,4 +1248,22` UNMARKED (21 lines): "authenticator key when available; otherwise fetch the newest matching code from the inten
  - `tools/browser_vault_tool.py@-669,4 +1248,22#moved` moved: `evals/prefix_rediet/worker.py`
  - `tools/browser_vault_tool.py@-677,0 +1275,2` UNMARKED (2 lines): "kind": {"type": "string", "enum": ["login", "payment", "address", "protected_field"], "de
  - `tools/browser_vault_tool.py@-678,0 +1278,4` UNMARKED (4 lines): "remember": {"type": "boolean", "default": True, "description": (
  - `tools/browser_vault_tool.py@-689,0 +1293,46` UNMARKED (27 lines): def _private_protected_field_refusal(task_id: str, browser_key: str) -> str | None:
  - `tools/browser_vault_tool.py@-689,0 +1293,46#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4780 more)
- def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
  - `tools/browser_vault_tool.py@-691,2 +1340,3` UNMARKED (3 lines): from tools.browser_tool import _active_sessions, _last_session_key
- def _fenced_page_op(task_id: Optional[str], fn) -> str:
  - `tools/browser_vault_tool.py@-697,3 +1347,4` UNMARKED (3 lines): ''_run_browser_command''; they must honour the Bot Desktop lease like every other page acc
- paid per request) and available wherever a browser is. Its actions:
  - `tools/browser_vault_tool.py@-7 +8` UNMARKED (1 lines): - ''browser_vault_list'' → handles + metadata (for logins this includes the
- def _fenced_page_op(task_id: Optional[str], fn) -> str:
  - `tools/browser_vault_tool.py@-701 +1352,2` UNMARKED (2 lines): session = _active_sessions.get(_last_session_key(task_id or "default")) or {}
- def _handle_vault(args: dict[str, Any], **kwargs) -> str:
  - `tools/browser_vault_tool.py@-705,0 +1358#moved` moved: `tools/browser_extension_router.py`, `tools/browser_tool_cdp.py`, `tools/browser_tool_eval_policy.py`, `tools/browser_tool_vision.py`
  - `tools/browser_vault_tool.py@-706,0 +1360,6` UNMARKED (4 lines): if task_browser_url(tid):
  - `tools/browser_vault_tool.py@-706,0 +1360,6#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4758 more)
  - `tools/browser_vault_tool.py@-710 +1369` UNMARKED (1 lines): return browser_vault_list(kind=args.get("kind"), origin=args.get("origin"))
  - `tools/browser_vault_tool.py@-714 +1373` UNMARKED (1 lines): "fill": lambda: browser_vault_fill(handle=handle, task_id=tid, remember=args.get("remember
  - `tools/browser_vault_tool.py@-716 +1375` UNMARKED (1 lines): "enter_code": lambda: browser_vault_enter_code(handle=handle, task_id=tid, remember=args.g
  - `tools/browser_vault_tool.py@-733 +1392,10` UNMARKED (3 lines): name="browser_vault_list",
- def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
  - `tools/browser_vault_tool.py@-92,2 +171,11` UNMARKED (7 lines): """Evaluate NON-SECRET JS on the current page (inspection, origin reads).
  - `tools/browser_vault_tool.py@-92,2 +171,11#moved` moved: `hermes_cli/tools_config_providers.py`, `tests/tools/test_browser_profile_live_cdp.py`, `tools/browser_tool.py`
  - `tools/browser_vault_tool.py@-97,8 +185,22` UNMARKED (8 lines): sup = supervisor.evaluate_runtime(expression)
  - `tools/browser_vault_tool.py@-97,8 +185,22#moved` moved: `CONTRIBUTING.md`, `acp_adapter/auth.py`, `acp_adapter/entry.py`, `acp_adapter/model_catalog.py` (+2042 more)

### `tools/delegate_tool.py`

- def _build_child_agent(
  - `tools/delegate_tool.py@-314 +334` UNMARKED (1 lines): session_db=child_session_db, parent_session_id=parent_sid, request_overrides=request_overr

### `tools/delegate_tool_resumable.py`

- def plan_recovery(path: Optional[Path], alive) -> dict[str, tuple[Any, Any, list
  - `tools/delegate_tool_resumable.py@-506 +506` UNMARKED (1 lines): rows = _read_only(path, lambda conn: conn.execute(
  - `tools/delegate_tool_resumable.py@-515,0 +516,29` UNMARKED (9 lines): def _read_only(path: Optional[Path], fn) -> Any:
  - `tools/delegate_tool_resumable.py@-515,0 +516,29#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/gateway_media.py`, `acp_adapter/gateway_server.py` (+4651 more)

### `tools/environments/base_session_env.py`

- def _export_dump_excluding_session_vars(tmp_path: str, excluded_names: Iterable[
  - `tools/environments/base_session_env.py@-117 +123,22` UNMARKED (4 lines): # ''>|'' truncates the mktemp file even when the session has ''noclobber'' (''set -C'') on
- def _snapshot_bootstrap_script(
  - `tools/environments/base_session_env.py@-142 +161,2` UNMARKED (1 lines): f"{_shell_state_dump(_SNAP_TMP, excluded_names)} && mv -f {_SNAP_TMP} {quoted_snap} "
- def _wrap_command_script(
  - `tools/environments/base_session_env.py@-166 +186,3` UNMARKED (1 lines): persist_env: bool = True, seed_snaps: Iterable[str] = ()) -> str:
  - `tools/environments/base_session_env.py@-179 +202,4` UNMARKED (4 lines): its parent"s shell state and its re-dump never writes the parent's file. ''relay_parent_ad
  - `tools/environments/base_session_env.py@-204 +236` UNMARKED (1 lines): f"{{ {_shell_state_dump(_SNAP_TMP, passthrough_names)} "
- def _split_cwd_marker(output: str, marker: str) -> tuple[str | None, str] | None
  - `tools/environments/base_session_env.py@-225 +264,3` UNMARKED (1 lines): cwd_path = output[first + len(marker) : last].removesuffix("\n")

### `tools/process_registry.py`

- class ProcessRegistry(ProcessTerminationMixin, ProcessCheckpointMixin, ProcessOu
  - `tools/process_registry.py@-1192 +1261,13` UNMARKED (1 lines): if is_restart_safe(session) or (persist_on_release and not use_pty and not _IS_WINDOWS and
  - `tools/process_registry.py@-1417,47 +1447` UNMARKED (1 lines): _log_delta_command = staticmethod(log_delta_command)
  - `tools/process_registry.py@-1573 +1562,2` UNMARKED (1 lines): if session._kill_deadline is None or time.monotonic() < session._kill_deadline:
  - `tools/process_registry.py@-1888 +1866` UNMARKED (1 lines): return session # no backend probe here: get() refreshes, callers that must not probe use t
  - `tools/process_registry.py@-2007,0 +1995` UNMARKED (1 lines): self._poll_observed.add(session_id)
  - `tools/process_registry.py@-2009,0 +1998` UNMARKED (1 lines): self._completion_consumed.add(session_id)
  - `tools/process_registry.py@-2011,2 +2000,4` UNMARKED (4 lines): if session.detached and session.pid_scope == "host" and not session.output_log:
  - `tools/process_registry.py@-2041,0 +2033` UNMARKED (1 lines): self._completion_consumed.add(session_id)
  - `tools/process_registry.py@-2072,0 +2067` UNMARKED (1 lines): self._completion_consumed.add(session_id)
  - `tools/process_registry.py@-2198 +2212` UNMARKED (1 lines): self._completion_consumed.add(session_id)
  - `tools/process_registry.py@-2241 +2274,7` UNMARKED (1 lines): timeout=remaining if remaining is not None else 5,
  - `tools/process_registry.py@-2370,2 +2415,2` UNMARKED (1 lines): self._refresh_detached_session(s) for s in selected
  - `tools/process_registry.py@-2521,2 +2594,7` UNMARKED (1 lines): all_sessions = (*self._running.values(), *self._finished.values())
  - `tools/process_registry.py@-2527,4 +2606,429` UNMARKED (3 lines): if source == "gateway_shutdown" and restart_survivors:
- _SESSION_ACTIONS = {
  - `tools/process_registry.py@-2680,0 +3197,8` UNMARKED (3 lines): def _handoff_process(session_id: str, args: dict, task_id: Optional[str]) -> dict:
  - `tools/process_registry.py@-2680,0 +3197,8#moved` moved: `tools/process_registry_handoff.py`

### `tools/process_registry_results.py`

- def _owns_result(owner: str, parent: str | None) -> bool:
  - `tools/process_registry_results.py@-84 +279,7` UNMARKED (1 lines): db = SessionDB(db_path=db_path, read_only=True) if db_path is not None else SessionDB(read
  - `tools/process_registry_results.py@-84 +279,7#moved` moved: `cron/delivery_queue.py`, `cron/missed_runs.py`, `gateway/delivery_recovery.py`, `hermes_cli/sessions_inspect.py` (+3 more)

### `tools/session_search_tool.py`

- import logging
  - `tools/session_search_tool.py@-12,0 +13#moved` moved: `agent/context_compressor.py`, `agent/context_compressor_durable.py`, `agent/conversation_compression_publication.py`, `agent/kanban_turn_recovery.py` (+805 more)
- def _get_message_storage_state(db, message_id) -> Optional[dict[str, Any]]:
  - `tools/session_search_tool.py@-217 +222` UNMARKED (1 lines): """Owning session and visibility flags (None if absent, database errors propagate)."""
  - `tools/session_search_tool.py@-219,3 +224` UNMARKED (1 lines): return db._read_one("SELECT session_id, active, compacted FROM messages WHERE id = ?", (me
- def _hydrate_hit(db, lineage_root: str, match_info: dict[str, Any], result_detai
  - `tools/session_search_tool.py@-419,0 +423,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _discover(db, query: str, role_filter: Optional[list[str]], limit: int, sort
  - `tools/session_search_tool.py@-461 +466,2` UNMARKED (2 lines): exclude_sources=list(_HIDDEN_SESSION_SOURCES), exclude_tool_calls=True,
- def _quiet(fn, default, msg, *log_args, with_exc: bool = False):
  - `tools/session_search_tool.py@-61 +62` UNMARKED (1 lines): """Best-effort ancillary call. Database failures must reach the agent, not become missing
  - `tools/session_search_tool.py@-63,0 +65,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _scroll(db, session_id: str, around_message_id: int, window: int = 5,
  - `tools/session_search_tool.py@-728,0 +735,7` UNMARKED (3 lines): def _open_current_db():
  - `tools/session_search_tool.py@-728,0 +735,7#moved` moved: `evals/sibling_prompt_cache/worker.py`, `gateway/session_incognito_boundary.py`, `gateway/telegram_topic_sessions.py`, `hermes_cli/config_migrations_alias_routing.py` (+185 more)
- def _loud(fn, log_msg, error_prefix, *log_args):
  - `tools/session_search_tool.py@-73,0 +77,2#moved` moved: `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py`, `acp_adapter/session.py` (+804 more)
- def _dispatch(query, role_filter, limit, db, current_session_id, session_id,
  - `tools/session_search_tool.py@-732,21 +745,62` UNMARKED (36 lines): """Mode dispatch inside one independent-reader scope; scroll wins with an anchor."""
  - `tools/session_search_tool.py@-732,21 +745,62#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `acp_adapter/catalog.py`, `acp_adapter/commands.py` (+11626 more)
- def session_search(query: str = "", role_filter: str | None = None, limit: int |
  - `tools/session_search_tool.py@-756,3 +810,5` UNMARKED (5 lines): current_session_id: str | None = None, session_id: str | None = None,
  - `tools/session_search_tool.py@-791,4 +846,4` UNMARKED (1 lines): db = _quiet(_open_current_db, None, "SessionDB unavailable for session_search")
  - `tools/session_search_tool.py@-791,4 +846,4#moved` moved: `gateway/session_kanban.py`, `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_workspace.py`, `hermes_cli/sqlite_safe_read.py` (+18 more)
  - `tools/session_search_tool.py@-799,3 +854,6` UNMARKED (3 lines): result = _dispatch(query, role_filter, limit, db, current_session_id, session_id,
  - `tools/session_search_tool.py@-799,3 +854,6#moved` moved: `hermes_cli/backup_restore.py`, `hermes_cli/data_cleanup_holders.py`, `hermes_cli/immutable_releases.py`, `hermes_cli/kanban_db_connect.py` (+11 more)
  - `tools/session_search_tool.py@-804 +862,5` UNMARKED (3 lines): # Cleanup must not discard a finished result: _quiet re-raises sqlite errors for reads.
  - `tools/session_search_tool.py@-804 +862,5#moved` moved: `acp_adapter/auth.py`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/edit_approval.py` (+1471 more)
- SESSION_SEARCH_SCHEMA = {
  - `tools/session_search_tool.py@-857,3 +919,6` UNMARKED (6 lines): "Discovery shape only. 'newest' and 'oldest' keep BM25 relevance "
- registry.register(
  - `tools/session_search_tool.py@-956,0 +1022` UNMARKED (1 lines): platform=kw.get("platform"),

### `tools/skills_tool_dedup.py`

- (module)
  - `tools/skills_tool_dedup.py@-2,6 +2,6` UNMARKED (1 lines): which forgets the task"s served views so the next view returns the full content again.

### `tools/terminal_tool.py`

- def _plan_execution(
  - `tools/terminal_tool.py@-1334 +1392` UNMARKED (1 lines): if timeout and timeout > FOREGROUND_MAX_TIMEOUT and bound_turn_origin() != "cron":
- def _run_foreground(
  - `tools/terminal_tool.py@-1422 +1452,2` UNMARKED (2 lines): metered: bool = True, execution_command: Optional[str] = None,
  - `tools/terminal_tool.py@-1425 +1456,3` UNMARKED (3 lines): is False for Hermes" own control-plane commands (''_host_local''). ''execution_command''
  - `tools/terminal_tool.py@-1427 +1460,2` UNMARKED (1 lines): max_retries = 0 if current_execution() else 3
  - `tools/terminal_tool.py@-1428,0 +1463#moved` moved: `agent/azure_identity_adapter.py`, `agent/credential_pool.py`, `hermes_cli/plugins_cmd.py`, `hermes_cli/web_server_oauth.py` (+3 more)
  - `tools/terminal_tool.py@-1448,0 +1484,13` UNMARKED (10 lines): deadline = cell_deadline.get()
  - `tools/terminal_tool.py@-1448,0 +1484,13#moved` moved: `tests/tools/test_terminal_tool_semaphore.py`, `tools/code_kernel.py`
  - `tools/terminal_tool.py@-1452,0 +1502,2` UNMARKED (2 lines): **({"yield_after_seconds": yield_after_seconds, "yield_when": yield_when}
  - `tools/terminal_tool.py@-1455,0 +1507,5` UNMARKED (4 lines): return _error_json(_redact_terminal_error_text(
- def terminal_tool(
  - `tools/terminal_tool.py@-1597,0 +1644,4` UNMARKED (4 lines): ''survive_restart'' (background-only, local non-PTY only) keeps an explicitly bounded,
  - `tools/terminal_tool.py@-1602,0 +1653,3` UNMARKED (2 lines): if is_incognito(session_id) and (background or persist_on_release or survive_restart or pt
  - `tools/terminal_tool.py@-1603,0 +1657,3` UNMARKED (1 lines): def _metered(plan, result, **kwargs):
  - `tools/terminal_tool.py@-1603,0 +1657,3#moved` moved: `agent/acp_client.py`, `agent/agent_runtime_helpers.py`, `agent/anthropic_message_convert.py`, `agent/auxiliary_client.py` (+795 more)
  - `tools/terminal_tool.py@-1613,0 +1670,4` UNMARKED (2 lines): # A trailing ''&'' is a background spawn, whose registry and log persist.
  - `tools/terminal_tool.py@-1613,0 +1670,4#moved` moved: `agent/title_generator.py`, `gateway/run_plugin_injection.py`, `gateway/shutdown_flush.py`, `tools/cronjob_tools.py` (+2 more)
  - `tools/terminal_tool.py@-1618,0 +1681,6` UNMARKED (3 lines): if survive_restart and (not background or pty or env_type != "local"):
  - `tools/terminal_tool.py@-1669,0 +1736,2` UNMARKED (2 lines): if plan.promoted_from_foreground_timeout is not None and is_incognito(session_id):
  - `tools/terminal_tool.py@-1673,0 +1742,8` UNMARKED (7 lines): # Approval and pre-exec security checks above saw the user-supplied command; only the
  - `tools/terminal_tool.py@-1674,0 +1751,3` UNMARKED (1 lines): execution_command = wrap_semaphore_command(
- TERMINAL_SCHEMA = {
  - `tools/terminal_tool.py@-1733 +1818` UNMARKED (1 lines): "description": f"Max seconds (default 180, interactive foreground max {FOREGROUND_MAX_TIME
  - `tools/terminal_tool.py@-1746 +1831` UNMARKED (1 lines): "description": "With background=true. true: one notification on exit; use it for anything
  - `tools/terminal_tool.py@-1761,0 +1847,5` UNMARKED (1 lines): "description": "With background=true and the local non-PTY backend: keep this idempotent b
  - `tools/terminal_tool.py@-1771,0 +1862,5` UNMARKED (2 lines): from tools.node_execution import node_handler, node_schema
  - `tools/terminal_tool.py@-1771,0 +1862,5#moved` moved: `tests/tools/test_node_file_policy.py`, `tools/file_tools.py`
- def _handle_terminal(args, **kw):
  - `tools/terminal_tool.py@-1819,0 +1916,5` UNMARKED (2 lines): "survive_restart only applies to local non-PTY background commands. Retry as "
- Use read_file, search_files, patch and write_file instead of cat/head/tail, grep
  - `tools/terminal_tool.py@-183 +187,7` UNMARKED (6 lines): Foreground (default): returns INSTANTLY when the command finishes, even with a high timeou
- def _handle_terminal(args, **kw):
  - `tools/terminal_tool.py@-1830 +1931,4` UNMARKED (4 lines): "strings (notify on output pattern match). Use JSON booleans, "
- def _resolve_container_task_id(task_id: Optional[str]) -> str:
  - `tools/terminal_tool.py@-542 +596,4` UNMARKED (4 lines): 2. Local delegated children and registered runs own separate shell snapshots,
  - `tools/terminal_tool.py@-546 +603` UNMARKED (1 lines): 4. Session key present (WebUI per-session, gateway per-message): persistent
  - `tools/terminal_tool.py@-553 +610` UNMARKED (1 lines): 5. No session key (CLI, cron, kanban workers): ''shared:<key>'' when opted in
  - `tools/terminal_tool.py@-568,0 +632,3` UNMARKED (3 lines): shell_owner = _local_shell_owner(task_id) if scope.env_type == "local" else None
  - `tools/terminal_tool.py@-574,2 +640,2` UNMARKED (2 lines): # on non-local backends inherit the same session key and share the parent"s container (#16
- def _safe_parse_import_env(name: str, default: Any, converter, type_label: str):
  - `tools/terminal_tool.py@-77 +78,2` UNMARKED (2 lines): # Interactive foreground timeout cap; cron keeps its requested deadline inline.
- def _get_env_config() -> dict[str, Any]:
  - `tools/terminal_tool.py@-889 +963,3` UNMARKED (1 lines): env_type = "ssh" if execution else _tenv("TERMINAL_ENV", "local")
  - `tools/terminal_tool.py@-911 +987` UNMARKED (1 lines): cwd, host_cwd = ("~", None) if execution else _resolve_config_cwd(env_type, mount_docker_c
  - `tools/terminal_tool.py@-913 +989#moved` moved: `apps/desktop/electron/main.ts`, `apps/desktop/src/app/settings/delegation-settings-integration.test.tsx`, `evals/provider_fallback/probe_104260.py`, `evals/provider_fallback/probe_104360.py` (+74 more)
  - `tools/terminal_tool.py@-945,0 +1022` UNMARKED (1 lines): "docker_auto_mount_host_state": _tenv_bool("TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE", "true"
  - `tools/terminal_tool.py@-957,0 +1035,5` UNMARKED (2 lines): config.update(ssh_host=execution.node.ssh, ssh_user="", ssh_port=22, ssh_key="",
  - `tools/terminal_tool.py@-957,0 +1035,5#moved` moved: `agent/auxiliary_task_config.py`, `agent/azure_identity_adapter.py`, `agent/fallback_session_route.py`, `agent/transports/chat_completions.py` (+48 more)

### `tools/terminal_tool_background.py`

- def _spawn(process_registry, *, env, env_type, command, cwd, effective_task_id,
  - `tools/terminal_tool_background.py@-104 +169,5` UNMARKED (4 lines): from tools.process_registry_ssh import SSHLaunchUncertain
- def _apply_async_support(proc_session, result_data, notify_on_complete, watch_pa
  - `tools/terminal_tool_background.py@-127,0 +197,54` UNMARKED (12 lines): no-op, so a PID mentioned in prose can"t masquerade as a transfer.
  - `tools/terminal_tool_background.py@-127,0 +197,54#moved` moved: `agent/checkpoints/pruning.py`, `agent/plugin_session_fork.py`, `agent/relay_runtime.py`, `agent/side_question_lease.py` (+58 more)
- def spawn_background_process(
  - `tools/terminal_tool_background.py@-172,0 +320,7` UNMARKED (5 lines): if survive_restart and is_delegated_child_context():
  - `tools/terminal_tool_background.py@-238,0 +387,9` UNMARKED (8 lines): if survive_restart or getattr(proc_session, "ssh_binding", None) is not None:
- def _looks_like_homebrew_ci_poller(command: str) -> bool:
  - `tools/terminal_tool_background.py@-84 +146` UNMARKED (1 lines): def _stamp_gateway_routing(proc_session, get_session_env, *, parent_session_id="") -> None
- def _stamp_gateway_routing(proc_session, get_session_env) -> None:
  - `tools/terminal_tool_background.py@-92 +154,2` UNMARKED (2 lines): value = parent_session_id if attr == "parent_session_id" and parent_session_id else get_se
- def _spawn(process_registry, *, env, env_type, command, cwd, effective_task_id,
  - `tools/terminal_tool_background.py@-96 +159,2` UNMARKED (1 lines): session_key, effective_pty, persist_on_release: bool = False,

### `tools/tool_result_storage.py`

- def referenced_spillover_names(spill_dir) -> set | None:
  - `tools/tool_result_storage.py@-108,0 +109#moved` moved: `hermes_state_media.py`
  - `tools/tool_result_storage.py@-118,3 +119,4` UNMARKED (2 lines): with prune_sql(conn):
  - `tools/tool_result_storage.py@-118,3 +119,4#moved` moved: `cron/delivery_queue.py`, `cron/executions.py`, `cron/incidents.py`, `cron/notepad.py` (+125 more)
  - `tools/tool_result_storage.py@-128,0 +131#moved` moved: `hermes_state_deletion.py`, `hermes_state_maintenance.py`, `hermes_state_media.py`, `hermes_state_search.py`
- def _write_to_spillover(content: str, filename: str):
  - `tools/tool_result_storage.py@-203,0 +207,3#moved` moved: `agent/agent_runtime_helpers.py`, `agent/auxiliary_health.py`, `agent/background_review.py`, `agent/credential_pool_errors.py` (+55 more)
- def maybe_persist_tool_result(content: str, tool_name: str, tool_use_id: str, en
  - `tools/tool_result_storage.py@-443 +449,5` UNMARKED (2 lines): message = _build_persisted_message(preview, has_more, len(persisted_content), path)
  - `tools/tool_result_storage.py@-443 +449,5#moved` moved: `acp_adapter/edit_approval.py`, `agent/anthropic_adapter.py`, `agent/chat_completion_helpers.py`, `agent/history_commentary.py` (+57 more)
- def enforce_turn_budget(tool_messages: list[dict], env=None,
  - `tools/tool_result_storage.py@-524 +534` UNMARKED (1 lines): config: BudgetConfig = DEFAULT_BUDGET, env_for_message=None) -> list[dict]:
  - `tools/tool_result_storage.py@-538,0 +549` UNMARKED (1 lines): result_env = env_for_message(tool_messages[idx], env) if env_for_message else env
  - `tools/tool_result_storage.py@-541 +552` UNMARKED (1 lines): parts, saved = _spill_largest_text_part(content, tool_use_id, result_env, config)
  - `tools/tool_result_storage.py@-549 +560` UNMARKED (1 lines): env=result_env, config=config, threshold=0)

### `tui_gateway/agent_factory.py`

- def _stored_session_runtime_overrides(row: dict | None) -> dict:
  - `tui_gateway/agent_factory.py@-129,0 +160,2` UNMARKED (1 lines): row = fallback_restored_row(row) or {} # a fallback"s route is turn-scoped, also across a
  - `tui_gateway/agent_factory.py@-129,0 +160,2#moved` moved: `gateway/platforms/api_server.py`
- def _resolve_model() -> str:
  - `tui_gateway/agent_factory.py@-15 +15,5` UNMARKED (3 lines): route = _default_route(cfg)
  - `tui_gateway/agent_factory.py@-15 +15,5#moved` moved: `hermes_cli/status.py`
- def _stored_session_runtime_overrides(row: dict | None) -> dict:
  - `tui_gateway/agent_factory.py@-174,2 +206,4` UNMARKED (3 lines): "model": model, "provider": provider or None, "base_url": base_url or None, "api_mode": ap
- def _persist_live_session_runtime(session: dict | None) -> None:
  - `tui_gateway/agent_factory.py@-225,0 +260,4` UNMARKED (2 lines): # The live route is the session"s own (a pick, or the restored primary), not a fallback's.
  - `tui_gateway/agent_factory.py@-225,0 +260,4#moved` moved: `agent/chat_completion_helpers.py`, `agent/fallback_route_recovery.py`, `hermes_state_mutation_prepared.py`, `hermes_state_sessions.py`
  - `tui_gateway/agent_factory.py@-232,0 +271,2` UNMARKED (2 lines): if "model_pinned" in session:
- def _load_reasoning_config(model: str = "") -> dict | None:
  - `tui_gateway/agent_factory.py@-274,2 +314` UNMARKED (1 lines): """Via the shared chokepoint :func:'hermes_constants.resolve_reasoning_config'.
  - `tui_gateway/agent_factory.py@-277 +316,2` UNMARKED (2 lines): Precedence lives there: the default route"s declared preset effort, then the
- def _resolve_model() -> str:
  - `tui_gateway/agent_factory.py@-28,0 +33,18` UNMARKED (4 lines): def _default_route(cfg: dict):
  - `tui_gateway/agent_factory.py@-28,0 +33,18#moved` moved: `acp_adapter/session.py`, `agent/context_compressor.py`, `agent/conversation_compression_feasibility.py`, `agent/reasoning_params.py` (+29 more)
- def _config_model_target() -> tuple[str, str]:
  - `tui_gateway/agent_factory.py@-49,2 +71,10` UNMARKED (7 lines): dashboard/CLI model changes never reach an open chat. Empty model = "no preference" → no-o
- def _tool_lifecycle_required_for_ui(name: str) -> bool:
  - `tui_gateway/agent_factory.py@-510 +550` UNMARKED (1 lines): def _resolve_runtime_with_fallback(resolve_kwargs: dict | None = None, *, pinned_model: st
- def _resolve_runtime_with_fallback(resolve_kwargs: dict | None = None) -> _Runti
  - `tui_gateway/agent_factory.py@-518 +558,5` UNMARKED (3 lines): chain = _load_fallback_model() or []
  - `tui_gateway/agent_factory.py@-518 +558,5#moved` moved: `agent/agent_init_fallback.py`, `cron/scheduler.py`, `gateway/run_config_loaders.py`, `hermes_cli/cli_agent_setup_mixin.py` (+3 more)
  - `tui_gateway/agent_factory.py@-531,2 +575,9` UNMARKED (8 lines): "Primary %s for model %s (%s), falling back to %s model %s",
  - `tui_gateway/agent_factory.py@-538,0 +590,7` UNMARKED (5 lines): def _has_model_pin(model_override: dict | str | None) -> bool:
- def _resolve_agent_model_runtime(model_override, provider_override) -> tuple[str
  - `tui_gateway/agent_factory.py@-574 +632,2` UNMARKED (2 lines): resolution = (_resolve_runtime_with_fallback(resolve_kwargs, pinned_model=model)
- def _make_agent(
  - `tui_gateway/agent_factory.py@-656,0 +716` UNMARKED (1 lines): preflight_fallback = runtime.pop("_preflight_fallback", None)
  - `tui_gateway/agent_factory.py@-661,0 +722,2` UNMARKED (1 lines): if session is not None and _has_model_pin(model_override):
  - `tui_gateway/agent_factory.py@-661,0 +722,2#moved` moved: `tui_gateway/model_switch.py`
  - `tui_gateway/agent_factory.py@-686,0 +749` UNMARKED (1 lines): model_pinned=_has_model_pin(model_override),
  - `tui_gateway/agent_factory.py@-692,0 +756,2` UNMARKED (1 lines): if preflight_fallback is not None:
  - `tui_gateway/agent_factory.py@-692,0 +756,2#moved` moved: `gateway/platforms/api_server.py`

### `tui_gateway/change_watcher.py`

- def _pet_changed_payload() -> dict:
  - `tui_gateway/change_watcher.py@-191,14 +191,5` UNMARKED (5 lines): # path -> (database/WAL mtime, session-table digest, incremental projection). Journal-back
- def _session_db_content_sig(db_path: Path):
  - `tui_gateway/change_watcher.py@-263,2 +238` UNMARKED (1 lines): the subset of columns they have.
  - `tui_gateway/change_watcher.py@-266,2 +240,38` UNMARKED (15 lines): cached = _sessions_db_sig_cache.get(cache_key)
  - `tui_gateway/change_watcher.py@-266,2 +240,38#moved` moved: `agent/agent_runtime_helpers.py`, `agent/kanban_turn_recovery.py`, `agent/shared_primary_cooldown.py`, `cron/scheduler_review.py` (+303 more)

### `tui_gateway/contracts/canonical_sessions.py`

- class CanonicalListParams(ProfileParams):
  - `tui_gateway/contracts/canonical_sessions.py@-185 +185` UNMARKED (1 lines): # ''hermes -c'' rule (''hermes_state_sessions_workspace.workspace_key''): its git repo roo

### `tui_gateway/contracts/groups_bot_relay.py`

- (module)
  - `tui_gateway/contracts/groups_bot_relay.py@-1 +1,2` UNMARKED (2 lines): """Hosted Group Chat rooms (''groups.*''), cross-connection bot relay (''bot_relay.*''),
- method("bot_relay.reply", params=BotRelayReplyParams, result=OkResult,
  - `tui_gateway/contracts/groups_bot_relay.py@-671,0 +673,65` UNMARKED (31 lines): # ── browser controller ──────────────────────────────────────────────────────────────────
  - `tui_gateway/contracts/groups_bot_relay.py@-671,0 +673,65#moved` moved: `agent/subagent_lifecycle.py`, `gateway/browser_control_broker.py`, `gateway/session_contract.py`, `hermes_cli/gateway_runtime.py` (+9 more)

### `tui_gateway/methods_prompt.py`

- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_prompt.py@-1211,0 +1226,2` UNMARKED (2 lines): kwargs.pop("_preflight_fallback", None)
- def _final_response_text(result) -> str:
  - `tui_gateway/methods_prompt.py@-1430,11 +1446,3` UNMARKED (2 lines): text = result.get("final_response", str(result)) if isinstance(result, dict) else str(resu
  - `tui_gateway/methods_prompt.py@-1430,11 +1446,3#moved` moved: `acp_adapter/server.py`, `gateway/platforms/webhook.py`, `hermes_cli/cli_chat_turn_mixin.py`, `hermes_cli/cli_single_query.py` (+4 more)
- def _run_after_agent_ready(
  - `tui_gateway/methods_prompt.py@-676 +677` UNMARKED (1 lines): queue_token=None, hosted_images=None,
  - `tui_gateway/methods_prompt.py@-707 +708,2` UNMARKED (2 lines): image_paths=hosted_images, terminal_callback=hosted_terminal_callback, turn_author=turn_au
- def _claim_submit_turn(
  - `tui_gateway/methods_prompt.py@-836,2 +838` UNMARKED (1 lines): # Per-turn client surfaces shared with canonical admission (gateway.session_surface).
- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_prompt.py@-909,0 +911,12` UNMARKED (8 lines): # Stop/Esc cancels the session-owned record. Resolve that terminal state before
  - `tui_gateway/methods_prompt.py@-909,0 +911,12#moved` moved: `.github/actions/setup-pm/prune/index.mjs`, `acp_adapter/session.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py` (+4747 more)
  - `tui_gateway/methods_prompt.py@-932 +945` UNMARKED (1 lines): display_kind=display_kind, moa_token=params.get("moa_token"))
  - `tui_gateway/methods_prompt.py@-962 +975,2` UNMARKED (1 lines): user_turn=display_kind is None and turn_author is None, queue_token=params.get("moa_token"
  - `tui_gateway/methods_prompt.py@-990 +1004` UNMARKED (1 lines): queue_token=params.get("moa_token"), hosted_images=hosted_images),

### `tui_gateway/methods_tools.py`

- from pathlib import Path
  - `tui_gateway/methods_tools.py@-11,0 +16#moved` moved: `tui_gateway/agent_callbacks.py`, `tui_gateway/compute_host_bridge.py`, `tui_gateway/prompt_turn.py`, `tui_gateway/session_lifecycle.py`
- def _(rid, params: dict) -> dict:
  - `tui_gateway/methods_tools.py@-1210 +1232,4` UNMARKED (4 lines): if action in {"status", "use"}:
  - `tui_gateway/methods_tools.py@-1212 +1237` UNMARKED (1 lines): return _ok(rid, {"connected": bool(url), "url": url, "browser_use": is_browser_use_cli_mod
- def _cmd_moa(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-684,2 +689,2` UNMARKED (2 lines): # A running turn owns the live agent. Queue the one-shot state alongside the
  - `tui_gateway/methods_tools.py@-687,4 +692,20` UNMARKED (15 lines): # The token is the only MoA state a client may carry. Preset and restore data remain
  - `tui_gateway/methods_tools.py@-687,4 +692,20#moved` moved: `tui_gateway/prompt_turn.py`
  - `tui_gateway/methods_tools.py@-698 +719,2#moved` moved: `tui_gateway/prompt_turn.py`
- Helper names must not collide with server.py's own (``_cmd_`` / ``_toolset_`` /
  - `tui_gateway/methods_tools.py@-7,0 +8,4` UNMARKED (2 lines): from tui_gateway.methods_tools_plugin_settings import (
- def _cmd_retry(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-736,0 +759#moved` moved: `hermes_state_rewind.py`, `tests/agent/test_reference_handoff_active_turn.py`, `tui_gateway/session_workdir.py`
  - `tui_gateway/methods_tools.py@-747 +770` UNMARKED (1 lines): content = retryable_user_text(live_view.get("content"))
  - `tui_gateway/methods_tools.py@-754 +777,2` UNMARKED (2 lines): assert rewound is not None
- def _cmd_goal(rid, params, session, name, arg):
  - `tui_gateway/methods_tools.py@-781 +805,2` UNMARKED (1 lines): max_turns = goals.normalize_goal_max_turns(value)
  - `tui_gateway/methods_tools.py@-781 +805,2#moved` moved: `tui_gateway/prompt_turn.py`
  - `tui_gateway/methods_tools.py@-786,4 +811,6` UNMARKED (1 lines): with scoped_current_session_id(sid_key):
  - `tui_gateway/methods_tools.py@-786,4 +811,6#moved` moved: `evals/process_result_receipt_probe.py`, `gateway/session_viewer_automation.py`, `gateway/slash_commands_goals.py`, `tests/gateway/test_goal_peer_rpc.py` (+6 more)

### `tui_gateway/model_switch.py`

- def _switch_request(raw_input: str, parsed_flags, persist_override) -> tuple[str
  - `tui_gateway/model_switch.py@-176,0 +190,2` UNMARKED (2 lines): if getattr(f, "is_all", False):
- def _snapshot_agent_model_runtime(agent) -> dict:
  - `tui_gateway/model_switch.py@-23 +24,5` UNMARKED (5 lines): "reasoning_override": copy.deepcopy(getattr(agent, "reasoning_override", None)),
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-29,0 +35,8` UNMARKED (3 lines): def _restore_reasoning() -> None:
  - `tui_gateway/model_switch.py@-29,0 +35,8#moved` moved: `agent/route_binding.py`, `tests/agent/test_switch_model_reasoning_override.py`
- def _apply_model_switch(
  - `tui_gateway/model_switch.py@-294,0 +321` UNMARKED (1 lines): from tui_gateway.methods_config_set import _refuse_managed_model_pick
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-32,2 +45,6` UNMARKED (5 lines): _restore_reasoning()
  - `tui_gateway/model_switch.py@-41,2 +58` UNMARKED (1 lines): _restore_reasoning()
- def _apply_switch_reasoning(sid: str, session, agent, effort: str, *, persist_gl
  - `tui_gateway/model_switch.py@-423,0 +452,4` UNMARKED (3 lines): # Explicit for delegation too; the --once restore swaps reasoning_config back, which
  - `tui_gateway/model_switch.py@-423,0 +452,4#moved` moved: `gateway/slash_commands_model.py`
- def _restore_agent_model_runtime(agent, snapshot: dict | None) -> None:
  - `tui_gateway/model_switch.py@-56,2 +70` UNMARKED (1 lines): _restore_reasoning()

### `tui_gateway/prompt_turn.py`

- def _complete_turn_payload(session: dict, st: _TurnRun, status_note: str | None,
  - `tui_gateway/prompt_turn.py@-950,3 +1093,9` UNMARKED (1 lines): payload = {"text": visible_raw, "usage": _get_usage(agent), "status": status}

### `tui_gateway/server.py`

- def _clear_pending(sid: str | None = None) -> None:
  - `tui_gateway/server.py@-1108,0 +1438,208` UNMARKED (28 lines): """Runtime fields persisted with a stored session (model column, ''billing_provider'', JSO
  - `tui_gateway/server.py@-1108,0 +1438,208#moved` moved: `acp_adapter/commands.py`, `acp_adapter/gateway_commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/gateway_session_meta.py` (+1177 more)
- _BOOL_WORDS = {
  - `tui_gateway/server.py@-1257,0 +1795,271` UNMARKED (6 lines): """Via the shared chokepoint :func:'hermes_constants.resolve_reasoning_config'.
  - `tui_gateway/server.py@-1257,0 +1795,271#moved` moved: `acp_adapter/model_catalog.py`, `acp_adapter/server.py`, `acp_adapter/session.py`, `acp_adapter/tools.py` (+541 more)
- def _session_info(agent, session: dict | None = None) -> dict:
  - `tui_gateway/server.py@-1505 +2313` UNMARKED (1 lines): from hermes_cli.version_info import get_version_info_nonblocking
  - `tui_gateway/server.py@-1507 +2315` UNMARKED (1 lines): info.update(version=getattr(get_version_info_nonblocking(), "base_version", ""), release_d
  - `tui_gateway/server.py@-1511 +2319#moved` moved: `CONTRIBUTING.es.md`, `CONTRIBUTING.md`, `agent/display.py`, `agent/inline_tool_executors.py` (+183 more)
  - `tui_gateway/server.py@-1515 +2323` UNMARKED (1 lines): info["tools"].setdefault(registry.get_toolset_for_tool(name) or "other", []).append(name)
- class _RuntimeFallbackResolution(NamedTuple):
  - `tui_gateway/server.py@-1602,0 +2411,228` UNMARKED (44 lines): def _resolve_runtime_with_fallback(resolve_kwargs: dict | None = None, *, pinned_model: st
  - `tui_gateway/server.py@-1602,0 +2411,228#moved` moved: `acp_adapter/auth.py`, `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py` (+3130 more)
- def _hydrate_session_cwd(sid: str, key: str, session_db, profile_home: str | Non
  - `tui_gateway/server.py@-1656,0 +2693,443` UNMARKED (28 lines): def _load_resume_transcript(db, stored_id: str, *, model_history_only: bool = False) -> tu
  - `tui_gateway/server.py@-1656,0 +2693,443#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2275 more)
- def _finish_reload(rid, params: dict, *, coalesced: bool) -> dict:
  - `tui_gateway/server.py@-1946,0 +3426,9#moved` moved: `.github/known-flakes.json`, `acp_adapter/commands.py`, `acp_adapter/content.py`, `acp_adapter/server.py` (+3358 more)
- _WORKER_BLOCKED_COMMANDS: frozenset[str] = frozenset({"snapshot", "snap"})
  - `tui_gateway/server.py@-1956,0 +3445,51#moved` moved: `acp_adapter/commands.py`, `acp_adapter/entry.py`, `acp_adapter/server.py`, `acp_adapter/session.py` (+984 more)
- from . import (
  - `tui_gateway/server.py@-2005 +3542` UNMARKED (1 lines): methods_browser_control as _methods_browser_control, methods_bot_relay as _methods_bot_rel
  - `tui_gateway/server.py@-2017,0 +3555,2` UNMARKED (1 lines): _server_stored_session_runtime_overrides = _stored_session_runtime_overrides
- for _m in (
  - `tui_gateway/server.py@-2024 +3563` UNMARKED (1 lines): _methods_browser_control, _methods_session, _methods_prompt, _methods_config,
- del _m
  - `tui_gateway/server.py@-2031,0 +3571,3` UNMARKED (2 lines): # Keep the server"s persisted-route reader visible after module registration binds factory
- def _shutdown_sessions() -> None:
  - `tui_gateway/server.py@-262,8 +277` UNMARKED (1 lines): _shutdown_live_sessions()
- def write_json(obj: dict) -> bool:
  - `tui_gateway/server.py@-584 +576,3` UNMARKED (1 lines): return _publish_session_frame(obj, t)
- logger = logging.getLogger(__name__)
  - `tui_gateway/server.py@-62,0 +64,14` UNMARKED (4 lines): def _bootstrap_served_profile_store(db_path: Path) -> None:
  - `tui_gateway/server.py@-62,0 +64,14#moved` moved: `agent/acp_client.py`, `agent/auxiliary_health.py`, `agent/nous_rate_guard.py`, `agent/shared_primary_cooldown.py` (+176 more)
- def _current_session_steer_authority(session_id: str) -> tuple[Transport | None,
  - `tui_gateway/server.py@-817,0 +812,322` UNMARKED (19 lines): def _bind_build_profile_scopes(profile_home: str | None) -> "_TurnScopes | None":
  - `tui_gateway/server.py@-817,0 +812,322#moved` moved: `acp_adapter/commands.py`, `acp_adapter/gateway_server.py`, `acp_adapter/server.py`, `agent/agent_init.py` (+577 more)
- def _active_config_path() -> Path:
  - `tui_gateway/server.py@-854 +1170,2` UNMARKED (2 lines): home = override if isinstance(override, str) and override else _launch_home()
- def _set_session_context(session_key: str, cwd: str | None = None, *, ui_session
  - `tui_gateway/server.py@-933,0 +1251` UNMARKED (1 lines): browser_control_principal = browser_control_transport_family = ""
  - `tui_gateway/server.py@-941,0 +1260,9` UNMARKED (8 lines): transport = sess.get("transport")
  - `tui_gateway/server.py@-941,0 +1260,9#moved` moved: `tui_gateway/session_transports.py`
  - `tui_gateway/server.py@-944 +1271,3` UNMARKED (1 lines): cwd=resolved, ui_session_id=ui_session_id, cron_session="")
  - `tui_gateway/server.py@-944 +1271,3#moved` moved: `gateway/platforms/api_server.py`

### `tui_gateway/session_compression.py`

- def _apply_live_compression_config(agent: Any, cfg: dict | None) -> None:
  - `tui_gateway/session_compression.py@-121,2 +134,3` UNMARKED (2 lines): for key, fallback, min_value, strict in _COMPRESSION_INT_KEYS:
  - `tui_gateway/session_compression.py@-123,0 +138,3` UNMARKED (1 lines): setattr(cc, key, max(min_value, _parse_config_int(raw, default)))
  - `tui_gateway/session_compression.py@-123,0 +138,3#moved` moved: `acp_adapter/catalog.py`, `acp_adapter/content.py`, `acp_adapter/events.py`, `acp_adapter/gateway_media.py` (+2064 more)
- def _derived_default_threshold_percent(agent: Any, compression: dict) -> float:
  - `tui_gateway/session_compression.py@-76 +76,14` UNMARKED (8 lines): def _default_compression_int(key: str, fallback: int) -> int:
  - `tui_gateway/session_compression.py@-76 +76,14#moved` moved: `acp_adapter/session.py`, `acp_adapter/tools.py`, `agent/api_error_summary.py`, `agent/api_request_hooks.py` (+395 more)
- _COMPRESSION_INT_KEYS = (
  - `tui_gateway/session_compression.py@-78,5 +91,5` UNMARKED (5 lines): ("proactive_prune_tokens", 0, 0, True),

### `tui_gateway/session_reaper.py`

- def _repair_missing_ws_orphan_reaps() -> None:
  - `tui_gateway/session_reaper.py@-308,3 +315,6` UNMARKED (6 lines): closed_transports = [
- def _schedule_startup_orphan_sweep() -> None:
  - `tui_gateway/session_reaper.py@-582,0 +592,16` UNMARKED (5 lines): def _shutdown_live_sessions() -> None:
  - `tui_gateway/session_reaper.py@-582,0 +592,16#moved` moved: `agent/relay_runtime.py`, `hermes_cli/cli_config_load.py`, `hermes_cli/cli_tui_runtime_mixin.py`, `hermes_cli/source_build.py` (+8 more)

### `tui_gateway/session_workdir.py`

- def _rewind_active_session_history(
  - `tui_gateway/session_workdir.py@-877 +877,2` UNMARKED (1 lines): from agent.context_compressor import history_before_user_originated_turn, user_originated_

### `uv.lock`

- wheels = [
  - `uv.lock@-1258,0 +1527,24` UNMARKED (3 lines): version = "5.3.3"
  - `uv.lock@-1329,0 +1622,15` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/da/c6/8bb3f0584e7affe13f092906b42
- dependencies = [
  - `uv.lock@-1549 +1856,2` UNMARKED (2 lines): { name = "lxml", version = "6.1.2", source = { registry = "https://pypi.org/simple" }, mar
- wheels = [
  - `uv.lock@-1727,0 +2063,13` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/f5/22/900cb125c76b7aaa450ce02fd72
  - `uv.lock@-1776,0 +2125,9` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/50/79/66800aadf48771f6b62f7eb014e
  - `uv.lock@-1807,0 +2165,194` UNMARKED (4 lines): sdist = { url = "https://files.pythonhosted.org/packages/33/eb/3b534c6f8e157f9ddbf2a153512
  - `uv.lock@-1823,0 +2375,117` UNMARKED (6 lines): version = "4.1.6"
  - `uv.lock@-1832,0 +2501,14` UNMARKED (3 lines): "python_full_version >= '3.14' and 'android' not in platform_release and sys_platform == '
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-1836,0 +2519,4` UNMARKED (2 lines): "(python_full_version >= '3.14' and platform_machine != 'ARM64' and 'android' not in platf
- grpc = [
  - `uv.lock@-2123,0 +2893,57` UNMARKED (3 lines): "python_full_version >= '3.14' and 'android' not in platform_release and sys_platform == '
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-2127,0 +2954,4` UNMARKED (2 lines): "(python_full_version >= '3.14' and platform_machine != 'ARM64' and 'android' not in platf
- dependencies = [
  - `uv.lock@-2318,0 +3158` UNMARKED (1 lines): { name = "portalocker", marker = "sys_platform == 'win32'" },
- documents = [
  - `uv.lock@-2406,5 +3246,14` UNMARKED (14 lines): { name = "beautifulsoup4", marker = "(platform_machine != 'ARM64' and 'android' not in pla
- google-meet = [
  - `uv.lock@-2444,2 +3293,8` UNMARKED (2 lines): { name = "playwright", marker = "'android' not in platform_release" },
- kittentts = [
  - `uv.lock@-2451,3 +3306,6` UNMARKED (3 lines): { name = "kittentts", marker = "(platform_machine != 'ARM64' and platform_machine != 'x86_
- piper = [
  - `uv.lock@-2490 +3348` UNMARKED (1 lines): { name = "piper-tts", marker = "(platform_machine != 'ARM64' and platform_machine != 'x86_
- stt-whisper = [
  - `uv.lock@-2504,2 +3362,2` UNMARKED (2 lines): { name = "av", marker = "(platform_machine != 'ARM64' and platform_machine != 'x86_64') or
- voice = [
  - `uv.lock@-2554,2 +3412,2` UNMARKED (2 lines): { name = "av", marker = "(platform_machine != 'ARM64' and platform_machine != 'x86_64') or
- wheels = [
  - `uv.lock@-2822,0 +3699,124` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/0d/60/6236c856fe74d7aa448e34bd995
- dependencies = [
  - `uv.lock@-2996 +3989,2` UNMARKED (1 lines): { name = "filelock", version = "4.2.6", source = { registry = "https://pypi.org/simple" },
- wheels = [
  - `uv.lock@-3206,0 +4213,24` UNMARKED (3 lines): version = "1.44"
  - `uv.lock@-3215,0 +4246,18` UNMARKED (3 lines): version = "3.10"
- requires-dist = [
  - `uv.lock@-3289,0 +4353,188` UNMARKED (11 lines): version = "1.7.6"
- wheels = [
  - `uv.lock@-3314,0 +4566,144` UNMARKED (4 lines): "(python_full_version >= '3.14' and platform_machine != 'ARM64' and 'android' not in platf
- source = { registry = "https://pypi.org/simple" }
  - `uv.lock@-3318,0 +4714,3` UNMARKED (1 lines): "(python_full_version >= '3.14' and platform_machine == 'ARM64' and sys_platform == 'win32
- wheels = [
  - `uv.lock@-3437,0 +4876,53` UNMARKED (4 lines): sdist = { url = "https://files.pythonhosted.org/packages/92/ab/d1297139c0e2ceb151ae564c8c4
  - `uv.lock@-4002,0 +5625,9` UNMARKED (3 lines): version = "2.28.0"
- dependencies = [
  - `uv.lock@-4367 +6010` UNMARKED (1 lines): { name = "et-xmlfile", marker = "(platform_machine != 'ARM64' and 'android' not in platfor
- wheels = [
  - `uv.lock@-4446,0 +6090,14` UNMARKED (3 lines): version = "0.66b1"
  - `uv.lock@-4477,0 +6135,32` UNMARKED (6 lines): version = "0.66b1"
  - `uv.lock@-4534,0 +6272,54` UNMARKED (3 lines): { name = "numpy", marker = "(platform_machine != 'ARM64' and 'android' not in platform_rel
  - `uv.lock@-4596,0 +6397,22` UNMARKED (6 lines): sdist = { url = "https://files.pythonhosted.org/packages/c9/04/11ead62e4a8da2e9804459a0ffa
  - `uv.lock@-4775,0 +6598,9` UNMARKED (3 lines): version = "4.14.5"
- dependencies = [
  - `uv.lock@-4781 +6612,2` UNMARKED (1 lines): { name = "greenlet", version = "3.5.6", source = { registry = "https://pypi.org/simple" },
- resolution-markers = [
  - `uv.lock@-5,2 +5,7` UNMARKED (7 lines): "(python_full_version >= '3.14' and platform_machine != 'ARM64' and 'android' not in platf
- wheels = [
  - `uv.lock@-504,0 +585,13` UNMARKED (3 lines): version = "1.9.5"
  - `uv.lock@-5098,0 +6981,22` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/ca/99/c346e3474853801ec5ecf4c3ee6
- dependencies = [
  - `uv.lock@-5497 +7442` UNMARKED (1 lines): { name = "lxml", version = "6.1.2", source = { registry = "https://pypi.org/simple" } },
  - `uv.lock@-5557,4 +7502,5` UNMARKED (5 lines): { name = "lxml", version = "6.1.2", source = { registry = "https://pypi.org/simple" }, mar
- wheels = [
  - `uv.lock@-577,0 +680,22` UNMARKED (2 lines): { name = "soupsieve", marker = "(platform_machine != 'ARM64' and 'android' not in platform
  - `uv.lock@-6242,0 +8420,20` UNMARKED (3 lines): version = "6.2.0"
  - `uv.lock@-6292,0 +8490,13` UNMARKED (3 lines): version = "2.74.0"
  - `uv.lock@-6496,0 +8699,9` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/01/7c/a64eedae0b7776cb9602d2da9cb
  - `uv.lock@-6590,0 +8802,48` UNMARKED (38 lines): version = "2.0.57"
- dependencies = [
  - `uv.lock@-6822 +9146,2` UNMARKED (1 lines): { name = "filelock", version = "4.2.6", source = { registry = "https://pypi.org/simple" },
- wheels = [
  - `uv.lock@-697,0 +822,44` UNMARKED (3 lines): version = "7.3.0"
  - `uv.lock@-7051,0 +9389,9` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/58/49/423c660a9b548ebe875562c4f47
  - `uv.lock@-7442,0 +9845,18` UNMARKED (11 lines): sdist = { url = "https://files.pythonhosted.org/packages/8f/48/b49b0d4ec3abc1e20368ccb22c6
  - `uv.lock@-7637,0 +10067,133` UNMARKED (127 lines): version = "4.0.2"
  - `uv.lock@-966,0 +1131,19` UNMARKED (7 lines): version = "0.2.179"
  - `uv.lock@-993,0 +1177,28` UNMARKED (2 lines): sdist = { url = "https://files.pythonhosted.org/packages/dd/96/8dc291c7fd243dcb0d83f9dc043

### `website/docs/developer-guide/programmatic-integration.md`

- Build on the methods the gateway serves. The methods answered by an older handle
  - `website/docs/developer-guide/programmatic-integration.md@-103 +103` UNMARKED (1 lines): **Answered by an older handler** (164): 'billing.auto_reload', 'billing.charge', 'billing.

### `website/docs/reference/mcp-config-reference.md`

- mcp_servers:
  - `website/docs/reference/mcp-config-reference.md@-36,0 +37` UNMARKED (1 lines): caller_identity: false
  - `website/docs/reference/mcp-config-reference.md@-60,0 +62` UNMARKED (1 lines): | 'caller_identity' | bool | both | Send the calling session"s identity ('profile', 'sessi

### `website/docs/user-guide/configuration.md`

- compression:
  - `website/docs/user-guide/configuration.md@-1114 +1185` UNMARKED (1 lines): proactive_prune_tokens: 48000 # Default tokens trigger for the no-LLM tool-result prune (0
- The value is the **first rung** of an escalating ladder, not a fixed interval: c
  - `website/docs/user-guide/configuration.md@-1146 +1217` UNMARKED (1 lines): 'context_timeout_seconds' (default '120') is the same **inactivity budget** for in-agent '
  - `website/docs/user-guide/configuration.md@-1160 +1231` UNMARKED (1 lines): 'proactive_prune_tokens' enables a deterministic, no-LLM prune of old tool-result payloads
- When the summary streams (compression always streams over chat-completions and R
  - `website/docs/user-guide/configuration.md@-1210 +1281` UNMARKED (1 lines): Raising 'timeout' alone does **not** widen the progress window — a request configured for
- default.
  - `website/docs/user-guide/configuration.md@-1241,0 +1313,15` UNMARKED (8 lines): ## Post-Update Admission Canary
- For a user/plugin policy gate at the same point — keep the agent going with yo
  - `website/docs/user-guide/configuration.md@-1442 +1528` UNMARKED (1 lines): By default, when a standing goal is active, Hermes judges whether each assistant response
- goals:
  - `website/docs/user-guide/configuration.md@-1446 +1532,4` UNMARKED (3 lines): notices: true # Show routine goal lifecycle output for this profile
  - `website/docs/user-guide/configuration.md@-1450,0 +1540,12` UNMARKED (6 lines): 'goals.notices' (fork setting) defaults to 'true' and is read separately for each profile.
- If you do not want Hermes to auto-generate titles after the first exchange, set
  - `website/docs/user-guide/configuration.md@-1605,0 +1707,6` UNMARKED (3 lines): 'auxiliary.title_generation.min_words' / 'max_words' set the title word budget (defaults 3
- still disables both stages.
  - `website/docs/user-guide/configuration.md@-1611,0 +1719,29` UNMARKED (26 lines): When the opening message contains a link, the model title is informed by that page. Only t
- auxiliary:
  - `website/docs/user-guide/configuration.md@-1764,0 +1901` UNMARKED (1 lines): link_context: true # set false to stop using the first link"s page title/description
- Run `hermes setup --portal` — one OAuth gets you a model provider and all four
  - `website/docs/user-guide/configuration.md@-19 +19,66` UNMARKED (43 lines): ## Incognito Gateway Topics
  - `website/docs/user-guide/configuration.md@-19 +19,66#moved` moved: `CANDIDATE_SLICE2_CONFIG.yaml`, `hermes_cli/config_migrations_delegation_roles.py`
- agent:
  - `website/docs/user-guide/configuration.md@-2153,0 +2291,2` UNMARKED (2 lines): fast_expiry_seconds: 1800 # gateway session fast: 30 minutes, then normal
  - `website/docs/user-guide/configuration.md@-2163 +2302` UNMARKED (1 lines): '/fast normal|fast|ultrafast|auto|cold' switches the mode for the session. Add '--global'
  - `website/docs/user-guide/configuration.md@-2165 +2304` UNMARKED (1 lines): **Cost note:** both providers bill fast requests at a multiplier on standard rates (Anthro
- Differences from `agent.service_tier`: the tier is always on for that provider (
  - `website/docs/user-guide/configuration.md@-2188,0 +2328,21` UNMARKED (7 lines): #### Proxies that forward the vendor"s own fast mode
  - `website/docs/user-guide/configuration.md@-2188,0 +2328,21#moved` moved: `hermes_cli/models_fast_route.py`, `plugin-catalog/0bull.yaml`, `plugin-catalog/0g-provider.yaml`, `plugin-catalog/0xarchive.yaml` (+2607 more)
- web:
  - `website/docs/user-guide/configuration.md@-2984,0 +3145` UNMARKED (1 lines): extract_direct: true # default: fetch plain files and local Hermes docs before the extract
  - `website/docs/user-guide/configuration.md@-2995,0 +3157,4` UNMARKED (3 lines): # Exa Lite pause (default: true). Every Exa search uses full Auto.
  - `website/docs/user-guide/configuration.md@-3012,0 +3178,2` UNMARKED (1 lines): **Direct extraction:** 'web.extract_direct' defaults to 'true'. After URL safety and websi
  - `website/docs/user-guide/configuration.md@-3021 +3188` UNMARKED (1 lines): **Exa:** Set 'EXA_API_KEY' in '~/.hermes/.env'. Supports 'category' filtering ('company',
- browser:
  - `website/docs/user-guide/configuration.md@-3030,0 +3198,7` UNMARKED (7 lines): backend: "" # Native auto routing; browser-use, camofox or off may be selected explicitly
  - `website/docs/user-guide/configuration.md@-3050,0 +3225,5` UNMARKED (4 lines): managed_persistence: false # When true, Camofox sessions persist cookies/logins across res
  - `website/docs/user-guide/configuration.md@-3050,0 +3225,5#moved` moved: `website/docs/user-guide/features/browser.md`
- instead of failing silently.
  - `website/docs/user-guide/configuration.md@-3060 +3239` UNMARKED (1 lines): **Turning browsing off.** Set 'browser.backend: off', or disable the 'browser'
- See the [browser feature page](./features/browser.md#browser_dialog) for the ful
  - `website/docs/user-guide/configuration.md@-3071,9 +3250,8` UNMARKED (7 lines): **Native browser settings.** This fork retains built-in browser and Camofox tools,
- notice, shown the same way, naming the path it pinned so you can check your brow
  - `website/docs/user-guide/configuration.md@-3088,0 +3267,11` UNMARKED (5 lines): **Remembering logins:** Vault password and verification-code fills tick a safe Remember me
  - `website/docs/user-guide/configuration.md@-3088,0 +3267,11#moved` moved: `website/docs/developer-guide/browser-vault-plugin.md`, `website/docs/user-guide/features/credential-vault.md`
- delegation:
  - `website/docs/user-guide/configuration.md@-3262,0 +3452,2` UNMARKED (1 lines): transient_retry_window_seconds: 180 # Subagent/cron/side-agent runs retry transient provid
  - `website/docs/user-guide/configuration.md@-3262,0 +3452,2#moved` moved: `website/docs/user-guide/features/fallback-providers.md`
  - `website/docs/user-guide/configuration.md@-3268,0 +3460,31` UNMARKED (9 lines): ### Named Delegation Routes
  - `website/docs/user-guide/configuration.md@-3268,0 +3460,31#moved` moved: `.github/ISSUE_TEMPLATE/bug_report.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/ISSUE_TEMPLATE/setup_help.yml`, `CANDIDATE_SLICE2_CONFIG.yaml` (+13 more)
  - `website/docs/user-guide/configuration.md@-3270,0 +3493,4` UNMARKED (2 lines): **Billing fallback for pinned unattended runs:** Set 'delegation.allow_billing_fallback: t
- terminal:
  - `website/docs/user-guide/configuration.md@-430,0 +496` UNMARKED (1 lines): docker_auto_mount_host_state: true # Read-only credentials, skills and cache mounts
- Parallel subagents spawned via `delegate_task(tasks=[...])` share this one conta
  - `website/docs/user-guide/configuration.md@-510,0 +577,4` UNMARKED (2 lines): **Automatic host-state mounts:** 'docker_auto_mount_host_state' defaults to 'true'. Set it
- Every key under `terminal:` has an env-var override of the form `TERMINAL_<KEY_U
  - `website/docs/user-guide/configuration.md@-522,0 +593` UNMARKED (1 lines): | 'TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE' | 'docker_auto_mount_host_state' | 'true' / 'fal
- Every terminal command on the **local** and **SSH** backends runs in a fresh `ba
  - `website/docs/user-guide/configuration.md@-851 +922` UNMARKED (1 lines): | Profile functions / aliases | always present | always present |

### `website/docs/user-guide/features/credential-vault.md`

- reaches a sign-in page it has no login for, it asks you, right there, in a
  - `website/docs/user-guide/features/credential-vault.md@-10,2 +10,4` UNMARKED (4 lines): masked prompt. After that it just works. On the vault-fill path, passwords are
- with the same approval prompt as a dangerous command; declining writes nothing.
  - `website/docs/user-guide/features/credential-vault.md@-111,3 +150,6` UNMARKED (6 lines): Headless vault fills (cron, webhooks, the API server) cannot confirm and are
- Items live encrypted under `~/.hermes/vault/`, scoped to the profile. Labels,
  - `website/docs/user-guide/features/credential-vault.md@-123,2 +165,4` UNMARKED (4 lines): site origins and login identifiers are visible metadata. On the vault-fill path,
- That is why keychain storage is opt-in rather than the default.
  - `website/docs/user-guide/features/credential-vault.md@-176,0 +221,64` UNMARKED (50 lines): ### Protected fields from 1Password
- vault:
  - `website/docs/user-guide/features/credential-vault.md@-194,0 +303,22` UNMARKED (14 lines): ### Several 1Password accounts
  - `website/docs/user-guide/features/credential-vault.md@-197,2 +327,2` UNMARKED (2 lines): **Vault fills:** the filled password does not enter the model"s context through
- refused unless the page origin exactly matches the saved origin, checked again
  - `website/docs/user-guide/features/credential-vault.md@-201,6 +331,4` UNMARKED (4 lines): inside the page immediately before the write. Native Camofox uses its protected
- against a compromised right one.
  - `website/docs/user-guide/features/credential-vault.md@-213,0 +342,6` UNMARKED (5 lines): Nor does it isolate the browser from this machine. Masking and screenshot refusal
- title: Passwords & Logins
  - `website/docs/user-guide/features/credential-vault.md@-3 +3` UNMARKED (1 lines): description: Vault-first sign-in, card and address entry, with authorized 1Password fallba
- registered with the redactor so a later page read cannot echo it back.
  - `website/docs/user-guide/features/credential-vault.md@-36,0 +39,16` UNMARKED (15 lines): ### Protected date fields
- CLI through its non-interactive channel (`op signin` on stdin, `bw unlock
  - `website/docs/user-guide/features/credential-vault.md@-78 +96,2` UNMARKED (2 lines): memory. This vault path does not expose the master password, token, or login
- or the switch in **Settings → Passwords & Logins**.
  - `website/docs/user-guide/features/credential-vault.md@-85,0 +105,20` UNMARKED (18 lines): ## Authorized direct entry

### `website/docs/user-guide/features/plugins.md`

- Plugins can register the 27 lifecycle events currently accepted by `hermes_cli.p
  - `website/docs/user-guide/features/plugins.md@-359 +359` UNMARKED (1 lines): | **Observer** | 'post_tool_call', 'post_steer_delivered', 'post_llm_call', 'pre_api_reque
