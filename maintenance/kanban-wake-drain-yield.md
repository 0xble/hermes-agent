# Kanban wake drain yield

Fork patch identity: `kanban-wake-drain-yield`.

- **Required behavior:** every test or eval drain helper that loops `while adapter._background_tasks: await asyncio.gather(...)` must yield after each gather, so queued discard callbacks can remove finished tasks from the tracking set. This applies to `tests/gateway/test_kanban_wake_acceptance.py`, `tests/gateway/test_completion_admission.py` and `evals/heartbeat_idle_wire.py`.
- **Root cause:** Python 3.12+ can complete `asyncio.gather()` eagerly when all children are done. Without an explicit yield, the helper re-checks a set whose done callback is still queued, and busy-spins until the file timeout. The loop never yields, so an `asyncio.wait_for` bound around it cannot fire, and a regression shows up as a hang rather than an assertion.
- **Upstream prior art:** the whole of upstream PR [#131346](https://github.com/NousResearch/hermes-agent/pull/131346), merged as commit `89937f86858`, is adopted, covering all three helpers. That change also records that production `BasePlatformAdapter.cancel_background_tasks()` is bounded and filters done tasks, so the production shutdown drain is not the faulty path.
- **Hosted dependency check:** `scripts/ci/portable.py` installs `all` plus `messaging`. `messaging` declares the pinned `aiohttp==3.14.3`, which is already in `uv.lock`, so no dependency or lockfile change is needed.
- **Retirement:** retire this unit when `89937f86858` is an ancestor of the fork's `main`, or when no helper of this shape remains.
