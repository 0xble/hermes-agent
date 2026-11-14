"""Every MemoryManager fan-out scrubs secrets before a memory provider sees them (#115104).

Providers archive whatever they are handed, so a key echoed into tool output used to land verbatim in the
provider's store. Asserts on absence of the secret, never on a mask format.
"""
import json
import random
import re

import pytest

from agent import memory_manager
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider

_SECRET = "sk-proj-abc123def456ghi789jkl012"
_LEAK = f"export OPENAI_API_KEY={_SECRET} and Authorization: Bearer opaque0123456789abcdef"
_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


class _Recorder(MemoryProvider):
    pre_compress_checkpoint_api_version = 2

    def __init__(self):
        self.seen = []

    @property
    def name(self):
        return "recorder"

    def initialize(self, session_id="", **kw): pass
    def is_available(self): return True
    def system_prompt_block(self): return ""
    def get_tool_schemas(self): return [{"name": "rec_tool", "description": "d", "parameters": {"type": "object"}}]
    def prefetch(self, query, *, session_id=""): self.seen.append(query); return ""
    def queue_prefetch(self, query, *, session_id=""): self.seen.append(query)
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, **kw): self.seen.append((user_content, assistant_content, messages))
    def handle_tool_call(self, tool_name, args, **kw): self.seen.append(args); return "{}"
    def on_turn_start(self, turn_number, message, **kw): self.seen.append((message, kw))
    def on_session_end(self, messages): self.seen.append(messages)
    def on_pre_compress(self, messages, **kw): self.seen.append(messages); return ""
    def on_delegation(self, task, result, **kw): self.seen.append((task, result))
    def on_memory_write(self, action, target, content, metadata=None): self.seen.append((content, metadata))


def _transcript():
    return [{"role": "user", "content": "read the config"},
            {"role": "tool", "content": f"ov.conf: {_LEAK}"},
            {"role": "user", "content": [{"type": "text", "text": _LEAK}, _IMAGE]},
            {"role": "assistant", "tool_calls": [{"function": {"arguments": f'{{"cmd": "{_LEAK}"}}'}}]}]


_TRANSCRIPT_FANOUTS = {"sync_all", "on_session_end", "on_pre_compress"}
FANOUTS = {
    "sync_all": lambda m, t: m.sync_all(f"user {_LEAK}", f"assistant {_LEAK}", messages=t),
    "prefetch_all": lambda m, t: m.prefetch_all(f"recall {_LEAK}"),
    "queue_prefetch_all": lambda m, t: m.queue_prefetch_all(f"recall {_LEAK}"),
    "handle_tool_call": lambda m, t: m.handle_tool_call("rec_tool", {"q": _LEAK, "nested": [_LEAK]}),
    "on_turn_start": lambda m, t: m.on_turn_start(2, _LEAK, previous_message=_LEAK, author_name="Ada"),
    "on_session_end": lambda m, t: m.on_session_end(t),
    "on_pre_compress": lambda m, t: m.on_pre_compress(t, evidence_messages=t),
    "on_delegation": lambda m, t: m.on_delegation(f"task {_LEAK}", f"result {_LEAK}"),
    "on_memory_write": lambda m, t: m.on_memory_write("add", "memory", _LEAK, metadata={"old_text": _LEAK}),
}


def _manager():
    mgr, provider = MemoryManager(), _Recorder()
    mgr.add_provider(provider)
    return mgr, provider


@pytest.mark.parametrize("fanout", sorted(FANOUTS))
def test_every_provider_fanout_scrubs_secrets_without_touching_the_live_transcript(fanout, monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)
    (mgr, provider), transcript = _manager(), _transcript()
    FANOUTS[fanout](mgr, transcript)
    mgr.flush_pending(timeout=5.0)

    assert provider.seen, f"{fanout} never reached the provider"
    seen = repr(provider.seen)
    assert _SECRET not in seen and "opaque0123456789abcdef" not in seen
    if fanout in _TRANSCRIPT_FANOUTS:
        assert "read the config" in seen and repr(_IMAGE) in seen  # clean text and inline media pass through
    assert _SECRET in repr(transcript)  # providers got copies; the agent's own transcript is untouched


def test_scrub_holds_with_redact_secrets_off(monkeypatch):
    # Provider egress follows chat/cron egress: redact_secrets governs local logs, not what leaves the agent.
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    mgr, provider = _manager()
    mgr.sync_all("hi", _LEAK)
    mgr.flush_pending(timeout=5.0)
    assert _SECRET not in repr(provider.seen)


def test_key_detected_in_a_json_tool_result_is_also_masked_where_the_model_repeats_it_bare():
    # The #115104 incident shape: an opaque (no vendor prefix) key in an ov.conf dump inside a JSON tool result,
    # then echoed by the assistant in prose where no pattern alone could recognise it.
    key = "AQ.Ab8RN6Jx2kq9Zs0VwT4yLm3PbQe7HcUfGdA1nX5oIr"
    tool = json.dumps({"output": json.dumps({"embedding": {"api_key": key}, "model": "gemini"})})
    mgr, provider = _manager()
    mgr.sync_all("show my ov.conf", f"Your key is {key}.", messages=[{"role": "tool", "content": tool}])
    mgr.flush_pending(timeout=5.0)
    user, _assistant, messages = provider.seen[0]
    assert key not in repr(provider.seen)
    assert user == "show my ov.conf" and json.loads(json.loads(messages[0]["content"])["output"])["model"] == "gemini"


def test_on_turn_start_scrubs_user_text_but_hands_identity_through_raw():
    # A phone-number author id (SMS, Signal) matches the phone pattern; masking it would merge two senders.
    phone = "+1" + "555" + "0123456"
    mgr, provider = _manager()
    mgr.on_turn_start(2, "next ask", previous_message=_LEAK, author_id=phone, author_name=phone, model="test/model")
    message, kwargs = provider.seen[0]
    assert message == "next ask" and _SECRET not in kwargs["previous_message"]
    assert kwargs["author_id"] == phone and kwargs["author_name"] == phone and kwargs["model"] == "test/model"

def _legacy_learned_token_substitution(text, learned):
    pattern = re.compile("|".join(re.escape(token) for token in sorted(learned, key=len, reverse=True)))
    return pattern.sub("[redacted]", text)


def test_learned_token_substitution_matches_legacy_for_random_embedded_tokens():
    rng = random.Random(20261111)
    learned = frozenset({
        "alpha01234567890",
        "beta_01234567890",
        "gamma/01234567890",
    })
    alphabet = "abcXYZ012_-./+ "
    samples = [
        f"prefix{token}suffix" for token in learned
    ] + [
        f"prefixX{token}Ysuffix" for token in learned
    ]
    samples.extend(
        "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 80)))
        + rng.choice(tuple(learned))
        + "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 80)))
        for _ in range(500)
    )
    for text in samples:
        assert memory_manager._apply_learned_tokens(text, learned) == _legacy_learned_token_substitution(text, learned)


def test_sync_redaction_scrubs_each_repeated_transcript_string_once(monkeypatch):
    calls = 0
    real_scrub = memory_manager.redact_for_egress

    def counted_scrub(text):
        nonlocal calls
        calls += 1
        return real_scrub(text)

    monkeypatch.setattr(memory_manager, "redact_for_egress", counted_scrub)
    mgr, _provider = _manager()
    transcript = [
        {"role": "user", "content": f"message-{index:03d} distinct"}
        for index in range(400)
    ]
    for _ in range(50):
        mgr.sync_all("same user content", "same assistant content", messages=transcript)
    mgr.flush_pending(timeout=5.0)

    unique_strings = {"same user content", "same assistant content", "user", "message-000 distinct"}
    unique_strings.update(message["content"] for message in transcript)
    assert calls <= len(unique_strings) + 2


def test_scrub_cache_generation_changes_when_a_vault_value_is_registered():
    from agent.redact import clear_vault_redaction_values, register_vault_redaction_value

    secret = "vault-cache-invalidation-secret"
    text = f"value={secret}"
    clear_vault_redaction_values()
    try:
        assert memory_manager._scrub(text) == text
        register_vault_redaction_value(secret)
        assert secret not in memory_manager._scrub(text)
    finally:
        clear_vault_redaction_values()


def test_surrogate_transcript_redaction_matches_legacy_and_syncs_without_raising():
    from agent.redact import redact_for_egress

    surrogate_text = b"caf\xe9".decode("utf-8", "surrogateescape")
    content = f"{surrogate_text} and \ud800"
    expected = redact_for_egress(content)

    assert memory_manager._redact_for_provider(content) == expected

    mgr, provider = _manager()
    mgr.sync_all(content, content)
    mgr.flush_pending(timeout=5.0)
    assert provider.seen == [(expected, expected, None)]
