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


def _copy_tree(source: Path, target: Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", "*.pyc")
    shutil.copytree(source, target, ignore=ignore, symlinks=True)


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
    from hermes_cli.plugins_discovery import scan_directory, gate_manifest
    config = load_config() or {}
    plugin_config = config.get("plugins") or {}
    enabled = plugin_config.get("enabled")
    enabled = set(enabled) if enabled is not None else None
    disabled = set(plugin_config.get("disabled") or ())
    manager = get_plugin_manager()
    for manifest in scan_directory(Path(os.environ["HERMES_HOME"]) / "plugins", "user"):
        gate = gate_manifest(manifest, disabled, enabled)
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
    if target.exists() and (target / ".release-ready").exists():
        smoke_plugins(target, paths.home, plugin_dir=plugin_dir)
        return target, "existing"
    paths.releases.mkdir(parents=True, exist_ok=True)
    staging = paths.releases / f".{sha}.staging-{os.getpid()}"
    published = False
    try:
        _copy_tree(source, staging)
        if target.exists():
            # A crashed build never becomes an apparently usable release.
            shutil.rmtree(target)
        os.replace(staging, target)
        published = True
        prepare_venv(target, previous=read_pointer(paths.current), uv=uv)
        smoke_plugins(target, paths.home, plugin_dir=plugin_dir)
        (target / ".release-ready").write_text(sha + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if published:
            shutil.rmtree(target, ignore_errors=True)
        raise
    return target, "staged"


def promote(home: Path, candidate: Path) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    candidate = candidate.resolve()
    if not candidate.is_dir() or candidate.parent != paths.releases.resolve():
        raise ValueError(f"candidate is not a release under {paths.releases}: {candidate}")
    old = read_pointer(paths.current)
    if old:
        _atomic_symlink(paths.previous, old)
    _atomic_symlink(paths.current, candidate)
    return {"current": str(candidate), "previous": str(old) if old else None}


def _receipt_pins(home: Path) -> set[Path]:
    pins: set[Path] = set()
    for path in (home / "logs" / "update_receipts").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
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


def rollback(home: Path) -> dict[str, str | None]:
    paths = ReleasePaths.for_home(home)
    previous = read_pointer(paths.previous)
    if previous is None:
        raise RuntimeError("no previous release is available")
    return promote(paths.home, previous)


def resolved_release(home: Path) -> Path | None:
    return read_pointer(ReleasePaths.for_home(home).current)


def detached_worker_env(home: Path, release: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Pin worker executable/cwd/import path to a resolved release, not ``current``."""
    env = dict(base or os.environ)
    env.update({"HERMES_RELEASE": str(release.resolve()),
                "PYTHONPATH": str(release.resolve()) + os.pathsep + env.get("PYTHONPATH", "")})
    return env
