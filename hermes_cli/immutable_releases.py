"""Immutable per-version release management for the Hermes runtime.

The source checkout remains the update authority.  A release is a detached copy
of one source revision, with its own virtual environment and an atomic
``current`` symlink in ``$HERMES_HOME``.  This module is deliberately small and
side-effect explicit so the transactional updater can use it as one stage.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import shutil
import subprocess
import sys
import time
import uuid
import re
import shlex
import tomllib
import plistlib
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence, Any

# Freeze the package's physical code tree before `current` can change. This is
# the installation owning the process, not any individual cron profile home.
_LOADED_CODE_ROOT = Path(__file__).resolve().parent.parent
LOADED_RELEASE_ROOT: Path | None = (
    _LOADED_CODE_ROOT if _LOADED_CODE_ROOT.parent.name == "releases"
    and (_LOADED_CODE_ROOT / ".hermes_build_sha").is_file() else None
)


@dataclass(frozen=True)
class ReleasePaths:
    home: Path
    releases: Path
    current: Path
    previous: Path

    @classmethod
    def for_home(cls, home: str | Path) -> "ReleasePaths":
        home = Path(home).expanduser().resolve()
        return cls(home, home / "releases", home / "current", home / "previous")

    def release(self, sha: str) -> Path:
        if not sha or "/" in sha or sha in {".", ".."}:
            raise ValueError(f"invalid release SHA: {sha!r}")
        return self.releases / sha


def release_sha(source: Path) -> str:
    """Return the exact git revision represented by *source*."""
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout.strip()


def _content_digest(release: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        path = release / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
    return digest.hexdigest()


def _python_version(python: Path) -> str:
    result = subprocess.run([str(python), "-c", "import platform; print(platform.python_version())"],
                            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=_release_subprocess_env())
    return result.stdout.strip()


def _release_python(release: Path) -> Path:
    return release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _interpreter_process_executable(python: Path) -> Path:
    """Observe the kernel image of the intended interpreter, including framework launchers."""
    python = python.absolute()
    identity = python.stat()
    return _probe_interpreter_process_executable(
        str(python), identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns)


@lru_cache(maxsize=32)
def _probe_interpreter_process_executable(python: str, *identity: int) -> Path:
    result = subprocess.run(
        [python, "-I", "-c", "import psutil; print(psutil.Process().exe())"],
        env=_release_subprocess_env(), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10,
    )
    executable = Path(result.stdout.strip())
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("intended interpreter did not report an executable image")
    return executable.resolve()


def _copy_tree(source: Path, target: Path, *, home: Path | None = None) -> None:
    """Copy a synthetic test tree without following the destination or mutable state."""
    source, target = source.resolve(), target.resolve()
    excluded = {target, target.parent}
    if home is not None:
        excluded.add(home.resolve())
    excluded = {path for path in excluded if path != source and source in path.parents}
    names = {".git", ".worktrees", ".venv", "venv", "__pycache__", "node_modules", "releases"}

    def ignore(directory: str, entries: list[str]) -> set[str]:
        root = Path(directory).resolve()
        return {name for name in entries if name in names or name.endswith(".pyc")
                or any(root / name == path or path in (root / name).parents for path in excluded)}

    shutil.copytree(source, target, ignore=ignore, symlinks=True)


def _stage_git_tree(source: Path, staging: Path, sha: str) -> None:
    """Materialize only files tracked at the exact commit, never local caches."""
    import tarfile
    archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", sha],
                             capture_output=True, check=True).stdout
    staging.mkdir(parents=True)
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            dest = (staging / member.name).resolve()
            if (member.name.startswith("/") or ".." in Path(member.name).parts
                    or staging.resolve() not in dest.parents or not (member.isfile() or member.isdir())):
                raise RuntimeError("unsafe git archive member")
        # The explicit file/directory-only validation also covers Python 3.11
        # before tarfile's data filter became available (3.11.4).
        tar.extractall(staging)


def _atomic_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        os.symlink(os.path.relpath(target, link.parent), tmp)
        os.replace(tmp, link)
    finally:
        with suppress_oserror():
            tmp.unlink()


class suppress_oserror:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def read_pointer(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _release_subprocess_env(release: Path | None = None) -> dict[str, str]:
    """Keep inherited credentials/network settings, not interpreter or uv target overrides."""
    env = os.environ.copy()
    for key in ("CONDA_DEFAULT_ENV", "CONDA_PREFIX", "VIRTUAL_ENV", "PYTHONHOME",
                "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT",
                "__PYVENV_LAUNCHER__", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON",
                "UV_ACTIVE", "UV_CONFIG_FILE"):
        env.pop(key, None)
    if release is not None:
        env["UV_PROJECT_ENVIRONMENT"] = str(release.resolve() / ".venv")
    env["UV_NO_CONFIG"] = "1"
    # Deterministically leave bytecode generation to Python on first import.
    # Smoke imports may still create disposable __pycache__ files, removed on relocation.
    env["UV_COMPILE_BYTECODE"] = "0"
    return env


def _source_install_python(source: Path, home: Path | None = None) -> Path:
    """Use the migration-bound interpreter, not a worktree's incidental .venv."""
    if home is not None:
        journal = ReleasePaths.for_home(home).home / "release-layout.json"
        if journal.exists():
            record = json.loads(journal.read_text(encoding="utf-8-sig"))
            if Path(record["source"]).resolve() != source.resolve():
                raise RuntimeError("migration journal refers to a different checkout")
            if record.get("source_python"):
                python = Path(record["source_python"])
                if not python.is_file():
                    raise RuntimeError(f"source interpreter unavailable: {python}")
                return python
    for name in ("venv", ".venv"):
        python = source / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.is_file():
            return python
    return Path(sys.executable)


def _active_locked_extras(source_python: Path, project: Path) -> list[str]:
    """Infer selected leaf extras from installed direct requirements.

    Wheel metadata lists *available*, not selected, extras. A uniquely named
    installed requirement identifies a partially installed leaf group; groups
    with all applicable direct requirements installed identify the remainder.
    """
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.markers import default_environment

    project_data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8-sig"))
    groups = project_data["project"].get("optional-dependencies", {})
    installed = set(_active_distributions(source_python))
    declared: dict[str, set[str]] = {}
    for extra, requirements in groups.items():
        if extra in {"all", "termux", "termux-all"}:
            continue  # composite extras are reconstructed from their leaf groups
        direct = set()
        for raw in requirements:
            req = Requirement(raw)
            if canonicalize_name(req.name) == "hermes-agent":
                continue
            if req.marker is None or req.marker.evaluate({**default_environment(), "extra": extra}):
                direct.add(canonicalize_name(req.name))
        if direct:
            declared[extra] = direct
    unique = {name for extra, names in declared.items() for name in names
              if sum(name in other for other in declared.values()) == 1}
    selected = {extra for extra, names in declared.items()
                if names <= installed or bool(names & installed & unique)}
    # Editable/direct-url metadata can preserve an explicit extras selector.
    # Provides-Extra alone is only the list of *available* groups, not evidence
    # that every group was installed.
    metadata_script = ("import importlib.metadata as m,json,re; "
                       "print(json.dumps(sorted(set(x.strip() for d in m.distributions() "
                       "if d.metadata.get('Name','').lower().replace('_','-')=='hermes-agent' "
                       "for text in [d.read_text('direct_url.json') or ''] "
                       "for group in re.findall(r'hermes-agent\\[([^]]+)\\]', text) "
                       "for x in group.split(',')))))")
    metadata_result = subprocess.run([str(source_python), "-c", metadata_script], check=True,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", env=_release_subprocess_env())
    selected.update(extra for extra in json.loads(metadata_result.stdout) if extra in declared)

    # Old transitive packages are not evidence of an enabled feature. In particular,
    # legacy PyYAML/importlib-metadata must not enable an unused embedded server.
    return sorted(selected)


def _build_venv(release: Path, *, uv: str = "uv", extras: Sequence[str] = (),
                python: Path | None = None) -> None:
    # PM supplies its pinned Python ABI and locked build engine. The migration-bound
    # source interpreter can remain 3.11 and is never a candidate build target.
    from pm import build_environment
    build_environment(source=release, out=release / ".venv", extras=extras,
                      env=_release_subprocess_env(release), frozen=True, explicit=True)


def _active_distributions(python: Path) -> dict[str, str]:
    script = ("import importlib.metadata as m,json; "
              "import re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower(): d.version "
              "for d in m.distributions() if d.metadata.get('Name')}))")
    result = subprocess.run([str(python), "-c", script], check=True,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_release_subprocess_env())
    return json.loads(result.stdout)


def _active_plugin_entrypoints(python: Path, names: set[str] | None = None) -> set[tuple[str, str, str]]:
    script = ("import importlib.metadata as m,json,re; "
              "groups={'hermes_agent.plugins','hermes_agent.plugin_capabilities'}; "
              "print(json.dumps(sorted((re.sub(r'[-_.]+','-',d.metadata['Name']).lower(),e.group,e.name,e.value) "
              "for d in m.distributions() if d.metadata.get('Name') "
              "for e in d.entry_points if e.group in groups)))")
    result = subprocess.run([str(python), "-c", script], check=True,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_release_subprocess_env())
    return {(group, name, value) for dist, group, name, value in json.loads(result.stdout)
            if names is None or dist in names}


def restore_active_distributions(source: Path, candidate: Path, *, uv: str = "uv",
                                 source_python: Path | None = None) -> None:
    """Carry active lazy/tool/entry-point plugin packages into the candidate.

    Do not claim readiness if an active package cannot be reproduced. Never
    modify the source interpreter; uv installs against the candidate alone.
    """
    source_python = source_python or _source_install_python(source)
    candidate_python = _release_python(candidate)
    if not source_python.is_file():
        raise RuntimeError(f"source interpreter unavailable: {source_python}")
    installed = _active_distributions(source_python)
    target = _active_distributions(candidate_python)
    lock = candidate / "uv.lock"
    if not lock.is_file():
        raise RuntimeError(f"candidate lockfile missing: {lock}")
    locked = {re.sub(r"[-_.]+", "-", p["name"]).lower()
              for p in tomllib.loads(lock.read_text(encoding="utf-8-sig"))["package"]}
    locked_versions = {name: target[name] for name in locked if name in target}
    extras = {name: version for name, version in installed.items()
              if name not in locked and name not in {"hermes-agent", "hermes-agent-cli"}}
    missing = [f"{name}=={version}" for name, version in sorted(extras.items())
               if target.get(name) != version]
    if missing:
        subprocess.run([uv, "pip", "install", "--no-deps", "--python", str(candidate_python), *missing],
                       env=_release_subprocess_env(candidate), check=True)
    target = _active_distributions(candidate_python)
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    project_file = candidate / "pyproject.toml"
    declared = (tomllib.loads(project_file.read_text(encoding="utf-8-sig"))
                .get("project", {}).get("optional-dependencies", {}) if project_file.is_file() else {})
    selected = _active_locked_extras(source_python, candidate) if declared else []
    required = {canonicalize_name(Requirement(raw).name)
                for extra in selected for raw in declared[extra]
                if Requirement(raw).marker is None or Requirement(raw).marker.evaluate({"extra": extra})}
    unmatched = [name for name in installed if name not in {"hermes-agent", "hermes-agent-cli"}
                 and ((name not in target and (name not in locked or name in required))
                      or (name not in locked and target.get(name) != installed[name]))]
    if unmatched:
        raise RuntimeError(f"candidate distribution parity failed: {', '.join(sorted(unmatched))}")
    changed = [name for name in locked if name in target and name in locked_versions
               and target[name] != locked_versions[name]]
    if changed:
        raise RuntimeError(f"candidate lock distributions changed: {', '.join(sorted(changed))}")
    absent = _active_plugin_entrypoints(source_python) - _active_plugin_entrypoints(candidate_python)
    if absent:
        raise RuntimeError(f"candidate lost installed plugin entry points: {sorted(absent)}")


def prepare_venv(release: Path, previous: Path | None = None, *, uv: str = "uv",
                 source: Path | None = None, source_python: Path | None = None) -> tuple[Path, str]:
    """Build at the final release path: venv scripts and metadata are not relocatable."""
    if source is not None:
        source_python = source_python or _source_install_python(source)
        extras = _active_locked_extras(source_python, release)
    else:
        extras = []
    _build_venv(release, uv=uv, extras=extras, python=source_python)
    if source is not None:
        restore_active_distributions(source, release, uv=uv, source_python=source_python)
    return release / ".venv", "built"


def smoke_plugins(release: Path, home: Path, *, plugin_dir: Path | None = None) -> None:
    """Load enabled plugins with the candidate interpreter before promotion."""
    python = _release_python(release)
    if not python.exists():
        raise RuntimeError(f"candidate interpreter missing: {python}")
    plugin_dir = plugin_dir or home / "plugins"
    # Never let an import probe run against the real profile: plugin module bodies are
    # arbitrary Python, even when register() itself is deliberately not called.
    with tempfile.TemporaryDirectory(prefix="hermes-plugin-smoke-") as sandbox:
        isolated = Path(sandbox)
        if (home / "config.yaml").exists():
            shutil.copy2(home / "config.yaml", isolated / "config.yaml")
        if plugin_dir.exists():
            if any(path.is_symlink() for path in plugin_dir.rglob("*")):
                raise RuntimeError("plugin smoke refuses symlinks in shared plugin tree")
            shutil.copytree(plugin_dir, isolated / "plugins")
        env = _release_subprocess_env(release)
        env.update({"HERMES_HOME": str(isolated), "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
        result = subprocess.run(
            [str(python), "-m", "hermes_cli.immutable_releases", "--smoke-imports"],
            cwd=release, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"candidate plugin smoke failed: {detail}")


def _smoke_imports() -> None:
    """Import enabled directory plugins without invoking their register() methods."""
    from hermes_cli.config import load_config
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.plugins_discovery import (
        discover_entrypoint_manifests, gate_manifest, manifest_key,
        resolve_manifest_winners, scan_directory,
    )
    config = load_config() or {}
    plugin_config = config.get("plugins") or {}
    enabled = plugin_config.get("enabled")
    enabled = set(enabled) if enabled is not None else None
    disabled = set(plugin_config.get("disabled") or ())
    manager = get_plugin_manager()
    manifests = scan_directory(Path(os.environ["HERMES_HOME"]) / "plugins", "user")
    directory_keys = {manifest_key(m) for m in manifests}
    manifests.extend(m for m in discover_entrypoint_manifests()
                     if manifest_key(m) not in directory_keys)
    for manifest in resolve_manifest_winners(manifests).values():
        gate = gate_manifest(manifest, disabled, enabled)
        # Model providers are loaded by providers/__init__.py rather than the
        # ordinary plugin registration pass.  They still need a candidate-code
        # import probe: gate_manifest deliberately returns a placeholder for them.
        if manifest.kind == "model-provider" and manifest.key not in disabled and manifest.name not in disabled:
            if manifest.source == "entrypoint":
                manager._load_entrypoint_module(manifest)
            else:
                manager._load_directory_module(manifest)
            continue
        if gate.action == "placeholder" and not gate.enabled:
            if enabled and (manifest.name in enabled or manifest.key in enabled):
                raise RuntimeError(f"enabled plugin {manifest.name}: {gate.error}")
            continue
        if gate.action not in {"load", "load_now"}:
            continue
        if manifest.source == "entrypoint":
            manager._load_entrypoint_module(manifest)
        else:
            manager._load_directory_module(manifest)


if __name__ == "__main__" and "--smoke-imports" in sys.argv:
    _smoke_imports()


def _publish_release(staging: Path, target: Path) -> None:
    """Atomic directory rename, refusing even a concurrently created empty target."""
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # renamex_np(..., RENAME_EXCL) is an atomic no-replace rename on macOS.
        if libc.renamex_np(os.fsencode(staging), os.fsencode(target), 0x00000004) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    elif sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # Test builds on Linux must also preserve the no-replace invariant.
        if not hasattr(libc, "renameat2"):
            raise RuntimeError("atomic no-replace release publishing is unavailable on this host")
        if libc.renameat2(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    else:
        raise RuntimeError("atomic no-replace release publishing is unsupported on this platform")


def _relocate_venv(staging: Path, target: Path) -> None:
    """Rewrite uv's absolute script/editable paths before publishing the built tree."""
    old, new = str(staging).encode(), str(target).encode()
    for root, _, names in os.walk(staging / ".venv"):
        for name in names:
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            if old not in raw:
                continue
            if path.suffix == ".pyc" and path.parent.name == "__pycache__":
                path.unlink()  # disposable bytecode embeds the old co_filename
                continue
            if b"\0" in raw:
                raise RuntimeError(f"cannot relocate binary with staging path: {path}")
            path.write_bytes(raw.replace(old, new))


def _release_is_ready(path: Path, sha: str) -> bool:
    return (path.is_dir() and all((path / name).is_file()
            and (path / name).read_text(encoding="utf-8-sig").strip() == sha
            for name in (".release-ready", ".hermes_build_sha")))


def _build_candidate_web(staging: Path) -> None:
    """Build generated assets from the archived revision, never the source tree."""
    web = staging / "web"
    if not (web / "package.json").is_file():
        return
    from pm import prepare_tools
    from pm.build_operations import verified_tools
    from pm.store import current_target
    target = current_target()
    store = prepare_tools(["node", "npm"], out=staging.parent / ".build-tools", target=target)
    tools = verified_tools(["node", "npm"], source_store=store, target=target)
    npm = str(tools.entries["npm"].binary)
    env = tools.environment(_release_subprocess_env())
    workspaces = ["--workspace", "web", "--include-workspace-root"]
    if (staging / "ui-tui" / "package.json").is_file():
        workspaces[:0] = ["--workspace", "ui-tui"]
    subprocess.run([npm, "ci", "--no-audit", "--no-fund", *workspaces],
                   cwd=staging, env=env, check=True)
    subprocess.run([npm, "run", "build", "--workspace", "web"], cwd=staging, env=env, check=True)
    if not (staging / "hermes_cli" / "web_dist" / "index.html").is_file():
        raise RuntimeError("candidate web build did not produce hermes_cli/web_dist/index.html")


def stage_release(source: Path, home: Path, *, sha: str | None = None,
                  uv: str = "uv", plugin_dir: Path | None = None,
                  source_python: Path | None = None) -> tuple[Path, str]:
    paths = ReleasePaths.for_home(home)
    sha = sha or release_sha(source)
    target = paths.release(sha)
    if target.is_symlink():
        raise RuntimeError(f"release target is a symlink, refusing to follow or replace: {target}")
    if _release_is_ready(target, sha):
        smoke_plugins(target, paths.home, plugin_dir=plugin_dir)
        return target, "existing"
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"release {target} already exists but is incomplete; refusing to replace a potentially pinned release")
    paths.releases.mkdir(parents=True, exist_ok=True)
    staging = paths.releases / f".staging-{sha}-{uuid.uuid4().hex}"
    try:
        source = source.resolve(strict=True)
        if (source / ".git").exists():
            _stage_git_tree(source, staging, sha)
        else:
            _copy_tree(source, staging, home=paths.home)
        if not (staging / "hermes_cli" / "immutable_releases.py").is_file():
            raise RuntimeError(f"revision {sha} predates immutable releases and cannot be staged")
        if (source / ".git").exists():
            _build_candidate_web(staging)
        prepare_venv(staging, previous=read_pointer(paths.current), uv=uv,
                     source=source if (source / ".git").exists() else None,
                     source_python=source_python or (_source_install_python(source, paths.home)
                                                    if (source / ".git").exists() else None))
        from scripts.write_install_stamp import build_stamp
        from hermes_cli.version_info import _git_version_info
        identity = _git_version_info(source, revision=sha) if (source / ".git").exists() else None
        stamp = build_stamp(commit=sha, branch="", dirty=False, source="local",
                            update_mechanism="self",
                            base_version=identity.base_version if identity and identity.base_version != "unknown" else None,
                            display_version=identity.display_version if identity else None,
                            distance=identity.distance if identity else None)
        stamp["branch"] = None
        stamp["commitDate"] = None
        if identity:
            stamp["commitDate"] = identity.commit_date
        (staging / "install-stamp.json").write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        (staging / ".hermes_build_sha").write_text(sha + "\n", encoding="utf-8")
        smoke_plugins(staging, paths.home, plugin_dir=plugin_dir)
        _relocate_venv(staging, target)
        (staging / ".release-ready").write_text(sha + "\n", encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"release {target} appeared during staging; refusing to replace it")
        _publish_release(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target, "staged"


def _sync_dir(path: Path) -> None:
    """Persist a rename/unlink, not only the bytes in the renamed file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"))


def _txn_path(paths: ReleasePaths) -> Path:
    return paths.home / "release-txn.json"


def _read_txn(paths: ReleasePaths) -> dict[str, Any] | None:
    path = _txn_path(paths)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if record["version"] != 1 or record["operation"] not in {
            "promote", "rollback", "first-migration", "first-migration-rollback"
        }:
            raise ValueError("unsupported release transaction")
        return record
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"unreadable release transaction {path}; manual repair required") from exc


def _write_txn(paths: ReleasePaths, record: dict[str, Any]) -> None:
    _atomic_json(_txn_path(paths), record)


def _finish_txn(paths: ReleasePaths, record: dict[str, Any]) -> None:
    # Keep a durable completion identity: after the pending record's final unlink,
    # an immediate CLI --rollback retry must not interpret the newly exchanged
    # previous pointer as a fresh request to roll back the rollback.
    _atomic_json(paths.home / "release-last-txn.json", record)
    # The record is the recovery authority until the final unlink. A crash
    # between cleanup operations may leave an unreferenced backup, never a
    # pending record with its required backup missing.
    _txn_path(paths).unlink(missing_ok=True)
    _sync_dir(paths.home)
    plist = record.get("plist")
    if plist:
        Path(plist["backup"]).unlink(missing_ok=True)
        _sync_dir(paths.home)


def _pointer_value(path: Path) -> str | None:
    if path.is_symlink():
        return str(path.resolve())
    if path.exists():
        raise RuntimeError(f"release pointer is not a symlink: {path}")
    return None


def _set_pointer(path: Path, target: str | None) -> None:
    if _pointer_value(path) == target:
        return
    if target is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_symlink(path, Path(target))
    _sync_dir(path.parent)


def _plist_backup(plist_path: Path | None, paths: ReleasePaths) -> dict[str, Any] | None:
    if plist_path is None or not plist_path.is_file():
        return None
    path = plist_path.resolve()
    body = path.read_bytes()
    backup = paths.home / f"release-plist-{uuid.uuid4().hex}.backup"
    _atomic_bytes(backup, body)
    return {"path": str(path), "backup": str(backup),
            "sha256": hashlib.sha256(body).hexdigest()}


def _plist_intent(plist_path: Path | None, plist_body: bytes | None,
                  paths: ReleasePaths) -> dict[str, Any] | None:
    if plist_body is not None and not isinstance(plist_body, bytes):
        raise TypeError("plist_body must be exact bytes")
    if plist_path is None:
        if plist_body is not None:
            raise ValueError("plist_body requires plist_path")
        return None
    if plist_body is None:
        raise ValueError("plist_path requires precomputed plist_body before transaction")
    if not plist_path.is_file():
        raise RuntimeError(f"installed launchd plist missing: {plist_path}")
    plist = _plist_backup(plist_path, paths)
    assert plist is not None
    plist["intended_body"] = base64.b64encode(plist_body).decode("ascii")
    plist["intended_sha256"] = hashlib.sha256(plist_body).hexdigest()
    return plist


def _ensure_plist_intent(plist: dict[str, Any]) -> None:
    intended = base64.b64decode(plist["intended_body"], validate=True)
    if hashlib.sha256(intended).hexdigest() != plist["intended_sha256"]:
        raise RuntimeError("recorded launchd plist intent failed hash verification")
    target = Path(plist["path"])
    if not target.is_file() or target.read_bytes() != intended:
        _atomic_bytes(target, intended)


def _verify_transaction(paths: ReleasePaths, record: dict[str, Any]) -> None:
    operation = record["operation"]
    expected_current = None if operation == "first-migration-rollback" else record["candidate"]
    expected_previous = None if operation == "first-migration-rollback" else record["previous_intended"]
    if (_pointer_value(paths.current), _pointer_value(paths.previous)) != (expected_current, expected_previous):
        raise RuntimeError("release pointers differ from transaction intent")
    journal = record.get("journal_intended")
    if journal is not None and json.loads((paths.home / "release-layout.json").read_text(encoding="utf-8-sig")) != journal:
        raise RuntimeError("release layout differs from transaction intent")
    plist = record.get("plist")
    if plist and "intended_body" in plist:
        actual = Path(plist["path"]).read_bytes()
        if actual != base64.b64decode(plist["intended_body"], validate=True) or hashlib.sha256(actual).hexdigest() != plist["intended_sha256"]:
            raise RuntimeError("launchd plist differs from transaction intent")


def _runs_hermes_main(argv: list[str], root: Path) -> bool:
    """Is ``argv`` an interpreter entering ``hermes_cli.main`` for exactly ``root``?

    Two launch shapes exist: ``python -m hermes_cli.main`` and the generated
    ``python -I -c <bootstrap>`` launcher that launchd services run. The second
    is accepted only when its code equals the bootstrap generated for ``root``,
    so another tree's or an arbitrary ``-c`` program cannot pass.
    """
    if argv[1:3] == ["-m", "hermes_cli.main"]:
        return True
    if len(argv) < 4:
        return False
    from hermes_cli._launchers import runtime_command
    return argv[1:4] == runtime_command(root, (), module="hermes_cli.main", python=argv[0])[1:4]


def acknowledge_running_release(home: Path, *, gateway_pid: int | None = None) -> bool:
    """Finish a pending reload only after observing its supervised gateway."""
    paths = ReleasePaths.for_home(home)
    record = _read_txn(paths)
    if not record or not record.get("requires_reload") or (
            record.get("candidate") is None and record["operation"] != "first-migration-rollback"):
        return False
    _verify_transaction(paths, record)
    plist = record.get("plist")
    if not plist or "intended_sha256" not in plist:
        return False
    body = Path(plist["path"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != plist["intended_sha256"]:
        return False
    try:
        definition = plistlib.loads(body)
    except (ValueError, TypeError, plistlib.InvalidFileException):
        return False
    if Path(definition.get("EnvironmentVariables", {}).get("HERMES_HOME", "")).resolve() != paths.home:
        return False
    label = definition.get("Label")
    if not isinstance(label, str) or Path(plist["path"]).stem != label:
        return False
    import psutil
    from hermes_cli.gateway_launchd import _launchctl_supervised_pid
    supervisor_pid = _launchctl_supervised_pid(label)
    if not supervisor_pid:
        return False
    try:
        supervisor = psutil.Process(supervisor_pid)
        if supervisor.pid != supervisor_pid:
            return False
        processes = [supervisor, *supervisor.children(recursive=True)]
        if gateway_pid is not None:
            processes = [p for p in processes if p.pid == gateway_pid]
        intended_root = (Path(record["source"]).resolve() if record["operation"] == "first-migration-rollback"
                         else Path(record["candidate"]).resolve())
        expected_sha = (record["source_sha"] if record["operation"] == "first-migration-rollback"
                        else intended_root.name)
        if intended_root.parent == paths.releases.resolve():
            if not _release_is_ready(intended_root, expected_sha):
                return False
        elif release_sha(intended_root) != expected_sha:
            return False
        if gateway_pid is not None and (gateway_pid != os.getpid() or _LOADED_CODE_ROOT != intended_root):
            return False
        for process in processes:
            argv = process.cmdline()
            # The wrapper embeds the command in its argv. Require the inner
            # interpreter's entrypoint before applying the canonical parser,
            # which also accepts a profile selector before `gateway run`.
            from gateway.status import looks_like_gateway_command_line
            if (not _runs_hermes_main(argv, intended_root) or
                    not looks_like_gateway_command_line(shlex.join(argv))):
                continue
            executable = Path(process.exe()).resolve()
            expected_python = (intended_root / ".venv" / "bin" / "python" if
                               intended_root.parent == paths.releases.resolve() else
                               Path(record["journal_original"]["source_python"]))
            if (executable == _interpreter_process_executable(expected_python) and
                    (gateway_pid is None or _LOADED_CODE_ROOT == intended_root) and
                    Path(process.cwd()).resolve() == intended_root and
                    not process.environ().get("PYTHONPATH")):
                _verify_transaction(paths, record)
                record["reload_ack"] = {"plist_sha256": plist["intended_sha256"],
                                        "launchd_pid": supervisor_pid,
                                        "gateway_pid": process.pid,
                                        "release_root": str(intended_root),
                                        "code_sha": expected_sha}
                record["reload_done"] = True
                _write_txn(paths, record)
                _finish_txn(paths, record)
                return True
    except (OSError, ValueError, KeyError, psutil.Error, subprocess.SubprocessError):
        return False
    return False


def wait_for_release_acknowledgement(home: Path, *, timeout_seconds: float = 180.0) -> bool:
    """Observe one already-issued reload until its supervised gateway acknowledges it.

    This is observation-only. It never calls the launchd reload callback and leaves
    the transaction durable when the bounded wait expires.
    """
    pending = ReleasePaths.for_home(home).home / "release-txn.json"
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while pending.exists():
        if acknowledge_running_release(home) or not pending.exists():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return True


def _run_transaction(paths: ReleasePaths, record: dict[str, Any],
                     reload_callback: Callable[[], Any] | None = None) -> dict[str, str | None]:
    operation = record["operation"]
    if (record.get("requires_reload") and reload_callback is None
            and not record.get("reload_done") and not record.get("reload_issued")
            and not (operation == "first-migration" and record.get("candidate") is None)):
        raise RuntimeError("pending release transaction requires its launchd refresh callback")
    plist = record.get("plist")
    if plist and hashlib.sha256(Path(plist["backup"]).read_bytes()).hexdigest() != plist["sha256"]:
        raise RuntimeError("launchd plist backup failed hash verification")
    if plist and "intended_body" in plist:
        intended = base64.b64decode(plist["intended_body"], validate=True)
        if hashlib.sha256(intended).hexdigest() != plist["intended_sha256"]:
            raise RuntimeError("recorded launchd plist intent failed hash verification")
    if record.get("reload_done"):
        # A durable observed ack, not a callback result, authorizes cleanup.
        _verify_transaction(paths, record)
        if not record.get("reload_ack"):
            raise RuntimeError("release reload pending: missing observed gateway acknowledgement")
        _finish_txn(paths, record)
        if operation == "first-migration-rollback":
            return {"current": record["source"], "previous": record["current_original"],
                    "source_sha": record["source_sha"]}
        result = {"current": record["candidate"], "previous": record["previous_intended"]}
        if operation == "first-migration":
            result["source_sha"] = record["source_sha"]
        return result
    for name, expected in (("current", record["current_original"]),
                           ("previous", record["previous_original"])):
        actual = _pointer_value(getattr(paths, name))
        intended = (None if operation == "first-migration-rollback" else
                    (record.get("candidate") if name == "current" else record["previous_intended"]))
        if actual not in (expected, intended):
            raise RuntimeError(f"release {name} pointer diverged during transaction: {actual}")
    if operation == "first-migration" and record.get("candidate") is None:
        journal = record["journal_intended"]
        _atomic_json(paths.home / "release-layout.json", journal)
        _set_pointer(paths.previous, record["previous_intended"])
        return {"current": None, "previous": record["previous_intended"]}

    if operation == "first-migration-rollback":
        source = Path(record["source"])
        if not source.is_dir() or release_sha(source) != record["source_sha"]:
            raise RuntimeError("source checkout changed since migration; refusing unsafe rollback")
        _set_pointer(paths.current, None)
        _set_pointer(paths.previous, None)
        if record.get("journal_intended") is not None:
            _atomic_json(paths.home / "release-layout.json", record["journal_intended"])
        if plist:
            _ensure_plist_intent(plist)
        result = {"current": record["source"], "previous": record["current_original"],
                  "source_sha": record["source_sha"]}
    else:
        candidate = Path(record["candidate"])
        if not _release_is_ready(candidate, candidate.name) or candidate.parent != paths.releases.resolve():
            raise RuntimeError(f"transaction candidate no longer ready: {candidate}")
        if record.get("journal_intended") is not None:
            _atomic_json(paths.home / "release-layout.json", record["journal_intended"])
        _set_pointer(paths.previous, record["previous_intended"])
        _set_pointer(paths.current, str(candidate))
        if plist and "intended_body" in plist:
            _ensure_plist_intent(plist)
        result = {"current": str(candidate), "previous": record["previous_intended"]}
        if operation == "first-migration":
            result["source_sha"] = record["source_sha"]
    if record.get("requires_reload") and not record.get("reload_done"):
        if not record.get("reload_issued"):
            if reload_callback is None:
                raise RuntimeError("pending release transaction requires its launchd refresh callback")
            # Write-ahead delivery: a crash after this fsynced marker may leave
            # the service unloaded, but cannot make a retry kill a starting job.
            # An operator must inspect the label and explicitly repair an absent
            # service; ordinary recovery is strictly observation-only.
            assert plist is not None and "intended_sha256" in plist
            record["reload_issued"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "plist_sha256": plist["intended_sha256"],
                "attempt": 1,
            }
            _write_txn(paths, record)
            reload_status = reload_callback()
            if reload_status not in (True, "deferred"):
                raise RuntimeError("launchd refresh callback failed; reload already issued, inspect service before manual repair")
        _verify_transaction(paths, record)
        if not acknowledge_running_release(paths.home):
            return dict(result, reload_pending=True)
    _verify_transaction(paths, record)
    if not record.get("requires_reload"):
        _finish_txn(paths, record)
    return result


def recover_pending_transaction(home: Path, reload_callback: Callable[[], Any] | None = None
                                ) -> dict[str, str | None] | None:
    """Converge the recorded intent, never infer an inverse from partial pointers."""
    paths = ReleasePaths.for_home(home)
    record = _read_txn(paths)
    if record is None:
        return None
    if record.get("requires_reload") and (record.get("candidate") is not None or
                                           record["operation"] == "first-migration-rollback"):
        # WAL can describe a crash at any point before pointers/plist were
        # applied. Replay the file intent idempotently before checking service.
        if record.get("reload_done"):
            if not record.get("reload_ack"):
                raise RuntimeError("release reload pending: missing observed gateway acknowledgement")
            _verify_transaction(paths, record)
            _finish_txn(paths, record)
            return _transaction_result(record)
        if reload_callback is None and not record.get("reload_issued"):
            raise RuntimeError("release reload pending: restart-authorized update required")
        return _run_transaction(paths, record, reload_callback)
    return _run_transaction(paths, record, reload_callback)


def _transaction_result(record: dict[str, Any]) -> dict[str, str | None]:
    if record["operation"] == "first-migration-rollback":
        return {"current": record["source"], "previous": record["current_original"],
                "source_sha": record["source_sha"]}
    result = {"current": record["candidate"], "previous": record["previous_intended"]}
    if record["operation"] == "first-migration":
        result["source_sha"] = record["source_sha"]
    return result


def activate_release(home: Path, candidate: Path, *, source: Path | None = None,
                     source_python: Path | None = None,
                     plist_path: Path | None = None, plist_body: bytes | None = None,
                     reload_callback: Callable[[], Any] | None = None,
                     before_flip: Callable[[], Any] | None = None,
                     operation: str = "promote", force_reload: bool = False) -> dict[str, str | None]:
    """Persist the exact pointers, layout and plist bytes before any live mutation.

    plist_body is the fully rendered *target* definition. The core writes those
    bytes atomically; reload_callback only reloads launchd and returns False on
    failure. It must tolerate retry after a crash before reload_done is durable.
    """
    paths = ReleasePaths.for_home(home)
    candidate = Path(candidate).resolve()
    if not _release_is_ready(candidate, candidate.name) or candidate.parent != paths.releases.resolve():
        raise ValueError(f"candidate is not a complete release under {paths.releases}: {candidate}")
    if operation not in {"promote", "rollback"}:
        raise ValueError(f"unsupported activation operation: {operation}")
    pending = _read_txn(paths)
    if pending is not None:
        if pending["operation"] == "first-migration" and pending.get("candidate") is None:
            if source is not None and str(Path(source).resolve()) != pending["source"]:
                raise RuntimeError("pending migration belongs to another source")
            if pending.get("plist") and plist_body is None and (source is not None or reload_callback is not None):
                raise ValueError("pending migration requires precomputed plist_body")
            pending["candidate"] = str(candidate)
            pending["journal_intended"] = dict(pending["journal_intended"], state="done")
            pending["requires_reload"] = reload_callback is not None
            if pending.get("plist") and plist_body is not None:
                pending["plist"]["intended_body"] = base64.b64encode(plist_body).decode("ascii")
                pending["plist"]["intended_sha256"] = hashlib.sha256(plist_body).hexdigest()
            _write_txn(paths, pending)
        else:
            result = recover_pending_transaction(paths.home, reload_callback)
            if result and result.get("current") == str(candidate):
                return result
            pending = None
    old = _pointer_value(paths.current)
    if old == str(candidate) and not force_reload:
        return {"current": old, "previous": _pointer_value(paths.previous)}
    if pending is None:
        if source is not None and old is None:
            pending = _migration_record(paths, source, plist_path, candidate=candidate,
                                        plist_body=plist_body, source_python=source_python)
            pending["requires_reload"] = reload_callback is not None
        else:
            plist = _plist_intent(plist_path, plist_body, paths)
            previous = _pointer_value(paths.previous)
            pending = {"version": 1, "operation": operation,
                       "current_original": old, "previous_original": previous,
                       "candidate": str(candidate), "previous_intended": previous if old == str(candidate) else old or previous,
                       "journal_original": None, "journal_intended": None,
                       "requires_reload": reload_callback is not None, "plist": plist}
        _write_txn(paths, pending)
    if before_flip is not None:
        # Compatibility hook: call after previous is recorded but before current.
        _set_pointer(paths.previous, pending["previous_intended"])
        before_flip()
    return _run_transaction(paths, pending, reload_callback)


def promote(home: Path, candidate: Path, *, before_flip=None) -> dict[str, str | None]:
    return activate_release(home, candidate, before_flip=before_flip)


def _receipt_pins(home: Path) -> set[Path]:
    pins: set[Path] = set()
    for path in (home / "logs" / "update_receipts").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot safely prune releases: unreadable update receipt {path}: {exc}") from exc
        # Successful historical transitions are audit records, not permanent
        # rollback leases: pinning every from/to path across the receipt archive
        # would prevent pruning on every normal update. Keep unresolved/failed
        # transitions, plus explicit release pins in any receipt below.
        transition = data.get("release_transition") or {}
        if data.get("outcome") != "success" and isinstance(transition, dict):
            for field in ("from_path", "to_path"):
                raw_path = transition.get(field)
                if isinstance(raw_path, str) and raw_path:
                    pins.add(Path(raw_path).resolve())
        raw = data.get("release") or data.get("release_path")
        if raw:
            pins.add(Path(raw).resolve())
        for step in data.get("steps", []):
            if isinstance(step, dict):
                for field in ("release", "release_path"):
                    if step.get(field):
                        pins.add(Path(step[field]).resolve())
    return pins


def _live_process_pins(home: Path) -> set[Path]:
    """Find release paths advertised by live Hermes processes (best effort, fail closed)."""
    pins: set[Path] = set()
    root = (home / "releases").resolve()
    def all_releases() -> set[Path]:
        return {p.resolve() for p in root.iterdir() if p.is_dir()}
    try:
        import psutil
    except ImportError:
        return all_releases()
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    if current_uid is None:
        return all_releases()
    try:
        for proc in psutil.process_iter(["uids", "name", "cmdline", "environ", "cwd", "exe"]):
            info = proc.info
            uids = info.get("uids")
            if uids is None:
                # Unknown ownership is not proof that a process is safe to ignore.
                return all_releases()
            if uids.real != current_uid and uids.effective != current_uid:
                continue
            env = info.get("environ")
            values = ([*env.values()] if isinstance(env, dict) else [])
            values.extend(info.get("cmdline") or [])
            values.extend((info.get("cwd"), info.get("exe")))
            unreadable = any(info.get(key) is None for key in ("cmdline", "environ", "cwd", "exe"))
            for value in values:
                if not isinstance(value, str):
                    continue
                if value.startswith(str(root)):
                    candidate = Path(value).resolve()
                    if root in candidate.parents:
                        relative = candidate.relative_to(root)
                        if relative.parts:
                            pins.add(root / relative.parts[0])
            executable = str(info.get("exe") or info.get("name") or "").lower()
            if unreadable and "python" in Path(executable).name:
                return all_releases()
    except (psutil.Error, OSError, AttributeError, TypeError):
        return all_releases()
    return pins


def retain(home: Path, *, extra_pins: Iterable[Path] = (), rollback_count: int = 3) -> list[Path]:
    paths = ReleasePaths.for_home(home)
    releases = sorted((p for p in paths.releases.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    keep = {p.resolve() for p in (read_pointer(paths.current), read_pointer(paths.previous)) if p}
    keep.update(p.resolve() for p in extra_pins)
    keep.update(_live_process_pins(paths.home))
    keep.update(_receipt_pins(paths.home))
    current_previous = {p.resolve() for p in (read_pointer(paths.current), read_pointer(paths.previous)) if p}
    keep.update(p.resolve() for p in [p for p in releases if p.resolve() not in current_previous][:rollback_count])
    removed = []
    for release in releases:
        if release.resolve() in keep:
            continue
        shutil.rmtree(release)
        removed.append(release)
    return removed


def _source_python_valid(python: Path, source: Path) -> bool:
    """Only use interpreters which can import this checkout, not the active release."""
    if not python.is_file():
        return False
    probe = ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
             "import hermes_cli; "
             "assert pathlib.Path(hermes_cli.__file__).resolve().parent == "
             "pathlib.Path(sys.argv[1]).resolve() / 'hermes_cli'")
    try:
        return subprocess.run([str(python), "-c", probe, str(source)],
                              capture_output=True, timeout=15,
                              env=_release_subprocess_env()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def source_checkout_python(home: Path, source: Path) -> Path:
    """Resolve the migration-bound source interpreter for CLI and gateway re-entry."""
    journal = ReleasePaths.for_home(home).home / "release-layout.json"
    try:
        record = json.loads(journal.read_text(encoding="utf-8-sig"))
        if Path(record["source"]).resolve() != source.resolve():
            raise RuntimeError(f"migration journal points to another checkout: {journal}")
        recorded = record.get("source_python")
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"cannot read source migration record {journal}: {exc}") from exc
    suffix = Path("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidates = ([Path(recorded)] if recorded else []) + [source / "venv" / suffix, source / ".venv" / suffix]
    for python in candidates:
        if _source_python_valid(python, source):
            return python
    raise RuntimeError(
        f"No usable source checkout interpreter for {source}. Checked {', '.join(map(str, candidates))}. "
        "Restore the recorded Python environment or create source/venv (or source/.venv) "
        "with hermes_cli installed, then retry the update.")


def _migration_record(paths: ReleasePaths, source: Path, plist_path: Path | None,
                      *, candidate: Path | None = None,
                      plist_body: bytes | None = None,
                      source_python: Path | None = None) -> dict[str, Any]:
    source = source.resolve(strict=True)
    source_python = Path(source_python) if source_python is not None else _source_install_python(source, paths.home)
    journal = paths.home / "release-layout.json"
    if not _source_python_valid(source_python, source):
        raise RuntimeError(f"migration requires a source interpreter importing hermes_cli from {source}: {source_python}")
    original = json.loads(journal.read_text(encoding="utf-8-sig")) if journal.exists() else None
    if original is not None:
        if Path(original["source"]).resolve() != source:
            raise RuntimeError("release migration journal refers to a different checkout")
        data = dict(original)
        data.setdefault("source_python", str(source_python))
    else:
        data = {"source": str(source), "source_python": str(source_python),
                "source_sha": release_sha(source), "plist": None}
        if plist_path is not None and plist_path.exists():
            import plistlib
            raw = plist_path.read_bytes()
            definition = plistlib.loads(raw)
            if Path(definition["EnvironmentVariables"]["HERMES_HOME"]).resolve() != paths.home:
                raise RuntimeError("installed plist belongs to another Hermes home")
            if definition["Label"] != plist_path.stem:
                raise RuntimeError("installed plist label does not match its path")
            data["plist"] = {"path": str(plist_path), "body": base64.b64encode(raw).decode("ascii")}
    data["state"] = "done" if candidate is not None else "in-progress"
    plist = (_plist_intent(plist_path, plist_body, paths) if candidate is not None
             else _plist_backup(plist_path, paths))
    return {"version": 1, "operation": "first-migration", "source": str(source),
            "source_sha": data["source_sha"], "candidate": str(candidate) if candidate else None,
            "current_original": None, "previous_original": _pointer_value(paths.previous),
            "previous_intended": str(source), "journal_original": original,
            "journal_intended": data, "plist": plist,
            "requires_reload": False}


def restore_source_layout(home: Path, *, plist_path: Path | None = None,
                          reload_callback: Callable[[], Any] | None = None) -> dict[str, str | None]:
    """Finish a first-migration rollback, including after interrupted unlinks."""
    paths = ReleasePaths.for_home(home)
    pending = _read_txn(paths)
    if pending is not None:
        if pending["operation"] == "first-migration-rollback":
            return _run_transaction(paths, pending, reload_callback)
        recover_pending_transaction(paths.home, reload_callback)
    journal = paths.home / "release-layout.json"
    data = json.loads(journal.read_text(encoding="utf-8-sig"))
    source = Path(data["source"]).resolve(strict=True)
    if read_pointer(paths.previous) != source:
        raise RuntimeError("previous is not the recorded source checkout")
    if release_sha(source) != data["source_sha"]:
        raise RuntimeError("source checkout changed since migration; refusing unsafe rollback")
    current = read_pointer(paths.current)
    if current is None or current.parent != paths.releases.resolve():
        raise RuntimeError("there is no current release to reverse")
    intended = dict(data, state="rolled-back")
    plist = None
    if data.get("plist"):
        import base64
        original = base64.b64decode(data["plist"]["body"], validate=True)
        backup = paths.home / f"release-plist-{uuid.uuid4().hex}.backup"
        _atomic_bytes(backup, original)
        plist = {"path": data["plist"]["path"], "backup": str(backup),
                 "sha256": hashlib.sha256(original).hexdigest(),
                 "intended_body": base64.b64encode(original).decode("ascii"),
                 "intended_sha256": hashlib.sha256(original).hexdigest()}
    record = {"version": 1, "operation": "first-migration-rollback",
              "source": str(source), "source_sha": data["source_sha"],
              "current_original": str(current), "previous_original": str(source),
              "candidate": None, "previous_intended": None,
              "journal_original": data, "journal_intended": intended, "plist": plist,
              "requires_reload": reload_callback is not None}
    _write_txn(paths, record)
    return _run_transaction(paths, record, reload_callback)


def migration_plist(home: Path) -> tuple[Path, bytes] | None:
    """Read back the exact original plist, rather than regenerating a lookalike."""
    import base64
    data = json.loads((ReleasePaths.for_home(home).home / "release-layout.json").read_text(encoding="utf-8-sig"))
    if data["plist"] is None:
        return None
    return Path(data["plist"]["path"]), base64.b64decode(data["plist"]["body"], validate=True)


def abandon_failed_switch(home: Path, *, candidate: Path, previous: Path) -> None:
    """Archive a verified unacknowledged promotion before a guardian rollback.

    The forward WAL cannot be passed to rollback(), because recovery would
    complete that failed promotion first. Archive the exact WAL bytes, which
    retain the plist backup path and hash for postmortem inspection; the backup
    remains at that path. Remove only this matched WAL.
    """
    paths = ReleasePaths.for_home(home)
    record = _read_txn(paths)
    if record is None:
        return
    if (record.get("operation") != "promote" or record.get("reload_ack") or
            not record.get("reload_issued") or record.get("candidate") != str(candidate) or
            record.get("previous_intended") != str(previous)):
        raise RuntimeError("pending release transaction is not the failed promotion")
    _verify_transaction(paths, record)
    pending = _txn_path(paths)
    archive = paths.home / f"release-abandoned-{uuid.uuid4().hex}.json"
    _atomic_bytes(archive, pending.read_bytes())
    # If the transaction changed while the archive was written, do not delete it.
    if json.loads(pending.read_text(encoding="utf-8-sig")) != record:
        raise RuntimeError("release transaction changed during guardian archive")
    pending.unlink()
    _sync_dir(paths.home)


def rollback(home: Path, *, plist_path: Path | None = None, plist_body: bytes | None = None,
             reload_callback: Callable[[], Any] | None = None) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    pending = _read_txn(paths)
    if pending is not None:
        operation = pending["operation"]
        result = recover_pending_transaction(paths.home, reload_callback)
        if operation in {"rollback", "first-migration-rollback"}:
            assert result is not None
            return result
    # An os._exit immediately after the pending record's deletion is also an
    # interrupted CLI invocation. Repeating it cannot reverse the completed
    # rollback merely because previous now identifies the old current.
    last_path = paths.home / "release-last-txn.json"
    if last_path.is_file():
        last = json.loads(last_path.read_text(encoding="utf-8-sig"))
        if last.get("operation") in {"rollback", "first-migration-rollback"}:
            try:
                _verify_transaction(paths, last)
            except (OSError, RuntimeError, ValueError):
                pass  # The observed state changed; this is a distinct transition.
            else:
                if last["operation"] == "first-migration-rollback":
                    return {"current": last["source"], "previous": last["current_original"],
                            "source_sha": last["source_sha"]}
                return {"current": last["candidate"], "previous": last["previous_intended"]}
    previous = read_pointer(paths.previous)
    if previous is None:
        raise RuntimeError("no previous release is available")
    if previous.parent != paths.releases.resolve():
        return restore_source_layout(home, plist_path=plist_path, reload_callback=reload_callback)
    return activate_release(paths.home, previous, plist_path=plist_path, plist_body=plist_body,
                            reload_callback=reload_callback, operation="rollback")


def resolved_release(home: Path) -> Path | None:
    paths = ReleasePaths.for_home(home)
    release = read_pointer(paths.current)
    if release is None or release.parent != paths.releases.resolve():
        return None
    return release if _release_is_ready(release, release.name) else None


def update_source_checkout(home: Path, running_root: Path) -> Path | None:
    """Resolve the only authorized git checkout for an update from a release.

    Never guess from PATH or a sibling checkout: the migration journal binds this
    installation to the original source directory, and the current pointer must
    actually identify the running release.
    """
    paths = ReleasePaths.for_home(home)
    running_root = Path(running_root).resolve()
    if (running_root / ".git").exists():
        return running_root
    if resolved_release(paths.home) != running_root:
        return None
    try:
        record = json.loads((paths.home / "release-layout.json").read_text(encoding="utf-8-sig"))
        source = Path(record["source"]).resolve(strict=True)
        if source == running_root or not (source / ".git").exists():
            return None
        if release_sha(source) is None:
            return None
        return source
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        return None


def worker_launch_spec(release_root: Path | None, base_env: dict[str, str]
                       ) -> tuple[str, Path, dict[str, str]]:
    """Select the owning physical interpreter, cwd and environment as one unit.

    A source checkout retains the existing interpreter and import-path behavior.
    A release never inherits a `current` entry even for another profile; no
    consumer may reassemble only a subset of this specification.
    """
    env = dict(base_env)
    if release_root is None:
        from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
        return sys.executable, _LOADED_CODE_ROOT, pin_hermes_tree_on_pythonpath(env, _LOADED_CODE_ROOT)

    root = Path(release_root).resolve()
    venv = root / ".venv"
    executable = _release_python(root)
    if not executable.is_file():
        raise RuntimeError(f"cron release interpreter unavailable: {executable}")
    binary = str(executable.parent)

    def physical_entries(value: str) -> list[str]:
        # An inherited `current` path changes meaning during the worker's life.
        return [entry for entry in value.split(os.pathsep)
                if entry and "current" not in Path(entry).parts and entry != str(root)
                and entry != binary and entry != str(venv)]

    env.update({"HERMES_RELEASE": str(root), "VIRTUAL_ENV": str(venv),
                "PATH": os.pathsep.join([binary, *physical_entries(env.get("PATH", ""))]),
                "PYTHONPATH": os.pathsep.join([str(root), *physical_entries(env.get("PYTHONPATH", ""))])})
    return str(executable), root, env
