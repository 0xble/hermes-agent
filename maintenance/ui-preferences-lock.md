# UI preferences lock

Fork patch identity: `ui-preferences-lock`.

## Required behavior

Native Desktop and the Webapp merge keys into the same preferences document
under their shared directory lock. Native writes keep the bounded JSON
transaction synchronous after acquisition, including owner publication and
release. Waiting for libuv filesystem workers while holding this lock can
exhaust Python's existing 3-second acquisition budget under suite contention.
Acquisition remains asynchronous when another process holds the lock. Preserve
the existing wait and stale limits, ownership check, atomic rename and key merge.

## Provenance

Gate attempt 8 on `5bdb45a445445eee02ed6e7215e5dc9bc8068ad7` failed in the
real concurrent Node/Python writer contract with `PreferencesBusy`. The same
test passed alone. Retire this adaptation when upstream provides a critical
section that does not queue behind unrelated filesystem pool work.

Contract tests: `tests/hermes_cli/test_ui_preferences.py`
