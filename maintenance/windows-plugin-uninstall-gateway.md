# Windows live plugin uninstall gateway survival

Patch identity: `windows-plugin-uninstall-gateway`.

The live Windows contract keeps the gateway alive when uninstalling an imported
pure-Python plugin, a plugin holding a file open, or a mapped native extension.
Do not skip those cases or replace the gateway with a fake. Keep the gateway log
in assertion failures so future intermittent exits carry their own traceback.

## Source contract

The control listener is available before gateway startup completes. Its
`reload-plugins` request can run a foreground discovery sweep while startup
requires complete discovery. A finished background discovery thread does not
make the current manager's readiness flag stable: force reload clears
`_discovery_complete` under `_discovery_lock` until it completes. Strict startup
must acquire that lock before interpreting a false flag as failure. Keep both
the already-finished background worker and the worker finishing during join on
this path; a failed reload must still refuse startup rather than serve partial
policy state. See [the discovery owner](oneshot-plugin-hook-discovery.md).

## Incident evidence and limits

Scheduled nightly [41834945580](https://github.com/0xble/hermes-agent/actions/runs/41834945580)
(6297cf3664d), Native OS job 126591315875, reported all three survival assertions
with gateway return code 1. Its assertion omitted the child log. Diagnostic
[41867834960](https://github.com/0xble/hermes-agent/actions/runs/41867834960),
Native OS job 126706617241, passed all three cases in 26.7 seconds; no failing
gateway traceback exists in that run. Its unrelated installer failures must not
be presented as this regression's failure.

The portable event-gated regression demonstrates a real source race and pins its
fix. It is a plausible explanation of the original early gateway exit, not a
traceback-confirmed attribution. Never claim the original crash was conclusively
root-caused from a diagnostic run that did not reproduce it.

Contract tests: `tests/hermes_cli/test_plugin_startup_reload_barrier.py`,
`tests/hermes_cli/test_plugin_uninstall_windows_live.py`.

Retire the patch when the accepted upstream baseline protects the same strict
startup/readiness boundary and the native Windows survival contract passes.
