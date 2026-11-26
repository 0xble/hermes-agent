# Pin an authorized session during resume

Fork patch identity: `api-resume-retirement`.

After authorization, `session.resume` reads history off-loop before adding its viewer. Idle retirement could remove that exact `LiveSession` before `transcript_target()` read it, raising `KeyError` and returning JSON-RPC `-32603`. Resume holds the existing attaching pin across its asynchronous work and releases it even on cancellation. Retirement checks pins again after its durable busy read so an attachment that started during that read is visible.

This is separate from PR #1390's lost FIFO wakeup: no scheduling or admission policy changes here.

Contract tests: `tests/gateway/test_api_resume_retirement.py`, `tests/gateway/test_session_resume_compression_tip.py`.

Retire when upstream keeps the authorized session live across history preparation. Revert to roll back; no persistent data format changes.
