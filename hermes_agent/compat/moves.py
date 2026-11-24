"""Old module name -> current module name.

A key is added when a module moves and is never removed; when the target moves again, only its
value changes. Plain dicts and no imports, because a process from an older release reads this file.

A module folded into one that already existed is a key too, only when a catalog-pinned plugin imports
it and the target defines every name the old module published, unchanged. Path guards judge the
target by the first key that names it, so the oldest one is listed first.
"""

MOVES: dict[str, str] = {
    "tools.plugin_guard": "hermes_cli.plugins_install_scan",
    "agent.message_content": "session_rules.turns",
    "agent.context_compressor_retry": "session_rules.turns",
    # Folded into sqlite_support by the storage interface (#151855); the holographic 0.29.x pin imports it.
    "hermes_state_wal": "sqlite_support",
    "hermes_state_worker_lifecycle": "persistence.sqlite.workers._lifecycle",
    "hermes_state_worker_context": "persistence.sqlite.workers._context",
    "hermes_state_worker_compression": "persistence.sqlite.workers._compression",
    "hermes_state_runtime_workers": "persistence.sqlite.workers._runtime_workers",
    "hermes_state_terminal": "persistence.sqlite.conversation._store.terminal",
    "hermes_state_timeline": "persistence.sqlite.conversation._store.timeline",
    "hermes_state_dbfile": "persistence.sqlite._state.dbfile",
    "hermes_state_guard": "persistence.sqlite._state.guard",
    "hermes_state_mutations": "persistence.sqlite.authority._mutation",
    "hermes_state_mutation_guards": "persistence.sqlite.authority._runtime_guards",
    "hermes_state_mutation_compress": "persistence.sqlite.authority._mutation_publish",
}
