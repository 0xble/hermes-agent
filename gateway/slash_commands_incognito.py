"""Per-topic privacy switches share the gateway's model routing and turn boundary."""
from copy import deepcopy
import asyncio


class GatewayIncognitoCommandsMixin:
    def _incognito_model_label(self, key):
        from gateway.run import _resolve_gateway_model
        agent = self._cached_agent_for(key)
        override = self._session_model_override(key) or {}
        return str(override.get("model") or getattr(agent, "model", None) or _resolve_gateway_model())

    def _incognito_reply(self, entry, error=None):
        text = f"Incognito {'ON' if entry.incognito else 'OFF'}. Active model: {self._incognito_model_label(entry.session_key)}."
        if entry.incognito:
            text += " Private history is RAM-only and is lost on /incognito off, /new, or restart."
        if error:
            text += "\nIncognito remains ON, keeping the current model. " + error
        return text

    async def _handle_incognito_command(self, event):
        action = (event.get_command_args() or "").strip().lower()
        if action not in ("", "on", "off", "status"):
            return "Usage: /incognito [on|off|status]"
        source = event.source
        adapter = self._intake_adapter_for(source)
        target_refusal = getattr(adapter, "incognito_target_refusal", None)
        if callable(target_refusal):
            reason = target_refusal(source)
            if reason:
                return reason
        key = self._session_key_for_source(source)
        # No await between reading the active-turn slot and taking the switch lock.
        # /status is safe mid-turn, toggles never mutate an in-flight agent.
        if action != "status" and getattr(self, "_running_agents", {}).get(key) is not None:
            return "Incognito was not changed. Wait for the active turn to finish, then retry."
        async with self._model_switch_lock():
            async with self._session_model_override_lock(key):
                if action != "status" and getattr(self, "_running_agents", {}).get(key) is not None:
                    return "Incognito was not changed. Wait for the active turn to finish, then retry."
                from contextvars import Context
                entry = await asyncio.to_thread(Context().run, self.session_store.get_or_create_session, source)
                self._rehydrate_session_model_override(key)
                if action == "status":
                    return self._incognito_reply(entry)
                enabled = not entry.incognito if not action else action == "on"
                if enabled == entry.incognito:
                    return self._incognito_reply(entry)
                if getattr(self, "_running_agents", {}).get(key) is not None or self._queue_depth(key, adapter=adapter):
                    return "Incognito was not changed. Wait for the active turn and queued messages to finish, then retry."
                from gateway.incognito import transition_scope
                with transition_scope(key):
                    if enabled:
                        return await self._enter_incognito(event, entry)
                    return await self._leave_incognito(entry)

    async def _enter_incognito(self, event, entry):
        key = entry.session_key
        # Public memory writes queued before this boundary are delivered first, while
        # the session is still public.
        cached = self._cached_agent_for(key)
        flush = getattr(cached, "flush_public_memory", None)
        if callable(flush):
            from agent.incognito import shared_worker_context
            await asyncio.to_thread(shared_worker_context().run, flush)
        # The durable flag must land before any private input can be admitted. On failure
        # propagate the exception, never acknowledge privacy that was not persisted.
        await asyncio.to_thread(self.session_store.set_incognito, key, True)
        from copy import copy
        from gateway.run import _CONVERSATION_SCOPED_STATE
        conversation = copy(self._session_state(key).conversation)
        # Retain sentinel identity and immutable pins. Copy only mutable containers.
        for name, value in vars(conversation).items():
            if isinstance(value, dict):
                setattr(conversation, name, deepcopy(value))
            elif isinstance(value, list):
                setattr(conversation, name, list(value))
        plain = {}
        for attr in _CONVERSATION_SCOPED_STATE:
            mapping = getattr(self, attr, None)
            if isinstance(mapping, dict):
                plain[attr] = (key in mapping, copy(mapping.get(key)))
            elif mapping is None:
                plain[attr] = (False, None)
        snapshots = getattr(self, "_incognito_gateway_snapshots", None)
        if snapshots is None:
            snapshots = self._incognito_gateway_snapshots = {}
        snapshots[key] = (conversation, plain)
        agent = self._cached_agent_for(key)
        if agent is not None:
            await asyncio.to_thread(agent.set_incognito, True)
        from gateway.run import _load_gateway_config
        cfg = await asyncio.to_thread(_load_gateway_config)
        section = cfg.get("incognito") or {}
        request = section.get("model") if isinstance(section, dict) else None
        if not request:
            return self._incognito_reply(entry)
        if not isinstance(request, str) or request.startswith("-") or " --" in request:
            return self._incognito_reply(entry, "Invalid incognito.model, expected a model name or provider/model route.")
        try:
            profile_home = None
            if getattr(getattr(self, "config", None), "multiplex_profiles", False):
                profile_home = self._resolve_profile_home_for_source(event.source)
            ctx = self._model_switch_context(event.source, key, profile_home=profile_home, persist_global=False)
            result, error = await self._perform_model_switch(ctx, request, None, event.source)
            if error is not None:
                return self._incognito_reply(entry, error)
            override = {
                "model": result.new_model, "provider": result.target_provider,
                "api_key": result.api_key, "base_url": result.base_url, "api_mode": result.api_mode,
                "request_overrides": dict(result.request_overrides or {}),
                "capabilities": dict(result.runtime_capabilities or {}),
            }
            await asyncio.to_thread(self.session_store.set_incognito_model, key, override)
            # Retain the pre-on agent and its frozen prompt for off. A configured route
            # needs a separate agent, not a mutation of the original prompt-cache prefix.
            parked = getattr(self, "_incognito_parked_agents", None)
            if parked is None:
                parked = self._incognito_parked_agents = {}
            lock = getattr(self, "_agent_cache_lock", None)
            cache = getattr(self, "_agent_cache", {})
            if lock:
                with lock:
                    parked[key] = cache.pop(key, None)
            else:
                parked[key] = cache.pop(key, None)
            self._session_model_overrides[key] = override
            self._session_state(key).conversation.model_override_scope = "session"
            self._session_state(key).conversation.model_role = None
        except Exception:
            return self._incognito_reply(entry, "Could not resolve the configured model route.")
        return self._incognito_reply(entry)

    async def _leave_incognito(self, entry):
        key = entry.session_key
        parked = getattr(self, "_incognito_parked_agents", {})
        agent = self._cached_agent_for(key)
        # Drop the private context before enabling durable writes again.
        if agent is not None:
            await asyncio.to_thread(agent.set_incognito, False)
            if getattr(agent, "_incognito_born", False) and key not in parked:
                # Built private, it never had the configured context engine or memory
                # writers. The next public turn rebuilds it from the saved session.
                self._evict_cached_agent(key)
        if key in parked:
            self._evict_cached_agent(key)
            original = parked.pop(key)
            if original is not None:
                agent = original[0] if isinstance(original, tuple) else original
                await asyncio.to_thread(agent.set_incognito, False)
                lock = getattr(self, "_agent_cache_lock", None)
                if lock:
                    with lock:
                        self._agent_cache[key] = original
                else:
                    self._agent_cache[key] = original
        await asyncio.to_thread(self.session_store.set_incognito, key, False)
        if entry.model_override:
            self._session_model_overrides[key] = deepcopy(entry.model_override)
        else:
            self._session_model_overrides.pop(key, None)
        snapshot = getattr(self, "_incognito_gateway_snapshots", {}).pop(key, None)
        if snapshot is not None:
            conversation, plain = snapshot
            self._session_state(key).conversation = conversation
            for attr, (present, value) in plain.items():
                mapping = getattr(self, attr, None)
                if isinstance(mapping, dict):
                    if present:
                        mapping[key] = value
                    else:
                        mapping.pop(key, None)
        else:
            # A restarted private interval has no RAM snapshot, restore its durable route.
            state = self._session_state(key)
            state.conversation.model_role = entry.model_role
            state.conversation.model_override_scope = entry.model_override_scope
        return self._incognito_reply(entry)
