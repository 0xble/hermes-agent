"""The pre-swap parent keeps the receipt until the post-swap child claims its hand-off.

A child that crashes while starting (an ImportError in the staged interpreter) exits non-zero
exactly like one that resumed the receipt and then failed. Only the claim (the child unlinking
the hand-off) moves ownership, so the parent records the failure whenever it is missing.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import immutable_update_handoff, main, update_cmd, update_receipt
from hermes_cli.update_handoff import write_handoff


@pytest.fixture
def hand_off(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    markers = []
    monkeypatch.setattr(main, "_write_update_incomplete_marker", lambda: markers.append("incomplete"),
                        raising=False)
    release = home / "releases" / ("b" * 40)
    release.mkdir(parents=True)
    token = {"resume_needed": True}
    update_receipt.begin_update_receipt()
    update_receipt.record_step("pre_update_backup", True, "disposable snapshot")

    def run(child):
        monkeypatch.setattr(immutable_update_handoff, "continue_update_in_fresh_interpreter", child)
        with pytest.raises(SystemExit) as exited:
            update_cmd._hand_off_post_swap(
                SimpleNamespace(), swap="immutable", branch="main",
                opts=SimpleNamespace(pre_update_version="old"), gateway_mode=True,
                had_desktop_app_before_update=False, _windows_gateway_resume=token,
                release=release, source=tmp_path / "source")
        current = update_receipt._current.get()
        return SimpleNamespace(
            code=exited.value.code, markers=markers, token=token,
            steps=current.data["steps"] if current else None,
            gateway_exit=(home / ".update_exit_code").read_text() if (home / ".update_exit_code").exists() else None,
            leftover=list((home / "logs" / "update_receipts").glob("post_swap_*.json")),
        )

    yield run, release
    update_receipt._current.set(None)


def _child(code, *, claims):
    def child(payload, *, argv_tail):
        handoff = write_handoff(payload)
        if claims:
            handoff.unlink()
        return code
    return child


def _assert_parent_recorded(result, detail, *, leftover=()):
    assert result.code == 1
    assert [step["name"] for step in result.steps] == ["pre_update_backup", "post_swap_handoff"]
    assert result.steps[-1]["ok"] is False and detail in result.steps[-1]["detail"]
    assert result.markers == ["incomplete"]
    assert result.gateway_exit == "1"
    assert result.token["resume_needed"] is True
    assert result.leftover == list(leftover)


@pytest.mark.parametrize("code", [0, 1])
def test_child_that_never_claims_leaves_the_receipt_with_the_parent(hand_off, code):
    run, _release = hand_off
    _assert_parent_recorded(run(_child(code, claims=False)), f"exited {code} before claiming")


@pytest.mark.parametrize("code", [0, 1])
def test_claiming_child_owns_receipt_resume_and_exit_code(hand_off, code):
    run, _release = hand_off
    result = run(_child(code, claims=True))
    assert result.code == code
    assert result.steps is None
    assert result.markers == []
    assert result.gateway_exit is None
    assert result.token["resume_needed"] is False


def test_unremovable_leftover_hand_off_still_leaves_the_receipt_with_the_parent(hand_off, monkeypatch):
    run, _release = hand_off
    real_unlink = Path.unlink
    leftover = []

    def child(payload, *, argv_tail):
        leftover.append(write_handoff(payload))

        def unlink(self, *args, **kwargs):
            if self == leftover[0]:
                raise PermissionError(13, "Permission denied", str(self))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        return 0

    _assert_parent_recorded(run(child), "exited 0 before claiming", leftover=leftover)


def test_child_that_could_not_start_leaves_the_receipt_with_the_parent(hand_off):
    run, _release = hand_off
    _assert_parent_recorded(run(_child(None, claims=False)), "could not start")


# The guard blocks any ``hermes update`` command line. This one runs with cwd and PYTHONPATH
# pinned to a throwaway release whose ``hermes_cli`` raises on import, so no updater code runs.
@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("posix")
def test_staged_interpreter_crashing_on_import_never_claims(hand_off):
    run, release = hand_off
    (release / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, release / ".venv" / "bin" / "python")
    (release / "hermes_cli").mkdir()
    (release / "hermes_cli" / "__init__.py").write_text("")
    (release / "hermes_cli" / "main.py").write_text(
        'raise ImportError("staged release cannot import its dependencies")\n')
    real_child = immutable_update_handoff.continue_update_in_fresh_interpreter
    _assert_parent_recorded(run(real_child), "exited 1 before claiming")
