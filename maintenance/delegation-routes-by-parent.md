# Retired parent routing map

Fork patch identity: `delegation-routes-by-parent`.

Retired during the v0.23.1 integration. Official Advanced Delegation `model_map`
options replace `delegation.model_preset`, named custom-subagent routes and the
`delegation.by_parent` map. Migration v54 removes the keys together with their
uncensored-parent exception. The main-chat `uncensored` model alias remains.
Plain delegation uses the plugin's default worker route.
