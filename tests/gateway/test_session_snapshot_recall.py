"""A private recall witness must pair native calls/returns, never prompt text."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from gateway.session_snapshot import _digest, recall_witness
from plugins.memory.hindsight.private_reads import PrivateReadResponse


SESSION = "snapshot-fixture-session"
MARKER = "snapshot-fixture-marker"
QUERY = "Which instrument does the fixture owner play?"
FACT = "The fixture owner plays the bassoon."
CALL = "snapshot-fixture-call"


def assistant_call(call_id=CALL, query=QUERY):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "hindsight_recall",
                    "arguments": json.dumps({"query": query}),
                },
            }
        ],
    }


def tool_return(call_id=CALL):
    response = PrivateReadResponse(
        value=None,
        query_sha256=_digest(QUERY),
        bank_sha256=_digest("fixture-bank"),
        session_sha256=_digest(SESSION),
        epoch=7,
        result_nonempty=True,
    )
    return {
        "role": "tool",
        "name": "hindsight_recall",
        "tool_call_id": call_id,
        "content": response.tool_result(FACT),
    }


@pytest.fixture
def case():
    prefix = [
        {"role": "user", "content": "Public prefix"},
        {"role": "assistant", "content": "Public reply"},
    ]
    provider = SimpleNamespace(
        name="hindsight",
        _session_id=SESSION,
        _read_only=True,
        _privacy_epoch=7,
        _bank_id="fixture-bank",
    )
    messages = prefix + [
        {"role": "user", "content": MARKER},
        assistant_call(),
        tool_return(),
    ]
    return dict(
        messages=messages,
        boundary=deepcopy(prefix),
        saved=deepcopy(prefix),
        session_id=SESSION,
        provider=provider,
        probe={"marker": MARKER, "expected_fact": FACT},
        private=True,
        busy=False,
    )


def test_witness_returns_only_hashes_and_booleans_without_mutating_state(case):
    before = deepcopy(case)
    observed = recall_witness(**case)
    assert all(
        observed[key] is True
        for key in (
            "matched",
            "private_boundary",
            "provider_read_only",
            "result_nonempty",
            "expected_fact_match",
        )
    )
    assert observed["marker_sha256"] == _digest(MARKER)
    assert observed["call_id_sha256"] == _digest(CALL)
    assert observed["query_sha256"] == _digest(QUERY)
    assert observed["result_text_sha256"] == _digest(FACT)
    assert observed["source"] == "private_tool_return"
    assert observed["tool_name"] == "hindsight_recall"
    encoded = json.dumps(observed)
    assert all(
        value not in encoded
        for value in (MARKER, QUERY, FACT, SESSION, CALL, "fixture-bank")
    )
    assert case == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("private", False),
        ("busy", True),
        ("boundary", None),
        ("saved", None),
        ("boundary", [{"role": "user", "content": "Unrelated prefix"}]),
        ("session_id", "other-session"),
    ],
)
def test_unknown_public_or_changed_boundary_never_witnesses(case, key, value):
    case[key] = value
    assert recall_witness(**case)["matched"] is False


def test_known_empty_prefix_is_not_unknown(case):
    case.update(messages=case["messages"][2:], boundary=[], saved=[])
    assert recall_witness(**case)["matched"] is True


@pytest.mark.parametrize(
    "key,value",
    [
        ("name", "other-provider"),
        ("_session_id", "other-session"),
        ("_read_only", False),
        ("_read_only", 1),
        ("_privacy_epoch", 8),
        ("_privacy_epoch", True),
        ("_bank_id", "other-bank"),
    ],
)
def test_provider_identity_flag_and_dispatch_epoch_are_required(case, key, value):
    setattr(case["provider"], key, value)
    assert recall_witness(**case)["matched"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("no_store", False),
        ("no_store", 1),
        ("result_nonempty", False),
        ("result_nonempty", 1),
        ("epoch", 8),
        ("epoch", True),
        ("session_sha256", _digest("other-session")),
        ("bank_sha256", _digest("other-bank")),
        ("query_sha256", _digest("other-query")),
        ("result_text_sha256", _digest("other-result")),
    ],
)
def test_proof_must_match_actual_dispatch_and_result(case, key, value):
    row = case["messages"][-1]
    payload = json.loads(row["content"])
    payload["_hermes_private_read"][key] = value
    row["content"] = json.dumps(payload)
    assert recall_witness(**case)["matched"] is False


@pytest.mark.parametrize("location", ["boundary", "saved"])
def test_marker_in_either_public_prefix_is_rejected(case, location):
    case[location][0]["content"] = MARKER
    assert recall_witness(**case)["private_boundary"] is False


def test_expected_fact_must_be_in_actual_tool_result_not_marker_prompt_or_query(case):
    case["messages"][2]["content"] += " " + FACT
    case["messages"].insert(0, {"role": "system", "content": FACT})
    case["boundary"].insert(0, {"role": "system", "content": FACT})
    case["probe"]["expected_fact"] = "A different instrument."
    case["messages"][3]["content"] += " A different instrument."
    observed = recall_witness(**case)
    assert observed["matched"] is True
    assert observed["expected_fact_match"] is False


@pytest.mark.parametrize("content", ["", "No relevant memories found."])
def test_empty_provider_response_does_not_become_success_via_fallback_text(
    case, content
):
    response = PrivateReadResponse(
        None, _digest(QUERY), _digest("fixture-bank"), _digest(SESSION), 7, False
    )
    case["messages"][-1]["content"] = response.tool_result(content)
    assert recall_witness(**case)["matched"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "unpaired",
        "wrong_return_id",
        "wrong_return_name",
        "user_instead_of_tool",
        "assistant_only",
        "return_before_call",
        "return_after_next_user",
        "call_before_marker",
        "duplicate_marker",
        "duplicate_call_id",
        "duplicate_return_id",
        "public_result",
        "error_result",
        "tampered_result",
        "invalid_arguments",
        "nonobject_arguments",
        "missing_query",
        "reflect_not_recall",
        "marker_in_assistant",
    ],
)
def test_untrusted_or_uncorrelated_rows_cannot_witness(case, mutation):
    rows = case["messages"]
    if mutation == "unpaired":
        del rows[-2]
    elif mutation == "wrong_return_id":
        rows[-1]["tool_call_id"] = "different-call"
    elif mutation == "wrong_return_name":
        rows[-1]["name"] = "hindsight_reflect"
    elif mutation == "user_instead_of_tool":
        rows[-1]["role"] = "user"
    elif mutation == "assistant_only":
        rows[-1]["role"] = "assistant"
    elif mutation == "return_before_call":
        rows[-2], rows[-1] = rows[-1], rows[-2]
    elif mutation == "return_after_next_user":
        rows.insert(-1, {"role": "user", "content": "Next turn"})
    elif mutation == "call_before_marker":
        rows[2], rows[3] = rows[3], rows[2]
    elif mutation == "duplicate_marker":
        rows.append({"role": "user", "content": MARKER})
    elif mutation == "duplicate_call_id":
        rows.insert(-1, assistant_call())
    elif mutation == "duplicate_return_id":
        rows.append(tool_return())
    elif mutation == "public_result":
        rows[-1]["content"] = json.dumps({"result": FACT})
    elif mutation == "error_result":
        payload = json.loads(rows[-1]["content"])
        payload["error"] = "provider failed"
        rows[-1]["content"] = json.dumps(payload)
    elif mutation == "tampered_result":
        payload = json.loads(rows[-1]["content"])
        payload["result"] = "Altered content " + FACT
        rows[-1]["content"] = json.dumps(payload)
    elif mutation in {"invalid_arguments", "nonobject_arguments", "missing_query"}:
        rows[-2]["tool_calls"][0]["function"]["arguments"] = {
            "invalid_arguments": "{",
            "nonobject_arguments": "[]",
            "missing_query": "{}",
        }[mutation]
    elif mutation == "reflect_not_recall":
        rows[-2]["tool_calls"][0]["function"]["name"] = "hindsight_reflect"
    elif mutation == "marker_in_assistant":
        rows[2]["role"] = "assistant"
    else:
        raise AssertionError("Unknown fixture mutation")
    assert recall_witness(**case)["matched"] is False


def test_text_block_user_content_and_native_tool_name_field(case):
    case["messages"][2]["content"] = [{"type": "text", "text": MARKER}]
    case["messages"][-1]["tool_name"] = case["messages"][-1].pop("name")
    assert recall_witness(**case)["matched"] is True


def test_db_only_prefix_metadata_is_not_mistaken_for_a_changed_boundary(case):
    case["boundary"][0].update(
        timestamp=123, _db_persisted=True, message_uid="fixture-row"
    )
    assert recall_witness(**case)["matched"] is True


def test_a_later_turn_does_not_retroactively_supply_the_marked_turn_result(case):
    case["messages"][-1]["content"] = json.dumps({"result": FACT})
    case["messages"] += [
        {"role": "user", "content": "Another question"},
        assistant_call(),
        tool_return(),
    ]
    assert recall_witness(**case)["matched"] is False
