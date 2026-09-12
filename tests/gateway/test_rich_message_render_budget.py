"""Rendering bounds apply before walking hostile inline/button sequences."""
from collections.abc import Sequence

import pytest

from plugins.platforms.telegram.rich_messages import project_rich_message


class CountingSequence(Sequence):
    def __init__(self, item):
        self.item = item
        self.reads = 0

    def __len__(self):
        return 1_000_000

    def __getitem__(self, index):
        if index >= len(self):
            raise IndexError
        self.reads += 1
        assert self.reads < 50, "renderer walked beyond its budget"
        return self.item


@pytest.mark.parametrize("kind", ["paragraph", "buttons", "table"])
def test_structural_sequences_stop_at_budget(kind):
    values = CountingSequence("x" if kind == "paragraph" else {"text": "x"})
    block = {"type": kind}
    block[{"paragraph": "text", "buttons": "buttons", "table": "cells"}[kind]] = (
        values if kind == "paragraph" else CountingSequence(values)
    )
    result = project_rich_message({"blocks": [block]}, max_nodes=12, max_chars=100)
    assert result.truncated
    assert values.reads <= 12
    assert len(result.text) <= 100


@pytest.mark.parametrize("kind,field", [("paragraph", "text"), ("mathematical_expression", "expression")])
def test_large_text_is_sliced_before_render_allocation(kind, field):
    class NoFormattingUntilSliced(str):
        def __format__(self, spec):
            raise AssertionError("unbounded input formatted")
        def splitlines(self, *args, **kwargs):
            raise AssertionError("unbounded input split")
        def strip(self, *args, **kwargs):
            raise AssertionError("unbounded input stripped")
    result = project_rich_message({"blocks": [{"type": kind, field: NoFormattingUntilSliced("x" * 100_000)}]}, max_chars=32)
    assert result.truncated
    assert len(result.text) <= 32


def test_structural_output_also_respects_tiny_character_budget():
    values = CountingSequence({"type": "divider"})
    result = project_rich_message({"blocks": values}, max_nodes=1_000_000, max_chars=8)
    assert values.reads <= 9
    assert result.truncated and len(result.text) <= 8


def test_malformed_scalar_fields_are_not_stringified():
    class NoStringification(list):
        def __str__(self):
            raise AssertionError("unbounded malformed container stringified")
    value = NoStringification(["x"] * 1000)
    result = project_rich_message({"blocks": [
        {"type": value},
        {"type": "mathematical_expression", "expression": value},
    ]}, max_chars=8)
    assert len(result.text) <= 8
