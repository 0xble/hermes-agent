"""Persisted goal tool receipts honor profile policy and the legacy receipt preference."""
import json

import pytest

from agent.inline_tool_executors import _goal_receipt_text


@pytest.mark.parametrize("action", ["set", "revise", "replace", "pause", "resume", "complete"])
@pytest.mark.parametrize("notices", [False, True])
@pytest.mark.parametrize("auto_notices", [False, True])
def test_persisted_goal_receipts_obey_both_preferences(action, notices, auto_notices):
    from hermes_constants import get_hermes_home
    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  notices: {str(notices).lower()}\n  auto_notices: {str(auto_notices).lower()}\n")
    result = json.dumps({"success": True, "action": action, "persisted": True, "notice": "Goal receipt"})
    assert _goal_receipt_text("goal_set", result) == ("Goal receipt" if notices and auto_notices else "")
    assert _goal_receipt_text("goal_set", json.dumps({"success": False, "persisted": False, "notice": "bad"})) == ""
    assert _goal_receipt_text("loop_set", json.dumps({
        "success": True, "persisted": True, "notice": "Loop receipt"})) == "Loop receipt"


def test_tool_receipts_use_real_profile_scope_without_launch_cache(tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.goal_notices import goal_notices_enabled
    quiet, normal = tmp_path / "quiet", tmp_path / "normal"
    for home, content in [(quiet, "goals:\n  notices: false\n"), (normal, "{}\n")]:
        home.mkdir()
        (home / "config.yaml").write_text(content)
    result = json.dumps({"success": True, "persisted": True, "notice": "Completed goal"})
    for home, expected in [(quiet, ""), (normal, "Completed goal"), (quiet, "")]:
        token = set_hermes_home_override(home)
        try:
            assert goal_notices_enabled() is (home == normal)
            assert _goal_receipt_text("goal_set", result) == expected
        finally:
            reset_hermes_home_override(token)