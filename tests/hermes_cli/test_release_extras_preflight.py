"""Activation must protect the selected PM environment even outside candidate code."""
import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases
from pm.environments import runtime_facts_path


def release(home, name, extras):
    root = home / "releases" / name
    root.mkdir(parents=True)
    for marker in (".release-ready", ".hermes_build_sha"):
        (root / marker).write_text(name, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\nversion='1'\n[project.optional-dependencies]\n" +
        "\n".join(f"{extra}=['{package}>=1,<2']" for extra, package in extras.items()), encoding="utf-8")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    return root


def selected(root, tmp_path, extras):
    from pm.environments import install_state_dir
    env = install_state_dir(root) / "environments" / ("gen-" + root.name)
    venv.EnvBuilder(with_pip=False).create(env)
    facts = runtime_facts_path(root)
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(env), "extras": extras}}}), encoding="utf-8")
    return env


def package(env, name, module):
    site = next((env / "lib").glob("python*/site-packages"))
    info = site / (name.replace('-', '_') + "-1.0.dist-info")
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n", encoding="utf-8")
    (info / "top_level.txt").write_text(module, encoding="utf-8")
    (site / (module + ".py")).write_text("class Hindsight: pass\n", encoding="utf-8")


@pytest.mark.parametrize("extra,distribution,module", [
    ("hindsight", "hindsight-client", "hindsight_client"), ("widget", "widget-client", "widget_client")])
@pytest.mark.parametrize("damage", ["absent", "metadata-only", "broken-import"])
@pytest.mark.parametrize("route", ["promote", "rollback", "recover"])
def test_activation_outside_candidate_checks_generation_loss_by_operation(tmp_path, monkeypatch, extra, distribution, module, route, damage):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    a = release(home, "A", {extra: distribution})
    b = release(home, "B", {extra: distribution})
    releases.promote(home, a)
    package(selected(a, tmp_path, [extra]), distribution, module)
    # The payload has the client, but PM's committed generation wins at boot.
    package(b / ".venv", distribution, module)
    generation = selected(b, tmp_path, [])
    if damage != "absent":
        package(generation, distribution, module)
        payload = next((generation / "lib").glob(f"python*/site-packages/{module}.py"))
        if damage == "metadata-only":
            payload.unlink()
        else:
            payload.write_text("raise ModuleNotFoundError('client dependency missing')\n", encoding="utf-8")
    if route == "rollback":
        (home / "previous").symlink_to(b)
    if route == "rollback":
        releases.rollback(home)
        assert releases.read_pointer(home / "current") == b
        return
    with pytest.raises(RuntimeError, match=extra):
        if route == "recover":
            with monkeypatch.context() as m:
                m.setattr(releases, "_run_transaction", lambda *args, **kwargs: {})
                # Model a crash after an accepted preflight and durable intent.
                m.setattr("hermes_cli.immutable_release_preflight.activation_preflight", lambda *args, **kwargs: None)
                releases.promote(home, b)
            # Resuming an update still enforces the forward regression rule.
            releases.recover_pending_transaction(home)
        else:
            releases.promote(home, b)
    assert releases.read_pointer(home / "current") == a
    # A corrected committed environment permits the same transition.
    from pm.environments import project_python
    package(Path(project_python(b)).parent.parent, distribution, module)
    releases.recover_pending_transaction(home) if route == "recover" else releases.promote(home, b)
    assert releases.read_pointer(home / "current") == b


def test_configured_provider_warns_on_first_activation_without_an_origin(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
    candidate = release(home, "B", {"hindsight": "hindsight-client"})
    plugin = candidate / "plugins/memory/hindsight"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: hindsight\npip_dependencies: ['hindsight-client>=1,<2']\n", encoding="utf-8")
    # The candidate owns its readiness API, even when the caller is older code.
    (candidate / "plugins/memory/__init__.py").write_text(
        "import importlib.util\n"
        "class Provider:\n"
        " def is_available(self): return importlib.util.find_spec('hindsight_client') is not None\n"
        " def unavailable_reason(self): return 'hindsight-client missing'\n"
        "def load_memory_provider(name, register_skills=False): return Provider()\n", encoding="utf-8")
    releases.promote(home, candidate)
    assert releases.read_pointer(home / "current") == candidate
    assert "hindsight-client" in caplog.text and "availability unknown" in caplog.text


def test_candidate_owned_carry_repairs_before_the_transaction_gate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    previous = release(home, "A", {"hindsight": "hindsight-client"})
    candidate = release(home, "B", {"hindsight": "hindsight-client"})
    releases.promote(home, previous)
    package(selected(previous, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
    generation = selected(candidate, tmp_path, [])
    monkeypatch.setattr("pm.paths.repo_root", lambda: candidate)
    calls = []

    def sync(extras, **kwargs):
        assert kwargs == {"explicit": True, "project_root": candidate, "evict_incompatible_plugins": True}
        calls.append(extras)
        package(generation, "hindsight-client", "hindsight_client")

    monkeypatch.setattr("pm.sync_venv", sync)
    releases.promote(home, candidate)
    assert calls == [["hindsight"]]
    assert releases.read_pointer(home / "current") == candidate


@pytest.mark.parametrize("operation", ["rollback", "restore", "promote"])
def test_transition_across_differing_extras_warns_and_checks_retained_extra(tmp_path, monkeypatch, caplog, operation):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    a = release(home, "A", {"hindsight": "hindsight-client"})
    b = release(home, "B", {"hindsight": "hindsight-client", "newthing": "newthing-client"})
    package(selected(a, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
    generation = selected(b, tmp_path, ["hindsight", "newthing"])
    package(generation, "hindsight-client", "hindsight_client")
    package(generation, "newthing-client", "newthing_client")
    releases.promote(home, a)
    releases.promote(home, b)
    if operation == "rollback":
        releases.rollback(home)
    else:
        releases.activate_release(home, a, operation=operation)
    assert releases.read_pointer(home / "current") == a
    assert "newthing" in caplog.text and "no longer declares" in caplog.text
    assert "operator" in caplog.text.lower()


@pytest.mark.parametrize("operation", ["rollback", "restore", "first-migration-rollback"])
def test_recovery_never_repairs_target_environment(tmp_path, monkeypatch, operation):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = release(home, "A", {"hindsight": "hindsight-client"})
    b = release(home, "B", {"hindsight": "hindsight-client"})
    releases.promote(home, a)
    package(selected(a, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
    package(selected(b, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
    releases.promote(home, b)
    monkeypatch.setattr("pm.paths.repo_root", lambda: a)
    monkeypatch.setattr(releases, "carry_active_extras", lambda *args: pytest.fail("recovery attempted PM repair"))
    if operation == "first-migration-rollback":
        # A real source-layout journal exercises the transaction's separate arm.
        source = tmp_path / "source"
        a.rename(source)
        package(selected(source, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "pyproject.toml"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                        "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "source"], check=True)
        (home / "previous").unlink()
        (home / "previous").symlink_to(source)
        monkeypatch.setattr("pm.paths.repo_root", lambda: source)
        (home / "release-layout.json").write_text(json.dumps({
            "source": str(source), "source_sha": releases.release_sha(source), "plist": None, "state": "done"}), encoding="utf-8")
        result = releases.rollback(home)
        assert result["current"] == str(source)
        assert not (home / "current").exists()
    else:
        releases.activate_release(home, a, operation=operation)
        assert releases.read_pointer(home / "current") == a


@pytest.mark.parametrize("operation", ["promote", "rollback", "restore"])
@pytest.mark.parametrize("expires_in_probe", [False, True])
def test_activation_spends_one_shared_probe_budget(tmp_path, monkeypatch, caplog, operation, expires_in_probe):
    from hermes_cli import immutable_release_preflight as preflight
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = release(home, "A", {"hindsight": "hindsight-client"})
    b = release(home, "B", {"hindsight": "hindsight-client"})
    for root in (a, b):
        package(selected(root, tmp_path, ["hindsight"]), "hindsight-client", "hindsight_client")
    releases.promote(home, a)
    releases.promote(home, b)
    (home / "config.yaml").write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
    clock = [100.0]
    monkeypatch.setattr(preflight.time, "monotonic", lambda: clock[0])
    real_run, budgets = subprocess.run, []

    def spend(command, **kwargs):
        if "-c" in command:
            budgets.append(kwargs["timeout"])
            if expires_in_probe and len(budgets) == 3:
                clock[0] += kwargs["timeout"]
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            result = real_run(command, **kwargs)
            clock[0] += 0.25
            return result
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", spend)
    monkeypatch.setattr(releases, "carry_active_extras", lambda *args: pytest.fail("budgeted activation attempted repair"))
    releases.activate_release(home, a, operation=operation, deadline=115.75)
    assert releases.read_pointer(home / "current") == a
    assert "budget exhausted" in caplog.text
    assert budgets == [0.75, 0.5, 0.25]


@pytest.mark.parametrize("damage", ["unrelated-module", "absent", "broken-primary"])
def test_distribution_probe_imports_only_primary_module(tmp_path, monkeypatch, damage):
    from hermes_cli import immutable_release_preflight as preflight
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    candidate = release(home, "A", {})
    env = candidate / ".venv"
    site = next((env / "lib").glob("python*/site-packages"))
    if damage != "absent":
        package(env, "widget-client", "widget_client")
        (site / "widget_client-1.0.dist-info/top_level.txt").write_text("unrelated\nwidget_client\n", encoding="utf-8")
        (site / "unrelated.py").write_text("raise RuntimeError('must not import unrelated tooling')\n", encoding="utf-8")
    if damage == "broken-primary":
        (site / "widget_client.py").write_text("raise ImportError('primary client broken')\n", encoding="utf-8")
    observed = preflight._probe(candidate, preflight._PROBE, ["widget-client"], home)["widget-client"]
    if damage == "absent":
        assert "error" in observed and "version" not in observed
    elif damage == "broken-primary":
        assert observed["missing"] and "primary client broken" in observed["missing"][0]
    else:
        assert observed == {"version": "1.0", "missing": []}
