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
                            check=True, capture_output=True, text=True)
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


def _build_venv(release: Path, *, uv: str = "uv") -> None:
    subprocess.run([uv, "sync", "--frozen", "--python", sys.executable], cwd=release, check=True)


def prepare_venv(release: Path, previous: Path | None = None, *, uv: str = "uv") -> tuple[Path, str]:
    """Build at the final release path: venv scripts and metadata are not relocatable."""
    _build_venv(release, uv=uv)
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
        env = os.environ.copy()
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


def stage_release(source: Path, home: Path, *, sha: str | None = None,
                  uv: str = "uv", plugin_dir: Path | None = None) -> tuple[Path, str]:
    paths = ReleasePaths.for_home(home)
    sha = sha or release_sha(source)
    target = paths.release(sha)
    if (target.is_dir() and (target / ".release-ready").is_file()
            and (target / ".release-ready").read_text(encoding="utf-8").strip() == sha
            and (target / ".hermes_build_sha").is_file()
            and (target / ".hermes_build_sha").read_text(encoding="utf-8").strip() == sha):
        smoke_plugins(target, paths.home, plugin_dir=plugin_dir)
        return target, "existing"
    paths.releases.mkdir(parents=True, exist_ok=True)
    staging = paths.releases / f".{sha}.staging-{os.getpid()}"
    published = False
    try:
        source = source.resolve(strict=True)
        # Git archives contain only tracked bytes at SHA; an ignored HERMES_HOME
        # nested in the checkout cannot enter the artifact or recurse into itself.
        if (source / ".git").exists():
            _stage_git_tree(source, staging, sha)
        else:
            _copy_tree(source, staging, home=paths.home)
        if target.exists():
            # A crashed build never becomes an apparently usable release.
            shutil.rmtree(target)
        os.replace(staging, target)
        published = True
        prepare_venv(target, previous=read_pointer(paths.current), uv=uv)
        (target / ".hermes_build_sha").write_text(sha + "\n", encoding="utf-8")
        if (target / ".hermes_build_sha").read_text(encoding="utf-8").strip() != sha:
            raise RuntimeError("release identity stamp mismatch")
        smoke_plugins(target, paths.home, plugin_dir=plugin_dir)
        (target / ".release-ready").write_text(sha + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if published:
            shutil.rmtree(target, ignore_errors=True)
        raise
    return target, "staged"


def promote(home: Path, candidate: Path, *, before_flip=None) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    candidate = candidate.resolve()
    if (not candidate.is_dir() or candidate.parent != paths.releases.resolve()
            or not (candidate / ".release-ready").is_file()
            or (candidate / ".release-ready").read_text(encoding="utf-8").strip() != candidate.name):
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
        except (OSError, ValueError):
            continue
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
    try:
        import psutil
        for proc in psutil.process_iter(["cmdline", "environ", "cwd", "exe"]):
            try:
                env = proc.info.get("environ") or {}
                values = list(env.values()) + list(proc.info.get("cmdline") or [])
                values.extend([proc.info.get("cwd"), proc.info.get("exe")])
            except (psutil.Error, OSError):
                # Unknown process identity cannot justify deleting a release.
                return {p.resolve() for p in root.iterdir() if p.is_dir()}
            for value in values:
                if not isinstance(value, str):
                    continue
                candidate = Path(value).resolve() if value.startswith(str(root)) else None
                if candidate and root in candidate.parents:
                    relative = candidate.relative_to(root)
                    if relative.parts:
                        pins.add(root / relative.parts[0])
    except ImportError:
        return {p.resolve() for p in root.iterdir() if p.is_dir()}
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
    if journal.exists():
        data = json.loads(journal.read_text(encoding="utf-8"))
        if Path(data["source"]).resolve() != source:
            raise RuntimeError("release migration journal refers to a different checkout")
    else:
        data = {"source": str(source), "source_sha": release_sha(source), "plist": None}
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
    marker = release / ".release-ready"
    return release if marker.is_file() and marker.read_text(encoding="utf-8").strip() == release.name else None


def detached_worker_env(home: Path, release: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Pin worker executable/cwd/import path to a resolved release, not ``current``."""
    env = dict(base or os.environ)
    env.update({"HERMES_RELEASE": str(release.resolve()),
                "PYTHONPATH": str(release.resolve()) + os.pathsep + env.get("PYTHONPATH", "")})
    return env
