"""Gateway slash commands that switch or tune the model route:
/model, /codex-runtime, /reasoning, /fast, /personality.

Split out of ``gateway/slash_commands.py``; bound onto ``GatewayRunner`` through
``GatewaySlashCommandsMixin``. Origin internals are imported lazily inside the bodies to avoid
the import cycle.
"""

from __future__ import annotations

from agent.notice_style import ContextNotice

import asyncio
import contextlib
import dataclasses
import logging
import time
from typing import Any, Optional, cast

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from hermes_cli.config import atomic_config_write
from hermes_cli.model_presets import preset_ref_name
from utils import base_url_host_matches

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

# /fast argument -> (service tier, persisted value, i18n label key; None = value.upper()).
_FAST_SELECTIONS = {
    "fast": ("priority", "fast", "gateway.fast.label_fast"),
    "on": ("priority", "fast", "gateway.fast.label_fast"),
    "normal": (None, "normal", "gateway.fast.label_normal"),
    "off": (None, "normal", "gateway.fast.label_normal"),
    "auto": ("auto", "auto", None),
    "cold": ("cold", "cold", None),
    "ultrafast": ("ultrafast", "ultrafast", None),
}


def _fast_route_supports(model: str, runtime: dict, tier: Optional[str] = None) -> bool:
    """The turn's own gate (``run_turn.py::_resolve_turn_agent_config``): a tier /fast accepts must be
    one the session's next request actually carries, so proxies and other providers are refused."""
    from hermes_cli.models import resolve_fast_mode_overrides

    return resolve_fast_mode_overrides(
        model, provider=runtime.get("provider"), base_url=runtime.get("base_url"), tier=tier) is not None


# /reasoning display-toggle arguments -> show_reasoning value.
_REASONING_DISPLAY_TOGGLES = {"show": True, "on": True, "hide": False, "off": False}


def _model_switch_skew_guard() -> Optional[str]:
    """Refuse a model switch when the gateway is running stale code: a first-time lazy import on
    a new code path can crash on a stale cached dependency. Scoped to the highest-risk trigger."""
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=t("gateway.model.err_skew", boot_rev=boot_rev, disk_rev=disk_rev),
    )


async def _persist_model_switch_to_config(result, config_path) -> None:
    """Write-through a resolved /model switch to the profile config at ``config_path``, off the
    event loop (the route comparison can do cold-start disk I/O)."""
    from hermes_cli.model_switch import persist_model_selection
    await asyncio.to_thread(persist_model_selection, result, config_path)


@dataclasses.dataclass
class _ModelSwitchContext:
    """Everything a /model switch needs beyond the target: current route + persistence policy."""

    session_key: str
    source: Any
    config_path: Any
    persist_global: bool
    one_turn: bool = False
    reasoning_effort: str = ""  # `--reasoning <level>` riding with the pick (typed path only)
    model_override_scope: str = "session"  # session, temporary, or once
    pick_ttl_hours: float = 0.0  # opt-in wall-clock lifetime for a plain pick
    restore_snapshot: Optional[dict] = None
    current_model: str = ""
    current_provider: str = "openrouter"
    # The provider actually configured/overridden (None = unset): ``current_provider`` defaults to
    # openrouter for switch_model, which must not label the configured model in metrics.
    route_provider: Optional[str] = None
    current_base_url: str = ""
    current_api_key: str = ""
    user_provs: Any = None
    custom_provs: Any = None
    excluded_provs: list = dataclasses.field(default_factory=list)

    def read_config(self) -> None:
        """Fill the current route from ``config_path``; fail-open to the defaults."""
        from gateway.run import _load_gateway_config
        try:
            cfg = _load_gateway_config(config_path=self.config_path)
            if not cfg:
                return
            from hermes_cli.model_preset_runtime import resolve_default_route
            route = resolve_default_route(cfg)
            self.current_model = route.model
            self.current_provider = route.provider or self.current_provider
            self.route_provider = route.provider or None
            self.current_base_url = route.base_url
            self.user_provs = cfg.get("providers")
            try:
                from hermes_cli.config import get_compatible_custom_providers
                self.custom_provs = get_compatible_custom_providers(cfg)
            except Exception:
                self.custom_provs = cfg.get("custom_providers")
            excl = cfg.get("model_catalog", {}).get("excluded_providers")
            if isinstance(excl, list):
                self.excluded_provs = excl
        except Exception:
            pass

    def apply_override(self, override: dict) -> None:
        """A session /model override supersedes the configured route."""
        if override:
            self.current_model = override.get("model", self.current_model)
            self.current_provider = override.get("provider", self.current_provider)
            self.route_provider = override.get("provider", self.route_provider)
            self.current_base_url = override.get("base_url", self.current_base_url)
            self.current_api_key = override.get("api_key", self.current_api_key)



_TEXT_LISTING_MODELS = 5


def _model_provider_listing_lines(providers) -> list[str]:
    """Text-list body for ``/model`` with no args on platforms without a picker."""
    lines: list[str] = []
    for p in providers:
        tag = t("gateway.model.current_tag") if p["is_current"] else ""
        lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
        if p["models"]:
            shown = p["models"][:_TEXT_LISTING_MODELS]  # uncapped rows arrive full; this is a preview
            model_strs = ", ".join(f"`{m}`" for m in shown)
            hidden = p["total_models"] - len(shown)
            extra = t("gateway.model.more_models_suffix", count=hidden) if hidden > 0 else ""
            lines.append(f"  {model_strs}{extra}")
        elif p.get("api_url"):
            lines.append(f"  `{p['api_url']}`")
        lines.append("")
    return lines


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime, /reasoning, /fast, /personality)."""

    # ----------------------------------------------------------------- /model

    async def _perform_model_switch(
        self, ctx: _ModelSwitchContext, raw_input: str, explicit_provider, source
    ):
        """Resolve a /model switch off-loop. Returns ``(result, None)`` or ``(None, error_text)``."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import switch_model

        skew_error = _model_switch_skew_guard()
        if skew_error:
            return None, skew_error
        # Off-loop: switch_model() can hit a synchronous models.dev fetch (15s) on a cold cache.
        result = await asyncio.to_thread(
            switch_model, raw_input=raw_input, current_provider=ctx.current_provider,
            current_model=ctx.current_model, current_base_url=ctx.current_base_url,
            current_api_key=ctx.current_api_key, is_global=ctx.persist_global,
            explicit_provider=explicit_provider, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs,
        )
        if not result.success:
            return None, t("gateway.model.error_prefix", error=result.error_message)
        try:
            from hermes_cli.context_switch_guard import enrich_model_switch_warnings_for_gateway
            # Off-loop: merge_preflight_compression_warning() runs the sync provider probe ladder.
            await asyncio.to_thread(
                enrich_model_switch_warnings_for_gateway, result, self, session_key=ctx.session_key,
                source=source, custom_providers=ctx.custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)
        return result, None

    def _switch_cached_agent_model(self, result, ctx: _ModelSwitchContext, picker: bool) -> Optional[str]:
        """In-place swap on the cached agent; returns the error reply when it failed.

        The agent rolls back to the OLD model/client and re-raises; the commit (DB, override,
        eviction, config) is aborted so the next message doesn't rebuild a broken agent.
        """
        cached_agent = self._cached_agent_for(ctx.session_key)
        if cached_agent is None:
            return None
        try:
            cached_agent.switch_model(
                new_model=result.new_model, new_provider=result.target_provider,
                api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode,
                capabilities=getattr(result, "runtime_capabilities", None),
            )
        except Exception as exc:
            logger.warning(
                "%s model switch failed for cached agent: %s", "Picker" if picker else "In-place", exc
            )
            return t(
                "gateway.model.error_prefix",
                error=t("gateway.model.err_switch_failed", model=result.new_model, error=exc, current=ctx.current_model),
            )
        return None

    async def _record_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        """Persist a committed switch: session DB, next-turn note, config write-through, override map.

        Returns the warning for a ``--global`` switch whose ``config.yaml`` write or stale-override
        cleanup failed (the switch then stays a session override), else ``None``.
        """
        async with self._session_model_override_lock(ctx.session_key):
            return await self._record_model_switch_locked(
                result, ctx, source=source, one_turn=one_turn, picker=picker,
            )

    async def _record_model_switch_locked(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        from hermes_cli.model_switch import format_model_for_display

        # Persist the new model to the session DB so the dashboard shows the updated model (#34850).
        _sess_db = getattr(self, "_session_db", None)
        if _sess_db is not None:  # so the dashboard shows the updated model
            try:
                _sess_entry = await self.async_session_store.get_or_create_session(source)
                # Typed path: consume the auto-reset flag so the next message's cleanup does not
                # wipe the override stored below.
                if not picker and getattr(_sess_entry, "was_auto_reset", False):
                    # See #48031.
                    _sess_entry.was_auto_reset = False
                await _sess_db.update_session_model(
                    _sess_entry.session_id, result.new_model, provider=result.target_provider,
                    base_url=result.base_url, api_mode=result.api_mode,
                )
            except Exception as exc:
                logger.debug("Failed to persist model switch to DB: %s", exc)
        # Prepended to the next user message (no system messages mid-history). Display form strips
        # opaque Palantir RID prefixes; the override map keeps the full ID for the wire.
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[ctx.session_key] = (
            f"[Note: model was just switched from {format_model_for_display(ctx.current_model)} to "
            f"{format_model_for_display(result.new_model)} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        self._bump_session_model_override_epoch(ctx.session_key)
        self._session_model_overrides[ctx.session_key] = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        if ctx.pick_ttl_hours and not one_turn:
            self._session_model_overrides[ctx.session_key]["expires_at"] = time.time() + ctx.pick_ttl_hours * 3600
        self._session_state(ctx.session_key).conversation.model_override_scope = (
            "once" if one_turn else getattr(ctx, "model_override_scope", "session")
        )
        if one_turn:
            # A repeated --once before the turn runs must keep the EARLIEST snapshot: the later
            # command's snapshot is the first temporary model, not the user's standing override.
            self._claim_one_turn_restore(ctx.session_key, ctx.restore_snapshot)
        elif not picker and hasattr(self, "_pending_one_turn_model_restores"):
            self._pending_one_turn_model_restores.pop(ctx.session_key, None)
        # A --global switch has ONE durable authority: config.yaml. Write it first; on success drop
        # the session override (memory + store) — a redundant copy would shadow every later global
        # change after a restart (#100314: a stale override resumed `gpt-5.6-sol-900k` as the base
        # 272K model). On failure keep the override so the switch truthfully survives as session-only.
        global_error: Optional[str] = None
        if ctx.persist_global:
            try:
                await _persist_model_switch_to_config(result, ctx.config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        # Precedence is session > channel_overrides > config.yaml: in a chat with a channel_overrides
        # model/provider the session override must stay, or the next turn runs the channel model.
        if ctx.persist_global and global_error is None and self._channel_override_for(source) is None:
            try:
                await self._persist_session_model_override_locked(ctx.session_key, None)
            except Exception as e:
                # Store still holds the stale copy: keep memory in agreement and report it (#100314).
                logger.warning("Failed to clear persisted session model override: %s", e)
                global_error = t("gateway.model.err_stale_override", error=e)
            else:
                self._session_model_overrides.pop(ctx.session_key, None)
        # Non-secret write-through so the override survives a restart (api_key/api_mode are
        # re-resolved on rehydration); a --once override must NOT outlive a restart.
        # Write-through the non-secret parts (model/provider/base_url) to the session store so the override
        # survives a gateway restart. api_key/api_mode are never persisted — they are re-resolved via
        # runtime provider resolution on rehydration. /model --once is intentionally EXCLUDED from the
        # write-through: a one-turn override must never survive a restart. The persisted value stays at the
        # pre-once state (the prior session override, or nothing), which is exactly what the finally-restore
        # reverts the in-memory dict to. (#29923 review defect: the original implementation wrote through,
        # so a crash before the restore rehydrated the once-model permanently.)
        elif not one_turn:
            try:
                await self._persist_session_model_override_locked(
                    ctx.session_key, self._session_model_overrides[ctx.session_key],
                    scope=getattr(ctx, "model_override_scope", "session"),
                )
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        # A durable concrete pick (session or global) replaces a role hold: role outranks the
        # default, so a surviving role would silently defeat the pick. --once keeps it.
        if not one_turn and (ctx.persist_global or getattr(ctx, "model_override_scope", "session") == "session"):
            with contextlib.suppress(Exception):
                await self._set_session_model_role_locked(ctx.session_key, None)
        self._evict_cached_agent(ctx.session_key)  # next turn builds fresh from the override
        return global_error

    async def _model_switch_confirmation(
        self, result, ctx: _ModelSwitchContext, *, one_turn: bool, picker: bool,
        global_error: Optional[str] = None,
    ) -> str:
        """Confirmation text with full metadata (display form shortens opaque Palantir IDs)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import format_model_for_display, resolve_display_context_length_async

        lines = [
            t("gateway.model.switched", model=format_model_for_display(result.new_model)),
            t("gateway.model.provider_label", provider=result.provider_label or result.target_provider),
        ]
        # Provider-aware chain: Codex OAuth, Nous and profile live-catalog caps win over the raw models.dev entry.
        mi = result.model_info
        model_cfg: dict = {}
        config_ctx = None
        configured_route = None
        cfg = _load_gateway_config()
        with contextlib.suppress(Exception):
            model_cfg = cfg.get("model", {})
            from hermes_cli.model_preset_runtime import resolve_default_route
            configured_route = resolve_default_route(cfg)
            if isinstance(model_cfg, dict) and model_cfg.get("context_length") is not None:
                config_ctx = int(model_cfg["context_length"])
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        if configured_route is None:
            from hermes_cli.model_preset_runtime import DefaultRoute
            configured_route = DefaultRoute()
        ctx_len = await resolve_display_context_length_async(
            result.new_model, result.target_provider,
            base_url=result.base_url or ctx.current_base_url or "",
            api_key=result.api_key or ctx.current_api_key or "", model_info=mi, api_mode=result.api_mode or "",
            custom_providers=ctx.custom_provs, config_context_length=config_ctx,
            configured_model=configured_route.model,
            configured_provider=configured_route.provider,
            configured_base_url=configured_route.base_url,
        )
        if ctx_len:
            lines.append(t("gateway.model.context_label", tokens=f"{ctx_len:,}"))
        if mi and mi.max_output:
            lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
        if mi:
            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
        openrouter_claude = (
            base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower()
        )
        if not picker and (openrouter_claude or result.api_mode == "anthropic_messages"):
            lines.append(t("gateway.model.prompt_caching_enabled"))
        if result.warning_message:
            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
        if getattr(result, "usage_notice", ""):
            lines.append(f"⚠️ {result.usage_notice}")
        if ctx.persist_global and global_error is not None:
            # Never claim a clean global commit the disk did not take (#100314).
            lines.append(t("gateway.model.warning_prefix", warning=global_error))
            lines.append(t("gateway.model.session_only_hint"))
        elif ctx.persist_global:
            lines.append(t("gateway.model.saved_global"))
        elif one_turn:
            lines.append(t("gateway.model.next_turn_only"))
        elif ctx.pick_ttl_hours:
            lines.append(t("gateway.model.pick_expires", hours=f"{ctx.pick_ttl_hours:g}"))
        else:
            lines.append(t("gateway.model.session_only_hint"))
        return "\n".join(lines)

    async def _commit_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool = False
    ) -> str:
        """Apply a resolved switch (cached agent, session, config) and build the confirmation; shared
        by the typed path and the picker callback (``picker=True`` never carries --once).

        Entry for the picker / cost-confirm callbacks, which fire outside ``_handle_model_command``
        and take the switch lock themselves; the typed path already holds it."""
        async with self._model_switch_lock():
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=picker)

    def _channel_override_for(self, source):
        """This chat's ``channel_overrides`` entry (model/provider), or None."""
        from gateway.run import _get_channel_override
        cfg = getattr(self, "config", None)
        if not cfg or source is None:
            return None
        return _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )

    def _model_switch_lock(self) -> asyncio.Lock:
        """Runner-wide lock over a /model command's read-resolve-commit. Slash commands bypass the busy
        guard while no agent runs, so a second /model on the same (or another) session otherwise
        interleaves with the first's store/config awaits: two ``--global`` picks left config.yaml on
        whichever thread wrote last and a ``--global`` cleanup wiped a session pick issued after it
        (#100314). Lazy: the runner is built without __init__ in tests."""
        lock = self.__dict__.get("_model_switch_lock_obj")
        if lock is None:
            lock = self.__dict__["_model_switch_lock_obj"] = asyncio.Lock()
        return lock

    def _record_switch_metrics(self, result, ctx: _ModelSwitchContext, source) -> None:
        """Slash dispatch does not install the routed profile's scope, so a multiplexed runner binds
        the owning home for the switch row and its switch_away friction."""
        from hermes_cli.observability.shared_metrics_events import record_model_switch
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with contextlib.suppress(Exception):
                home = self._resolve_profile_home_for_source(source)
        session_id = None
        with contextlib.suppress(Exception):
            session_id = getattr(self._cached_agent_for(ctx.session_key), "session_id", None)
        token = set_hermes_home_override(str(home)) if home else None
        try:
            record_model_switch(
                from_provider=ctx.route_provider, to_provider=result.target_provider, surface="gateway",
                from_model=ctx.current_model, session_id=session_id)
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    async def _commit_model_switch_locked(self, result, ctx: _ModelSwitchContext, *, source, picker: bool) -> str:
        one_turn = False if picker else ctx.one_turn
        from gateway.session_setting_commands import defer_local_model_pick, local_authority_live
        authority, live = local_authority_live(self, source)
        level = ""
        if live is not None:
            # A local session runs its frozen policy: the owner's canonical model mutation is the
            # only commit, landing at the next turn boundary (this command is the running turn).
            if one_turn:
                return t("gateway.model.error_prefix", error=t("gateway.model.err_once_local"))
            from hermes_constants import parse_reasoning_effort
            effort = (ctx.reasoning_effort or "").strip().lower()
            level = effort if parse_reasoning_effort(effort) is not None else ""
            await asyncio.to_thread(defer_local_model_pick, authority, live, result, level)
            self._record_switch_metrics(result, ctx, source)
            global_error = None
            if ctx.persist_global:
                try:
                    await _persist_model_switch_to_config(result, ctx.config_path)
                except Exception as e:
                    logger.warning("Failed to persist model switch", exc_info=True)
                    global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        else:
            error = self._switch_cached_agent_model(result, ctx, picker)
            if error is not None:
                return error
            self._record_switch_metrics(result, ctx, source)
            global_error = await self._record_model_switch(
                result, ctx, source=source, one_turn=one_turn, picker=picker)
        reply = await self._model_switch_confirmation(
            result, ctx, one_turn=one_turn, picker=picker, global_error=global_error,
        )
        if level:
            # Committed with the local pick: only the config write-through and the reply remain.
            saved = (ctx.persist_global and global_error is None
                     and self._save_gateway_config_key("agent.reasoning_effort", level))
            reply += "\n" + t("gateway.reasoning.set_global" if saved else "gateway.reasoning.set_session",
                              effort=level)
        elif ctx.reasoning_effort and not one_turn:
            # `/model X --reasoning <level>`: same applier as /reasoning, same scope as the pick.
            # The record step already evicted the cached agent, so the pin lands on the rebuild.
            from gateway.run import _platform_config_key
            if ctx.model_override_scope == "temporary":
                reply += "\n" + await self._apply_reasoning_selection_off_loop(
                    ctx.session_key, _platform_config_key(source.platform), ctx.reasoning_effort,
                    persist_global=ctx.persist_global and global_error is None, scope="temporary")
            else:
                reply += "\n" + await self._apply_reasoning_selection_off_loop(
                    ctx.session_key, _platform_config_key(source.platform), ctx.reasoning_effort,
                    persist_global=ctx.persist_global and global_error is None)
        reply += ("\n" + t("gateway.model.local_next_turn") if live is not None else "")
        return ContextNotice(reply) if global_error is None else reply

    def _model_switch_context(
        self, source, session_key: str, *, profile_home=None, **fields,
    ) -> _ModelSwitchContext:
        """Context for a model switch on ``session_key``: current route from the owning profile's
        config, with any session override applied (one builder for every switch entry point). The
        persisted override is rehydrated first: after a gateway restart the in-memory map is empty
        until the session's next turn, and resolving against the global route would let a
        model-only switch silently change the session's provider."""
        from gateway.run import _hermes_home
        ctx = _ModelSwitchContext(
            session_key=session_key, source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml", **fields,
        )
        ctx.read_config()
        if session_key:
            self._rehydrate_session_model_override(session_key)
        ctx.apply_override(self._session_model_overrides.get(session_key, {}))
        return ctx

    async def resolve_session_model_selection(
        self, source, model: str, provider: str = "", *, session_key: str = "",
    ):
        """Resolve ``model``/``provider`` the way ``/model`` does against ``session_key``'s current
        route (the configured route when empty), committing nothing. Lets a caller with no slash
        event (the ``telegram_topic`` tool) validate a pick before an irreversible step:
        ``(ctx, result, None)`` or ``(None, None, error_text)``.

        A pick ``/model`` would ask the user to confirm (cost, data-training tier, large-context
        switch) is refused: an agent caller cannot give that confirmation, so the user makes the
        switch with ``/model`` where the confirm prompt is shown."""
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(source)
        ctx = self._model_switch_context(source, session_key, profile_home=profile_home, persist_global=False)
        result, error = await self._perform_model_switch(ctx, model, provider, source)
        if error is not None:
            return None, None, error
        warning = await self._model_selection_warning(ctx, result)
        if warning is not None:
            picked = getattr(result, "new_model", None) or model
            return None, None, (
                f"{warning.title}: {picked} needs the user's confirmation, so it cannot be "
                f"set here. Leave the model unset and have the user run /model {picked} "
                f"in the topic.\n\n{warning.message}")
        return ctx, result, None

    async def commit_session_model_selection(
        self, ctx: _ModelSwitchContext, result, source, *, announce: bool = True,
    ) -> Optional[str]:
        """Commit a resolved pick as the session override of the session ``source`` routes to: the
        ``/model --session`` commit (cached agent, state.db, write-through for restarts) without its
        reply or switch-away metrics. Error text or None.

        This remains the typed ``/model`` contract: a target that started a turn is refused instead
        of swapping its client mid-turn. Agent-facing topic edits use
        :meth:`commit_session_model_selection_with_status`, which records the same override but
        queues a busy target for its next turn."""
        error, _status = await self._commit_session_model_selection(
            ctx, result, source, announce=announce, queue_if_running=False,
        )
        return error

    async def commit_session_model_selection_with_status(
        self, ctx: _ModelSwitchContext, result, source, *, announce: bool = True,
    ) -> tuple[Optional[str], str]:
        """Commit a topic-tool model pick and report whether it applied now or queued for next turn.

        The running agent is never swapped. The durable session override changes immediately, while
        the cache signature forces the next turn to build the selected route after the current turn
        releases its agent."""
        return await self._commit_session_model_selection(
            ctx, result, source, announce=announce, queue_if_running=True,
        )

    async def _commit_session_model_selection(
        self, ctx: _ModelSwitchContext, result, source, *, announce: bool, queue_if_running: bool,
    ) -> tuple[Optional[str], str]:
        ctx = dataclasses.replace(ctx, session_key=self._session_key_for_source(source), source=source)
        async with self._model_switch_lock():
            running = self._is_session_running(ctx.session_key)
            if running and not queue_if_running:
                return ("That session started a turn, so its model cannot change now. "
                        "Retry when it is idle, or use /model there."), "unchanged"
            if not running:
                error = self._switch_cached_agent_model(result, ctx, False)
                if error is not None:
                    return error, "unchanged"
            await self._record_model_switch(result, ctx, source=source, one_turn=False, picker=False)
            if not announce:
                (getattr(self, "_pending_model_notes", None) or {}).pop(ctx.session_key, None)
        return None, "queued" if running else "applied"

    async def _send_model_picker(self, event: MessageEvent, source, adapter, session_key: str, listing_kwargs: dict, on_model_selected) -> bool:
        """Send the interactive /model picker; False when nothing was sent (text fallback). *source*
        is session-key-normalized so the picker's thread metadata lands where the next turn reads."""
        from hermes_cli.model_switch_providers import list_picker_providers
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(
                list_picker_providers, max_models=50, include_moa=True, **listing_kwargs
            )
        except Exception:
            providers = []
        if not providers:
            return False
        result = await adapter.send_model_picker(
            chat_id=source.chat_id, providers=providers,
            current_model=listing_kwargs["current_model"],
            current_provider=listing_kwargs["current_provider"], session_key=session_key,
            on_model_selected=on_model_selected,
            metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
        )
        return bool(result.success)

    async def _model_listing_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home
    ) -> Optional[str]:
        """``/model`` with no args: interactive picker where supported, else the text list."""
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.providers import get_label

        listing_kwargs = dict(
            current_provider=ctx.current_provider, current_base_url=ctx.current_base_url,
            current_model=ctx.current_model, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs, excluded_providers=ctx.excluded_provs,
            # Chat `/model` is a read path: catalogs come from the disk cache and stale ones warm
            # in the background, and only the selected custom endpoint is probed live, so one
            # degraded provider can't stall the reply (#74003). Mirrors the GUI read path.
            non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=True,
        )
        adapter = self._delivery_adapter_for(ctx.source)
        if adapter is not None and getattr(type(adapter), "send_model_picker", None) is not None:
            async def _picker_switch(model_id: str, provider_slug: str) -> str:
                # The picker callback binds the raw event source (pre-normalization).
                result, error = await self._perform_model_switch(ctx, model_id, provider_slug, event.source)
                if error is not None:
                    return error
                return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

            async def _on_model_selected(_chat_id: str, model_id: str, provider_slug: str) -> str:
                if profile_home is None:
                    return await _picker_switch(model_id, provider_slug)
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(profile_home):
                    return await _picker_switch(model_id, provider_slug)

            if await self._send_model_picker(event, ctx.source, adapter, ctx.session_key, listing_kwargs, _on_model_selected):
                return None  # Picker sent — adapter handles the response

        lines = [t("gateway.model.current_label", model=ctx.current_model or t("gateway.shared.unknown_value"),
                   provider=get_label(ctx.current_provider)), ""]
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(list_authenticated_providers, max_models=_TEXT_LISTING_MODELS, **listing_kwargs)
            lines.extend(_model_provider_listing_lines(providers))
        except Exception:
            pass
        lines.append(t("gateway.model.usage_switch_model"))
        lines.append(t("gateway.model.usage_switch_provider"))
        lines.append(t("gateway.model.usage_persist"))
        return "\n".join(lines)

    async def _model_selection_warning(self, ctx: _ModelSwitchContext, result):
        """The selection-guard warning (cost, data-policy, large-context switch) a resolved pick must
        confirm before it applies, or None. One check for ``/model`` and agent-driven selection.

        The unified registry runs off-loop — pricing lookups may hit models.dev on a cache miss."""
        try:
            from hermes_cli.model_selection_guards import (
                combined_selection_warning, selection_context_for_agent)
            return await asyncio.to_thread(
                combined_selection_warning, result.new_model, provider=result.target_provider,
                base_url=result.base_url or ctx.current_base_url or "",
                api_key=result.api_key or ctx.current_api_key or "", model_info=result.model_info,
                selection_context=selection_context_for_agent(self._cached_agent_for(ctx.session_key)),
            )
        except Exception:
            return None

    async def _model_selection_guard_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, result, *, commit=None,
    ) -> tuple[bool, Optional[str]]:
        """Selection-guard confirmation for the typed path (pickers confirm via their own UI).
        Returns ``(fired, reply)``; the reply is None when the platform rendered confirm buttons itself.
        """
        warning = await self._model_selection_warning(ctx, result)
        if warning is None:
            return False, None

        async def _on_cost_confirm(choice: str) -> str:
            if choice == "cancel":
                return t("gateway.model.switch_cancelled", model=ctx.current_model or t("gateway.shared.unknown_value"))
            # "once" and "always" both proceed — selection guards have no persistent opt-out.
            if commit is not None:
                return await commit()
            return await self._commit_model_switch(result, ctx, source=ctx.source)

        _p = self._typed_command_prefix_for(event.source.platform)
        message = t("gateway.model.guard_confirm", title=warning.title, message=warning.message, prefix=_p)
        return True, await self._request_slash_confirm(
            event=event, command="model", title=warning.title, message=message, handler=_on_cost_confirm,
        )

    def _model_reset_all_is_authorized(self, source) -> bool:
        """Require an explicit gateway admin; without slash gating, use the configured DM owner list."""
        from gateway.slash_access import policy_for_runner_source

        policy = policy_for_runner_source(self, source)
        if policy.enabled:
            return bool(policy.is_admin(getattr(source, "user_id", None)))
        chat_type = str(getattr(source, "chat_type", "") or "").lower()
        if chat_type not in {"dm", "direct", "private"}:
            return False
        # A restricted env owner grant requires principal membership. General inbound
        # authorization also accepts roles, relays, bots and pairing, which are not owner grants.
        from gateway.authz_mixin import (
            GatewayAuthorizationMixin, _auth_env, _coerce_allow_set, _env_truthy, authz_field,
            _principal_matches_allowlist, _registry_entry,
        )

        platform = getattr(source, "platform", None)
        entry = _registry_entry(platform)
        platform_name = getattr(platform, "value", "")
        allow_env = authz_field(platform_name, "allowed_users_env", entry) or ""
        allow_all_env = authz_field(platform_name, "allow_all_env", entry) or ""
        platform_allowed = _coerce_allow_set(_auth_env(allow_env))
        configured = platform_allowed | _coerce_allow_set(_auth_env("GATEWAY_ALLOWED_USERS"))
        user_id = getattr(source, "user_id", None)
        if (
            configured and "*" not in configured and not _env_truthy(allow_all_env)
            and user_id and not getattr(source, "is_bot", False)
        ):
            if platform_allowed:
                configured |= cast(GatewayAuthorizationMixin, self)._adapter_resolved_allowlist_ids(source)
            if _principal_matches_allowlist(source, user_id, configured):
                return True
        # Preserve the explicit adapter allow_from fallback, including its legacy wildcard.
        # Keep these adapter attributes local rather than widening shared inbound authorization.
        adapter = self._delivery_adapter_for(source)
        extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
        raw_allow = extra.get("allow_from")
        if raw_allow is None:
            for attr in ("_allow_from", "allow_from", "_allowed_user_ids", "allowed_user_ids"):
                with contextlib.suppress(Exception):
                    raw_allow = getattr(adapter, attr, None)
                    if raw_allow is not None:
                        break
        if raw_allow is None:
            return False
        if isinstance(raw_allow, (list, tuple, set, frozenset)):
            allowed = {str(item).strip() for item in raw_allow if str(item).strip()}
        else:
            allowed = {item.strip() for item in str(raw_allow).split(",") if item.strip()}
        normalize = getattr(adapter, "normalize_user_id", None)
        if callable(normalize):
            allowed = {normalize(item) or item for item in allowed}
        user_id = getattr(source, "user_id", "")
        return "*" in allowed or user_id in allowed

    async def _set_session_model_role(self, session_key: str, role: Optional[str]) -> None:
        async with self._session_model_override_lock(session_key):
            await self._set_session_model_role_locked(session_key, role)

    async def _set_session_model_role_locked(self, session_key: str, role: Optional[str]) -> None:
        store = getattr(self, "session_store", None)
        if store is not None:
            await self._persist_model_route_edit_locked(
                [session_key],
                asyncio.to_thread(store.set_model_role, session_key, role, clear_overrides=bool(role)),
                lambda key: self._publish_role_edit(key, role),
            )
        else:
            self._publish_role_edit(session_key, role)

    def _publish_role_edit(self, session_key: str, role: Optional[str]) -> None:
        self._pending_one_turn_model_restores.pop(session_key, None)
        # Running turns retain their assignment snapshot. The next turn sees this state.
        self._apply_role_memory(session_key, role)

    async def _migrate_telegram_model_overrides_to_role(self, role: str) -> tuple[list[str], list[str]]:
        """Convert concrete model overrides on existing Telegram sessions in the live store.

        This is deliberately not a snapshot/database scan. The store is the authority for
        the current routing keys. Eligible keys are converted in one routing snapshot and
        persist; in-memory state is then updated per session. A store without the batch
        writer falls back to the per-session setter.
        """
        converted: list[str] = []
        failures: list[str] = []
        store = getattr(self, "session_store", None)
        if store is None:
            return [], ["session store unavailable"]
        try:
            entries = await asyncio.to_thread(store.list_sessions)
        except Exception as exc:
            logger.warning("Failed to read role migration sessions", exc_info=True)
            return [], [str(exc)]
        def eligible_keys(entries):
            keys = []
            for entry in entries:
                if str(getattr(getattr(entry, "platform", None), "value", "")) != "telegram":
                    continue
                if getattr(entry, "model_role", None):
                    continue
                override = getattr(entry, "model_override", None) or {}
                model = str(override.get("model") or "").strip()
                if model and not preset_ref_name(model):
                    keys.append(entry.session_key)
            return keys

        eligible = eligible_keys(entries)
        batch = getattr(store, "set_model_roles", None)
        if callable(batch) and eligible:
            try:
                async with contextlib.AsyncExitStack() as locks:
                    for key in sorted(eligible):
                        await locks.enter_async_context(self._session_model_override_lock(key))
                    # A role edit may have committed while this batch waited for a lock.
                    current = set(eligible_keys(await asyncio.to_thread(store.list_sessions)))
                    eligible = [key for key in eligible if key in current]
                    if not eligible:
                        return [], []
                    await self._persist_model_route_edit_locked(
                        eligible, asyncio.to_thread(batch, eligible, role, clear_overrides=True),
                        lambda key: self._publish_role_edit(key, role),
                    )
            except Exception as exc:
                logger.warning("Failed to persist role migration snapshot", exc_info=True)
                return [], [str(exc)]
            converted = list(eligible)
            return converted, failures
        for session_key in eligible:
            try:
                await self._set_session_model_role(session_key, role)
                converted.append(session_key)
            except Exception as exc:
                logger.warning("Failed to persist role migration for %s", session_key, exc_info=True)
                failures.append(f"{session_key}: {exc}")
        return converted, failures

    def _apply_role_memory(self, session_key: str, role: Optional[str]) -> None:
        state = self._session_state(session_key)
        state.conversation.model_role = role
        if role:
            state.conversation.model_override = None
            state.conversation.reasoning_override = None
            self._session_model_overrides.pop(session_key, None)
            self._session_reasoning_overrides.pop(session_key, None)
        self._evict_cached_agent(session_key)

    async def _reset_all_session_overrides(self) -> int:
        """Clear persisted and in-memory model/reasoning picks without changing a live turn's route."""
        keys: set[str] = set()
        store = getattr(self, "session_store", None)
        if store is not None:
            with contextlib.suppress(Exception):
                keys.update(await asyncio.to_thread(store.override_session_keys))
            with contextlib.suppress(Exception):
                keys.update(await asyncio.to_thread(store.role_session_keys))
        for mapping_name in ("_session_model_overrides", "_session_reasoning_overrides"):
            with contextlib.suppress(Exception):
                keys.update(getattr(self, mapping_name, {}).keys())
        running = set((getattr(self, "_running_agents", {}) or {}).keys())
        pending = getattr(self, "_bulk_reset_pending_sessions", None)
        if not isinstance(pending, set):
            pending = self._bulk_reset_pending_sessions = set()
        for session_key in sorted(keys):
            if session_key in running:
                pending.add(session_key)
            await self._persist_session_model_override(session_key, None, scope="session")
            if store is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(store.set_model_role, session_key, None)
            if session_key in running:
                # Clear the persisted pick without touching the in-flight agent's reasoning.
                self._set_session_reasoning_override(session_key, None, scope="session")
            else:
                self._set_reasoning_override(session_key, None, scope="session")
            with contextlib.suppress(Exception):
                state = self._peek_session_state(session_key)
                if state is not None:
                    state.conversation.model_override = None
                    state.conversation.model_override_scope = "session"
                    state.conversation.reasoning_override = None
                    state.conversation.reasoning_override_scope = "session"
                    state.conversation.model_role = None
            if session_key not in running:
                self._evict_cached_agent(session_key)
        return len(keys)

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model. Taken under the switch lock BEFORE the first await so
        concurrent commands commit in issue order (see ``_model_switch_lock``)."""
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_role_command(self, event, request, source, session_key, profile_home):
        from gateway.run import _load_gateway_config
        from hermes_cli.model_preset_runtime import resolve_model_ref, ModelPresetError
        role = request.target.strip()[1:].strip().lower()
        if (request.is_global or request.is_session or request.is_once
                or request.explicit_provider or request.reasoning_effort):
            return t("gateway.model.role_flags_unsupported")
        if not role:
            return t("gateway.model.role_empty")
        try:
            # Validate against the config of the profile that owns this session.
            route = resolve_model_ref("@" + role, _load_gateway_config() if profile_home is None
                                      else _load_gateway_config(profile_home / "config.yaml"))
        except ModelPresetError as exc:
            return f"❌ {exc}"

        async def commit_role():
            await self._set_session_model_role(session_key, role)
            return ContextNotice(t("gateway.model.role_set", role=role))

        if route.model:
            ctx = self._model_switch_context(source, session_key, profile_home=profile_home,
                                             persist_global=False)
            result, error = await self._perform_model_switch(ctx, route.model, route.provider, source)
            if error is not None:
                return error
            fired, reply = await self._model_selection_guard_reply(event, ctx, result, commit=commit_role)
            if fired:
                return reply
        return await commit_role()

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior
        from gateway.attended_model import parse_attended_config
        from gateway.run import _load_gateway_config

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        request = parse_model_switch_args(event.get_command_args().strip())  # single-owner parser
        if request.errors:
            return f"❌ {request.error_messages()[0]}"  # gateway decoration over canonical copy
        if request.force_refresh:  # bust the disk cache so the picker shows live data
            with contextlib.suppress(Exception):
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
        # Normalize like a message turn (Telegram DM topic recovery) before deriving the override
        # key, so the override lands under the key the next turn reads.
        # Check for session override. See #30479.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        if request.is_all:
            if (
                request.target.strip().lower() not in {"reset", "attended"}
                or request.explicit_provider
                or request.reasoning_effort
                or request.is_global
                or request.is_session
                or request.is_once
            ):
                return t("gateway.model.reset_all_usage")
            if not self._model_reset_all_is_authorized(source):
                return t("gateway.model.reset_all_admin_only")
            if request.target.strip().lower() == "attended":
                converted, failures = await self._migrate_telegram_model_overrides_to_role("attended")
                detail = ""
                if failures:
                    detail = " (" + "; ".join(failures[:3]) + ("; …" if len(failures) > 3 else "") + ")"
                return t("gateway.model.attended_migrated", converted=len(converted),
                         failures=len(failures), detail=detail)
            count = await self._reset_all_session_overrides()
            return ContextNotice(t("gateway.model.reset_all_done", count=count))
        if request.target.strip() == "@" or preset_ref_name(request.target):
            return await self._handle_model_role_command(event, request, source, session_key, profile_home)

        from gateway.model_pick import model_pick_deadline, model_pick_ttl_hours
        model_config = _load_gateway_config()
        attended_enabled = parse_attended_config(model_config).enabled
        pick_ttl = model_pick_ttl_hours(model_config)
        conversation = self._session_state(session_key).conversation
        role_set = bool(getattr(conversation, "model_role", None))
        if not role_set:
            store = getattr(self, "session_store", None)
            if store is not None:
                with contextlib.suppress(Exception):
                    role_set = bool(await asyncio.to_thread(store.get_model_role, session_key))
        if request.target.strip().lower() == "reset" and not request.explicit_provider:
            await self._rehydrate_session_model_override_async(session_key)
        if request.target.strip().lower() == "reset" and not request.explicit_provider and (
            attended_enabled or role_set or pick_ttl or model_pick_deadline(conversation.model_override)
        ):
            async with self._session_model_override_lock(session_key):
                await self._persist_session_model_override_locked(session_key, None)
                await self._set_session_model_role_locked(session_key, None)
            self._session_model_overrides.pop(session_key, None)
            conversation.model_override_scope = "session"
            self._evict_cached_agent(session_key)
            return ContextNotice(t("gateway.model.reset_done"))
        requested_global = request.is_global
        effective_global = resolve_persist_behavior(
            request.is_global, request.is_session, is_once=request.is_once,
            explicit_provider=request.explicit_provider,
        )
        if (attended_enabled or pick_ttl) and not request.is_global and not request.is_session and not request.is_once:
            effective_global = False
        model_scope = (
            "once" if request.is_once else
            "session" if request.is_session or requested_global or effective_global else
            "temporary" if attended_enabled or pick_ttl else "session"
        )
        ctx = self._model_switch_context(
            source, session_key, profile_home=profile_home,
            persist_global=effective_global,
            one_turn=request.is_once,
            reasoning_effort=request.reasoning_effort,
            model_override_scope=model_scope,
            pick_ttl_hours=pick_ttl if model_scope == "temporary" else 0.0,
            restore_snapshot=self._snapshot_session_model_override(session_key) if request.is_once else None,
        )
        if not request.target and not request.explicit_provider:
            return await self._model_listing_reply(event, ctx, profile_home)
        result, error = await self._perform_model_switch(ctx, request.target, request.explicit_provider, source)
        if error is not None:
            return error
        guard_fired, guard_reply = await self._model_selection_guard_reply(event, ctx, result)
        if guard_fired:
            return guard_reply
        return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)

    # -------------------------------------------------- usage-window limit: one-tap switch

    def _schedule_usage_limit_offer(self, event: MessageEvent, limit: dict, *, session_key: str = "",
                                    generation: Optional[int] = None) -> None:
        """After a usage-window refusal's reply: offer the plan's recommended model as a button where the
        platform already has quick-choice buttons, else one line of text.

        Runs as the adapter's post-delivery callback, so the offer always lands under the reply (the reply
        is sent after this turn returns). The tap switches this session only (never the default) and
        re-sends the refused message, like /retry. No credits button in a chat: anyone in the room could
        spend the owner's credits, so the Portal is named instead. Hermes never switches on its own."""
        try:
            adapter = self._delivery_adapter_for(event.source) if event.source is not None else None
            register = getattr(adapter, "register_post_delivery_callback", None)
            if session_key and callable(register):
                register(session_key, lambda: self._offer_usage_limit_switch(event, limit, generation),
                         generation=generation)
                return
            self._track_background_task(self._offer_usage_limit_switch(event, limit, generation))
        except Exception:
            logger.debug("usage-limit offer scheduling failed", exc_info=True)

    async def _offer_usage_limit_switch(self, event: MessageEvent, limit: dict,
                                        generation: Optional[int] = None) -> None:
        from agent.plan_limits import limit_next_steps_from_payload
        try:
            suggestions = [s for s in (limit.get("suggestions") or []) if isinstance(s, dict) and s.get("model")]
            source = event.source
            if source is None:
                return
            prefix = self._typed_command_prefix_for(source.platform)
            adapter = self._delivery_adapter_for(source)
            session_key = self._session_key_for_source(source)
            if suggestions and adapter is not None and getattr(type(adapter), "send_choice_picker", None) is not None:
                async def _on_choice(_chat_id: str, value: str) -> str:
                    if generation is not None and self._usage_limit_offer_is_stale(session_key, generation):
                        # A newer turn ran since the refusal: /retry would re-run that one, not the refused one.
                        return t("platform.discord.picker.choice_expired")
                    return await self._accept_usage_limit_switch(
                        event, value, offer=None if generation is None else (session_key, generation))

                choices = [{"value": str(s["model"]),
                            "label": t("gateway.usage_limit.switch_choice", model=str(s.get("label") or s["model"]),
                                       note=str(s.get("note") or "")),
                            "is_current": False} for s in suggestions]
                if await self._try_send_choice_picker(
                        event, session_key, title=t("gateway.usage_limit.picker_title"), choices=choices,
                        on_choice_selected=_on_choice):
                    return
            text = limit_next_steps_from_payload(limit, model_command=f"{prefix}model", topup_command=None, portal=True)
            if adapter is not None and text:
                await adapter.send(source.chat_id, text, metadata=self._reply_metadata(event))
        except Exception:
            logger.debug("usage-limit offer failed", exc_info=True)

    def _usage_limit_offer_is_stale(self, session_key: str, generation: int) -> bool:
        """True once the session has started a newer run than the refused one. Fails open (not stale)."""
        try:
            return int(self._session_state(session_key).persistent.run_generation) != int(generation)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _accept_usage_limit_switch(self, event: MessageEvent, model_id: str, *,
                                         offer: Optional[tuple[str, int]] = None) -> str:
        """The tap: switch this session to *model_id* on Nous, then re-send the refused message.
        ``offer`` is the refused turn's ``(session_key, run_generation)``."""
        from gateway.run import _hermes_home
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        ctx = _ModelSwitchContext(session_key=session_key, source=source,
                                  config_path=(profile_home or _hermes_home) / "config.yaml", persist_global=False)

        async def _switch() -> str:
            ctx.read_config()
            ctx.apply_override(self._session_model_overrides.get(session_key, {}))
            result, error = await self._perform_model_switch(ctx, model_id, "nous", event.source)
            if error is not None:
                return error
            return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

        before = self._session_model_overrides.get(session_key)
        if profile_home is None:
            reply = await _switch()
        else:
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                reply = await _switch()
        after = self._session_model_overrides.get(session_key)
        switched = after is not None and after is not before and after.get("provider") == "nous"
        if switched:
            # Only after the switch landed (an error reply leaves the override unchanged). Exactly a typed /retry from the same sender (rewinds the refused turn, runs the message
            # again, and the adapter delivers the reply). Called directly, the re-run's reply would be
            # returned to nobody.
            self._track_background_task(self._dispatch_usage_limit_retry(event, offer))
        return reply

    async def _dispatch_usage_limit_retry(self, event: MessageEvent, offer: Optional[tuple[str, int]] = None) -> None:
        from gateway.platforms.event import MessageType
        adapter = self._delivery_adapter_for(event.source)
        # Re-checked here, with no await before the /retry: the switch awaited model resolution, and a
        # turn that ran meanwhile is what /retry would rewind. The switch itself stands.
        if adapter is None or (offer is not None and self._usage_limit_offer_is_stale(*offer)):
            return
        try:
            await adapter.handle_message(MessageEvent(
                text="/retry", message_type=MessageType.TEXT, source=event.source,
                raw_message=event.raw_message, channel_prompt=event.channel_prompt))
        except Exception:
            logger.debug("usage-limit retry dispatch failed", exc_info=True)

    # -------------------------------------------------- /codex-runtime, /personality

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str:
        """Handle /codex-runtime; a real change evicts the cached agent so the new api_mode applies
        on the next message (avoids prompt-cache invalidation mid-session)."""
        from hermes_cli import codex_runtime_switch as crs

        new_value, errors = crs.parse_args(event.get_command_args().strip() if event else "")
        if errors:
            return "❌ " + "\n❌ ".join(errors)
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return t("gateway.codex_runtime.config_load_failed", error=exc)
        result = crs.apply(
            load_config(), new_value, persist_callback=(save_config if new_value is not None else None),
        )
        if result.success and new_value is not None and result.requires_new_session:
            try:
                self._evict_cached_agent(self._session_key_for_source(event.source))
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change", exc_info=True)
        reply = f"{'✓' if result.success else '✗'} {result.message}"
        return ContextNotice(reply) if result.success and new_value is not None else reply

    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """Handle /personality — list or set a personality (hermes_cli.personality owns the state)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.personality import (
            active_personality_name,
            available_personalities,
            describe_personality,
            persist_personality,
            resolve_personality,
        )

        args = event.get_command_args().strip()
        try:
            config = _load_gateway_config()
        except Exception:
            config = {}
        personalities = available_personalities(config)
        if not args:
            current = active_personality_name(config)
            lines = [t("gateway.personality.header"), t("gateway.personality.none_option")]
            for name, prompt in personalities.items():
                marker = " ✓" if name == current else ""
                lines.append(
                    t("gateway.personality.item", name=f"{name}{marker}", preview=describe_personality(prompt))
                )
            lines.append(t("gateway.personality.usage"))
            return "\n".join(lines)
        try:
            name, _new_prompt = resolve_personality(args, config)
        except ValueError:
            available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
            return t("gateway.personality.unknown", name=args.lower(), available=available)
        # Persists the selection only (never agent.system_prompt, a user-owned overlay) into the
        # routed profile's config.yaml; the next turn re-resolves the prompt — no process-global state.
        if not persist_personality(name):
            return t("gateway.personality.save_failed", error=t("gateway.personality.err_config_write"))
        if not name:
            return ContextNotice(t("gateway.personality.cleared"))
        return ContextNotice(t("gateway.personality.set_to", name=name))

    # ----------------------------------------------------------- /reasoning, /fast

    def _save_gateway_config_key(self, key_path: str, value) -> bool:
        """Save a dot-separated key to config.yaml (shared by /reasoning, /fast and their pickers)."""
        from gateway.slash_commands import _nested_dict
        from gateway.run import _gateway_config_home
        from hermes_cli.config import read_user_config_raw
        config_path = _gateway_config_home() / "config.yaml"
        try:
            user_config = read_user_config_raw(config_path)  # raw: never persist merged defaults
            *parents, leaf = key_path.split(".")
            _nested_dict(user_config, *parents)[leaf] = value
            atomic_config_write(config_path, user_config)
            return True
        except Exception as e:
            logger.error("Failed to save config key %s: %s", key_path, e)
            return False

    def _live_agent_for_session_control(self, session_key: str):
        """Return this session's running agent without touching its cached prompt or transcript.

        A turn claimed but not yet built holds the pending sentinel; it has no request state, so
        it counts as no live agent (the idle cached agent is evicted instead)."""
        from gateway.run import _AGENT_PENDING_SENTINEL

        agent = (getattr(self, "_running_agents", None) or {}).get(session_key)
        return None if agent is _AGENT_PENDING_SENTINEL else agent

    def _evict_idle_agent_after_session_control(self, session_key: str) -> None:
        if self._live_agent_for_session_control(session_key) is None:
            self._evict_cached_agent(session_key)

    def _set_reasoning_override(self, session_key: str, value, source=None, *, scope: Optional[str] = None) -> None:
        """Apply a session override to the live tool loop's next model request."""
        self._set_session_reasoning_override(session_key, value, scope=scope)
        agent = self._live_agent_for_session_control(session_key)
        if agent is not None:
            effective = value if value is not None else self._resolve_session_reasoning_config(
                source=source, session_key=session_key, model=getattr(agent, "model", ""),
            )
            agent.reasoning_config = dict(effective) if isinstance(effective, dict) else effective
            agent.reasoning_override = None if value is None else dict(value)
            agent._pre_fallback_reasoning_override = None  # supersedes a pick set aside by fallback
        else:
            self._evict_idle_agent_after_session_control(session_key)

    async def _apply_reasoning_selection_off_loop(self, session_key: str, *args, **kwargs) -> str:
        """Apply /reasoning off-loop while preserving the existing session-state write order."""
        if session_key:
            self._session_state(session_key)
        return await asyncio.to_thread(self._apply_reasoning_selection, session_key, *args, **kwargs)

    def _apply_reasoning_selection(
        self, session_key: str, platform_key: str, value: str, persist_global: bool = False,
        scope: Optional[str] = None,
    ) -> str:
        """Apply a /reasoning argument (typed or picked) and return the reply."""
        from hermes_constants import parse_reasoning_effort

        value = (value or "").strip().lower()
        from gateway.session_setting_commands import local_live_for_route
        live = local_live_for_route(self, session_key)
        show = _REASONING_DISPLAY_TOGGLES.get(value)
        if show is not None:  # per-platform display toggle
            self._show_reasoning = show
            if live is not None:
                live.show_reasoning = show  # what this local session's viewers stream (config.get show_reasoning)
            self._save_gateway_config_key(f"display.platforms.{platform_key}.show_reasoning", show)
            key = "gateway.reasoning.display_set_on" if show else "gateway.reasoning.display_set_off"
            return ContextNotice(t(key, platform=platform_key))
        if value == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            self._set_reasoning_override(session_key, None)
            self._reasoning_config = self._load_reasoning_config()
            return ContextNotice(t("gateway.reasoning.reset_done"))

        parsed = parse_reasoning_effort(value)
        if parsed is None:
            return t("gateway.reasoning.unknown_arg", arg=value)
        self._reasoning_config = parsed
        if persist_global:
            if self._save_gateway_config_key("agent.reasoning_effort", value):
                # A local session runs its frozen level, not config.yaml's: it takes the level too.
                self._set_reasoning_override(session_key, parsed if live is not None else None)
                reply = t("gateway.reasoning.set_global", effort=value)
                from gateway.run import _load_gateway_config
                from hermes_cli.model_preset_runtime import resolve_default_route
                try:
                    route = resolve_default_route(_load_gateway_config(config_path=self.config_path))
                except Exception:
                    route = None
                if route and route.name and route.reasoning_effort:
                    reply += "\n" + t(
                        "gateway.reasoning.preset_owns_effort",
                        preset="@" + route.name, preset_effort=route.reasoning_effort, effort=value,
                    )
                return ContextNotice(reply)
            self._set_reasoning_override(session_key, parsed)
            return t("gateway.reasoning.set_global_save_failed", effort=value)
        self._set_reasoning_override(session_key, parsed, scope=scope)
        return ContextNotice(t("gateway.reasoning.set_session", effort=value))

    async def _try_send_choice_picker(
        self, event: MessageEvent, session_key: str, title: str, choices: list, on_choice_selected,
    ) -> bool:
        """Send an interactive choice picker when the adapter *type* supports it (the /model gate);
        a failed send returns False (text fallback) instead of erroring."""
        adapter = self._delivery_adapter_for(event.source)
        if adapter is None or getattr(type(adapter), "send_choice_picker", None) is None:
            return False
        try:
            result = await adapter.send_choice_picker(
                chat_id=event.source.chat_id, title=title, choices=choices, session_key=session_key,
                on_choice_selected=on_choice_selected, metadata=self._reply_metadata(event),
            )
            return bool(getattr(result, "success", False))
        except Exception as e:
            logger.warning("send_choice_picker failed, falling back to text: %s", e)
            return False

    async def _handle_reasoning_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reasoning command — manage reasoning effort and display toggle."""
        from gateway.run import _platform_config_key
        from hermes_constants import VALID_REASONING_EFFORTS

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        reasoning_session_scope = "--session" in raw_args.replace("—", "--").split()
        attended_enabled = False
        with contextlib.suppress(Exception):
            from gateway.attended_model import parse_attended_config
            from gateway.run import _load_gateway_config
            attended_enabled = parse_attended_config(_load_gateway_config()).enabled

        # See #30479.
        _reasoning_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        from gateway.session_setting_commands import local_live
        if (_live := local_live(self, event.source)) is not None:
            self._show_reasoning = getattr(_live, "show_reasoning", None) is True
        # Effective model (session /model override wins) so per-model reasoning_overrides display.
        _session_model = str(
            ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}).get("model") or ""
        )
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source, session_key=session_key, model=_session_model,
        )
        platform_key = _platform_config_key(event.source.platform)
        if raw_args:  # typed path — same applier the picker uses
            return await self._apply_reasoning_selection_off_loop(
                session_key, platform_key, args, persist_global=persist_global,
                scope="session" if reasoning_session_scope else ("temporary" if attended_enabled else None))
        rc = self._reasoning_config
        # Labels tell the truth about the route: a Hermes-internal step (``ultra``) that the wire
        # clamps is shown as "ultra (sends max on this route)" instead of a distinct level (#61634).
        from agent.reasoning_effort import effort_display_label
        from gateway.run import _load_gateway_config
        from hermes_cli.codex_runtime_switch import get_current_runtime
        _session_route = ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {})
        from hermes_cli.model_preset_runtime import DefaultRoute, resolve_default_route
        _default_route = None
        with contextlib.suppress(Exception):
            _default_route = resolve_default_route(_load_gateway_config(config_path=self.config_path))
        _default_route = _default_route or DefaultRoute()
        _route = (
            _session_route.get("provider") or _default_route.provider,
            _session_model or _default_route.model,
            get_current_runtime({"model": {"provider": _default_route.provider, "default": _default_route.model}}),
        )
        if rc is None:
            level, current_effort = t("gateway.reasoning.level_default"), "medium"
        elif rc.get("enabled") is False:
            level, current_effort = t("gateway.reasoning.level_disabled"), "none"
        else:
            current_effort = rc.get("effort", "medium")
            level = effort_display_label(current_effort, *_route)
        display_state = t("gateway.reasoning.display_on") if self._show_reasoning else t("gateway.reasoning.display_off")
        has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
        scope = t("gateway.reasoning.scope_session") if has_session_override else t("gateway.reasoning.scope_global")

        async def _on_reasoning_choice(_chat_id: str, value: str) -> str:
            return await self._apply_reasoning_selection_off_loop(
                session_key, platform_key, value,
                scope="temporary" if attended_enabled else None,
            )

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.reasoning.picker_title", level=level, scope=scope, display=display_state),
            choices=[
                {"value": "none", "label": t("gateway.reasoning.choice_none"), "is_current": current_effort == "none"},
                *({"value": lv, "label": effort_display_label(lv, *_route), "is_current": lv == current_effort}
                  for lv in VALID_REASONING_EFFORTS),
                *({"value": v, "label": t(f"gateway.reasoning.choice_{v}"), "is_current": False}
                  for v in ("reset", "show", "hide")),
            ],
            on_choice_selected=_on_reasoning_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.reasoning.status", level=level, scope=scope, display=display_state)

    def _apply_live_service_tier(self, session_key: str, tier: str | None) -> None:
        """Change only inference request fields; leave the already-built request untouched."""
        agent = self._live_agent_for_session_control(session_key)
        if agent is None:
            self._evict_idle_agent_after_session_control(session_key)
            return
        from hermes_cli.models import resolve_fast_mode_overrides
        from agent.fast_mode import DEFAULT_WINDOW_SECONDS
        import time

        baseline = getattr(agent, "_gateway_base_request_overrides", None)
        if baseline is None:
            baseline = dict(getattr(agent, "request_overrides", None) or {})
            agent._gateway_base_request_overrides = dict(baseline)
        overrides = dict(baseline)
        fast_overlay: dict = {}
        if tier == "priority":
            fast_overlay = dict(resolve_fast_mode_overrides(
                agent.model, provider=getattr(agent, "provider", None),
                base_url=getattr(agent, "base_url", None),
            ) or {})
            overrides.update(fast_overlay)
        agent.request_overrides = overrides
        agent.service_tier = tier
        expiry_loader = getattr(self, "_session_service_tier_expiry", None)
        try:
            expiry_at, _ = expiry_loader(session_key) if callable(expiry_loader) else (0.0, 0.0)
        except (AttributeError, TypeError, ValueError):
            expiry_at = 0.0
        from agent.fast_mode import set_gateway_fast_expiry_state
        set_gateway_fast_expiry_state(agent, expiry_at, tier, overlay=fast_overlay)
        agent._fast_until = (time.monotonic() + getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS)
                             if tier == "auto" else 0.0)

    def _apply_fast_selection(self, session_key: str, value: str, persist: bool = False) -> str:
        """Apply a /fast argument (typed or picked) and return the reply."""
        selection = _FAST_SELECTIONS.get(value)
        if selection is None:
            return t("gateway.fast.unknown_arg", arg=value)
        tier, saved_value, label_key = selection
        label = t(label_key) if label_key else value.upper()
        self._service_tier = tier
        from gateway.session_setting_commands import local_live_for_route
        if persist and self._save_gateway_config_key("agent.service_tier", saved_value):
            if local_live_for_route(self, session_key) is not None:
                # A local session runs its frozen tier, not config.yaml's: it takes the tier too.
                self._set_session_service_tier_override(session_key, tier)
            else:
                self._set_session_service_tier_override(session_key, None, clear=True)  # global wins
            self._apply_live_service_tier(session_key, tier)
            return ContextNotice(t("gateway.fast.saved", label=label) + self._fast_route_ignored_note(session_key, tier))
        # Session override — also the fallback after a failed config write (as /reasoning --global).
        self._set_session_service_tier_override(session_key, tier)
        self._apply_live_service_tier(session_key, tier)
        return ContextNotice(t("gateway.fast.session_only", label=label) + self._fast_route_ignored_note(session_key, tier))

    def _fast_route_ignored_note(self, session_key: str, tier: str | None) -> str:
        """A warning when the session's route never receives fast-mode params (a proxy): /fast is
        accepted but changes nothing, so the reply must not imply it applied."""
        if not tier:
            return ""
        from hermes_cli.models import fast_mode_route_ignored

        try:
            model, runtime = self._resolve_session_agent_runtime(session_key=session_key)
        except Exception:
            return ""
        if not fast_mode_route_ignored(model, runtime.get("provider"), runtime.get("base_url")):
            return ""
        return "\n" + t("gateway.fast.route_ignored")

    async def _handle_fast_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /fast — the CLI Priority Processing toggle; session-scoped unless ``--global``
        (persists agent.service_tier, parity with /model)."""
        from agent.fast_mode import FAST_ARG_ALIASES, service_tier_word
        from gateway.run import _load_gateway_config, _resolve_gateway_model

        # The /reasoning parser strips --global (any position) and normalizes unicode dashes.
        args, persist_global = self._parse_reasoning_command_args(event.get_command_args().strip().lower())
        # Normalized like /model and /reasoning (#30479): the tier override must land under the key
        # the next turn reads, and the eligibility check must see that turn's route.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        transition = self._resolve_session_service_tier(
            session_key=session_key, report_transition=True,
        )
        if isinstance(transition, tuple):
            self._service_tier, expiry_notice = transition
        else:
            self._service_tier, expiry_notice = transition, None
        user_config = _load_gateway_config()
        try:
            # Off the loop: credential resolution can refresh an OAuth token, as the turn does.
            model, runtime = await asyncio.to_thread(
                self._resolve_session_agent_runtime, source=source, session_key=session_key, user_config=user_config)
        except Exception:
            # No usable route at all (the turn fails too): judge the configured model alone.
            model, runtime = _resolve_gateway_model(user_config), {}
        if not _fast_route_supports(model, runtime):
            reply = t("gateway.fast.not_supported")
            return reply + ("\n\n" + expiry_notice if expiry_notice else "")
        args = FAST_ARG_ALIASES.get(args, args)
        from agent.codex_service_tiers import ultrafast_offered
        ultrafast = ultrafast_offered(model, provider=runtime.get("provider"), base_url=runtime.get("base_url"),
                                      access_token=runtime.get("api_key"))
        if args == "ultrafast" and not ultrafast:
            reply = t("gateway.fast.ultrafast_not_supported", model=model)
            return reply + ("\n\n" + expiry_notice if expiry_notice else "")
        if args and args != "status":
            reply = self._apply_fast_selection(session_key, args, persist=persist_global)
            if expiry_notice:
                combined = expiry_notice + "\n\n" + reply
                return ContextNotice(combined) if getattr(reply, "notice_style", None) == "context" else combined
            return reply
        mode = service_tier_word(self._service_tier)
        status = {"fast": t("gateway.fast.status_fast"), "normal": t("gateway.fast.status_normal")}.get(mode, mode)
        expiry_at, _ = self._session_service_tier_expiry(session_key)
        if expiry_at and self._service_tier in {"priority", "ultrafast"}:
            import math
            import time
            from agent.fast_mode import format_expiry_remaining
            remaining = max(0.0, expiry_at - time.time())
            status += t("gateway.fast.status_expiring", remaining=format_expiry_remaining(math.ceil(remaining)))
        if expiry_notice:
            status += "\n\n" + expiry_notice

        async def _on_fast_choice(_chat_id: str, value: str) -> str:
            return self._apply_fast_selection(session_key, value, persist=persist_global)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.fast.picker_title", mode=status),
            choices=[
                {"value": v, "label": t(f"gateway.fast.choice_{v}"), "is_current": mode == v}
                for v in ("fast", "normal", "auto", "cold", *(("ultrafast",) if ultrafast else ()))
            ],
            on_choice_selected=_on_fast_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.fast.status", mode=status)
