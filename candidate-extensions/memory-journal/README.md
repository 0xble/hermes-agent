# Memory journal candidate plugin

This extension observes successful built-in `memory` writes, stores before and
after snapshots in a profile-local hash-chained journal, and exposes a
parent-only `memory_undo` tool. Staged or failed writes are never journaled.
Undo refuses when the profile changed after the entry or when the requested
entry is not the latest one.
