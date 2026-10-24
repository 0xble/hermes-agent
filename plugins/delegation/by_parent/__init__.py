"""Parent-dependent delegation, implemented only through the backend contract."""
from collections.abc import Mapping

from agent.delegation_provider import DelegationProvider, DelegationRouteError


class ByParent(DelegationProvider):
    @property
    def name(self):
        return "by_parent"

    @property
    def display_name(self):
        return "By Parent"

    @property
    def summary(self):
        return "Select a child alias from the parent model at spawn time."

    def route_task(self, task, config):
        from hermes_cli.live_model_aliases import current_config, is_live_alias, resolve_live_alias
        snapshot = current_config()
        routes = self.settings(config).get("routes", {})
        if not isinstance(routes, Mapping):
            raise DelegationRouteError("delegation.backends.by_parent.routes must be a mapping")
        expanded = {}
        for key, value in routes.items():
            if not isinstance(key, str) or not key.strip():
                raise DelegationRouteError("delegation.backends.by_parent.routes keys must be non-empty model names")
            model = resolve_live_alias(key, snapshot).model if is_live_alias(key) else key
            if model in expanded:
                raise DelegationRouteError(f"Duplicate delegation.backends.by_parent.routes model {model!r}")
            if not isinstance(value, str) or not value.strip():
                raise DelegationRouteError("delegation.backends.by_parent.routes values must be alias names or inherit")
            if value != "inherit":
                # Values are alias names, not inline routes or concrete model IDs.
                resolve_live_alias("alias:" + value, snapshot)
            expanded[model] = value
        selected = expanded.get(task.get("parent_model"))
        if selected is None:
            return dict(config)
        if selected == "inherit":
            return {"inherit": True}
        # A match REPLACES the default delegation route and its fallback/reasoning.
        return {"model": "alias:" + selected, "fallback_providers": [], "label": selected}


def register(ctx):
    ctx.register_delegation_provider(ByParent())
