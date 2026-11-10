# Retired parent routing map

Fork patch identity: `delegation-routes-by-parent`.

Retired during the v0.23.1 integration. Advanced Delegation `model_map` options
replace `delegation.model_preset`, named custom-subagent routes and the general
`delegation.by_parent` map. The v54 migration removes those keys.

The sole configured exception was `uncensored -> inherit`. The official plugin
receives task/config JSON without a parent identity, so it cannot express that
exception. The minimal retained `inherit_parent_aliases: [uncensored]` check
uses upstream aliases at both delegate_task and the lifecycle entry point.
It passes an empty route to upstream credential/reasoning construction, retaining
the live parent's model, endpoint, fallbacks and effort. An explicit plugin option
still selects that option. See [dynamic aliases](dynamic-model-aliases.md).
