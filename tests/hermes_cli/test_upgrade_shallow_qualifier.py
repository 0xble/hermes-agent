"""Only the measured historical pre-swap fetch gap is an expected failure."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _shallow_install as qualifier
from tests.e2e.core.upgrade.git import test_shallow_install_checked as checked


def test_current_source_is_never_classified_as_the_historical_gap():
    source = I.H.WORKTREE.joinpath("hermes_cli", "update_cmd.py").read_text()
    assert qualifier._has_prefetch_heal(source)


class World:
    install_bytes = 100

    def diag(self, *args):
        return "historical pre-swap test"

    def refs(self, *args):
        return []


def run(**overrides):
    value = {
        "w": World(), "checks": [], "grafts": 4,
        "cp1": subprocess.CompletedProcess([], 1,
            "✗ Failed to fetch updates from origin.\n"
            "  git fetch timed out after 300s with no response from the remote\n", ""),
        "head1": "historical", "base": "historical", "has_prefetch_heal": False,
        "shape1": {"shallow": "true", "promisor": "-", "filter": "-"},
        "fetches1": [SimpleNamespace(body_bytes=1000, status=200, fault="")],
        "mark1": 0,
    }
    value.update(overrides)
    return value


def test_measured_n1_plain_fetch_timeout_is_the_existing_gap():
    with pytest.raises(pytest.xfail.Exception, match="#123254"):
        checked.test_passive_checks_do_not_push_the_next_update_into_orphan_divergence(run())


@pytest.mark.parametrize("overrides", [
    {"has_prefetch_heal": True},
    {"head1": "updated"},
    {"fetches1": [SimpleNamespace(body_bytes=1000, status=200, fault="stall")]},
    {"fetches1": [SimpleNamespace(body_bytes=1000, status=200, fault="cut")]},
    {"fetches1": [SimpleNamespace(body_bytes=20, status=200, fault="")]},
    {"fetches1": [SimpleNamespace(body_bytes=1000, status=503, fault="")]},
    {"shape1": {"shallow": "false", "promisor": "-", "filter": "-"}},
    {"cp1": subprocess.CompletedProcess([], 1, "authentication failed", "")},
    {"cp1": subprocess.CompletedProcess([], 1,
        "Failed to fetch updates from origin. git fetch timed out after 900s", "")},
])
def test_nearby_failures_remain_hard_failures(overrides):
    with pytest.raises(AssertionError, match="N-1 -> HEAD update failed"):
        checked.test_passive_checks_do_not_push_the_next_update_into_orphan_divergence(run(**overrides))
