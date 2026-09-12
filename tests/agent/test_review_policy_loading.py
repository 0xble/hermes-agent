"""Manual review must never recover a broken policy into executable tools."""
import json
from types import SimpleNamespace

import pytest

from agent import review_engine
from hermes_cli import config, managed_scope


@pytest.fixture
def policy_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    managed_scope.invalidate_managed_cache()
    return home / "config.yaml", managed / "config.yaml"


def dispatch(monkeypatch):
    called = []
    monkeypatch.setattr("tools.delegate_tool.delegate_task", lambda **kw: (
        called.append(kw) or json.dumps({"status": "dispatched", "delegation_id": "fixture"})))
    def run():
        return review_engine.start_review(SimpleNamespace(), [{"role": "user", "content": "Review this"}])
    return called, run


@pytest.mark.parametrize("layer", [0, 1])
@pytest.mark.parametrize("broken", ["auxiliary: [", "[]", "false", "auxiliary: []", "auxiliary: {review: false}"])
@pytest.mark.parametrize("warm", [False, True])
def test_broken_layer_refuses_before_review_dispatch(policy_files, monkeypatch, layer, broken, warm):
    target = policy_files[layer]
    if warm:
        target.write_text("auxiliary: {review: {tool_policy: inspection_only}}")
        config.load_config_readonly()
    target.write_text(broken)
    called, run = dispatch(monkeypatch)
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        run()
    assert called == []


@pytest.mark.parametrize("layer", [0, 1])
def test_unreadable_layer_refuses_before_dispatch(policy_files, monkeypatch, layer):
    target = policy_files[layer]
    target.mkdir()  # deterministic unreadable-as-file on every test platform
    called, run = dispatch(monkeypatch)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        run()
    assert not called


@pytest.mark.parametrize("user,managed,expected", [
    (None, None, "legacy_unrestricted"),
    ("auxiliary: {review: {}}", None, "legacy_unrestricted"),
    ("auxiliary: {review: {tool_policy: legacy_unrestricted}}",
     "auxiliary: {review: {tool_policy: inspection_only}}", "inspection_only"),
])
def test_valid_effective_policy_dispatches(policy_files, monkeypatch, user, managed, expected):
    for path, body in zip(policy_files, (user, managed)):
        if body is not None:
            path.write_text(body)
    called, run = dispatch(monkeypatch)
    assert run()["status"] == "dispatched"
    assert called[0]["child_tool_policy"] == expected


def test_managed_change_during_policy_load_refuses(policy_files, monkeypatch):
    user, managed = policy_files
    user.write_text("{}")
    managed.write_text("auxiliary: {review: {tool_policy: inspection_only}}")
    original = config._merge_managed_overlay
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("strict"):
            managed.write_text("auxiliary: [")
        return result
    monkeypatch.setattr(config, "_merge_managed_overlay", changed)
    called, run = dispatch(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        run()
    assert not called


def test_repaired_policy_recovers_without_changing_general_loader(policy_files, monkeypatch):
    user, managed = policy_files
    managed.write_text("auxiliary: [")
    assert config.load_config_readonly()["auxiliary"]["review"]["tool_policy"] == "legacy_unrestricted"
    called, run = dispatch(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        run()
    managed.write_text("auxiliary: {review: {tool_policy: inspection_only}}")
    run()
    assert len(called) == 1
    assert called[0]["child_tool_policy"] == "inspection_only"


def test_transient_managed_read_failure_cannot_fall_open(policy_files, monkeypatch):
    import builtins
    _, managed = policy_files
    managed.write_text("auxiliary: {review: {tool_policy: inspection_only}}")
    real_open = builtins.open
    reads = []
    def fail_second(path, *args, **kwargs):
        if str(path) == str(managed):
            reads.append(path)
            if len(reads) > 1:
                raise PermissionError("transient managed access refusal")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", fail_second)
    called, run = dispatch(monkeypatch)
    with pytest.raises(RuntimeError, match="cannot be read"):
        run()
    assert len(reads) == 2
    assert not called


@pytest.mark.parametrize("layer", [0, 1])
def test_dangling_config_symlink_is_not_intentional_omission(policy_files, monkeypatch, layer):
    target = policy_files[layer]
    try:
        target.symlink_to("missing-config.yaml")
    except OSError:
        pytest.skip("symlink creation unavailable")
    called, run = dispatch(monkeypatch)
    with pytest.raises(RuntimeError, match="cannot be read"):
        run()
    assert not called
