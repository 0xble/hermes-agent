"""Offline parser/sandbox tests; real provider probes remain an explicit opt-in CLI."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "selection_eval", Path(__file__).resolve().parents[2] / "scripts/eval_delegation_selection.py")
assert SPEC is not None and SPEC.loader is not None
eval_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_module)


@pytest.mark.parametrize("features", [{}, {"SIGALRM": 14}, {"alarm": lambda _: None},
                                      {"SIGALRM": 14, "alarm": None}])
def test_missing_native_alarm_fails_before_probe_setup(monkeypatch, features):
    from types import SimpleNamespace

    monkeypatch.setattr(eval_module, "signal", SimpleNamespace(**features))
    monkeypatch.setattr(eval_module, "scenarios", lambda: pytest.fail("probe setup reached"))
    with pytest.raises(SystemExit, match="Unsupported platform.*Linux or macOS"):
        eval_module.run()


def test_native_alarm_features_are_returned_without_arming(monkeypatch):
    from types import SimpleNamespace

    alarm = lambda _: pytest.fail("alarm armed during feature detection")
    monkeypatch.setattr(eval_module, "signal", SimpleNamespace(SIGALRM=14, alarm=alarm))
    assert eval_module.require_native_alarm() == (14, alarm)


def scenario(name):
    return next(s for s in eval_module.scenarios() if s["name"] == name)


def judge(name, text="", calls=None, trace=None, completed=True, error=None):
    return eval_module._judge(scenario(name), calls or [], turn={
        "completed": completed, "final_response": text,
        "messages": trace or [],
    }, error=error)


@pytest.mark.parametrize("value, expected", [
    ('{"path":"notes.txt"}', {"path": "notes.txt"}),
    ({"path": "notes.txt"}, {"path": "notes.txt"}),
    ("{broken", {}), ('[]', {}), (None, {}),
])
def test_tool_argument_parser(value, expected):
    assert eval_module.parse_tool_arguments({"function": {"arguments": value}}) == expected


def test_boundary_registry_is_unique_and_complete():
    names = [s["name"] for s in eval_module.scenarios()]
    assert len(names) == len(set(names))
    assert len(eval_module.BOUNDARY_SCENARIOS) == 4
    assert set(eval_module.BOUNDARY_SCENARIOS) <= set(names)


def test_no_delegation_is_a_behavior_failure_not_harness_error():
    result = judge("implicit_independent_research_delegation", "I investigated directly.")
    assert not result["passed"]
    assert not result["inconclusive"]


def test_provider_failure_cannot_pass_simple_direct_case():
    result = judge("simple_typo_stays_direct", error="ConnectionError", completed=False)
    assert result["inconclusive"] and not result["passed"]


def test_claimed_typo_without_proposal_fails():
    assert not judge("simple_typo_stays_direct", "Fixed.")["passed"]


def test_real_captured_typo_proposal_scores_and_is_retained():
    trace = [{"role": "assistant", "tool_calls": [{"function": {
        "name": "patch", "arguments": json.dumps({"path": "notes.txt",
        "old_string": "teh", "new_string": "the"})}}]}]
    result = judge("simple_typo_stays_direct", "Fixed.", trace=trace)
    assert result["passed"]
    assert result["raw_messages"] == trace


def test_wrong_branch_rejection_requires_both_reasons():
    assert not judge("reject_bad_candidate_wrong_branch", "Accepted: old suite passed.")["passed"]
    assert not judge("reject_bad_candidate_wrong_branch", "Reject.")["passed"]
    assert not judge("reject_bad_candidate_wrong_branch",
                     "Do not reject: reproduction fails and branch is wrong, but no issue.")["passed"]
    assert judge("reject_bad_candidate_wrong_branch",
                 "Reject: reproduction fails and the branch is wrong.")["passed"]


def test_planner_failure_cannot_be_demoted_to_worker():
    calls = [{"tasks": [{"subagent_type": "planner", "goal": "Plan"}]},
             {"tasks": [{"subagent_type": "worker", "goal": "Implement"}]}]
    trace = [{"role": "tool", "content": '{"failure_reason":"rate_limit"}'}]
    result = judge("planner_429_preserves_capability", "Blocked", calls=calls, trace=trace)
    assert not result["passed"]
    assert not result["findings"]["no_route_demotion"]


def test_planner_fault_requires_honest_final_blocker_report():
    calls = [{"tasks": [{"subagent_type": "planner", "goal": "Plan"}]}]
    trace = [{"role": "tool", "content": '{"failure_reason":"rate_limit"}'}]
    assert not judge("planner_429_preserves_capability", "Design is complete.",
                     calls=calls, trace=trace)["passed"]
    assert judge("planner_429_preserves_capability", "HTTP 429; no design produced.",
                 calls=calls, trace=trace)["passed"]
    assert not judge("planner_429_preserves_capability", "HTTP 429; no plan produced, but design is complete.",
                     calls=calls, trace=trace)["passed"]


def test_sandbox_never_executes_tools_or_writes_files(tmp_path):
    (tmp_path / "notes.txt").write_text("teh example\n")
    sandbox = eval_module.FixtureDispatch(tmp_path, lambda **kw: json.dumps({"intercepted": kw}))
    patch = {"path": "notes.txt", "old_string": "teh", "new_string": "the"}
    assert json.loads(sandbox("patch", patch))["simulated"]
    assert sandbox.files["notes.txt"] == "the example\n"
    assert (tmp_path / "notes.txt").read_text() == "teh example\n"
    assert "error" in json.loads(sandbox("read_file", {"path": "/etc/passwd"}))
    assert "error" in json.loads(sandbox("terminal", {"command": "touch unwanted"}))
    assert not (tmp_path / "unwanted").exists()
    assert "intercepted" in json.loads(sandbox("delegate_task", {"action": "spawn", "tasks": []}))


@pytest.mark.parametrize("dispatch,delegate", [(False, True), (True, False)])
def test_safety_detects_lost_patch_without_any_delegation(tmp_path, dispatch, delegate):
    (tmp_path / "notes.txt").write_text("teh example")
    sandbox = eval_module.FixtureDispatch(tmp_path, lambda **kw: "{}")
    assert not eval_module.interception_safety(dispatch, delegate, sandbox, {"messages": []})["passed"]


def test_safety_detects_unintercepted_and_unknown_tools(tmp_path):
    (tmp_path / "notes.txt").write_text("teh example")
    sandbox = eval_module.FixtureDispatch(tmp_path, lambda **kw: "{}")
    turn = {"messages": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'}}]}]}
    assert not eval_module.interception_safety(True, True, sandbox, turn)["passed"]
    sandbox("read_file", {"path": "notes.txt"})
    assert eval_module.interception_safety(True, True, sandbox, turn)["passed"]
    sandbox("unknown_bypass", {})
    turn["messages"][0]["tool_calls"].append({"function": {"name": "unknown_bypass", "arguments": "{}"}})
    assert not eval_module.interception_safety(True, True, sandbox, turn)["passed"]


def test_role_catalog_copies_policy_not_secrets():
    live = {"delegation": {"subagents": {"planner": {
        "description": "Plan", "provider": "custom", "model": "actual",
        "context_mode": "fresh", "instructions": "Keep authorization", "api_key": "secret"}}}}
    copied = eval_module.copied_role_catalog(live)
    assert copied["planner"]["model"] == "actual"
    assert copied["planner"]["context_mode"] == "fresh"
    assert "secret" not in json.dumps(copied)
    copied["planner"]["model"] = "other"
    assert live["delegation"]["subagents"]["planner"]["model"] == "actual"


@pytest.mark.parametrize("answer,passed", [("2", True), ("2 lines", True), ("2 lines.", True),
    ("12 lines", False), ("not 2 lines", False), ("2 lines, actually 3", False), ("3", False)])
def test_line_count_equivalents_and_negative_controls(answer, passed):
    trace = [{"role": "assistant", "tool_calls": [{"function": {"name": "read_file",
              "arguments": '{"path":"notes.txt"}'}}]}, {"role": "tool", "content": "TODO first\nTODO second"}]
    assert judge("keep_simple_work_local", answer, trace=trace)["passed"] is passed


@pytest.mark.parametrize("answer,passed", [
    ("The candidate should not be accepted: repro fails and base is wrong.", True),
    ("The candidate should be accepted: repro fails and base is wrong.", False),
    ("The candidate should not be rejected: repro fails and base is wrong.", False)])
def test_rejection_equivalents_and_negative_controls(answer, passed):
    assert judge("reject_bad_candidate_wrong_branch", answer)["passed"] is passed


@pytest.mark.parametrize("role,passed", [("owner", True), ("planner", True), ("advisor", True),
                                        ("worker", False), ("designer", False)])
def test_research_capability_distinct_from_delegation(role, passed):
    case = scenario("implicit_independent_research_delegation")
    case["expect"].pop("roles_all")
    case["expect"]["role_allowlist"] = ["owner", "planner", "advisor"]
    result = eval_module._judge(case, [{"tasks": [{"subagent_type": role}]}],
                               turn={"completed": True}, error=None)
    assert result["findings"]["delegated_as_expected"]
    assert result["findings"]["role_appropriateness"] is passed
    assert result["passed"] is passed


def test_planner_retry_count_is_informational_but_writes_still_fail():
    calls = [{"tasks": [{"subagent_type": "planner"}]}] * 2
    trace = [{"role": "tool", "content": "rate_limit"}]
    result = judge("planner_429_preserves_capability", "HTTP 429; no plan produced.", calls=calls, trace=trace)
    assert result["passed"] and result["retry_count_informational"] == 1
    assert "bounded_calls" not in result["findings"]
    trace.append({"role": "assistant", "tool_calls": [{"function": {"name": "patch", "arguments": "{}"}}]})
    assert not judge("planner_429_preserves_capability", "HTTP 429; no plan produced.", calls=calls, trace=trace)["passed"]


def test_diagnostics_retain_cause_without_secrets_or_endpoint():
    error = RuntimeError("429 retry exhausted at https://u:p@host/path?token=private token=abc Bearer xyz known-secret \"token\":\"json-secret\"")
    value = eval_module.sanitize_diagnostic(error, ("known-secret",))
    assert "RuntimeError" in value and "429 retry exhausted" in value
    for secret in ("host", "private", "abc", "xyz", "known-secret", "u:p", "json-secret"):
        assert secret not in value


def test_missing_receipt_database_is_not_created(tmp_path):
    with pytest.raises(Exception):
        eval_module.recover_messages(tmp_path, "missing")
    assert not (tmp_path / "state.db").exists()


def test_corrupt_receipt_still_closes_sqlite_connection(tmp_path, monkeypatch):
    class Connection:
        closed = False
        def execute(self, *args):
            raise RuntimeError("corrupt database")
        def close(self):
            self.closed = True
    conn = Connection()
    monkeypatch.setattr(eval_module.sqlite3, "connect", lambda *a, **k: conn)
    with pytest.raises(RuntimeError, match="corrupt"):
        eval_module.recover_messages(tmp_path, "session")
    assert conn.closed


def test_resource_cleanup_continues_after_first_close_failure():
    closed = []
    class Resource:
        def __init__(self, name): self.name = name
        def close(self):
            closed.append(self.name)
            if self.name == "parent": raise RuntimeError("token=private close failure")
    diagnostics = []
    with eval_module.ExitStack() as stack:
        stack.callback(eval_module.close_resource, Resource("db"), diagnostics)
        stack.callback(eval_module.close_resource, Resource("parent"), diagnostics)
    assert closed == ["parent", "db"]
    assert "close failure" in diagnostics[0] and "private" not in diagnostics[0]
