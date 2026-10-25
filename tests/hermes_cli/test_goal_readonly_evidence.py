"""Browser-console evidence is readonly only for the no-expression inspection shape."""

import pytest

from hermes_cli.goals import _evidence_only_read_only_status


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("{}", True),
        ('{"clear": false}', True),
        ('{"clear": true}', False),
        ('{"clear": 0}', False),
        ('{"clear": null}', False),
        ('{"expression": ""}', False),
        ('{"expression": null}', False),
        ('{"expression": "document.title"}', False),
        ('{"expression": "document.querySelector(\\"button\\").click()"}', False),
        ("{truncated", False),
        ('"not an object"', False),
        ('{"unknown": false}', False),
        ('[]', False),
        ("", False),
    ],
)
def test_browser_console_requires_a_readonly_call_shape(call, expected):
    evidence = [{"tool": "browser_console", "call": call, "output": "ok", "timestamp": 1}]
    assert _evidence_only_read_only_status(evidence) is expected


def test_other_readonly_tools_keep_their_existing_classification():
    evidence = [{"tool": "browser_snapshot", "call": "{\"full\": true}", "output": "ok", "timestamp": 1}]
    assert _evidence_only_read_only_status(evidence) is True
