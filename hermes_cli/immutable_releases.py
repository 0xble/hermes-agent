"""Immutable per-version release management for the Hermes runtime.

The source checkout remains the update authority.  A release is a detached copy
of one source revision, with its own virtual environment and an atomic
``current`` symlink in ``$HERMES_HOME``.  This module is deliberately small and
side-effect explicit so the transactional updater can use it as one stage.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import shutil
import subprocess
import sys
import uuid
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


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
        check=True, capture_output=True, text=True,
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
                            check=True, capture_output=True, text=True,
                            env=_release_subprocess_env())
    return result.stdout.strip()


def _release_python(release: Path) -> Path:
    return release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


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
            if staging.resolve() not in dest.parents and dest != staging.resolve():
                raise RuntimeError("unsafe git archive path")
            if member.issym() or member.islnk():
                raise RuntimeError("release archive contains symlink")
        tar.extractall(staging, filter="data")


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
            record = json.loads(journal.read_text(encoding="utf-8"))
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

    project_data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    groups = project_data["project"].get("optional-dependencies", {})
    lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
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
                                     capture_output=True, text=True, env=_release_subprocess_env())
    selected.update(extra for extra in json.loads(metadata_result.stdout) if extra in declared)

    def closure(extra: str) -> set[str]:
        pending = list(declared[extra])
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            for dep in packages.get(name, {}).get("dependencies", []):
                pending.append(canonicalize_name(dep["name"]))
        return seen

    closure_by_extra = {extra: closure(extra) for extra in declared}
    covered = set().union(*(closure_by_extra[e] for e in selected)) if selected else set()
    # A source may retain a locked transitive dependency after its parent was
    # removed. Bring it in through its lockfile extra, never an unpinned pip restore.
    remaining = (installed & packages.keys()) - covered - {"hermes-agent"}
    base = set()
    for req in project_data["project"].get("dependencies", []):
        dep = Requirement(req)
        if dep.marker is None or dep.marker.evaluate():
            base.add(canonicalize_name(dep.name))
    pending = list(base)
    while pending:
        name = pending.pop()
        if name in base and name not in packages:
            continue
        for dep in packages.get(name, {}).get("dependencies", []):
            child = canonicalize_name(dep["name"])
            if child not in base:
                base.add(child)
                pending.append(child)
    remaining -= base
    while remaining:
        options = [(len(remaining & names), extra) for extra, names in closure_by_extra.items()
                   if extra not in selected]
        count, extra = max(options, default=(0, ""))
        if not count:
            break  # subsequent parity check names every uncovered distribution
        selected.add(extra)
        remaining -= closure_by_extra[extra]
    return sorted(selected)


def _build_venv(release: Path, *, uv: str = "uv", extras: Sequence[str] = ()) -> None:
    cmd = [uv, "sync", "--frozen", "--python", sys.executable]
    for extra in extras:
        cmd.extend(("--extra", extra))
    subprocess.run(cmd, cwd=release, env=_release_subprocess_env(release), check=True)


def _active_distributions(python: Path) -> dict[str, str]:
    script = ("import importlib.metadata as m,json; "
              "import re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower(): d.version "
              "for d in m.distributions() if d.metadata.get('Name')}))")
    result = subprocess.run([str(python), "-c", script], check=True,
                            capture_output=True, text=True, env=_release_subprocess_env())
    return json.loads(result.stdout)


def _active_plugin_entrypoints(python: Path, names: set[str] | None = None) -> set[tuple[str, str, str]]:
    script = ("import importlib.metadata as m,json,re; "
              "groups={'hermes_agent.plugins','hermes_agent.plugin_capabilities'}; "
              "print(json.dumps(sorted((re.sub(r'[-_.]+','-',d.metadata['Name']).lower(),e.group,e.name,e.value) "
              "for d in m.distributions() if d.metadata.get('Name') "
              "for e in d.entry_points if e.group in groups)))")
    result = subprocess.run([str(python), "-c", script], check=True,
                            capture_output=True, text=True, env=_release_subprocess_env())
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
              for p in tomllib.loads(lock.read_text(encoding="utf-8"))["package"]}
    locked_versions = {name: target[name] for name in locked if name in target}
    extras = {name: version for name, version in installed.items()
              if name not in locked and name not in {"hermes-agent", "hermes-agent-cli"}}
    missing = [f"{name}=={version}" for name, version in sorted(extras.items())
               if target.get(name) != version]
    if missing:
        subprocess.run([uv, "pip", "install", "--no-deps", "--python", str(candidate_python), *missing],
                       env=_release_subprocess_env(candidate), check=True)
    target = _active_distributions(candidate_python)
    unmatched = [name for name in installed if name not in {"hermes-agent", "hermes-agent-cli"}
                 and (name not in target or (name not in locked and target[name] != installed[name]))]
    if unmatched:
        raise RuntimeError(f"candidate distribution parity failed: {', '.join(sorted(unmatched))}")
    changed = [name for name in locked if name in target and name in locked_versions
               and target[name] != locked_versions[name]]
    if changed:
        raise RuntimeError(f"candidate lock distributions changed: {', '.join(sorted(changed))}")
    absent = _active_plugin_entrypoints(source_python, set(extras)) - _active_plugin_entrypoints(candidate_python, set(extras))
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
    _build_venv(release, uv=uv, extras=extras)
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
            cwd=release, env=env, capture_output=True, text=True, timeout=90,
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
            and (path / name).read_text(encoding="utf-8").strip() == sha
            for name in (".release-ready", ".hermes_build_sha")))


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
            bundle = source / "hermes_cli" / "web_dist"
            if bundle.is_dir():
                shutil.copytree(bundle, staging / "hermes_cli" / "web_dist", dirs_exist_ok=True)
                if not (staging / "hermes_cli" / "web_dist" / "index.html").is_file():
                    raise RuntimeError("candidate web_dist lacks index.html")
        else:
            _copy_tree(source, staging, home=paths.home)
        if not (staging / "hermes_cli" / "immutable_releases.py").is_file():
            raise RuntimeError(f"revision {sha} predates immutable releases and cannot be staged")
        prepare_venv(staging, previous=read_pointer(paths.current), uv=uv,
                     source=source if (source / ".git").exists() else None,
                     source_python=source_python or (_source_install_python(source, paths.home)
                                                    if (source / ".git").exists() else None))
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


def promote(home: Path, candidate: Path, *, before_flip=None) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    candidate = candidate.resolve()
    if not _release_is_ready(candidate, candidate.name) or candidate.parent != paths.releases.resolve():
        raise ValueError(f"candidate is not a complete release under {paths.releases}: {candidate}")
    old = read_pointer(paths.current)
    if old == candidate:
        return {"current": str(candidate), "previous": str(read_pointer(paths.previous)) if read_pointer(paths.previous) else None}
    if old:
        _atomic_symlink(paths.previous, old)
    if before_flip is not None:
        before_flip()
    _atomic_symlink(paths.current, candidate)
    previous = old or read_pointer(paths.previous)
    return {"current": str(candidate), "previous": str(previous) if previous else None}


def _receipt_pins(home: Path) -> set[Path]:
    pins: set[Path] = set()
    for path in (home / "logs" / "update_receipts").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
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
    try:
        for proc in psutil.process_iter(["cmdline", "environ", "cwd", "exe"]):
            try:
                info = proc.info
                # psutil may suppress AccessDenied/NoSuchProcess and report None
                # for requested attrs. A process whose cwd is unreadable might
                # be executing inside the release we are about to delete.
                if any(info.get(key) is None for key in ("cmdline", "environ", "cwd", "exe")):
                    return all_releases()
                env = info["environ"]
                values = list(env.values()) + list(info["cmdline"])
                values.extend([info["cwd"], info["exe"]])
            except (psutil.Error, OSError, AttributeError, TypeError):
                return all_releases()
            for value in values:
                if not isinstance(value, str):
                    continue
                candidate = Path(value).resolve() if value.startswith(str(root)) else None
                if candidate and root in candidate.parents:
                    relative = candidate.relative_to(root)
                    if relative.parts:
                        pins.add(root / relative.parts[0])
    except (psutil.Error, OSError):
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
        record = json.loads(journal.read_text(encoding="utf-8"))
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


def begin_migration(home: Path, source: Path, plist_path: Path | None = None) -> bool:
    """Save the original checkout service definition before the first release flip.

    The migration journal and previous pointer survive a process crash; a rerun
    never replaces the original plist with an already-migrated one.
    """
    paths = ReleasePaths.for_home(home)
    if read_pointer(paths.current) is not None:
        return False
    source = source.resolve(strict=True)
    journal = paths.home / "release-layout.json"
    if not _source_python_valid(Path(sys.executable), source):
        raise RuntimeError(f"migration requires an interpreter importing hermes_cli from {source}: {sys.executable}")
    if journal.exists():
        data = json.loads(journal.read_text(encoding="utf-8"))
        if Path(data["source"]).resolve() != source:
            raise RuntimeError("release migration journal refers to a different checkout")
        # An interrupted migration may have written an older journal without the
        # interpreter; preserve its original plist while completing that record.
        data.setdefault("source_python", sys.executable)
    else:
        data = {"source": str(source), "source_python": sys.executable,
                "source_sha": release_sha(source), "plist": None}
        if plist_path is not None and plist_path.exists():
            import base64
            import plistlib
            raw = plist_path.read_bytes()
            definition = plistlib.loads(raw)
            if Path(definition["EnvironmentVariables"]["HERMES_HOME"]).resolve() != paths.home:
                raise RuntimeError("installed plist belongs to another Hermes home")
            if definition["Label"] != plist_path.stem:
                raise RuntimeError("installed plist label does not match its path")
            data["plist"] = {"path": str(plist_path), "body": base64.b64encode(raw).decode("ascii")}
    journal.parent.mkdir(parents=True, exist_ok=True)
    tmp = journal.with_name(f".{journal.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, journal)
    finally:
        tmp.unlink(missing_ok=True)
    _atomic_symlink(paths.previous, source)
    return True


def restore_source_layout(home: Path) -> dict[str, str | None]:
    """Reverse the first migration, preserving the candidate as previous."""
    paths = ReleasePaths.for_home(home)
    journal = paths.home / "release-layout.json"
    data = json.loads(journal.read_text(encoding="utf-8"))
    source = Path(data["source"]).resolve(strict=True)
    if read_pointer(paths.previous) != source:
        raise RuntimeError("previous is not the recorded source checkout")
    # Never point launchd at a checkout that has advanced past the saved
    # migration revision. Refuse dirty trees before changing any pointer/plist.
    expected_sha = data["source_sha"]
    actual_sha = release_sha(source)
    status = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"],
                            capture_output=True, text=True, check=True)
    if status.stdout.strip():
        raise RuntimeError(f"source checkout is dirty; cannot restore migration revision {expected_sha}")
    if actual_sha != expected_sha:
        restored = subprocess.run(["git", "-C", str(source), "reset", "--hard", expected_sha],
                                  capture_output=True, text=True)
        if restored.returncode or release_sha(source) != expected_sha:
            raise RuntimeError(f"could not restore source checkout revision {expected_sha}: {restored.stderr}")
    current = read_pointer(paths.current)
    if current is None:
        raise RuntimeError("there is no current release to reverse")
    # Keep current complete at every crash boundary, even during reversal. Once
    # the source plist has been reloaded the pointer can be removed by a later run.
    _atomic_symlink(paths.current, source)
    _atomic_symlink(paths.previous, current)
    return {"current": str(source), "previous": str(current), "source_sha": data["source_sha"]}


def migration_plist(home: Path) -> tuple[Path, bytes] | None:
    """Read back the exact original plist, rather than regenerating a lookalike."""
    import base64
    data = json.loads((ReleasePaths.for_home(home).home / "release-layout.json").read_text(encoding="utf-8"))
    if data["plist"] is None:
        return None
    return Path(data["plist"]["path"]), base64.b64decode(data["plist"]["body"], validate=True)


def rollback(home: Path) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    previous = read_pointer(paths.previous)
    if previous is None:
        raise RuntimeError("no previous release is available")
    if previous.parent != paths.releases.resolve():
        return restore_source_layout(home)
    return promote(paths.home, previous)


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
        record = json.loads((paths.home / "release-layout.json").read_text(encoding="utf-8"))
        source = Path(record["source"]).resolve(strict=True)
        if source == running_root or not (source / ".git").exists():
            return None
        if release_sha(source) is None:
            return None
        return source
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        return None


def detached_worker_env(home: Path, release: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Pin worker executable/cwd/import path to a resolved release, not ``current``."""
    env = dict(base or os.environ)
    root = release.resolve()
    venv = root / ".venv"
    binary = str(venv / ("Scripts" if os.name == "nt" else "bin"))
    previous = env.get("PATH", "").split(os.pathsep)
    env.update({"HERMES_RELEASE": str(root), "VIRTUAL_ENV": str(venv),
                "PATH": os.pathsep.join([binary] + [p for p in previous if p and p != binary]),
                "PYTHONPATH": str(root) + os.pathsep + env.get("PYTHONPATH", "")})
    return env
