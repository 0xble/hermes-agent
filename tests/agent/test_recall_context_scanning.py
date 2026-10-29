"""Recall scaffolding removal preserves producer semantics without suffix rescans."""
import json
import random
import re
import statistics
import timeit
from itertools import product

import pytest

from agent.synthetic_prompt import _RECALL_CONTEXT_PREFIXES, _strip_recall_context


def _original_strip(text):
    """Reference implementation from attachment-recall-query (#526)."""
    descriptions = []
    while text:
        matches = [match for pattern in _RECALL_CONTEXT_PREFIXES
                   if (match := pattern.search(text))]
        if not matches:
            break
        prefix = min(matches, key=lambda match: match.start())
        if "description" in prefix.re.groupindex:
            description = prefix.group("description").strip()
            if description:
                descriptions.append(description)
        text = (text[:prefix.start()] + text[prefix.end():]).strip()
    return text, descriptions


OPEN = "[The user sent an image~ Here's what I can see:\n"
ADVICE = "[If you need a closer look, use vision_analyze with image_url: /cache/x.jpg]"
IMAGE = OPEN + "Screen [dark mode]\n\nUnbalanced [quote\n]\n" + ADVICE
FILE = "[User sent a file: invoice [final] copy.txt]"
REPLY = '[Replying to: ' + json.dumps('Quotes " and brackets ]\n\nMore context') + ']\n\n'


def test_equivalent_to_original_on_producer_and_malformed_corpus():
    atoms = [
        "", "plain human text", "\n\n", FILE, IMAGE, OPEN,
        "[The user attached an image. Here's what it contains:\n\n]\n" + ADVICE,
        "[The user attached an image: /cache/a [b].jpg]\n"
        "[Examine it with the vision_analyze tool using image_url: /cache/a [b].jpg]",
        "[voice message could not be transcribed]", REPLY,
        "[The user attached an image.\nmissing boundary]",
        "[User sent a file: no closing bracket", "\n" + ADVICE,
        "[Replying to: \"unterminated", "[Replying to: \"escaped \\\" quote\"]\n\n",
        "[Triggering message id: `42` — use as `message_id` for reply/react/pin via the discord tools.]\n\n",
    ]
    corpus = [left + '\n\n' + right for left, right in product(atoms, repeat=2)]
    corpus += ["  untouched whitespace  ", "\t", FILE + '\n', IMAGE + '\n\ncaption',
               IMAGE.replace('/cache/x.jpg]', '/cache/x.jpg] not a note'),
               OPEN * 15, FILE * 20, REPLY * 10]
    # Removing an inner note can make an outer note complete across the seam.
    for outer in (FILE, IMAGE, REPLY):
        corpus.extend(outer[:cut] + FILE + '\n\n' + outer[cut:]
                      for cut in range(len(outer)))
    rng = random.Random(526)
    corpus.extend(''.join(rng.choices(atoms, k=8)) for _ in range(1000))
    for text in corpus:
        assert _strip_recall_context(text) == _original_strip(text), repr(text)


@pytest.mark.parametrize("shape", ["pure", "mixed", "same_line", "trigger"])
def test_adversarial_work_scales_with_input_size(shape):
    def crafted(count):
        if shape == 'pure':
            return OPEN * count
        if shape == 'mixed':
            return (FILE + '\n') * count + (OPEN + '...\n') * count
        if shape == 'trigger':
            return '[Triggering message id: `x' * count
        return '[User sent a file: ' * count

    # Use a large size ratio and repeated medians, not an absolute speed requirement.
    small, large = crafted(20), crafted(160)
    def measure(text):
        return statistics.median(timeit.repeat(lambda: _strip_recall_context(text), number=2, repeat=5))
    small_time, large_time = measure(small), measure(large)
    size_ratio = len(large) / len(small)
    assert large_time / small_time < size_ratio * 2.5, (small_time, large_time, size_ratio)


@pytest.mark.parametrize('pattern', [r'no bracket', r'\[(a?)*', r'\[(?<=x)y', r'(?i)\[case'])
def test_registry_changes_outside_the_bounded_subset_fail_explicitly(pattern):
    from agent.recall_context import strip_recall_context

    with pytest.raises(ValueError):
        strip_recall_context('[example]', (re.compile(pattern),))
