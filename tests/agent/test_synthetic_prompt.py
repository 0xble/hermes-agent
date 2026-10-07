"""The shared synthetic-prompt classifier drives both memory gates: auto-recall and retention.

Behavior is asserted through the real producers' formatters (process and delegation notices, goal
continuations, the cron preamble) and the real call sites: the turn-start prefetch in
``build_turn_context`` and the post-turn ``_sync_external_memory_for_turn`` path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.memory_manager import MemoryManager
from agent.synthetic_prompt import auto_recall_query, human_prompt_text
from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE
from plugins.memory.hindsight.retention import filter_retain_messages
from tools.delegation_resume import AUTO_RESUME_NOTICE_OPEN
from tools.process_registry_notifications import PROCESS_NOTIFICATION_END, format_process_notification

HUMAN = "What did we decide about the deploy pipeline?"


def _process_notice() -> str:
    from gateway.run_notifications import _mark_internal_notification

    evt = {"type": "completion", "session_id": "proc_1", "command": "./bin/ci gate", "exit_code": 0,
           "output": "all checks passed"}
    return _mark_internal_notification(format_process_notification(evt))


def _delegation_notice() -> str:
    evt = {"type": "async_delegation", "delegation_id": "deleg_1", "status": "completed",
           "goal": "Review the release notes", "result": "No findings."}
    return format_process_notification(evt)


def _goal_prompt() -> str:
    return CONTINUATION_PROMPT_TEMPLATE.format(goal="Ship the deploy pipeline change")


def _goal_prompt_after_wait() -> str:
    from hermes_cli.goals import GOAL_WAIT_LIFTED_NOTE_OPEN

    return _goal_prompt() + "\n\n" + GOAL_WAIT_LIFTED_NOTE_OPEN + "pid 4242 has exited. Verify its result before continuing.]"


def _cron_prompt() -> str:
    from cron.scheduler_prompt import _CRON_HINT

    return _CRON_HINT + "\n\nSummarize overnight alerts."


def _resume_notice() -> str:
    return AUTO_RESUME_NOTICE_OPEN + f"is eligible for conservative recovery.\n{PROCESS_NOTIFICATION_END}"


# (prompt, display_kind, platform) as each producer delivers it to the agent today.
SYNTHETIC_TURNS = {
    "gateway process notice": (_process_notice, "internal_notification", "telegram"),
    "legacy unmarked process notice": (_process_notice, None, "telegram"),
    "delegation result": (_delegation_notice, "internal_notification", "telegram"),
    "legacy unmarked delegation result": (_delegation_notice, None, "telegram"),
    "goal continuation": (_goal_prompt, None, "telegram"),
    "goal continuation after a lifted wait": (_goal_prompt_after_wait, None, "telegram"),
    "cron run": (_cron_prompt, None, "cron"),
    "delegation recovery notice": (_resume_notice, "internal_notification", "telegram"),
    "unrecognized internal wake": (lambda: "Reply with exactly HERMES_READY 1234", "internal_notification", "telegram"),
    "tui crash continuation": (lambda: "Finish the migration", "auto_continue", "tui"),
}


@pytest.mark.parametrize("name", sorted(SYNTHETIC_TURNS))
def test_synthetic_turns_have_no_human_text_and_no_recall_query(name):
    build, display_kind, platform = SYNTHETIC_TURNS[name]
    prompt = build()

    assert human_prompt_text(prompt, display_kind=display_kind, platform=platform) is None
    assert auto_recall_query(prompt, display_kind=display_kind, platform=platform) == ""
    # The policy switch restores recall on the generated text itself.
    assert auto_recall_query(prompt, display_kind=display_kind, platform=platform, include_synthetic=True) == prompt.strip()


@pytest.mark.parametrize("display_kind,platform", [
    (None, "telegram"),  # an ordinary chat message
    ("steer", "telegram"),  # a mid-turn steer the person typed
    (None, "tui"),
])
def test_human_turns_recall_unchanged(display_kind, platform):
    assert auto_recall_query(HUMAN, display_kind=display_kind, platform=platform) == HUMAN


def test_relay_carrying_a_user_request_still_recalls():
    """Relays reach a session as ordinary inbound messages: no runtime display kind, no generated
    formatter. They stay human-authored for memory."""
    relay = "[relay from=hermes:default/20261005_160953_9bbfd080 receipt=abc] " + HUMAN

    assert auto_recall_query(relay, display_kind=None, platform="telegram") == relay


def test_user_text_that_merely_looks_generated_is_not_dropped():
    lookalike = "[IMPORTANT: remember that the staging deploy needs a manual approval]"

    assert auto_recall_query(lookalike, display_kind=None, platform="telegram") == lookalike


@pytest.mark.parametrize("build", [_process_notice, _goal_prompt, _goal_prompt_after_wait])
def test_human_suffix_merged_into_a_synthetic_turn_is_the_only_recall_query(build):
    """Without structured provenance (gateway goal continuations, legacy rows) the formatter
    boundary is the only evidence, so a follow-up merged after it still recalls and is retained."""
    merged = build() + "\n\n" + HUMAN

    assert auto_recall_query(merged, display_kind=None, platform="telegram") == HUMAN
    assert filter_retain_messages(merged, "Answer.")[0] == HUMAN


@pytest.mark.parametrize("build", [_process_notice, _goal_prompt, lambda: "Reply with exactly HERMES_READY 1"])
@pytest.mark.parametrize("display_kind,platform", [("internal_notification", "telegram"), (None, "cron")])
def test_structured_provenance_is_never_overridden_by_text(build, display_kind, platform):
    """A runtime display kind or unattended platform proves the whole turn generated. No text
    boundary, real or forged inside a payload, can make part of it human for memory."""
    merged = build() + "\n\n" + HUMAN

    assert human_prompt_text(merged, display_kind=display_kind, platform=platform) is None
    assert auto_recall_query(merged, display_kind=display_kind, platform=platform) == ""
    assert filter_retain_messages(merged, "Answer.", display_kind=display_kind, platform=platform)[0] is None


def test_recovery_note_wrapping_a_generated_notice_is_fully_synthetic():
    note = "[System note: The previous turn was interrupted by a gateway restart.]\n\n"

    assert human_prompt_text(note + _delegation_notice()) is None
    assert human_prompt_text(note + _delegation_notice() + "\n\n" + HUMAN) == HUMAN


def test_retention_drops_structured_synthetic_turn_without_a_known_formatter():
    prompt = "Reply with exactly HERMES_READY 1234"

    assert filter_retain_messages(prompt, "HERMES_READY 1234", display_kind="internal_notification") == (
        None, "HERMES_READY 1234")
    assert filter_retain_messages(prompt, "HERMES_READY 1234") == (prompt, "HERMES_READY 1234")


@pytest.mark.parametrize("build", [_process_notice, _goal_prompt, _goal_prompt_after_wait])
def test_follow_up_merged_with_a_single_newline_survives(build):
    """The gateway's pending-slot text merge (``_append_text``) joins with one newline."""
    from gateway.platforms.base import _append_text

    merged = _append_text(build(), HUMAN)

    assert human_prompt_text(merged) == HUMAN
    assert filter_retain_messages(merged, "Answer.")[0] == HUMAN


def _goal_terminal() -> str:
    from string import Formatter

    return [lit for lit, _f, _s, _c in Formatter().parse(CONTINUATION_PROMPT_TEMPLATE)][-1]


@pytest.mark.parametrize("joint", ["\n", "\n\n"], ids=["single newline", "blank line"])
def test_payload_copying_the_terminal_stays_generated(joint):
    """Safety invariant (#320): when the closing paragraph appears more than once, the LAST copy is
    the generated boundary, so a goal that copies it can never leak its own text, or the real
    closing paragraph after it, as human. The single-newline form is the review's exact case."""
    payload = CONTINUATION_PROMPT_TEMPLATE.format(goal=_goal_terminal() + joint + "INNER")

    assert human_prompt_text(payload) is None
    assert auto_recall_query(payload) == ""
    assert filter_retain_messages(payload, "[SILENT]") == (None, None)


def test_merged_follow_up_quoting_the_closing_paragraph_keeps_only_text_after_the_quote():
    """Accepted limitation of the last-copy rule: text cannot tell a person's quote of the closing
    paragraph from a payload copy, so the boundary moves to the quote. Text after the quote still
    recalls and is retained; the quote and anything before it are skipped for memory only. The
    message itself is still delivered and answered."""
    from agent.prompt_builder import format_steer_marker
    from gateway.platforms.base import _append_text

    quoted = "I quoted:" + _goal_terminal()

    assert human_prompt_text(_append_text(_goal_prompt(), quoted + "\nRemember X")) == "Remember X"
    assert human_prompt_text(_goal_prompt() + format_steer_marker(quoted + "\nRemember X")) == "Remember X"
    assert human_prompt_text(_append_text(_goal_prompt(), quoted)) is None


def test_text_glued_to_the_template_terminal_is_not_a_human_suffix():
    assert human_prompt_text(_goal_prompt() + " and also this") is None


def _render(template: str, payload: str, field: str) -> str:
    from string import Formatter

    fields = {name for _lit, name, _spec, _conv in Formatter().parse(template) if name}
    return template.format(**{name: payload if name == field else "x" for name in fields})


def _templates():
    from agent.synthetic_prompt import _INJECTED_TURN_PATTERNS

    return [template for _kind, template, _matcher in _INJECTED_TURN_PATTERNS]


def _template_id(template: str) -> str:
    import hermes_cli.goals as goals
    import hermes_cli.heartbeat as heartbeat
    import hermes_cli.loops as loops

    return next(name for module in (goals, heartbeat, loops) for name, value in vars(module).items()
                if name.endswith("_TEMPLATE") and value is template)


def test_review_repro_contract_continuation_nested_in_a_plain_goal_stays_generated():
    """Review of fb3e903: the contract matcher used to win first and end inside the outer goal."""
    from hermes_cli.goals import CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE

    inner = CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(goal="inner", contract_block="- c") + "\nGENERATED PAYLOAD"
    outer = CONTINUATION_PROMPT_TEMPLATE.format(goal=inner)

    for display_kind in (None, "internal_notification"):
        assert human_prompt_text(outer, display_kind=display_kind) is None
        assert auto_recall_query(outer, display_kind=display_kind) == ""
        assert filter_retain_messages(outer, "[SILENT]", display_kind=display_kind) == (None, None)
    assert human_prompt_text(outer + "\n" + HUMAN) == HUMAN


@pytest.mark.parametrize("outer", _templates(), ids=_template_id)
@pytest.mark.parametrize("inner", _templates(), ids=_template_id)
def test_a_template_nested_in_another_templates_payload_never_leaks(outer, inner):
    """Every generated template can carry any other complete one inside a payload field (a goal
    that quotes a continuation, a loop prompt that quotes a wakeup). Templates that share an
    opening can each match, and only the outer one's boundary is real. Whatever the field, the
    payload stays generated and a real follow-up after the outer prompt still survives."""
    from string import Formatter

    nested = _render(inner, "inner payload", next(
        name for _lit, name, _spec, _conv in Formatter().parse(inner) if name)) + "\nGENERATED PAYLOAD"
    for field in {name for _lit, name, _spec, _conv in Formatter().parse(outer) if name}:
        prompt = _render(outer, nested, field)
        assert human_prompt_text(prompt) is None, field
        assert human_prompt_text(prompt + "\n" + HUMAN) == HUMAN, field


@pytest.mark.parametrize("revision_lines", [
    "- v2 (agent, agent, no user authority): reason — changed: goal\n    earlier goal: Ship the old change",
    # An earlier goal is rendered in full, so it may span paragraphs or copy the closing paragraph.
    "- v2 (agent, agent, no user authority): reason — changed: goal\n    earlier goal: Ship A\n\nSecond paragraph",
    "- v2 (agent, agent, no user authority): reason — changed: goal\n    earlier goal: Ship A"
    + CONTINUATION_PROMPT_TEMPLATE.split("{goal}", 1)[1] + "\nLEAK",
], ids=["plain", "multi-paragraph earlier goal", "earlier goal copying the terminal"])
def test_revised_continuation_is_generated_through_its_end(revision_lines):
    """The revision block has no closing marker and carries multi-line goal text, so nothing after it
    can be proven human: a revised continuation is generated in full, including any merged text."""
    from hermes_cli.goals import CONTINUATION_REVISIONS_TEMPLATE

    revised = _goal_prompt() + CONTINUATION_REVISIONS_TEMPLATE.format(revision_lines=revision_lines)

    assert human_prompt_text(revised) is None
    assert human_prompt_text(revised + "\n\n" + HUMAN) is None


def test_cron_runs_are_unattended_so_even_the_task_text_skips_recall():
    """Documented default: a cron run has no person present, so the stored job text after the
    generated preamble is not a human message either."""
    assert human_prompt_text("Summarize overnight alerts.", platform="cron") is None
    assert auto_recall_query(_cron_prompt(), platform="cron", include_synthetic=True) == _cron_prompt().strip()


@pytest.mark.parametrize("module_name,template_name", [
    ("hermes_cli.goals", "CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE"),  # 6 fields
    ("hermes_cli.loops", "WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE"),  # 4 fields
])
def test_crafted_near_miss_is_classified_in_linear_time(module_name, template_name):
    """A person can start a message with a template's opening and repeat its interior literals
    without the terminal one. Matching must stay linear: this ran for seconds with one greedy
    ``.*`` per field and runs synchronously on the turn path and the memory-sync worker."""
    import importlib
    import time
    from string import Formatter

    template = getattr(importlib.import_module(module_name), template_name)
    literals = [lit for lit, _f, _s, _c in Formatter().parse(template)]
    opening, interior = literals[0], [lit for lit in literals[1:-1] if lit]
    # Measured with the former one-``.*``-per-field regex: 0.8s at 3 KB, 46s at 6 KB.
    crafted = opening + "".join((lit + "x") * 400 for lit in interior) + "no terminal here"
    assert len(crafted) > 10_000

    started = time.perf_counter()
    for _ in range(5):
        assert human_prompt_text(crafted) == crafted.strip()
    assert time.perf_counter() - started < 1.0


def test_trivial_suffix_still_skips_recall():
    assert auto_recall_query(_goal_prompt() + "\n\nok", display_kind=None, platform="telegram") == ""


# -- call sites ---------------------------------------------------------------------------------


def _turn_agent(*, display_kind=None, platform="telegram", recall_synthetic_turns=False):
    manager = MagicMock()
    manager.prefetch_all.return_value = ""
    manager.recall_synthetic_turns = recall_synthetic_turns
    return SimpleNamespace(_memory_manager=manager, _user_turn_count=3, session_id="s-1",
                           _turn_display_kind=display_kind, platform=platform)


def test_turn_start_skips_recall_for_a_delegation_result_but_still_notifies_providers():
    agent = _turn_agent(display_kind="internal_notification")

    assert _memory_turn_start_and_prefetch(agent, _delegation_notice()) == ""
    agent._memory_manager.prefetch_all.assert_not_called()
    agent._memory_manager.on_turn_start.assert_called_once()


def test_turn_start_recalls_on_the_human_suffix_only():
    agent = _turn_agent(display_kind=None)

    _memory_turn_start_and_prefetch(agent, _goal_prompt() + "\n\n" + HUMAN)
    agent._memory_manager.prefetch_all.assert_called_once_with(HUMAN, session_id="s-1")


def test_turn_start_skips_recall_for_text_merged_into_a_runtime_notification():
    agent = _turn_agent(display_kind="internal_notification")

    _memory_turn_start_and_prefetch(agent, _process_notice() + "\n\n" + HUMAN)
    agent._memory_manager.prefetch_all.assert_not_called()


def test_turn_start_honors_the_recall_synthetic_turns_switch():
    agent = _turn_agent(recall_synthetic_turns=True)
    prompt = _goal_prompt()

    _memory_turn_start_and_prefetch(agent, prompt)
    agent._memory_manager.prefetch_all.assert_called_once_with(prompt.strip(), session_id="s-1")


def test_build_turn_context_stashes_the_turn_display_kind_for_memory():
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    agent._memory_manager = MagicMock(prefetch_all=MagicMock(return_value=""), recall_synthetic_turns=False)
    _build(agent, user_message=_delegation_notice(), persist_user_display_kind="internal_notification")
    assert agent._turn_display_kind == "internal_notification"
    agent._memory_manager.prefetch_all.assert_not_called()

    # A cached agent's next human turn must not inherit the previous internal kind.
    _build(agent, user_message=HUMAN)
    assert agent._turn_display_kind is None
    agent._memory_manager.prefetch_all.assert_called_once_with(HUMAN, session_id=agent.session_id)


def _post_turn_agent(*, display_kind, platform="telegram"):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock(recall_synthetic_turns=False)
    agent.session_id = "s-1"
    agent.platform = platform
    agent._turn_display_kind = display_kind
    return agent


def test_post_turn_syncs_a_synthetic_turn_with_provenance_but_queues_no_recall():
    agent = _post_turn_agent(display_kind="internal_notification")

    agent._sync_external_memory_for_turn(original_user_message=_process_notice(),
                                         final_response="NO_REPLY", interrupted=False)

    kwargs = agent._memory_manager.sync_all.call_args.kwargs
    assert (kwargs["display_kind"], kwargs["platform"]) == ("internal_notification", "telegram")
    agent._memory_manager.queue_prefetch_all.assert_not_called()


def test_post_turn_queues_recall_for_a_human_turn():
    agent = _post_turn_agent(display_kind=None)

    agent._sync_external_memory_for_turn(original_user_message=HUMAN, final_response="We kept it.",
                                         interrupted=False)
    agent._memory_manager.queue_prefetch_all.assert_called_once_with(HUMAN, session_id="s-1")


def test_manager_forwards_provenance_only_to_providers_that_accept_it():
    class Legacy:
        name = "legacy"

        def __init__(self):
            self.calls = []

        def sync_turn(self, user_content, assistant_content, *, session_id=""):
            self.calls.append((user_content, session_id))

    class Aware(Legacy):
        name = "aware"

        def sync_turn(self, user_content, assistant_content, *, session_id="", display_kind=None, platform=None):
            self.calls.append((user_content, session_id, display_kind, platform))

    manager = MemoryManager()
    legacy, aware = Legacy(), Aware()
    manager._providers = [legacy, aware]  # type: ignore[list-item]  # duck-typed sync_turn signatures
    manager.sync_all("notice", "ok", session_id="s-1", display_kind="internal_notification", platform="cron")
    assert manager.flush_pending(timeout=5) is True

    assert legacy.calls == [("notice", "s-1")]
    assert aware.calls == [("notice", "s-1", "internal_notification", "cron")]


def test_hindsight_sync_turn_drops_a_structured_synthetic_turn(monkeypatch):
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._mode, provider._auto_retain = "local_external", True
    built = []
    monkeypatch.setattr(provider, "_build_turn_messages",
                        lambda u, a, **kw: built.append((u, kw)) or [])

    provider.sync_turn("Reply with exactly HERMES_READY 1", "HERMES_READY 1", session_id="s-1",
                       display_kind="internal_notification", platform="telegram")
    assert built == [("Reply with exactly HERMES_READY 1",
                      {"display_kind": "internal_notification", "platform": "telegram"})]
    assert HindsightMemoryProvider._build_turn_messages(
        provider, "Reply with exactly HERMES_READY 1", "[SILENT]", display_kind="internal_notification") == []


# -- buffered recall across generated turns -------------------------------------------------------


class _BufferingProvider:
    """Hindsight's default async contract: ``queue_prefetch`` buffers a recall keyed on that turn's
    message, and the next ``prefetch`` returns the buffer whatever its own query is."""

    name = "buffering"

    def __init__(self):
        self.buffer, self.consumed_by, self.queued = "", [], []

    def queue_prefetch(self, query, *, session_id=""):
        self.queued.append(query)
        self.buffer = f"recall for: {query}"

    def prefetch(self, query, *, session_id=""):
        self.consumed_by.append(query)
        result, self.buffer = self.buffer, ""
        return result

    def discard_prefetch(self):
        self.buffer = ""

    def sync_turn(self, user_content, assistant_content, *, session_id="", display_kind=None, platform=None):
        pass

    def on_turn_start(self, turn_number, message, **kwargs):
        pass

    def recall_status(self):
        return None


@pytest.fixture()
def clock(monkeypatch):
    import agent.memory_manager as memory_manager

    now = [1_000_000.0]
    monkeypatch.setattr(memory_manager, "_now", lambda: now[0])
    return now


def _session_with_buffering_provider(max_age=1800.0):
    from tests.agent.test_turn_context import _FakeAgent

    agent = _FakeAgent()
    agent._memory_manager = MemoryManager(prefetch_max_age_seconds=max_age)
    provider = _BufferingProvider()
    agent._memory_manager._providers = [provider]  # type: ignore[list-item]  # duck-typed provider
    return agent, provider


def _run_turn(agent, message, *, display_kind=None):
    """One turn in production order: build_turn_context (turn-start recall), then the post-turn
    sync and queue on the same agent. Returns the recall injected into this turn."""
    from run_agent import AIAgent
    from tests.agent.test_turn_context import _build

    ctx = _build(agent, user_message=message, persist_user_display_kind=display_kind)
    AIAgent._sync_external_memory_for_turn(agent, original_user_message=message, final_response="Done.",
                                           interrupted=False)
    assert agent._memory_manager.flush_pending(timeout=5) is True
    return ctx.ext_prefetch_cache


def test_buffered_recall_for_the_last_human_turn_survives_generated_turns_within_the_age_limit(clock):
    agent, provider = _session_with_buffering_provider()
    later = "And what about the rollback plan?"

    assert _run_turn(agent, HUMAN) == ""
    clock[0] += 600
    assert _run_turn(agent, _delegation_notice(), display_kind="internal_notification") == ""
    clock[0] += 600
    assert _run_turn(agent, _goal_prompt()) == ""
    clock[0] += 300

    assert _run_turn(agent, later) == f"recall for: {HUMAN}"
    # Generated turns neither consumed nor replaced the buffer.
    assert provider.consumed_by == [HUMAN, later]
    assert provider.queued == [HUMAN, later]


def test_buffered_recall_older_than_the_age_limit_is_dropped(clock):
    agent, provider = _session_with_buffering_provider()

    _run_turn(agent, HUMAN)
    clock[0] += 1200
    _run_turn(agent, _delegation_notice(), display_kind="internal_notification")
    clock[0] += 601  # 1801s after HUMAN queued its recall

    assert _run_turn(agent, "And what about the rollback plan?") == ""
    # The next human turn queued a fresh recall, which the turn after it may use.
    clock[0] += 60
    assert _run_turn(agent, "Who owns it?") == "recall for: And what about the rollback plan?"


def test_generated_turn_never_consumes_the_buffer_even_when_it_is_stale(clock):
    agent, provider = _session_with_buffering_provider()

    _run_turn(agent, HUMAN)
    clock[0] += 7200
    assert _run_turn(agent, _process_notice(), display_kind="internal_notification") == ""
    assert provider.consumed_by == [HUMAN]
    assert provider.buffer == f"recall for: {HUMAN}"  # dropped only when a human turn would use it


def test_zero_prefetch_max_age_disables_the_bound(clock):
    from agent.memory_manager import DEFAULT_PREFETCH_MAX_AGE_S, parse_prefetch_max_age

    assert parse_prefetch_max_age(None) == DEFAULT_PREFETCH_MAX_AGE_S
    assert parse_prefetch_max_age("not a number") == DEFAULT_PREFETCH_MAX_AGE_S
    assert parse_prefetch_max_age("600") == 600.0
    assert parse_prefetch_max_age(0) is None

    agent, _ = _session_with_buffering_provider(max_age=parse_prefetch_max_age(0))
    _run_turn(agent, HUMAN)
    clock[0] += 86_400
    assert _run_turn(agent, "And what about the rollback plan?") == f"recall for: {HUMAN}"


def test_agent_init_reads_prefetch_max_age_from_config(monkeypatch):
    import agent.agent_init as agent_init

    class _Provider(_BufferingProvider):
        def is_available(self):
            return True

        def initialize(self, **kwargs):
            pass

        def get_tool_schemas(self):
            return []

    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: _Provider())
    monkeypatch.setattr(agent_init, "_memory_provider_init_kwargs", lambda agent, platform: {"session_id": "s"})
    for configured, expected in ((None, 1800.0), (90, 90.0), (0, None)):
        memory = {"provider": "buffering", "memory_enabled": False, "user_profile_enabled": False}
        if configured is not None:
            memory["prefetch_max_age_seconds"] = configured
        agent = SimpleNamespace(enabled_toolsets=None, disabled_toolsets=None, tools=None)
        agent_init._init_memory(agent, {"memory": memory}, False, "cli")
        assert agent._memory_manager._prefetch_max_age == expected


def test_hindsight_discard_prefetch_drops_the_buffer_and_an_in_flight_worker():
    import threading

    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._mode, provider._auto_recall, provider._prefetch_waits_for_retain = "local_external", True, False
    release = threading.Event()
    provider._do_recall = lambda query: (release.wait(5), (f"- {query}", 1))[1]

    provider.queue_prefetch("old topic")
    provider.discard_prefetch()
    release.set()
    provider._prefetch_thread.join(timeout=5)
    assert provider.prefetch("new topic") == ""


def test_retaindb_and_honcho_discard_prefetch_drop_their_buffers():
    from plugins.memory.honcho import HonchoMemoryProvider
    from plugins.memory.retaindb import RetainDBMemoryProvider

    retaindb = RetainDBMemoryProvider()
    retaindb._context_result, retaindb._dialectic_result = "old context", "old synthesis"
    retaindb._agent_model = {"memory_count": 1}
    retaindb.discard_prefetch()
    assert retaindb.prefetch("new topic") == ""

    honcho = HonchoMemoryProvider()
    honcho._prefetch_result, honcho._prefetch_result_fired_at = "old dialectic", 3
    honcho.discard_prefetch()
    assert honcho._consume_pending_dialectic() == ""


def test_prefetch_generation_publishes_only_the_current_token():
    from agent.memory_provider import PrefetchGeneration

    generation, published = PrefetchGeneration(), []
    first = generation.begin()
    second = generation.begin(lambda: published.append("reset"))
    for token in (first, second):
        with generation.publishing(token) as current:
            published.append((token, current))
    generation.discard()
    with generation.publishing(second) as current:
        published.append((second, current))
    assert published == ["reset", (first, False), (second, True), (second, False)]


def test_retaindb_discard_prefetch_drops_every_in_flight_worker():
    """Review of fb3e903: a blocked ``_store`` fetch released after the discard republished."""
    import threading
    from unittest.mock import MagicMock

    from plugins.memory.retaindb import RetainDBMemoryProvider

    provider = RetainDBMemoryProvider()
    provider._client = MagicMock()
    started, release = threading.Barrier(4), threading.Event()

    def blocked(value):
        def fetch(*_args, **_kwargs):
            started.wait(5)
            release.wait(5)
            return value
        return fetch

    provider._client.query_context.side_effect = blocked({"results": [{"content": "old topic memory"}]})
    provider._client.get_profile.return_value = {}
    provider._client.ask_user.side_effect = blocked({"answer": "old synthesis"})
    provider._client.get_agent_model.side_effect = blocked({"memory_count": 1, "persona": "old persona"})

    provider.queue_prefetch("old topic")
    started.wait(5)  # all three workers are inside their fetch
    provider.discard_prefetch()
    release.set()
    for thread in provider._prefetch_threads:
        thread.join(timeout=5)
    assert provider.prefetch("new topic") == ""


def test_retaindb_current_batch_still_publishes():
    from unittest.mock import MagicMock

    from plugins.memory.retaindb import RetainDBMemoryProvider

    provider = RetainDBMemoryProvider()
    provider._client = MagicMock()
    provider._client.query_context.return_value = {"results": [{"content": "fresh memory"}]}
    provider._client.get_profile.return_value = {}
    provider._client.ask_user.return_value = {"answer": "fresh synthesis"}
    provider._client.get_agent_model.return_value = {"memory_count": 0}
    provider.queue_prefetch("topic")
    for thread in provider._prefetch_threads:
        thread.join(timeout=5)
    result = provider.prefetch("next")
    assert "fresh memory" in result and "fresh synthesis" in result


def _honcho_with_blocked_dialectic(answer):
    import threading
    from unittest.mock import MagicMock

    from plugins.memory.honcho import HonchoMemoryProvider

    provider = HonchoMemoryProvider()
    provider._manager, provider._session_key, provider._session_initialized = MagicMock(), "s", True
    provider._turn_count, provider._last_dialectic_turn, provider._dialectic_empty_streak = 5, 2, 3
    entered, release = threading.Event(), threading.Event()

    def dialectic_query(*_args, **_kwargs):
        entered.set()
        release.wait(5)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    provider._manager.dialectic_query.side_effect = dialectic_query
    provider._spawn_dialectic("old topic", thread_name="honcho-prefetch", fired_at=5, log_label="prefetch")
    assert entered.wait(5)
    return provider, release


@pytest.mark.parametrize("answer", ["old dialectic", "", RuntimeError("timeout")],
                         ids=["result", "empty", "failure"])
def test_honcho_discard_prefetch_drops_a_running_dialectic_and_its_cadence_updates(answer):
    """Review of fb3e903: a blocked dialectic released after the discard published its result. An
    obsolete run must not touch the pending slot, ``_last_dialectic_turn`` or the empty streak."""
    provider, release = _honcho_with_blocked_dialectic(answer)

    provider.discard_prefetch()
    release.set()
    provider._prefetch_thread.join(timeout=5)

    assert provider._consume_pending_dialectic() == ""
    assert (provider._prefetch_result, provider._prefetch_result_fired_at) == ("", -999)
    assert (provider._last_dialectic_turn, provider._dialectic_empty_streak) == (2, 3)


def test_honcho_current_dialectic_still_publishes_and_advances_cadence():
    provider, release = _honcho_with_blocked_dialectic("fresh dialectic")

    release.set()
    provider._prefetch_thread.join(timeout=5)

    assert (provider._last_dialectic_turn, provider._dialectic_empty_streak) == (5, 0)
    assert provider._consume_pending_dialectic() == "fresh dialectic"


def test_queued_recall_still_waiting_behind_a_slow_sync_is_dropped_with_the_expired_buffer(clock):
    import threading

    release = threading.Event()

    class _SlowSync(_BufferingProvider):
        def sync_turn(self, *args, **kwargs):
            release.wait(5)

    manager = MemoryManager(prefetch_max_age_seconds=1800.0)
    provider = _SlowSync()
    manager._providers = [provider]  # type: ignore[list-item]  # duck-typed provider
    manager.sync_all(HUMAN, "Done.", session_id="s-1")
    manager.queue_prefetch_all(HUMAN, session_id="s-1")
    clock[0] += 1801
    assert manager.prefetch_all("And what about the rollback plan?", session_id="s-1") == ""

    release.set()
    assert manager.flush_pending(timeout=5) is True
    assert provider.queued == []  # the expired request never reached the provider
