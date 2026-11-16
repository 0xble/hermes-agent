"""Isolated execution of the observer, finite helper and fake orchestration."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.drills.zero_cut_handover import native
from tests.drills.zero_cut_handover import native_work
from tests.drills.zero_cut_handover import native_witnesses as w
from tests.drills.zero_cut_handover.test_native import plan
from tests.drills.zero_cut_handover.test_native_witnesses import fixture, OWNER, SUCCESSOR, TOKEN


def qualified_plan(tmp_path):
    result = plan(tmp_path)
    result["passes"][1].update(before="b" * 40, after="b" * 40)
    result["passes"][2].update(before="b" * 40, after="b" * 40)
    result["approval_command"] = "{worker}"
    return result


def test_injected_effects_can_never_qualify_native(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERO_CUT_DRILLS_NATIVE_PARENT", "1")
    monkeypatch.setattr(native.h, "safety_guard", lambda *a, **kw: None)
    monkeypatch.setattr(native, "preflight", lambda *a: None)
    observed = []
    def step(*a, **kw):
        observed.append(kw["native"])
        return {"route": "update", "native": kw["native"], "passed": True, "complete": True}
    monkeypatch.setattr(native, "run_step", step)
    result = native.run_native(home=tmp_path, peer="peer", plan=qualified_plan(tmp_path),
                               output=tmp_path / "out", effects=object(), live=True)
    assert not result["passed"]
    assert observed == [False]


def test_child_env_uses_served_builder_not_launch_credentials(monkeypatch, tmp_path):
    from tools.environments import local
    builds, calls = [], []
    monkeypatch.setenv("OPENAI_API_KEY", "launch-secret-not-for-target")
    def build(*, base, target_home, inherit_credentials):
        builds.append((base, target_home, inherit_credentials))
        return {"HERMES_HOME": str(target_home), "TARGET_ONLY": "yes"}
    monkeypatch.setattr(local, "served_profile_child_env", build)
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"id": 4}', stderr="")
    monkeypatch.setattr(native.subprocess, "run", run)
    effects = native.native_effects(tmp_path)
    effects.run_command(["example"], cwd=tmp_path)
    effects.send_as_user("peer:7", "test")
    effects.read_chat("peer", 10)
    assert all("OPENAI_API_KEY" not in b[0] for b in builds)
    assert {b[2] for b in builds} == {False, True}
    assert all(c[1]["env"].get("TARGET_ONLY") == "yes" for c in calls)
    assert "--topic" in calls[1][0] and "7" in calls[1][0]


def test_effect_worker_records_duplicate_invocations_honestly(tmp_path):
    native_work.work(tmp_path, TOKEN, "admission", 0)
    native_work.work(tmp_path, TOKEN, "admission", 0)
    events = [json.loads(line) for line in (tmp_path / f"{TOKEN}-admission.jsonl").read_text().splitlines()]
    assert [event["phase"] for event in events] == ["start", "finish", "start", "finish"]
    assert not w.event_pair({"events": {"admission": events}}, TOKEN, "admission", final=True)


def test_fake_full_restart_flow_checks_queue_transition_and_durable_results(monkeypatch, tmp_path):
    monkeypatch.setattr(native.uuid, "uuid4", lambda: SimpleNamespace(hex=TOKEN))
    from agent.i18n import t
    class Watch:
        def __init__(self, home):
            self.seen = {".restart_notify.json": "hash", ".restart_last_processed.json": "hash"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(native, "MarkerWatch", Watch)
    sent = []
    def send(peer, text):
        sent.append((peer, text))
        return {"id": 100 + len(sent)}
    fx = replace(native.h.Effects(lambda *a, **kw: None, lambda *a: None, send,
                                  lambda *a: [], lambda *a: {}), sleep=lambda seconds: None)
    snapshots = []
    def snapshot(home, directory, token, effects, peer):
        index = len(snapshots)
        snap = fixture(final=index >= 4)
        old, new = dict(OWNER, hermes_home=str(home)), dict(SUCCESSOR, hermes_home=str(home), code_sha="a" * 40, manifest_sha="a" * 40)
        snap["owners"] = [old] if index < 3 else [dict(old, handover={"token_lock_released": True}), new] if index == 3 else [new]
        snap["chat"].append({"id": 999, "sender_id": 8899335561, "text": "Queued"})
        for i, (_, text) in enumerate(sent, 101):
            snap["chat"].append({"id": i, "outgoing": True, "text": text})
            snap["tables"]["messages"].append({"role": "user", "platform_message_id": str(i), "content": text})
        if index >= 4:
            snap["executions"][0]["code_sha"] = new["code_sha"]
            snap["chat"].append({"id": 2000, "sender_id": 8899335561, "text": t("gateway.startup.restarted")})
        snapshots.append(snap)
        return snap
    result = native.run_step(tmp_path, "peer", fx, qualified_plan(tmp_path), tmp_path / "evidence", "slash_restart",
                             {"before": "a" * 40, "after": "a" * 40, "branch": "qualified"}, snapshot=snapshot)
    # The fixture's successor SHA must match this restart's selected identity.
    assert not result.get("native")
    assert result["passed"]
    assert any(text.startswith("/queue ") for _, text in sent)
    assert (tmp_path / "evidence/transition-evidence.json").is_file()
    assert (tmp_path / "evidence/result.json").is_file()


def test_approval_already_executed_is_not_pending():
    snap = fixture()
    snap["events"]["approval"] = [dict(snap["events"]["long"][0], kind="approval")]
    assert not w.armed(snap, TOKEN, OWNER)["approval"]


@pytest.mark.parametrize("problem", ["dirty", "wrong_head", "wrong_target", "empty_change", "foreign_fleet", "occupied_slot"])
def test_ordinary_update_preflight_refuses_unqualified_native_state(tmp_path, problem):
    source = tmp_path / "source"
    source.mkdir()
    home = tmp_path / "home"
    release = home / "releases" / ("a" * 40)
    release.mkdir(parents=True)
    (home / "current").symlink_to(release, target_is_directory=True)
    invoked = []
    def run(argv, **kw):
        invoked.append(argv)
        result = ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            result = " M source.py" if problem == "dirty" else ""
        elif argv[:3] == ["git", "rev-parse", "HEAD"]:
            result = "c" * 40 if problem == "wrong_head" else "a" * 40
        elif argv[:2] == ["git", "rev-parse"]:
            result = "c" * 40 if problem == "wrong_target" else "b" * 40
        elif argv[:3] == ["git", "diff", "--stat"]:
            result = "" if problem == "empty_change" else " source.py | 1 +"
        elif "-c" in argv:
            result = json.dumps({"homes": [str(home)] if problem != "foreign_fleet" else [str(home), "/foreign"], "plan": {"runtimes": [{"pid": 10}]}})
        return SimpleNamespace(returncode=0, stdout=result, stderr="")
    def query(home, verb, slot):
        return {"pid": 10} if slot == "a" or problem == "occupied_slot" else None
    fx = native.h.Effects(run, lambda *a: None, lambda *a: pytest.fail("send before preflight"),
                          lambda *a: [], lambda *a: {}, query_control=query)
    with pytest.raises(ValueError):
        native.preflight(fx, home, source, {"before": "a" * 40, "after": "b" * 40, "branch": "qualified"})
    assert not any("update" in argv for argv in invoked)
