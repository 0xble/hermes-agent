"""Container structure must remain visible in gateway cache fingerprints."""
import pytest

from gateway.run_agent_cache import GatewayAgentCacheMixin


def signature(value, field):
    runtime = {"capabilities": {"nested": value}} if field == "capabilities" else {}
    cache_keys = {"nested": value} if field == "cache_keys" else {}
    return GatewayAgentCacheMixin._agent_config_signature("m", runtime, [], "", cache_keys=cache_keys)


@pytest.mark.parametrize("field", ["capabilities", "cache_keys"])
@pytest.mark.parametrize("left,right", [
    ({"a": 1}, [["str", "a", 1]]),
    ({}, []),
    ([], ()),
    ({"a": [1]}, ["dict", [["str", "a", ["list", [1]]]]]),
    ([1], ["list", [1]]),
    ((1,), ["tuple", [1]]),
    ({1, 2}, "{1, 2}"),
    (frozenset({1, 2}), {1, 2}),
])
def test_container_structure_changes_signature(field, left, right):
    assert signature(left, field) != signature(right, field)


@pytest.mark.parametrize("field", ["capabilities", "cache_keys"])
def test_nested_typed_keys_and_unordered_containers_are_stable(field):
    first = {"a": {True: "bool", "True": "str", 2: {"x", "y"}}, "b": (1, [2])}
    second = {"b": (1, [2]), "a": {2: {"y", "x"}, "True": "str", True: "bool"}}
    assert signature(first, field) == signature(second, field)
    second["a"][True] = "changed"
    assert signature(first, field) != signature(second, field)
