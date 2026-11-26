# Cron generation interpreter

Load when changing POSIX cron script interpreter selection or the PM generation workspace layout.

Fork patch identity: `cron-generation-interpreter`.

## Required behavior and adoption

PM snapshots put code in `<generation>/workspace` and dependencies in the sibling `<generation>/venv`. A script launched by snapshot code must use that generation, not a workspace-local fallback or the owning checkout's potentially newer selected environment. Keep the missing-interpreter error when the sibling is damaged; this patch does not authorize a fallback to the bare store Python.

The recognition boundary is the `environments/<generation>/workspace` layout plus PM's `.lease-managed` generation marker, created by `pm/packages.py::Venv.apply`. Reconcile both if upstream changes the layout or marker contract.

## Provenance and disposition

- Maintained-fork base: `a62929d3b785fea1a6a0c3aff7dea033f7b362ea`; fork-first candidate branch: `fix/cron-generation-interpreter`. Runtime activation is separate from this source delivery.
- Upstream guidance inspected at `f5eebaa6d845e6e34e564644129c6080dd2a843a`.
- Related, not adopted: [upstream PR 149551](https://github.com/NousResearch/hermes-agent/pull/149551) and [upstream PR 136038](https://github.com/NousResearch/hermes-agent/pull/136038), both open when inspected. They address externally built environments and relax the missing-venv fallback; neither supplies the generation-local mapping required here. No upstream contribution is claimed.

## Verification and retirement

`tests/cron/test_cron_script.py` owns the real-child snapshot regression, damaged-sibling refusal, ordinary workspace boundary, and existing checkout selection checks. Windows interpreter selection is intentionally unchanged.

Retire this divergence when upstream's released selector handles the snapshot layout while retaining the missing-interpreter refusal and ordinary-checkout behavior.
