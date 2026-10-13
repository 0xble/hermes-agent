"""Session-model hydration at the API loop/worker boundary."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.platforms.api_server")


class SessionModelRoutesMixin:
    async def _prepare_request_route_validation(self, session_key: Optional[str]) -> None:
        """Create state and commit hydration on the loop before route work is offloaded."""
        if not session_key:
            return
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner is None:
            return
        runner._session_state(session_key)
        try:
            await runner._rehydrate_session_model_override_async(session_key)
        except Exception:
            logger.debug("api_server failed to hydrate session /model override for %s", session_key, exc_info=True)

    def _session_model_override_for(self, session_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """Read a worker-safe snapshot; only the async prelude publishes hydration.

        Standalone synchronous agent creation retains lazy reads, but must never
        create runner state or run the loop-owned compare-and-set in a worker.
        """
        if not session_key:
            return None
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner is None:
                return None
            override = runner._session_model_overrides.get(session_key)
            if isinstance(override, dict):
                return dict(override)
            if runner._session_model_override_edit_pending(session_key):
                return None
            state = runner._peek_session_state(session_key)
            epoch = state.persistent.model_override_epoch if state else 0
            loaded = runner._load_session_model_override(session_key)
            current = runner._peek_session_state(session_key)
            if (current is not state
                    or (current and current.persistent.model_override_epoch != epoch)
                    or runner._session_model_override_edit_pending(session_key)):
                return None
            override = runner._session_model_overrides.get(session_key) or loaded
            return dict(override) if isinstance(override, dict) else None
        except Exception:
            logger.debug("api_server failed to read session /model override for %s", session_key, exc_info=True)
            return None

    def _request_route_conflict_error(
        self, *, session_id: Optional[str], gateway_session_key: Optional[str], requested_model: Optional[str],
        requested_provider: Optional[str], route: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return a 400-worthy conflict string for ambiguous route/provider mixes."""
        from gateway.platforms.api_server import _clean_request_string
        request_provider = _clean_request_string(requested_provider)
        if not request_provider or not isinstance(route, dict):
            return None
        if self._session_model_override_for(gateway_session_key or session_id):
            return None  # session /model wins over both, so nothing is ambiguous
        route_provider = _clean_request_string(route.get("provider"))
        route_api_key = _clean_request_string(route.get("api_key"))
        route_base_url = _clean_request_string(route.get("base_url"))
        route_alias = _clean_request_string(requested_model) or "requested model"
        if route_provider and request_provider != route_provider:
            return (
                f"Model route '{route_alias}' is pinned to provider '{route_provider}'. "
                f"Remove 'provider' or use '{route_provider}'.")
        if not route_provider and (route_api_key or route_base_url):
            return (
                f"Model route '{route_alias}' pins route credentials/base_url. "
                "Do not combine it with an explicit 'provider'.")
        return None
