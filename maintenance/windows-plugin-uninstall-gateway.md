# Windows live plugin uninstall gateway survival

Patch identity: `windows-plugin-uninstall-gateway`.

The live Windows contract keeps the gateway alive when uninstalling an imported
pure-Python plugin, a plugin holding a file open, or a mapped native extension.
Do not skip those cases or replace the gateway with a fake.

Scheduled nightly 41834945580 (6297cf3664d), Native OS job 126591315875,
reported all three assertions with gateway return code 1. The existing assertion
dropped the child gateway log, so the test now includes that log on failure;
this diagnostic preserves every existing acceptance assertion. Investigation and
hosted proof are in progress; this note must be completed with the source cause
and RED/GREEN receipts before delivery.
