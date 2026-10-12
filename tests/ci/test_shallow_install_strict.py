"""The shallow-install N-1 fetch-cap gap obeys strict acceptance like every known gap."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from tests.e2e.core._pending_fixes import STRICT_ACCEPTANCE_ENV
from tests.e2e.core.upgrade.git import _shallow_install as cell
from tests.e2e.core.upgrade.git import test_shallow_install as depth1
from tests.e2e.core.upgrade.git import test_shallow_install_checked as checked

_FETCH_CAP = "historical pre-swap plain fetch exceeded the byte bound and hit its 300s cap"


def _historical_cap_run() -> dict:
    out = "Failed to fetch updates from origin\ngit fetch timed out after 300s\n"
    world = SimpleNamespace(install_bytes=10, diag=lambda cp: "diag")
    return {
        "w": world, "cp1": subprocess.CompletedProcess(["hermes", "update"], 1, out, ""),
        "has_prefetch_heal": False, "base": "a" * 40, "head1": "a" * 40,
        "shape1": {"shallow": "true", "promisor": "-", "filter": "-"},
        "fetches1": [SimpleNamespace(status=200, fault=None, body_bytes=100)],
    }


@pytest.mark.parametrize("strict", [None, "0", "other-batch"])
def test_historical_fetch_cap_xfails_by_default(monkeypatch, strict):
    if strict is None:
        monkeypatch.delenv(STRICT_ACCEPTANCE_ENV, raising=False)
    else:
        monkeypatch.setenv(STRICT_ACCEPTANCE_ENV, strict)
    with pytest.raises(pytest.xfail.Exception, match="#123254"):
        cell._assert_first_n1_update(_historical_cap_run())


def test_strict_acceptance_refuses_the_historical_fetch_cap(monkeypatch):
    monkeypatch.setenv(STRICT_ACCEPTANCE_ENV, "1")
    try:
        with pytest.raises(AssertionError, match=_FETCH_CAP) as caught:
            cell._assert_first_n1_update(_historical_cap_run())
        assert any("not excused" in note for note in getattr(caught.value, "__notes__", []))
    except pytest.xfail.Exception:
        pytest.fail("strict acceptance must fail the historical fetch cap, not xfail")


@pytest.mark.parametrize("assertion", [
    depth1.test_stale_depth1_install_updates_without_fetching_the_whole_history,
    depth1.test_second_update_after_the_unshallowing_update_succeeds,
    checked.test_passive_checks_do_not_push_the_next_update_into_orphan_divergence,
])
def test_relocated_n1_cells_still_refuse_the_historical_cap(monkeypatch, assertion):
    monkeypatch.setenv(STRICT_ACCEPTANCE_ENV, "1")
    run = _historical_cap_run()
    run.update(shape0={"shallow": "true"}, checks=[], cp2=run["cp1"])
    with pytest.raises(AssertionError, match=_FETCH_CAP) as caught:
        assertion(run)
    assert any("not excused" in note for note in getattr(caught.value, "__notes__", []))


def test_unproved_timeout_is_never_excused(monkeypatch):
    monkeypatch.delenv(STRICT_ACCEPTANCE_ENV, raising=False)
    run = _historical_cap_run()
    run["has_prefetch_heal"] = True  # current code timing out is a real failure
    with pytest.raises(AssertionError, match="N-1 -> HEAD update failed") as caught:
        cell._assert_first_n1_update(run)
    assert _FETCH_CAP not in str(caught.value)
