"""S2 immutable-release invariants."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


def test_archive_fallback_rejects_unsafe_members(tmp_path, monkeypatch):
    import io
    import tarfile
    from types import SimpleNamespace

    def archive(name, kind):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = 0
            tar.addfile(member)
        return raw.getvalue()

    for index, (name, kind) in enumerate((("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                                          ("device", tarfile.CHRTYPE), ("link", tarfile.SYMTYPE))):
        monkeypatch.setattr(releases.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=archive(name, kind)))
        with pytest.raises(RuntimeError, match="unsafe git archive member"):
            releases._stage_git_tree(tmp_path, tmp_path / f"stage-{index}", "fake")
    monkeypatch.setattr(releases.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=archive("safe/file", tarfile.REGTYPE)))
    releases._stage_git_tree(tmp_path, tmp_path / "stage", "fake")
    assert (tmp_path / "stage" / "safe" / "file").is_file()


@pytest.mark.platforms("macos")
def test_release_plist_executes_selected_release_with_managed_source_launcher(tmp_path, monkeypatch):
    import plistlib
    import shlex
    import shutil
    import venv
    from hermes_cli import gateway, stderr_timestamp
    from hermes_cli._launchers import resolve_store_python
    from pm.environments import store_root

    home = tmp_path / "profile"
    source = tmp_path / "source"
    (home / "config.yaml").parent.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "gateway:\n  forward_only_handover:\n    enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "PROJECT_ROOT", source)
    monkeypatch.setattr(gateway, "get_python_path", lambda: sys.executable)
    runtime = store_root(source)
    tool = runtime / "python-test" / "bin" / "python3"
    tool.parent.mkdir(parents=True)
    tool.symlink_to(sys.executable)
    (runtime / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "python-test"}}}), encoding="utf-8")
    assert resolve_store_python(source) == tool
    launcher = source / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    output = home / "observed.json"
    source_probe = f"import pathlib; pathlib.Path({str(output)!r}).write_text('source', encoding='utf-8')"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-c", source_probe]) + "\n", encoding="utf-8")
    launcher.chmod(0o755)
    normal = plistlib.loads(gateway.generate_launchd_plist().encode())
    subprocess.run(normal["ProgramArguments"], check=True, timeout=30)
    assert output.read_text(encoding="utf-8") == "source"

    for marker in ("A", "B"):
        root = home / "releases" / marker
        _fake_release(root, marker)
        (root / ".venv" / "bin" / "python").unlink()
        venv.EnvBuilder(with_pip=False).create(root / ".venv")
        package = root / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(stderr_timestamp.__file__, package / "stderr_timestamp.py")
        (root / "hermes_bootstrap.py").write_text(
            "import os,pathlib\n"
            "pathlib.Path(os.environ['HERMES_HOME'], 'bootstrap-root').write_text(str(pathlib.Path(__file__).parent), encoding='utf-8')\n", encoding="utf-8")
        (package / "main.py").write_text(
            "import json,os,pathlib,sys\n"
            "pathlib.Path(os.environ['HERMES_HOME'], 'observed.json').write_text(json.dumps({'root':str(pathlib.Path(__file__).parent.parent), 'python':sys.executable, 'argv':sys.argv[1:]}), encoding='utf-8')\n", encoding="utf-8")

    for marker in ("A", "B", "A"):
        candidate = home / "releases" / marker
        releases.promote(home, candidate)
        release = plistlib.loads(gateway.generate_launchd_plist(release_target=candidate).encode())
        subprocess.run(release["ProgramArguments"], cwd=release["WorkingDirectory"],
                       env={**os.environ, **release["EnvironmentVariables"]}, check=True, timeout=30)
        observed = json.loads(output.read_text(encoding="utf-8"))
        assert observed == {"root": str(candidate), "python": str(candidate / ".venv/bin/python"),
                            "argv": ["gateway", "run", "--external-supervisor"]}
        assert (home / "bootstrap-root").read_text(encoding="utf-8") == str(candidate)
        assert release["EnvironmentVariables"]["PATH"].split(":")[0] == str(home / "current/.venv/bin")
        unchanged_keys = normal.keys() - {"ProgramArguments", "WorkingDirectory", "EnvironmentVariables"}
        assert {key: normal[key] for key in unchanged_keys} == {key: release[key] for key in unchanged_keys}


def _fake_release(path: Path, marker: str, *, lock: str = "same") -> None:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(f"[project]\nname='hermes-{marker}'\n")
    (path / "uv.lock").write_text(lock)
    venv = path / ".venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    (path / ".release-ready").write_text(path.name + "\n", encoding="utf-8")
    (path / ".hermes_build_sha").write_text(path.name + "\n", encoding="utf-8")


def test_git_staging_with_home_nested_in_checkout_reads_real_identity(tmp_path, monkeypatch):
    """The release is exactly tracked HEAD, not its own staging dir or profile state."""
    source = tmp_path / "source"
    module = source / "hermes_cli"
    module.mkdir(parents=True)
    from hermes_cli import version_info
    import shutil
    shutil.copy2(Path(version_info.__file__), module / "version_info.py")
    (module / "__init__.py").write_text("")
    (module / "main.py").write_text("print(__file__)\n")
    (module / "immutable_releases.py").write_text("# staging capability probe\n")
    (source / "pyproject.toml").write_text("[project]\nname='staged-probe'\nversion='1.0'\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "hermes_cli", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                    "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
    sha = releases.release_sha(source)
    home = source / "hermes_test"
    (home / "cache").mkdir(parents=True)
    (home / "cache" / "private.txt").write_text("never ship")
    (source / ".worktrees" / "other").mkdir(parents=True)
    bundle = source / "hermes_cli" / "web_dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("fresh built asset")
    monkeypatch.setattr(releases, "prepare_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: None)
    release, action = releases.stage_release(source, home)
    assert action == "staged"
    assert release == home / "releases" / sha
    assert not (release / "hermes_test").exists()
    assert not (release / ".worktrees").exists()
    assert not (release / ".git").exists()
    assert not (release / "hermes_cli" / "web_dist" / "index.html").exists()  # never copy A's untracked assets
    code = "import importlib.util, pathlib, sys; p=pathlib.Path(sys.argv[1]); s=importlib.util.spec_from_file_location('staged_version_info',p); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); m._resolve_stamp_file=lambda:p.parent.parent/'install-stamp.json'; print(m.get_code_identity(refresh=True)['sha'])"
    result = subprocess.run([sys.executable, "-c", code, str(release / "hermes_cli" / "version_info.py")],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == sha
    assert releases.stage_release(source, home)[1] == "existing"
    import venv
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    (home / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    releases.promote(home, release)
    from gateway import run as gateway_run
    monkeypatch.setattr(gateway_run, "__file__", str(release / "gateway" / "run.py"))
    argv = gateway_run._resolve_update_hermes_bin(home)
    assert argv is not None and argv[0] == str(source / ".venv" / "bin" / "python")
    launched = subprocess.run([*argv, "probe"], cwd=release,
                              capture_output=True, text=True)
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.strip() == str(source / "hermes_cli" / "main.py")


@pytest.mark.parametrize("layout", ["venv", ".venv", "external"])
def test_release_update_reentry_uses_validated_source_interpreter(tmp_path, monkeypatch, layout):
    """Gateway and CLI re-enter the same source interpreter, not release Python."""
    import venv
    from gateway import run as gateway_run
    from hermes_cli import main
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / "hermes_cli").mkdir()
    (source / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    interpreter = (tmp_path / "outside" if layout == "external" else source / layout) / "bin" / "python"
    venv.EnvBuilder(with_pip=False).create(interpreter.parent.parent)
    if layout == "external":
        for sibling in ("venv", ".venv"):
            venv.EnvBuilder(with_pip=False).create(source / sibling)
    home = tmp_path / "profile"
    release = home / "releases" / "A"
    _fake_release(release, "A")
    releases.promote(home, release)
    (home / "release-layout.json").write_text(json.dumps({
        "source": str(source), "source_python": str(interpreter) if layout == "external" else None,
    }), encoding="utf-8")
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(gateway_run, "__file__", str(release / "gateway" / "run.py"))
    argv = gateway_run._resolve_update_hermes_bin(home)
    assert argv is not None and argv[0] == str(interpreter)
    monkeypatch.setattr(main, "PROJECT_ROOT", release)
    monkeypatch.setattr(main, "get_hermes_home", lambda: home)
    called = []
    def intercept(path, argv):
        called.append((path, argv))
        raise RuntimeError("intercepted exec")
    monkeypatch.setattr(main.os, "execv", intercept)
    with pytest.raises(RuntimeError, match="intercepted exec"):
        main.cmd_update(object())
    assert called[0][0] == str(interpreter)
    assert called[0][1][3] == str(source)


def test_migration_records_source_interpreter_and_preserves_it_on_retry(tmp_path, monkeypatch):
    import venv
    source = tmp_path / "source"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "hermes_cli"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                    "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
    interpreter = tmp_path / "external" / "bin" / "python"
    venv.EnvBuilder(with_pip=False).create(interpreter.parent.parent)
    home = tmp_path / "profile"
    monkeypatch.setattr(releases.sys, "executable", str(interpreter))
    candidate = home / "releases" / "B"
    _fake_release(candidate, "B")
    result = releases.activate_release(home, candidate, source=source, source_python=interpreter)
    assert result["source_sha"] == releases.release_sha(source)
    record = json.loads((home / "release-layout.json").read_text())
    assert record["source_python"] == str(interpreter)
    assert releases.activate_release(home, candidate, source=source)["current"] == str(candidate)
    assert json.loads((home / "release-layout.json").read_text())["source_python"] == str(interpreter)
    assert releases.source_checkout_python(home, source) == interpreter


def test_source_interpreter_missing_fails_loudly(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    home = tmp_path / "profile"
    release = home / "releases" / "A"
    _fake_release(release, "A")
    releases.promote(home, release)
    (home / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    monkeypatch.setattr(releases, "release_sha", lambda _: "A")
    from gateway import run as gateway_run
    from hermes_cli import main
    monkeypatch.setattr(gateway_run, "__file__", str(release / "gateway" / "run.py"))
    monkeypatch.setattr(main, "PROJECT_ROOT", release)
    monkeypatch.setattr(main, "get_hermes_home", lambda: home)
    with pytest.raises(RuntimeError, match="No usable source checkout interpreter"):
        gateway_run._resolve_update_hermes_bin(home)
    with pytest.raises(RuntimeError, match="No usable source checkout interpreter"):
        main.cmd_update(object())


def test_release_with_unreadable_migration_journal_fails_loudly(tmp_path, monkeypatch):
    from gateway import run as gateway_run
    from hermes_cli import main
    home = tmp_path / "profile"
    release = home / "releases" / "A"
    _fake_release(release, "A")
    releases.promote(home, release)
    monkeypatch.setattr(gateway_run, "__file__", str(release / "gateway" / "run.py"))
    monkeypatch.setattr(main, "PROJECT_ROOT", release)
    monkeypatch.setattr(main, "get_hermes_home", lambda: home)
    with pytest.raises(RuntimeError, match="repair release-layout.json"):
        gateway_run._resolve_update_hermes_bin(home)
    with pytest.raises(RuntimeError, match="repair release-layout.json"):
        main.cmd_update(object())


def test_relocate_removes_compiled_editable_finder_but_rejects_other_binary(tmp_path):
    import py_compile
    staging, target = tmp_path / ".staging-B", tmp_path / "B"
    finder = staging / ".venv" / "lib" / "site-packages" / "__editable___probe.py"
    finder.parent.mkdir(parents=True)
    finder.write_text(f"ROOT = {str(staging)!r}\n", encoding="utf-8")
    cached = Path(py_compile.compile(str(finder), doraise=True))
    assert b"\0" in cached.read_bytes() and os.fsencode(staging) in cached.read_bytes()
    releases._relocate_venv(staging, target)
    assert not cached.exists()
    assert str(target) in finder.read_text(encoding="utf-8")
    binary = finder.parent / "native.so"
    binary.write_bytes(b"\0" + os.fsencode(staging))
    with pytest.raises(RuntimeError, match="cannot relocate binary"):
        releases._relocate_venv(staging, target)


def test_source_unchanged_head_dirty_blocks_migration_rollback(tmp_path):
    source, home = tmp_path / "source", tmp_path / "profile"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("clean", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                    "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "A"], check=True)
    sha = releases.release_sha(source)
    current = home / "releases" / "B"
    _fake_release(current, "B")
    releases.promote(home, current)
    releases._atomic_symlink(home / "previous", source)
    (home / "release-layout.json").write_text(json.dumps({"source": str(source), "source_sha": sha,
                                                           "plist": None}), encoding="utf-8")
    (source / "tracked.txt").write_text("dirty", encoding="utf-8")
    assert releases.rollback(home)["source_sha"] == sha
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "dirty"
    assert not (home / "current").exists()
    assert not (home / "previous").exists()


def test_worker_env_pins_physical_venv_and_subprocess_path(tmp_path):
    home = tmp_path / "home"
    physical = home / "releases" / "A"
    _fake_release(physical, "A")
    executable, cwd, env = releases.worker_launch_spec(physical, {
        "VIRTUAL_ENV": str(home / "current" / ".venv"),
        "PATH": str(home / "current" / ".venv" / "bin") + os.pathsep + "/usr/bin",
    })
    assert executable == str(physical / ".venv" / "bin" / "python")
    assert cwd == physical
    assert env["VIRTUAL_ENV"] == str(physical / ".venv")
    assert env["PATH"].split(os.pathsep) == [str(physical / ".venv" / "bin"), "/usr/bin"]
    assert env["HERMES_RELEASE"] == str(physical)


def test_untracked_source_web_assets_are_not_copied_into_release(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "home"
    (source / "hermes_cli" / "web_dist").mkdir(parents=True)
    (source / "hermes_cli" / "immutable_releases.py").write_text("# test\n", encoding="utf-8")
    (source / "hermes_cli" / "web_dist" / "asset.js").write_text("build", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "hermes_cli/immutable_releases.py"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                    "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "A"], check=True)
    monkeypatch.setattr(releases, "prepare_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: None)
    release, _ = releases.stage_release(source, home)
    assert not (release / "hermes_cli/web_dist/asset.js").exists()
    assert not (release / "hermes_cli/web_dist/index.html").exists()
    assert (home / "releases" / releases.release_sha(source)).exists()


def test_active_locked_extra_is_passed_to_frozen_pm_build(tmp_path, monkeypatch):
    project = tmp_path / "candidate"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\nversion='0.1.0'\n"
        "[project.optional-dependencies]\nmessaging=['python-telegram-bot==22.8']\n",
        encoding="utf-8")
    (project / "uv.lock").write_text(
        '[[package]]\nname = "python-telegram-bot"\nversion = "22.8"\n', encoding="utf-8")
    monkeypatch.setattr(releases, "_active_distributions", lambda _: {"python-telegram-bot": "22.8"})
    extras = releases._active_locked_extras(Path(sys.executable), project)
    assert extras == ["messaging"]
    calls = []
    monkeypatch.setattr("pm.build_environment", lambda **kwargs: calls.append(kwargs))
    releases._build_venv(project, extras=extras, python=Path(sys.executable))
    assert calls[0]["extras"] == ["messaging"]
    assert calls[0]["source"] == project
    assert calls[0]["out"] == project / ".venv"
    assert calls[0]["frozen"] and calls[0]["explicit"]
    assert "python" not in calls[0]  # PM selects the candidate ABI, not the old interpreter.


def test_orphan_transitive_package_does_not_enable_another_feature(tmp_path, monkeypatch):
    project = tmp_path / "candidate"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\n[project.optional-dependencies]\n"
        "sdk=['sdk-client==1.0']\nembedded=['embedded-server==1.0']\n", encoding="utf-8")
    (project / "uv.lock").write_text(
        '[[package]]\nname="sdk-client"\nversion="1.0"\n'
        '[[package]]\nname="embedded-server"\nversion="1.0"\n'
        'dependencies=[{name="old-transitive"}]\n'
        '[[package]]\nname="old-transitive"\nversion="1.0"\n', encoding="utf-8")
    monkeypatch.setattr(releases, "_active_distributions",
                        lambda _: {"sdk-client": "1.0", "old-transitive": "1.0"})
    assert releases._active_locked_extras(Path(sys.executable), project) == ["sdk"]


def test_missing_locked_extra_fails_closed(tmp_path, monkeypatch):
    import venv
    import zipfile
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    for root in (source, candidate):
        venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (candidate / "uv.lock").write_text('[[package]]\nname = "locked-extra"\nversion = "1.0"\n', encoding="utf-8")
    (candidate / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\n[project.optional-dependencies]\n"
        "feature=['locked-extra==1.0']\n", encoding="utf-8")
    wheel = tmp_path / "locked_extra-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("locked_extra/__init__.py", "")
        archive.writestr("locked_extra-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: locked-extra\nVersion: 1.0\n")
        archive.writestr("locked_extra-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("locked_extra-1.0.dist-info/RECORD", "")
    subprocess.run(["uv", "pip", "install", "--python", str(releases._release_python(source)), str(wheel)], check=True)
    with pytest.raises(RuntimeError, match="candidate distribution parity failed: locked-extra"):
        releases.restore_active_distributions(source, candidate)

def test_candidate_retains_active_optional_feature_from_local_wheel(tmp_path, monkeypatch):
    import venv
    import zipfile
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    for root in (source, candidate):
        venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (candidate / "uv.lock").write_text("package = []\n", encoding="utf-8")
    wheel = tmp_path / "optional_s2_feature-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("optional_s2_feature/__init__.py", "VALUE = 'present'\n")
        archive.writestr("optional_s2_feature-1.0.dist-info/METADATA",
                         "Metadata-Version: 2.1\nName: optional-s2-feature\nVersion: 1.0\n")
        archive.writestr("optional_s2_feature-1.0.dist-info/WHEEL",
                         "Wheel-Version: 1.0\nGenerator: s2-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("optional_s2_feature-1.0.dist-info/RECORD", "")
    subprocess.run(["uv", "pip", "install", "--python", str(releases._release_python(source)), str(wheel)],
                   check=True, capture_output=True)
    monkeypatch.setenv("UV_FIND_LINKS", str(tmp_path))
    monkeypatch.setenv("UV_OFFLINE", "1")
    releases.restore_active_distributions(source, candidate)
    probe = subprocess.run([str(releases._release_python(candidate)), "-c",
                            "import optional_s2_feature; print(optional_s2_feature.VALUE)"],
                           check=True, capture_output=True, text=True)
    assert probe.stdout.strip() == "present"


def test_normal_update_without_layout_opt_in_does_not_touch_release_or_plist(tmp_path, monkeypatch):
    from hermes_cli import update_cmd, gateway
    home = tmp_path / "profile"
    home.mkdir()
    plist = tmp_path / "service.plist"
    plist.write_bytes(b"original")
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {})
    monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", lambda: pytest.fail("launchd touched"))
    monkeypatch.setattr(releases, "stage_release", lambda *a, **kw: pytest.fail("release staged"))
    assert update_cmd._activate_immutable_release()
    assert not (home / "releases").exists()
    assert plist.read_bytes() == b"original"


@pytest.mark.platforms("macos")
def test_deferred_update_stages_without_promoting_or_reloading(tmp_path, monkeypatch):
    from hermes_cli import update_cmd, gateway
    home = tmp_path / "profile"
    previous = home / "releases" / "a"
    candidate = home / "releases" / "b"
    _fake_release(previous, "a")
    _fake_release(candidate, "b")
    releases.promote(home, previous)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda _: "b")
    monkeypatch.setattr(releases, "stage_release", lambda *a, **kw: (candidate, "existing"))
    monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", lambda: pytest.fail("launchd touched"))
    assert update_cmd._activate_immutable_release(defer=True)
    assert (home / "current").resolve() == previous
    assert not (home / "previous").exists()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("first_stage_fails", [False, True])
def test_outstanding_release_is_promoted_before_catchup_restart(tmp_path, monkeypatch, first_stage_fails):
    from hermes_cli import update_cmd
    home = tmp_path / "profile"
    a, b = (home / "releases" / name for name in ("A", "B"))
    _fake_release(a, "A")
    _fake_release(b, "B")
    releases.promote(home, a)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd._m(), "_build_web_ui", lambda _: True)
    attempted = []
    def stage(*args, **kwargs):
        attempted.append("stage")
        if first_stage_fails and len(attempted) == 1:
            raise RuntimeError("stage interrupted")
        return b, "existing"
    monkeypatch.setattr(releases, "stage_release", stage)
    if first_stage_fails:
        with pytest.raises(SystemExit) as exc:
            update_cmd._catch_up_immutable_release(defer=False)
        assert exc.value.code == 1
        assert (home / "current").resolve() == a
    # A deferred update leaves a complete candidate; the next normal update
    # activates it before catch-up is permitted to restart any gateway.
    update_cmd._catch_up_immutable_release(defer=True)
    assert (home / "current").resolve() == a
    update_cmd._catch_up_immutable_release(defer=False)
    assert (home / "current").resolve() == b
    assert (home / "previous").resolve() == a
    assert attempted


@pytest.mark.platforms("macos")
def test_noop_update_promotes_before_pending_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import update_cmd
    home = tmp_path / "profile"
    a, b = (home / "releases" / name for name in ("A", "B"))
    _fake_release(a, "A")
    _fake_release(b, "B")
    releases.promote(home, a)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: (b, "existing"))
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd._m(), "_build_web_ui", lambda _: True)
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *a: None)
    def restart(*, defer, checkout_complete):
        assert defer is False
        assert checkout_complete is True
        assert (home / "current").resolve() == b
    monkeypatch.setattr(update_cmd, "_apply_pending_fleet_restart_catchup", restart)
    update_cmd._catch_up_immutable_release(defer=False)
    update_cmd._apply_pending_fleet_restart_catchup(defer=False, checkout_complete=True)
    assert (home / "previous").resolve() == a


def test_true_noop_release_does_not_stage_or_rebuild(tmp_path, monkeypatch):
    from hermes_cli import update_cmd
    home = tmp_path / "profile"
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda _: "A")
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: pytest.fail("node rebuilt"))
    monkeypatch.setattr(releases, "stage_release", lambda *a, **kw: pytest.fail("release restaged"))
    update_cmd._catch_up_immutable_release(defer=False)
    assert (home / "current").resolve() == a
    assert not (home / "previous").exists()


@pytest.mark.platforms("macos")
def test_repeated_rollback_is_noop_without_fleet_relaunch(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli import gateway, update_cmd
    home = tmp_path / "profile"
    first, second = home / "releases" / "A", home / "releases" / "B"
    _fake_release(first, "A")
    _fake_release(second, "B")
    releases.promote(home, first)
    releases.promote(home, second)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")
    calls = []
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update",
                        lambda *args, **kwargs: calls.append("fleet") or SimpleNamespace(incomplete=False))
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda *args, **kwargs: None)
    update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
    update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
    assert calls == ["fleet"]
    assert "Already rolled back, nothing changed" in capsys.readouterr().out
    assert (home / "current").resolve() == first


@pytest.mark.parametrize("pointer", [
    pytest.param("dangling", marks=pytest.mark.platforms("macos")),
    "outside", "unready", "missing_python",
])
def test_gateway_interpreter_refuses_broken_immutable_current(tmp_path, monkeypatch, pointer):
    from hermes_cli import gateway
    home = tmp_path / "profile"
    root = home / "releases" / ("a" * 40)
    _fake_release(root, root.name)
    home.mkdir(exist_ok=True)
    current = home / "current"
    current.symlink_to(tmp_path / "missing" if pointer == "dangling" else
                       tmp_path / "outside" if pointer == "outside" else root)
    if pointer == "outside":
        (tmp_path / "outside").mkdir()
    if pointer == "unready":
        (root / ".release-ready").unlink()
    if pointer == "missing_python":
        (root / ".venv/bin/python").unlink()
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    with pytest.raises(RuntimeError, match="current.*hermes update --rollback"):
        gateway.get_python_path()


@pytest.mark.platforms("macos")
def test_gateway_interpreter_honors_managed_immutable_opt_in(tmp_path, monkeypatch):
    from hermes_cli import gateway
    home = tmp_path / "profile"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (managed / "config.yaml").write_text("updates:\n  immutable_releases: true\n")
    (home / "release-layout.json").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    with pytest.raises(RuntimeError, match="Invalid immutable release current pointer"):
        gateway.get_python_path()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("damage", [None, "source_changed", "dangling_current", "dangling_previous", "unfinished"])
def test_gateway_source_interpreter_after_migration_rollback(tmp_path, monkeypatch, damage):
    from hermes_cli import gateway

    source, home = tmp_path / "source", tmp_path / "profile"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "hermes_cli/__init__.py").write_text("")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    commit = ["git", "-C", str(source), "-c", "user.email=test@example.com",
              "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "source"]
    subprocess.run(commit, check=True)
    sha = releases.release_sha(source)
    release = home / "releases" / sha
    _fake_release(release, sha)
    (home / "current").symlink_to(release)
    (home / "previous").symlink_to(source)
    journal = home / "release-layout.json"
    record = {"source": str(source), "source_sha": sha, "source_python": sys.executable,
              "state": "done", "plist": None}
    journal.write_text(json.dumps(record))
    releases.restore_source_layout(home)
    assert not (home / "current").is_symlink() and not (home / "previous").is_symlink()
    assert json.loads(journal.read_text())["state"] == "rolled-back"
    (home / "config.yaml").write_text("updates:\n  immutable_releases: true\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    if damage == "source_changed":
        (source / "version.txt").write_text("changed")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(commit, check=True)
    elif damage in {"dangling_current", "dangling_previous"}:
        (home / damage.removeprefix("dangling_")).symlink_to(home / "absent")
    elif damage == "unfinished":
        record["state"] = "in-progress"
        journal.write_text(json.dumps(record))
    if damage:
        with pytest.raises(RuntimeError, match="Invalid immutable release current pointer"):
            gateway.get_python_path()
    else:
        assert gateway.get_python_path() == sys.executable
        plist = __import__("plistlib").loads(gateway.generate_launchd_plist().encode())
        # Plist rendering may wrap Python in macOS's Local Network identity.
        assert sys.executable in str(plist["ProgramArguments"])

@pytest.mark.platforms("macos")
def test_gateway_interpreter_existing_current_does_not_read_config(tmp_path, monkeypatch):
    from hermes_cli import gateway, config_effective
    home = tmp_path / "profile"
    release = home / "releases" / ("a" * 40)
    _fake_release(release, release.name)
    home.mkdir(exist_ok=True)
    (home / "current").symlink_to(release)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(config_effective, "load_user_config_effective",
                        lambda *a, **kw: pytest.fail("current pointer must not read config"))
    assert gateway.get_python_path() == str(home / "current/.venv/bin/python")


@pytest.mark.platforms("linux")
def test_gateway_interpreter_ignores_unsupported_dangling_pointer(tmp_path, monkeypatch):
    from hermes_cli import gateway
    home = tmp_path / "profile"
    home.mkdir()
    (home / "current").symlink_to(home / "missing")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    assert gateway.get_python_path() == gateway.sys.executable


def test_incomplete_checkout_never_receipts_success_during_acknowledged_catchup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import update_cmd, update_cmd_fleet, update_receipt
    home = tmp_path / "profile"
    root = home / "releases" / ("a" * 40)
    _fake_release(root, root.name)
    releases.promote(home, root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd_fleet, "_pending_fleet_restart_needed", lambda: True)
    monkeypatch.setattr(update_cmd_fleet, "_acknowledged_release_launchd_label", lambda *a: "test.label")
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *a: None)
    monkeypatch.setattr(update_cmd, "_catch_up_immutable_release", lambda **kw: None)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *a, **kw: SimpleNamespace(incomplete=False))
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda _outcome, **kw:
                        update_receipt.finalize_update_receipt("success" if kw["update_complete"] else "partial"))
    update_receipt.begin_update_receipt()
    update_cmd._apply_pending_fleet_restart_catchup(checkout_complete=False)
    assert update_receipt.read_latest_receipt()["outcome"] == "partial"


def test_nonready_current_refuses_legacy_update_before_build(tmp_path, monkeypatch):
    from hermes_cli import update_cmd
    home = tmp_path / "profile"
    broken = home / "releases" / "A"
    _fake_release(broken, "A")
    releases.promote(home, broken)
    (broken / ".release-ready").unlink()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {})
    with pytest.raises(RuntimeError, match="current release is not ready"):
        update_cmd._immutable_release_enabled()


def test_fleet_expected_candidate_sha_is_not_current_pointer(tmp_path, monkeypatch):
    from hermes_cli import update_receipt
    home = tmp_path / "profile"
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_profile_homes", lambda: [("default", home)])
    monkeypatch.setattr(update_receipt, "_socket_identity", lambda _: (123, {"code_sha": "A"}))
    monkeypatch.setattr(update_receipt, "_gateway_code_root", lambda pid, root: a)
    # A real fleet row with code_sha=A must be stale against intended B, not
    # current because the pointer still says A or external because roots differ.
    rows = update_receipt.collect_fleet_versions(
        expected_sha_override="B", expected_root_override=home / "releases" / "B")
    assert len(rows) == 1
    assert rows[0]["state"] == "stale"
    assert rows[0]["code_sha"] == "A"


def test_promote_is_atomic_and_rollback_round_trip(tmp_path):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    before = hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest()
    result = releases.promote(home, b)
    assert result["current"] is not None
    assert Path(result["current"]).resolve() == b.resolve()
    assert hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest() == before
    assert (home / "previous").resolve() == a.resolve()
    releases.rollback(home)
    assert (home / "current").resolve() == a.resolve()
    assert (home / "previous").resolve() == b.resolve()


def test_failed_plugin_smoke_does_not_flip_pointer(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad plugin")))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='source'\n")
    (source / "uv.lock").write_text("new")
    (source / "hermes_cli").mkdir()
    (source / "hermes_cli" / "immutable_releases.py").write_text("# test\n")
    monkeypatch.setattr(releases, "_build_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "release_sha", lambda _source: "c")
    with pytest.raises(RuntimeError, match="bad plugin"):
        releases.stage_release(source, home, uv="unused")
    assert (home / "current").resolve() == a.resolve()


def test_retention_keeps_live_and_rollback_pins(tmp_path):
    home = tmp_path / ".hermes"
    for i in range(7):
        p = home / "releases" / str(i)
        _fake_release(p, str(i))
        os.utime(p, (i, i))
    releases.promote(home, home / "releases" / "6")
    releases.promote(home, home / "releases" / "5")
    pinned = home / "releases" / "0"
    removed = releases.retain(home, extra_pins=[pinned])
    assert pinned not in removed
    assert (home / "releases" / "6").exists()
    assert (home / "releases" / "5").exists()
    assert len(list((home / "releases").iterdir())) >= 5


@pytest.mark.parametrize("owner,executable,pins_all", [
    ("other", "python3", False),
    ("self", "python3", True),
    ("self", "other-tool", False),
])
def test_unreadable_process_identity_scopes_retention(tmp_path, monkeypatch, owner, executable, pins_all):
    import psutil
    from types import SimpleNamespace
    home = tmp_path / "profile"
    for name in ("A", "B", "C", "D", "E"):
        _fake_release(home / "releases" / name, name)
    releases.promote(home, home / "releases" / "E")
    uid = os.getuid() if owner == "self" else os.getuid() + 1
    class Unreadable:
        info = {"uids": SimpleNamespace(real=uid, effective=uid), "name": executable,
                "cmdline": None, "environ": {}, "cwd": None, "exe": None}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([Unreadable()]))
    removed = releases.retain(home, rollback_count=1)
    assert (removed == []) is pins_all
    assert (home / "releases" / "A").exists() is pins_all


def test_retention_prunes_excess_without_uncertain_processes(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    paths = [home / "releases" / str(i) for i in range(7)]
    for i, path in enumerate(paths):
        _fake_release(path, str(i))
        os.utime(path, (i, i))
    releases.promote(home, paths[-1])
    releases.promote(home, paths[-2])
    monkeypatch.setattr(releases, "_live_process_pins", lambda _: {paths[0].resolve()})
    removed = releases.retain(home)
    assert paths[0].exists()
    assert len(removed) == 1
    assert removed == [paths[1]]


def test_sigkill_stage_and_flip_converge_with_complete_current(tmp_path):
    """Real child process dies at both commit boundaries; a second update converges."""
    home = tmp_path / "profile"
    source = tmp_path / "source"
    source.mkdir()
    (source / "version.txt").write_text("B", encoding="utf-8")
    (source / "hermes_cli").mkdir()
    (source / "hermes_cli" / "immutable_releases.py").write_text("# test\n")
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    script = """
import os, pathlib, signal, sys, time
from hermes_cli import immutable_releases as r
source, home, checkpoint, boundary = map(pathlib.Path, sys.argv[1:])
r._build_venv = lambda *args, **kwargs: None
r.smoke_plugins = lambda *args, **kwargs: None
candidate, _ = r.stage_release(source, home, sha='B')
def pause():
    checkpoint.write_text(str(os.getpid()), encoding='utf-8')
    while True: time.sleep(.1)
if boundary.name == 'stage': pause()
r.promote(home, candidate, before_flip=pause)
"""
    for boundary in ("stage", "flip"):
        checkpoint = tmp_path / f"{boundary}.ready"
        child = subprocess.Popen([sys.executable, "-c", script, str(source), str(home),
                                  str(checkpoint), boundary], cwd=Path(__file__).resolve().parents[2])
        try:
            import time
            deadline = time.monotonic() + 20
            while not checkpoint.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert checkpoint.exists(), f"updater exited {child.poll()} before {boundary}"
            child.kill()  # SIGKILL, no Python finally or cleanup handlers
            assert child.wait(timeout=5) < 0
            assert (home / "current").resolve() == a
            assert (home / "current" / ".release-ready").read_text().strip() == "A"
            if boundary == "flip":
                assert (home / "previous").resolve() == a
            # Simulate rerun in the surviving process. A ready candidate is reused;
            # an incomplete one is rebuilt at its final venv path.
            from unittest.mock import patch
            with patch.object(releases, "_build_venv"), patch.object(releases, "smoke_plugins"):
                candidate, _ = releases.stage_release(source, home, sha="B")
            releases.promote(home, candidate)
            assert (home / "current").resolve() == candidate
            assert (home / "current" / ".release-ready").read_text().strip() == "B"
            releases.rollback(home)
            assert (home / "current").resolve() == a
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

@pytest.mark.platforms("macos")
def test_existing_pointer_stale_plist_failure_restores_and_retry_repairs(tmp_path, monkeypatch):
    """A failed reload leaves a durable roll-forward transaction for retry."""
    from hermes_cli import gateway, gateway_launchd, update_cmd
    home = tmp_path / "profile"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    plist = tmp_path / "test.plist"
    plist.write_bytes(b"source")
    monkeypatch.setattr(update_cmd.sys, "platform", "darwin")
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda path: "b")
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: (b, "existing"))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kwargs: "candidate")
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: plist.read_bytes() == b"candidate")
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda path: False)
    assert not update_cmd._activate_immutable_release()
    assert (home / "current").resolve() == b
    assert (home / "previous").resolve() == a
    assert plist.read_bytes() == b"candidate"
    assert (home / "release-txn.json").exists()
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda path: True)
    monkeypatch.setattr(update_cmd, "_await_release_acknowledgement", lambda path: False)
    assert not update_cmd._activate_immutable_release()
    assert (home / "current").resolve() == b
    assert (home / "previous").resolve() == a
    assert plist.read_bytes() == b"candidate"
    assert (home / "release-txn.json").exists()  # Reload success is not gateway acknowledgement.


def test_worker_environment_is_resolved_release_not_current(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "a"
    _fake_release(release, "a")
    executable, cwd, env = releases.worker_launch_spec(release, {"PYTHONPATH": "old"})
    assert executable == str(release / ".venv" / "bin" / "python")
    assert cwd == release
    assert env["HERMES_RELEASE"] == str(release.resolve())
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(release.resolve()), "old"]


def test_build_venv_ignores_inherited_source_uv_target_and_preserves_other_envs(tmp_path, monkeypatch):
    """A real uv sync must build candidate, never an inherited source or live env."""
    import venv

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pyproject.toml").write_text(
        "[project]\nname = 'candidate-env-probe'\nversion = '0.1.0'\n"
        "requires-python = '>=3.11'\n[tool.uv]\npackage = false\n", encoding="utf-8")
    source_env = tmp_path / "source-venv"
    running_env = tmp_path / "running-release" / ".venv"
    for environment in (source_env, running_env):
        venv.EnvBuilder(with_pip=False).create(environment)
        (environment / "protected.dist-info").mkdir()
        (environment / "protected.dist-info" / "METADATA").write_text("keep", encoding="utf-8")
    subprocess.run(["uv", "lock", "--offline"], cwd=candidate, check=True,
                   capture_output=True, text=True)
    before = {path: (path.stat().st_mtime_ns, sorted(p.name for p in path.iterdir()))
              for path in (source_env, running_env)}
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(source_env))
    monkeypatch.setenv("VIRTUAL_ENV", str(running_env))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stale-checkout"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "stale-python"))
    releases._build_venv(candidate)
    assert releases._release_python(candidate).is_file()
    assert all((path.stat().st_mtime_ns, sorted(p.name for p in path.iterdir())) == snapshot
               for path, snapshot in before.items())
    assert all((path / "protected.dist-info" / "METADATA").read_text(encoding="utf-8") == "keep"
               for path in (source_env, running_env))


def test_prepare_venv_changed_lock_builds_fresh(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "old", lock="old")
    _fake_release(new, "new", lock="new")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv", extras=(), python=None: called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_prepare_venv_identical_lock_still_builds_in_place(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "same", lock="same")
    _fake_release(new, "same", lock="same")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv", extras=(), python=None: called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_retention_protects_real_process_cwd_and_receipt(tmp_path):
    home = tmp_path / "profile"
    release_paths = [home / "releases" / str(i) for i in range(9)]
    for i, path in enumerate(release_paths):
        _fake_release(path, str(i))
        os.utime(path, (i, i))
    releases.promote(home, release_paths[8])
    releases.promote(home, release_paths[7])
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "recovery.json").write_text(json.dumps({"release_path": str(release_paths[1])}))
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=release_paths[0])
    try:
        import psutil
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if Path(psutil.Process(worker.pid).cwd()).resolve() == release_paths[0].resolve():
                    break
            except psutil.Error:
                pass
            time.sleep(.02)
        else:
            pytest.fail("worker did not publish its release cwd before retention")
        assert release_paths[0].resolve() in releases._live_process_pins(home)
        removed = releases.retain(home)
        assert release_paths[0].exists() and release_paths[1].exists()
        assert release_paths[8].exists() and release_paths[7].exists()
        assert {path.name for path in release_paths if path.exists()} == {"0", "1", "4", "5", "6", "7", "8"}
        assert {path.name for path in removed} == {"2", "3"}
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_successful_receipt_history_does_not_pin_every_old_release(tmp_path):
    home = tmp_path / "profile"
    old = home / "releases" / "old"
    old.mkdir(parents=True)
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    payload = {"outcome": "success", "release_transition": {"from_path": str(old)}}
    (receipts / "historical.json").write_text(json.dumps(payload))
    assert old not in releases._receipt_pins(home)
    payload["outcome"] = "partial"
    (receipts / "unfinished.json").write_text(json.dumps(payload))
    assert old in releases._receipt_pins(home)


def test_retention_failure_is_advisory_after_verified_update(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases, update_cmd, update_cmd_fleet, update_receipt
    from hermes_cli.update_cmd_fleet import _GatewayRestartOutcome
    home = tmp_path / "profile"
    release = home / "releases" / "current"
    _fake_release(release, "current")
    releases.promote(home, release)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(update_cmd_fleet, "_collect_fleet_snapshot", lambda *a, **kw: [])
    monkeypatch.setattr(update_cmd_fleet, "_clear_fleet_restart_pending_marker", lambda: None)
    monkeypatch.setattr(update_cmd_fleet, "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
    monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *a: [])
    monkeypatch.setattr(immutable_releases, "retain", lambda *a: (_ for _ in ()).throw(OSError("prune blocked")))
    restart = _GatewayRestartOutcome(incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
        restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
        externally_supervised_profiles=[], killed_pids=set())
    update_receipt.begin_update_receipt()
    update_cmd_fleet._verify_fleet_after_update(restart, _pre_update_plan=None,
        _windows_gateway_resume=None, node_failures=[], update_complete=True, rollback=True)
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert any(s["name"] == "release_retention" and not s["ok"] and
               "prune blocked" in s["detail"] for s in receipt["steps"])


def test_verified_update_retains_real_process_pinned_old_release(tmp_path, monkeypatch):
    from hermes_cli import update_cmd, update_cmd_fleet, update_receipt
    from hermes_cli.update_cmd_fleet import _GatewayRestartOutcome
    home = tmp_path / "profile"
    releases_for_test = [home / "releases" / str(i) for i in range(7)]
    for i, release in enumerate(releases_for_test):
        _fake_release(release, str(i))
        os.utime(release, (i, i))
    releases.promote(home, releases_for_test[-1])
    releases.promote(home, releases_for_test[-2])
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              cwd=releases_for_test[0])
    try:
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
        monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **kw: None)
        monkeypatch.setattr(update_cmd_fleet, "_collect_fleet_snapshot", lambda *a, **kw: [])
        monkeypatch.setattr(update_cmd_fleet, "_clear_fleet_restart_pending_marker", lambda: None)
        monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
        monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *a: [])
        restart = _GatewayRestartOutcome(incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
            restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
            externally_supervised_profiles=[], killed_pids=set())
        update_receipt.begin_update_receipt()
        update_cmd_fleet._verify_fleet_after_update(restart, _pre_update_plan=None,
            _windows_gateway_resume=None, node_failures=[], update_complete=True, rollback=False)
        receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
        assert receipt["outcome"] == "success"
        assert releases_for_test[0].exists()
        assert releases_for_test[5].exists() and releases_for_test[6].exists()
        assert len([path for path in releases_for_test[1:5] if path.exists()]) >= 3
        assert any(s["name"] == "release_retention" and s["ok"] for s in receipt["steps"])
    finally:
        worker.terminate()
        worker.wait(timeout=5)


@pytest.mark.platforms("macos")
def test_real_staging_rejects_incompatible_plugin_and_keeps_pointer_and_receipt(tmp_path, monkeypatch):
    """Build one real checkout candidate; each plugin kind must fail its import probe before promotion."""
    from hermes_cli import update_cmd, update_receipt

    remote = Path(__file__).resolve().parents[2]
    source = tmp_path / "hermes-agent"
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                    str(remote), str(source)], check=True)
    sha = subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], check=True)
    # This fixture validates plugin import failure, not dependency synchronization.
    monkeypatch.setattr(releases, "restore_active_distributions", lambda *a, **kw: None)
    home = tmp_path / "profile"
    home.mkdir()
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    # Stage the complete checkout and real candidate venv once. Reusing this
    # immutable candidate exercises the production 'existing' smoke path for
    # each kind without rebuilding npm and uv five times in one test file.
    candidate, action = releases.stage_release(source, home)
    assert action == "staged" and candidate == home / "releases" / sha
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    for kind in ("standalone", "backend", "platform", "exclusive", "model-provider"):
        (plugin / "plugin.yaml").write_text(
            f"name: candidate-test\nversion: '1.0'\nkind: {kind}\n", encoding="utf-8")
        update_receipt.begin_update_receipt()
        try:
            assert update_cmd._activate_immutable_release() is False, kind
            update_receipt.finalize_update_receipt("partial")
            assert (home / "current").resolve() == a.resolve(), kind
            assert (home / "previous").exists() is False, kind
            receipt = json.loads((home / "logs" / "update_receipts" / "latest.json").read_text(encoding="utf-8"))
            assert receipt["outcome"] == "partial", kind
            assert any(s["name"] == "immutable_release" and not s["ok"] and
                       "candidate plugin smoke failed" in s["detail"] for s in receipt["steps"]), kind
        finally:
            update_receipt.finalize_update_receipt("partial")


def test_candidate_import_smoke_rejects_model_provider_import_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "_release_python", lambda _release: Path(sys.executable))
    home = tmp_path / "profile"
    plugin = home / "plugins" / "broken-model"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [broken-model]\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        "name: broken-model\nversion: '1.0'\nkind: model-provider\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "raise RuntimeError('incompatible model provider')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="incompatible model provider"):
        releases.smoke_plugins(Path(__file__).resolve().parents[2], home)


def test_candidate_smoke_cannot_pass_via_inherited_stale_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "_release_python", lambda _release: Path(sys.executable))
    home = tmp_path / "profile"
    plugin = home / "plugins" / "broken"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [broken]\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: broken\nversion: '1.0'\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("raise RuntimeError('candidate plugin broken')\n", encoding="utf-8")
    stale = tmp_path / "stale" / "hermes_cli"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("", encoding="utf-8")
    (stale / "immutable_releases.py").write_text("# stale copy bypasses candidate smoke\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(stale.parent))
    with pytest.raises(RuntimeError, match="candidate plugin broken"):
        releases.smoke_plugins(Path(__file__).resolve().parents[2], home)




def test_incomplete_active_release_survives_failed_restaging(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    active = home / "releases" / "A"
    _fake_release(active, "A")
    releases.promote(home, active)
    (active / ".release-ready").unlink()
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(RuntimeError, match="potentially pinned"):
        releases.stage_release(source, home, sha="A")
    assert (home / "current").resolve() == active
    assert (active / ".hermes_build_sha").exists()


def test_staging_failure_never_publishes_candidate(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "profile"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "hermes_cli" / "immutable_releases.py").write_text("# test\n")
    def failed_build(path, **kwargs):
        assert path.name.startswith(".staging-B-")
        raise RuntimeError("build failed")
    monkeypatch.setattr(releases, "prepare_venv", failed_build)
    with pytest.raises(RuntimeError, match="build failed"):
        releases.stage_release(source, home, sha="B")
    assert not (home / "releases" / "B").exists()
    assert list((home / "releases").iterdir()) == []


def test_locked_distribution_upgrade_survives_optional_restore(tmp_path, monkeypatch):
    import venv
    import zipfile
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    for root in (source, candidate):
        venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (candidate / "uv.lock").write_text('[[package]]\nname = "locked.x"\nversion = "2.0"\n')
    wheels = [("locked_x", "1.0"), ("locked_x", "2.0"), ("plugin_y", "1.0")]
    for name, version in wheels:
        wheel = tmp_path / f"{name}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{name}/__init__.py", f"VERSION = '{version}'\n")
            archive.writestr(f"{name}-{version}.dist-info/METADATA",
                             f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            archive.writestr(f"{name}-{version}.dist-info/WHEEL",
                             "Wheel-Version: 1.0\nGenerator: s2-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(f"{name}-{version}.dist-info/RECORD", "")
    for env, specs in ((source, ["locked_x==1.0", "plugin_y==1.0"]),
                       (candidate, ["locked_x==2.0"])):
        subprocess.run(["uv", "pip", "install", "--offline", "--no-deps", "--find-links", str(tmp_path),
                        "--python", str(releases._release_python(env)), *specs], check=True, capture_output=True)
    monkeypatch.setenv("UV_FIND_LINKS", str(tmp_path))
    monkeypatch.setenv("UV_OFFLINE", "1")
    releases.restore_active_distributions(source, candidate)
    result = subprocess.run([str(releases._release_python(candidate)), "-c",
                             "import locked_x,plugin_y;print(locked_x.VERSION,plugin_y.VERSION)"],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "2.0 1.0"


@pytest.mark.parametrize("manager", ["linux", "win32"])
def test_immutable_opt_in_rejects_non_launchd_before_staging(tmp_path, monkeypatch, manager):
    from hermes_cli import update_cmd
    home = tmp_path / "profile"
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(update_cmd.sys, "platform", manager)
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: pytest.fail("staged"))
    with pytest.raises(RuntimeError, match="macOS launchd"):
        update_cmd._cmd_update_impl(object(), gateway_mode=False)
    assert not home.exists()


def test_retention_refuses_unreadable_receipt(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    for name in ("A", "B", "C"):
        _fake_release(home / "releases" / name, name)
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "broken.json").write_text("{not-json")
    monkeypatch.setattr(releases, "_live_process_pins", lambda _: set())
    with pytest.raises(RuntimeError, match="unreadable update receipt"):
        releases.retain(home, rollback_count=0)
    assert all((home / "releases" / name).exists() for name in ("A", "B", "C"))


def test_promote_and_rollback_refuse_mismatched_build_stamp(tmp_path):
    home = tmp_path / "profile"
    a, b = home / "releases" / "A", home / "releases" / "B"
    _fake_release(a, "A")
    _fake_release(b, "B")
    releases.promote(home, a)
    releases.promote(home, b)
    (a / ".hermes_build_sha").write_text("wrong\n")
    with pytest.raises(ValueError, match="not a complete release"):
        releases.rollback(home)
    assert (home / "current").resolve() == b
    assert (home / "previous").resolve() == a




@pytest.mark.platforms("macos")
def test_update_stages_before_transaction_without_advancing_checkout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import gateway, update_cmd
    source, home, remote = tmp_path / "source", tmp_path / "profile", tmp_path / "origin.git"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(source)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
    sha_a = None
    for name in ("A", "B"):
        (source / "version.txt").write_text(name)
        subprocess.run(["git", "-C", str(source), "add", "version.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                        "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", name], check=True)
        if name == "A":
            sha_a = releases.release_sha(source)
        subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "main"], check=True)
    assert sha_a is not None
    subprocess.run(["git", "-C", str(source), "reset", "--hard", sha_a], check=True, capture_output=True)
    monkeypatch.setattr(releases, "_source_python_valid", lambda *args: True)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    monkeypatch.setattr(update_cmd._m(), "_run_pre_update_backup", lambda *args: None)
    monkeypatch.setattr(update_cmd._m(), "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(update_cmd._m(), "_resolve_update_branch", lambda *args: "main")
    monkeypatch.setattr(update_cmd._m(), "_warn_orphaned_update_autostashes", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *args: SimpleNamespace(
        gw_input_fn=None, assume_yes=True, switch_branch=False, pre_update_version="old",
        no_gateway_restart=False))
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan", lambda *args: None)
    monkeypatch.setattr(update_cmd._m(), "_desktop_packaged_executable", lambda *args: None)
    monkeypatch.setattr(update_cmd._m(), "_desktop_dist_exists", lambda *args: False)
    monkeypatch.setattr(update_cmd._m(), "_installed_desktop_apps", lambda *args: [])
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda: (False, ["git"], False))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")
    def inspect_before_stage(*args, **kwargs):
        assert not (home / "release-layout.json").exists()
        assert not (home / "release-txn.json").exists()
        assert releases.release_sha(source) == sha_a
        raise RuntimeError("staged before release transaction")
    monkeypatch.setattr(releases, "stage_release", inspect_before_stage)
    with pytest.raises(RuntimeError, match="staged before release transaction"):
        update_cmd._cmd_update_impl(SimpleNamespace(rollback=False), gateway_mode=False)
    assert releases.release_sha(source) == sha_a
    # Model an already completed and reversed legacy migration explicitly;
    # begin_migration deliberately leaves a pending transaction until promotion.
    journal = {"source": str(source), "source_python": sys.executable,
               "source_sha": sha_a, "plist": None, "state": "rolled-back"}
    home.mkdir(parents=True, exist_ok=True)
    (home / "release-layout.json").write_text(json.dumps(journal), encoding="utf-8")
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": False})
    def legacy_checkout(*args, **kwargs):
        assert not (home / "current").exists() and not (home / "previous").exists()
        assert releases.release_sha(source) == sha_a
        raise RuntimeError("legacy checkout selected")
    monkeypatch.setattr(update_cmd, "_prepare_checkout_for_update", legacy_checkout)
    update_cmd._catch_up_immutable_release(defer=False)
    with pytest.raises(RuntimeError, match="legacy checkout selected"):
        update_cmd._cmd_update_impl(SimpleNamespace(rollback=False), gateway_mode=False)
    assert not (home / "current").exists() and not (home / "previous").exists()
    assert releases.release_sha(source) == sha_a


@pytest.mark.platforms("macos")
def test_rollback_to_source_then_reactivate_records_source_sha(tmp_path, monkeypatch):
    from hermes_cli import gateway, update_cmd, update_receipt

    source, home = tmp_path / "hermes-agent", tmp_path / "profile"
    source.mkdir()
    (source / "version.txt").write_text("source", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "version.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=test@example.com",
                    "-c", "user.name=Test", "-c", "commit.gpgsign=false", "commit", "-qm", "source"], check=True)
    source_sha = releases.release_sha(source)
    release = home / "releases" / source_sha
    _fake_release(release, source_sha)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    monkeypatch.setattr(releases, "_source_python_valid", lambda *args: True)
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kwargs: (release, "existing"))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")

    assert update_cmd._activate_immutable_release()
    assert releases.rollback(home)["source_sha"] == source_sha
    assert not (home / "current").exists()
    assert not (home / "previous").exists()
    assert json.loads((home / "release-layout.json").read_text(encoding="utf-8"))["state"] == "rolled-back"
    update_receipt.begin_update_receipt()
    assert update_cmd._activate_immutable_release()
    update_receipt.finalize_update_receipt("success")
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
    assert receipt["release_transition"]["from_path"] == str(source)
    assert receipt["release_transition"]["from_sha"] == source_sha
    assert receipt["release_transition"]["to_sha"] == source_sha
    assert (home / "current").resolve() == release


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("marker_valid", [True, False])
def test_promotion_receipt_uses_build_marker_or_explains_unknown_identity(tmp_path, monkeypatch, marker_valid):
    from hermes_cli import gateway, update_cmd, update_receipt

    home = tmp_path / "profile"
    known_sha = "a" * 40
    old = home / "releases" / (known_sha if marker_valid else "old")
    candidate = home / "releases" / "candidate"
    _fake_release(old, old.name)
    _fake_release(candidate, "candidate")
    releases.promote(home, old)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda _: "b" * 40)
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kwargs: (candidate, "existing"))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")

    update_receipt.begin_update_receipt()
    assert update_cmd._activate_immutable_release()
    update_receipt.finalize_update_receipt("success")
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
    assert receipt["release_transition"]["from_sha"] == (known_sha if marker_valid else None)
    if not marker_valid:
        assert any("from_sha unknown" in step["detail"] for step in receipt["steps"])


def test_atomic_publish_refuses_existing_empty_target(tmp_path):
    staging, target = tmp_path / "staging", tmp_path / "B"
    staging.mkdir()
    target.mkdir()
    (staging / "sentinel").write_text("new")
    with pytest.raises(FileExistsError):
        releases._publish_release(staging, target)
    assert staging.is_dir() and target.is_dir()
    assert not (target / "sentinel").exists()


def test_real_uv_build_aside_relocates_interpreter_and_scripts(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "profile"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "hermes_cli" / "immutable_releases.py").write_text("# test\n")
    (source / "pyproject.toml").write_text(
        "[project]\nname='s2-relocation-probe'\nversion='0.1.0'\nrequires-python='>=3.11'\n"
        "[tool.uv]\npackage=false\n", encoding="utf-8")
    subprocess.run(["uv", "lock", "--offline"], cwd=source, check=True, capture_output=True)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kw: None)
    candidate, action = releases.stage_release(source, home, sha="B")
    assert action == "staged"
    result = subprocess.run([str(releases._release_python(candidate)), "-c",
                             "import sys;print(sys.prefix)"], capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()).resolve() == candidate / ".venv"
    assert not any(p.name.startswith(".staging-") for p in (home / "releases").iterdir())


def test_pre_s2_revision_fails_with_clear_message(tmp_path):
    source, home = tmp_path / "source", tmp_path / "profile"
    source.mkdir()
    (source / "version.txt").write_text("pre-S2")
    with pytest.raises(RuntimeError, match="predates immutable releases"):
        releases.stage_release(source, home, sha="A")
    assert not (home / "releases" / "A").exists()


def test_candidate_import_smoke_blocks_bad_enabled_plugin_without_writing_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "_release_python", lambda _release: Path(sys.executable))
    home = tmp_path / "profile"
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n")
    (plugin / "plugin.yaml").write_text("name: candidate-test\nversion: '1.0'\n")
    source = Path(__file__).resolve().parents[2]
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n"
    )
    with pytest.raises(RuntimeError, match="candidate plugin smoke failed"):
        releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
