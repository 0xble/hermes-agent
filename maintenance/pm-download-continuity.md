# PM Download Continuity

Patch identity: `pm-download-continuity`.

Periodic checkpoints may publish intermediate coverage while the socket drains. The forced-termination regression must wait for the full 8 MiB sent by its server to become durable while the request remains unfinished, then kill the child. Preserve its 10 s checkpoint bound, unpublished destination, resumed range offset, and final byte equality.

The test and downloader are unchanged from fork base `839d20ad5ad99e7b4cff49e3dd48a82c35eb4758` before this correction. Source inspection confirms that `pm/downloader.py` records flushed chunks, fsyncs each snapshot before atomic sidecar publication, and keeps later coverage eligible for subsequent checkpoints. This fork-only test correction preserves that implementation. The reported hosted first-sidecar shortfall was 6 MiB. The unchanged test passed 40/40 locally with 8 CPU load workers on 2026-11-07, so that failure was not reproduced locally.

Contract test: `tests/pm/test_download_continuity.py::test_forced_termination_recovers_checkpoint_before_request_finishes`.

Retire this adaptation when the adopted upstream test waits for the same coverage invariant. Roll back the focused commit carrying `Fork-Patch: pm-download-continuity` without changing downloader behavior.
