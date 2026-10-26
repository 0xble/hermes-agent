"""pm.extras: anchor availability, ensure_import, ensure_and_bind, and the
spec→extra install shim. Network-free — sync_venv is stubbed at the client
seam used by extras; the engine and worker have separate transaction tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import pm
from pm import client
from pm import extras


# ---- per-extra platform gates ([tool.hermes.extras-platforms]) ----


def test_extra_supported_ungated_extra_is_true():
    extras._PLATFORM_GATES = None
    assert extras.extra_supported("no-such-gate-for-this-one") is True


def test_ensure_import_raises_on_gated_off_extra(monkeypatch, synced):
    from packaging.markers import default_environment

    version = default_environment()["python_full_version"]
    monkeypatch.setattr(extras, "_PLATFORM_GATES", {
        "gated-extra": f"python_full_version < '{version}'",
    })
    monkeypatch.setattr(extras, "available", lambda e: False)
    with pytest.raises(pm.InstallError, match="not supported on this platform"):
        extras.ensure_import("gated-extra")
    assert synced == []


def test_sync_refuses_python_gated_extra_before_touching_environment(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path
    from packaging.markers import default_environment

    engine = importlib.import_module("pm.install")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    version = default_environment()["python_full_version"]
    monkeypatch.setattr(extras, "_PLATFORM_GATES", {
        "unavailable-engine": f"python_full_version < '{version}'",
    })
    # Installed caller anchors cannot make a new managed graph compatible.
    monkeypatch.setitem(sys.modules, "unavailable_engine", SimpleNamespace())
    assert extras.extra_supported("unavailable-engine")
    assert not extras.extra_supported("unavailable-engine", importable=lambda _: False)

    def refuse_environment_access(*args, **kwargs):
        pytest.fail("unsupported request reached dependency environment machinery")

    monkeypatch.setattr(engine, "get_package", refuse_environment_access)
    with pytest.raises(pm.InstallError, match="not supported by this Python/platform"):
        engine.sync_venv(["unavailable-engine"], explicit=True)


def test_declared_extra_gates_match_dependency_selection():
    import tomllib
    from pathlib import Path
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    optional = metadata["project"]["optional-dependencies"]
    targets = [
        ("linux", "Linux", "x86_64"), ("linux", "Linux", "aarch64"),
        ("darwin", "Darwin", "x86_64"), ("darwin", "Darwin", "arm64"),
        ("win32", "Windows", "AMD64"), ("win32", "Windows", "ARM64"),
        ("android", "Android", "aarch64"),
    ]
    for system, platform_system, machine in targets:
        for python in ("3.12", "3.13", "3.14"):
            environment = {**default_environment(), "sys_platform": system,
                           "platform_system": platform_system, "platform_machine": machine,
                           "python_version": python, "python_full_version": python + ".0"}
            for extra in metadata["tool"]["hermes"]["extras-platforms"]:
                selected = any(req.marker is None or req.marker.evaluate(environment)
                               for req in map(Requirement, optional[extra]))
                assert extras.extra_supported(extra, environment=environment,
                                              importable=lambda _: False) == selected, (
                    extra, system, machine, python,
                )


def test_faster_whisper_targets_are_gated(monkeypatch):
    """The local-STT extra's anchor is faster-whisper, which has no win_arm64 or darwin-x64 build.

    An extra whose anchor can never import there must be refused up front: without the gate,
    ensure_import rebuilt the whole dependency environment and still failed the anchor — on every
    status probe, forever. ``voice`` is deliberately NOT gated (its sounddevice/numpy are
    installable on those targets; see the selection test above), so the lazy STT path asks for
    ``stt-whisper``, the extra that carries only faster-whisper.
    """
    monkeypatch.setattr(extras, "_PLATFORM_GATES", None)
    targets = {
        "win32-arm64": {"sys_platform": "win32", "platform_system": "Windows",
                        "platform_machine": "ARM64", "os_name": "nt"},
        "darwin-x64": {"sys_platform": "darwin", "platform_system": "Darwin",
                       "platform_machine": "x86_64", "os_name": "posix"},
        "linux-x64": {"sys_platform": "linux", "platform_system": "Linux",
                      "platform_machine": "x86_64", "os_name": "posix"},
    }
    supported = {
        target: extras.extra_supported("stt-whisper", environment=environment,
                                       importable=lambda _: False)
        for target, environment in targets.items()
    }
    assert supported == {"win32-arm64": False, "darwin-x64": False, "linux-x64": True}


def test_documents_extra_is_gated_off_termux_and_android(monkeypatch):
    """PyMuPDF has no wheel for Bionic libc. Termux reports sys_platform 'linux' with an
    Android kernel release, so the gate must read platform_release, not only sys_platform."""
    monkeypatch.setattr(extras, "_PLATFORM_GATES", None)
    base = {"platform_system": "Linux", "platform_machine": "aarch64", "os_name": "posix"}
    targets = {
        "termux": {**base, "sys_platform": "linux", "platform_release": "5.15.148-android14-11-g1f8a"},
        "android": {**base, "sys_platform": "android", "platform_release": "5.15.148-android14-11-g1f8a"},
        "linux-aarch64": {**base, "sys_platform": "linux", "platform_release": "6.8.0-45-generic"},
        "win32-arm64": {"sys_platform": "win32", "platform_system": "Windows", "platform_machine": "ARM64",
                        "os_name": "nt", "platform_release": "11"},
    }
    supported = {
        target: extras.extra_supported("documents", environment=environment, importable=lambda _: False)
        for target, environment in targets.items()
    }
    assert supported == {"termux": False, "android": False, "linux-aarch64": True, "win32-arm64": False}


def test_runtime_marker_environment_includes_platform_release(monkeypatch):
    seen: dict[str, str] = {}
    monkeypatch.setattr(extras, "_platform_gates", lambda: {"probe": "'android' not in platform_release"})

    class Recorder:
        def __init__(self, marker):
            pass

        def evaluate(self, environment):
            seen.update(environment)
            return True

    import packaging.markers
    monkeypatch.setattr(packaging.markers, "Marker", Recorder)
    assert extras.extra_supported("probe", importable=lambda _: False) is True
    assert "platform_release" in seen


@pytest.fixture
def synced(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(client, "sync_venv", lambda x=None: calls.append(list(x or [])))
    return calls



def test_available_missing_module():
    assert extras.available("no-such-extra-anywhere") is False


@pytest.mark.parametrize(("extra", "module"), [
    ("teams", "microsoft_teams.apps"),
])
def test_available_counts_sys_modules_fakes(monkeypatch, extra, module):
    monkeypatch.setitem(sys.modules, module, SimpleNamespace())
    assert extras.available(extra) is True


def test_google_readiness_requires_its_oauth_imports(monkeypatch):
    present = {"googleapiclient", "google.auth", "google_auth_httplib2"}
    monkeypatch.setattr(extras, "_importable", lambda name: name in present)
    assert not extras.available("google")
    present.add("google_auth_oauthlib.flow")
    assert extras.available("google")


def test_available_unknown_extra_uses_underscore_guess(monkeypatch):
    monkeypatch.setitem(sys.modules, "some_new_thing", SimpleNamespace())
    assert extras.available("some-new-thing") is True


def test_ensure_import_noop_when_available(monkeypatch, synced):
    monkeypatch.setitem(sys.modules, "fal_client", SimpleNamespace())
    extras.ensure_import("fal")
    assert synced == []


def test_ensure_import_syncs_when_missing(monkeypatch, synced, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(extras, "available", lambda e: False)
    extras.ensure_import("fal")
    assert synced == [["fal"]]


def test_ensure_import_respects_terminal_decline_without_installing(monkeypatch, synced):
    import builtins

    monkeypatch.setattr(extras, "available", lambda _: False)
    monkeypatch.setattr(extras, "missing", lambda _: ["fal_client"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: True))
    prompts = []
    monkeypatch.setattr(builtins, "input", lambda text: prompts.append(text) or "n")
    with pytest.raises(pm.InstallError, match="declined"):
        extras.ensure_import("fal")
    # Users know the feature, not the Python modules behind it.
    assert "'fal' feature" in prompts[0] and "fal_client" not in prompts[0]
    assert synced == []


def test_ensure_import_propagates_install_error(monkeypatch):
    def boom(x=None):
        raise pm.InstallError("venv", "lazy installs are disabled")

    monkeypatch.setattr(client, "sync_venv", boom)
    monkeypatch.setattr(extras, "available", lambda e: False)
    with pytest.raises(pm.InstallError):
        extras.ensure_import("fal")


@pytest.mark.parametrize("failure", [None, "install", "import"])
def test_ensure_and_bind_preserves_target_on_failure(monkeypatch, failure):
    monkeypatch.setattr(extras, "available", lambda _: failure != "install")
    def sync(*args):
        raise pm.InstallError("venv", "install refused")
    monkeypatch.setattr(client, "sync_venv", sync)
    def importer():
        if failure == "import":
            raise ImportError("still broken")
        return {"NAME": 42}
    target = {"existing": "kept"}
    assert extras.ensure_and_bind("fal", importer, target) is (failure is None)
    assert target == ({"existing": "kept", "NAME": 42} if failure is None else {"existing": "kept"})




def test_every_anchor_extra_exists_in_pyproject():
    """Contract: ANCHORS maps real pyproject extras (no orphaned names)."""
    import tomllib
    from pathlib import Path

    py = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = set(py["project"]["optional-dependencies"])
    orphans = set(extras.ANCHORS) - declared
    assert not orphans, f"ANCHORS names extras pyproject does not declare: {sorted(orphans)}"


def test_legacy_selection_carries_extras_the_main_era_venv_lazily_installed(monkeypatch, tmp_path):
    """Migrating a main-era venv must keep opt-in extras it already had (FAL
    image generation, a messaging SDK), or the first PM launch prompts to
    reinstall them. Umbrella and gated-off extras are never carried."""
    monkeypatch.setattr(extras, "_PLATFORM_GATES", {"piper": "python_version < '0'"})
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "fal_client").mkdir(parents=True)
    (site / "telegram").mkdir()
    (site / "piper").mkdir()
    (site / "google").mkdir()
    (site / "google" / "auth").mkdir()
    (site / "exa_py.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
    (site / "hindsight_client").mkdir()

    selection = extras.legacy_selection(tmp_path)

    assert selection[0] == "all"
    assert {"fal", "telegram", "vertex", "exa"} <= set(selection)
    assert "messaging" not in selection
    assert "piper" not in selection
    assert "hindsight" not in selection  # Catalog plugin owns this dependency, not a core extra.
    assert extras.legacy_selection(tmp_path / "no-venv") == ["all"]




@pytest.mark.parametrize("provider_source", ["bundled", "catalog", "bundled-with-user-copy"])
def test_immutable_release_first_generation_keeps_provider_deps_without_extra(
    monkeypatch, tmp_path, provider_source,
):
    """First-generation selection keeps leaf extras; the selected plugin owns its deps."""
    import json
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from pm import paths
    from pm.environments import runtime_facts_path, selected_venv, site_packages
    from pm.install import _target_selection
    from pm.lock import Facts
    from pm.packages import Venv
    from tests.pm._fixtures import _wheel

    home = tmp_path / "home"
    candidate = home / "releases" / "new"
    candidate.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(paths, "repo_root", lambda: candidate)
    monkeypatch.setattr(paths, "lockfile_path", lambda: tmp_path / "tool-lock.json")
    uv = shutil.which("uv")
    assert uv, "the regression requires the real offline resolver"
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "hindsight_client", "0.10.1")
    (candidate / "pyproject.toml").write_text(
        '[project]\nname="hermes-agent"\nversion="1"\nrequires-python=">=3.11"\n'
        '[project.optional-dependencies]\nall=[]\nfal=[]\nhindsight=[]\n'
        '[tool.uv]\npackage=false\nno-index=true\noffline=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8",
    )
    subprocess.run([uv, "lock", "--python", sys.executable], cwd=candidate,
                   env=dict(os.environ), check=True, capture_output=True, timeout=60)
    config = home / "config.yaml"
    config.write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
    provider = (home / "plugins" / "hindsight" if provider_source == "catalog"
                else candidate / "plugins" / "memory" / "hindsight")
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_text("# MemoryProvider fixture\n", encoding="utf-8")
    (provider / "plugin.yaml").write_text(
        'name: hindsight\nversion: 1.0.0\npip_dependencies: ["hindsight-client>=0.10.1,<1"]\n',
        encoding="utf-8",
    )
    if provider_source == "bundled-with-user-copy":
        user_copy = home / "plugins" / "hindsight"
        user_copy.mkdir(parents=True)
        (user_copy / "plugin.yaml").write_text(
            'name: hindsight\npip_dependencies: ["not-the-active-provider==99"]\n',
            encoding="utf-8",
        )

    # The payload already carried both a real leaf extra and the provider client.
    # An undeclared anchor must be pruned, and the client must NOT become a core extra.
    site = site_packages(candidate / ".venv")
    for module in ("fal_client", "hindsight_client", "telegram"):
        (site / module).mkdir(parents=True)
    facts = Facts(runtime_facts_path(candidate), strict=True)
    assert facts.get("venv") is None, "this must exercise a brand-new install key"
    package = Venv(candidate)
    enabled, stamp, inputs = _target_selection(
        package, {}, extras=None, inputs={}, repair=False, shipped=None, frozen=None,
    )
    assert enabled == ["fal"]
    prepared = package.apply(enabled, explicit=True, **inputs)
    facts.record_state("venv", stamp, enabled, **prepared)
    fact = Facts(runtime_facts_path(candidate), strict=True).get("venv")
    assert fact is not None
    assert fact["extras"] == ["fal"]
    assert selected_venv(candidate) != candidate / ".venv"
    python = selected_venv(candidate) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run([str(python), "-I", "-c",
                            "import hindsight_client; print(hindsight_client.__version__)"],
                           check=True, capture_output=True, text=True, timeout=30)
    assert probe.stdout.strip() == "0.10.1"

    # Removing the provider must remove its dependency from the next generation,
    # not preserve a sticky hindsight extra inferred from the previous payload.
    config.write_text("memory:\n  provider: none\n", encoding="utf-8")
    enabled, next_stamp, inputs = _target_selection(
        package, fact, extras=None, inputs={}, repair=False, shipped=None, frozen=None,
    )
    assert enabled == ["fal"] and next_stamp != stamp
    prepared = package.apply(enabled, explicit=True, **inputs)
    facts.record_state("venv", next_stamp, enabled, **prepared)
    python = selected_venv(candidate) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-I", "-c",
                    "import importlib.util; assert importlib.util.find_spec('hindsight_client') is None"],
                   check=True, capture_output=True, text=True, timeout=30)


def test_core_dependency_anchors_do_not_infer_optional_extras(monkeypatch, tmp_path):
    """Core fastapi/Pillow in a payload must not manufacture web/vision selections."""
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "fastapi").mkdir(parents=True)
    (site / "PIL").mkdir()
    pillow_info = site / "Pillow-1.0.dist-info"
    pillow_info.mkdir()
    (pillow_info / "METADATA").write_text("Name: Pillow\n", encoding="utf-8")
    (pillow_info / "top_level.txt").write_text("PIL\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = [\"fastapi==1\", \"Pillow==1\"]\n"
        "[project.optional-dependencies]\nweb = [\"fastapi==1\"]\nvision = []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(extras, "_PLATFORM_GATES", {})
    monkeypatch.setattr("pm.environments.base_venv", lambda _root: tmp_path / "venv")
    monkeypatch.setattr("pm.environments.site_packages", lambda _venv: site)

    assert "web" not in extras.installed_selection(tmp_path)
    assert "vision" not in extras.installed_selection(tmp_path)
    assert extras.legacy_selection(tmp_path) == ["all"]


def test_runtime_marker_evaluation_answers_for_the_given_environment():
    """The delegate really evaluates the marker (in PM's runtime interpreter)."""
    import subprocess
    from pathlib import Path

    helper = Path(extras.__file__).with_name("_marker_eval.py")
    env = '{"sys_platform": "linux"}'
    out = [subprocess.run([sys.executable, str(helper), marker, env], capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()
           for marker in ("sys_platform == 'linux'", "sys_platform == 'win32'")]
    assert out == ["1", "0"]
