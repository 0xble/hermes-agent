"""Run the independent cross-surface journey within its own file timeout."""

from tests.e2e.core.history.test_prefix_stability import (
    assert_request_prefix_is_byte_stable,
    world as world,
)


def test_request_prefix_is_byte_stable_when_switching_surfaces(world):
    assert_request_prefix_is_byte_stable(world, "surface_switch")
