"""Recovery remains available when both generations have dependency damage."""
import subprocess
import plistlib

import pytest

from hermes_cli import immutable_release_preflight as preflight
from hermes_cli import immutable_releases as releases
from tests.hermes_cli.test_release_extras_preflight import package, release, selected


def provider(root, *, available=False, old_api=False, crash=False, reason="client unavailable"):
    directory = root / "plugins/memory"
    directory.mkdir(parents=True, exist_ok=True)
    signature = "name" if old_api else "name, register_skills=False"
    body = "raise RuntimeError('provider probe crashed')" if crash else "return Provider()"
    (directory / "__init__.py").write_text(
        "class Provider:\n"
        f" def is_available(self): return {available!r}\n"
        f" def unavailable_reason(self): return {reason!r}\n"
        f"def load_memory_provider({signature}): {body}\n", encoding="utf-8")


def pair(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    a, b = [release(home, name, {}) for name in ("A", "B")]
    releases.promote(home, a)
    releases.promote(home, b)
    (home / "config.yaml").write_text("memory: {provider: fixture}\n", encoding="utf-8")
    return home, a, b


@pytest.mark.parametrize("operation,damage", [
    ("rollback", "both-degraded"),
    ("rollback", "old-api"),
    ("restore", "crash"),
    ("first-migration-rollback", "crash"),
])
def test_recovery_activates_despite_provider_failure(tmp_path, monkeypatch, caplog, operation, damage):
    home, a, b = pair(tmp_path, monkeypatch)
    provider(a, old_api=damage == "old-api", crash=damage == "crash")
    provider(b)
    # The source-layout recovery arm is covered separately by the existing suite.
    if operation == "first-migration-rollback":
        preflight.activation_preflight(a, b, home, operation=operation)
        assert "Proceeding with recovery" in caplog.text
        return
    if operation == "rollback":
        releases.rollback(home)
    else:
        releases.activate_release(home, a, operation=operation)
    assert releases.read_pointer(home / "current") == a
    assert "Proceeding with recovery" in caplog.text
    assert {"both-degraded": "unavailable", "old-api": "register_skills", "crash": "crashed"}[damage] in caplog.text


@pytest.mark.parametrize("feature", ["extra", "provider", "provider-without-reason"])
def test_forward_identical_degradation_across_three_releases(tmp_path, monkeypatch, caplog, feature):
    home, a, b = pair(tmp_path, monkeypatch)
    c = release(home, "C", {"widget": "widget-client"} if feature == "extra" else {})
    if feature == "extra":
        for root in (a, b):
            (root / "pyproject.toml").write_text((c / "pyproject.toml").read_text(encoding="utf-8-sig"), encoding="utf-8")
            selected(root, tmp_path, ["widget"])
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    else:
        for root in (a, b, c):
            provider(root, reason="" if feature == "provider-without-reason" else "client unavailable")
    releases.rollback(home)
    releases.promote(home, b)
    releases.promote(home, c)
    assert releases.read_pointer(home / "current") == c
    assert "already degraded" in caplog.text
    # Restoring origin health turns the same candidate loss into a regression.
    if feature == "extra":
        package(selected(b, tmp_path, ["widget"]), "widget-client", "widget_client")
    else:
        provider(b, available=True)
    releases.activate_release(home, b, operation="restore")
    with pytest.raises(RuntimeError, match="re-run the update.*candidate") as refusal:
        releases.promote(home, c)
    assert str(c) in str(refusal.value)
    assert "repair the candidate environment explicitly" in str(refusal.value)
    assert releases.read_pointer(home / "current") == b


@pytest.mark.parametrize("operation", ["rollback", "restore"])
def test_exhausted_recovery_preflight_still_reloads(tmp_path, monkeypatch, caplog, operation):
    home, a, b = pair(tmp_path, monkeypatch)
    other = home / "profiles/other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("memory: {provider: fixture}\n", encoding="utf-8")
    monkeypatch.setattr("pm.plugins_state.dependency_homes", lambda: [other])
    clock = [100.0]
    monkeypatch.setattr(preflight.time, "monotonic", lambda: clock[0])
    probes, reloads = [], []

    def exhaust(candidate, script, values, home, deadline=None):
        probes.append(candidate)
        clock[0] = deadline
        raise subprocess.TimeoutExpired("probe", 1)

    def reload():
        assert clock[0] <= 115.0, "preflight consumed the 15-second reload/bootout reserve"
        assert releases.read_pointer(home / "current") == a
        reloads.append(True)
        return True

    monkeypatch.setattr(preflight, "_probe", exhaust)
    plist = home / "gateway.plist"
    body = plistlib.dumps({"Label": "fixture"})
    plist.write_bytes(body)
    releases.activate_release(home, a, operation=operation, reload_callback=reload, deadline=130.0,
                              plist_path=plist, plist_body=body)
    assert reloads == [True]
    assert probes == [a]
    assert "budget exhausted" in caplog.text


@pytest.mark.parametrize("operation", ["rollback", "restore"])
def test_recover_pending_recovery_transaction_keeps_probes_advisory(tmp_path, monkeypatch, caplog, operation):
    home, a, b = pair(tmp_path, monkeypatch)
    provider(a, crash=True)
    with monkeypatch.context() as context:
        context.setattr(releases, "_run_transaction", lambda *args, **kwargs: {})
        releases.activate_release(home, a, operation=operation)
    releases.recover_pending_transaction(home)
    assert releases.read_pointer(home / "current") == a
    assert "crashed" in caplog.text and "Proceeding with recovery" in caplog.text


def test_guardian_restore_reloads_even_when_provider_probe_crashes(tmp_path, monkeypatch, caplog):
    from hermes_cli import gateway_guardian as guardian
    home, a, b = pair(tmp_path, monkeypatch)
    provider(a, crash=True)
    body = plistlib.dumps({"Label": "fixture"})
    plist = home / "gateway.plist"
    plist.write_bytes(body)
    reloads = []
    monkeypatch.setattr(guardian, "_await_healthy", lambda *args: True)
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda *args: True)
    guardian._restore_running(home, plist, "fixture", a, body, [0], lambda: reloads.append(True) or True)
    assert reloads == [True]
    assert releases.read_pointer(home / "current") == a
    assert "crashed" in caplog.text and "Proceeding with recovery" in caplog.text


def test_forward_still_compares_environments_when_candidate_carry_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    a, b = [release(home, name, {"widget": "widget-client"}) for name in ("A", "B")]
    package(selected(a, tmp_path, ["widget"]), "widget-client", "widget_client")
    releases.promote(home, a)

    def fail(*args):
        raise RuntimeError("candidate carry failed")

    monkeypatch.setattr(releases, "carry_active_extras", fail)
    with pytest.raises(RuntimeError, match="release activation refused.*widget"):
        releases.promote(home, b)
    assert releases.read_pointer(home / "current") == a
