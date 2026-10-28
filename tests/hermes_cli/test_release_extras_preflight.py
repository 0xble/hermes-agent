"""Activation must protect the selected PM environment even outside candidate code."""
import json
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
        (root / marker).write_text(name)
    (root / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\nversion='1'\n[project.optional-dependencies]\n" +
        "\n".join(f"{extra}=['{package}>=1,<2']" for extra, package in extras.items()))
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    return root


def selected(root, tmp_path, extras):
    from pm.environments import install_state_dir
    env = install_state_dir(root) / "environments" / ("gen-" + root.name)
    venv.EnvBuilder(with_pip=False).create(env)
    facts = runtime_facts_path(root)
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(env), "extras": extras}}}))
    return env


def package(env, name, module):
    site = next((env / "lib").glob("python*/site-packages"))
    info = site / (name.replace('-', '_') + "-1.0.dist-info")
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n")
    (info / "top_level.txt").write_text(module)
    (site / (module + ".py")).write_text("class Hindsight: pass\n")


@pytest.mark.parametrize("extra,distribution,module", [
    ("hindsight", "hindsight-client", "hindsight_client"), ("widget", "widget-client", "widget_client")])
@pytest.mark.parametrize("damage", ["absent", "metadata-only", "broken-import"])
@pytest.mark.parametrize("route", ["promote", "rollback", "recover"])
def test_activation_outside_candidate_refuses_selected_generation_loss(tmp_path, monkeypatch, extra, distribution, module, route, damage):
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
            payload.write_text("raise ModuleNotFoundError('client dependency missing')\n")
    if route == "rollback":
        (home / "previous").symlink_to(b)
    with pytest.raises(RuntimeError, match=extra):
        if route == "recover":
            with monkeypatch.context() as m:
                m.setattr(releases, "_run_transaction", lambda *args: {})
                releases.promote(home, b)
            releases.recover_pending_transaction(home)
        elif route == "rollback":
            releases.rollback(home)
        else:
            releases.promote(home, b)
    assert releases.read_pointer(home / "current") == a
    # A corrected committed environment permits the same transition.
    from pm.environments import project_python
    package(Path(project_python(b)).parent.parent, distribution, module)
    releases.recover_pending_transaction(home) if route == "recover" else releases.promote(home, b)
    assert releases.read_pointer(home / "current") == b


def test_configured_provider_blocks_first_activation_without_an_active_extra(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: hindsight\n")
    candidate = release(home, "B", {"hindsight": "hindsight-client"})
    plugin = candidate / "plugins/memory/hindsight"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("")
    (plugin / "plugin.yaml").write_text("name: hindsight\npip_dependencies: ['hindsight-client>=1,<2']\n")
    # The candidate owns its readiness API, even when the caller is older code.
    (candidate / "plugins/memory/__init__.py").write_text(
        "import importlib.util\n"
        "class Provider:\n"
        " def is_available(self): return importlib.util.find_spec('hindsight_client') is not None\n"
        " def unavailable_reason(self): return 'hindsight-client missing'\n"
        "def load_memory_provider(name, register_skills=False): return Provider()\n")
    with pytest.raises(RuntimeError, match="hindsight-client"):
        releases.promote(home, candidate)
    assert not (home / "current").exists()
    package(candidate / ".venv", "hindsight-client", "hindsight_client")
    releases.promote(home, candidate)
    assert releases.read_pointer(home / "current") == candidate


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
