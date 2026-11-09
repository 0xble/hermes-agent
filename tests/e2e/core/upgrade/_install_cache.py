"""Download caches for the expensive historical-install scenarios.

Only verified archives, package caches and versioned managed interpreters are shared.
Each scenario still creates its own checkout,
install identity, environment, selection records and user data through the real installer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping
import hashlib
import json
import shutil
import subprocess
import tempfile
import time


class InstallCache:
    def __init__(self, root: Path, download_env: Mapping[str, str] | None = None):
        self.root = root
        self.deadline = time.monotonic() + 840
        # The runner explicitly supplies isolated content caches. Do not discover a cache by
        # probing the user's HOME, and never inherit a venv, selection record or user config.
        source = download_env or {}
        self.downloads = {key: Path(source[key]) for key in
                          ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache")
                          if source.get(key) and Path(source[key]).is_absolute()}

    def remaining(self, timeout: float) -> float:
        remaining = self.deadline - time.monotonic()
        assert remaining > 0, "historical-install file exhausted its 840s diagnostic budget"
        return min(timeout, remaining)

    def artifact(self, url: str, digest: str, *, timeout: float = 30) -> Path:
        """One bounded request, atomic publication, and no network on a verified cache hit."""
        from pm.filesystem import lock_fd

        uv_cache = self.downloads.get("UV_CACHE_DIR")
        root = uv_cache.with_name(uv_cache.name + "-pm-archives") if uv_cache else self.root / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        target = root / digest
        with (root / f"{digest}.lock").open("a+b") as lock:
            assert lock_fd(lock.fileno(), wait=True, timeout=self.remaining(30)), f"archive cache busy: {url}"
            if target.exists():
                with target.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                assert actual == digest, f"cached archive sha256 mismatch for {url}: {actual} != {digest}"
                return target
            timeout = self.remaining(timeout)
            with tempfile.NamedTemporaryFile(dir=root, prefix=".download-", delete=False) as stream:
                staged = Path(stream.name)
            try:
                cp = subprocess.run(
                    ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "0",
                     "--connect-timeout", str(min(10, timeout)), "--max-time", str(timeout),
                     "--output", str(staged), url],
                    capture_output=True, text=True, timeout=self.remaining(timeout + 5), check=False,
                )
                assert cp.returncode == 0, f"historical archive download failed: {url}: {cp.stderr}"
                with staged.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                assert actual == digest, f"archive sha256 mismatch for {url}: {actual} != {digest}"
                staged.replace(target)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AssertionError(f"historical archive download failed: {url}: {exc}") from exc
            finally:
                staged.unlink(missing_ok=True)
        return target

    def prepare(self, env: dict[str, str], ref: str | None = None) -> None:
        """Seed private PM stores with lock-pinned bytes, never installed selections or venvs."""
        from pm.artifact_mirror import mirror_url
        from pm.registry import all_packages, get_package, source_install_packages, walk
        from pm.store import current_target, extract, flatten_single_dir
        from tests.e2e.core.upgrade import _helpers as H

        if ref is None:
            lock = H.WORKTREE / "pm" / "lock.json"
            if not lock.is_file():
                return  # small local-Git helper consumers need no PM artifacts
            packages = json.loads(lock.read_text(encoding="utf-8-sig"))["packages"]
        else:
            cp = subprocess.run(["git", "show", f"{ref}:pm/lock.json"], cwd=H.WORKTREE,
                                capture_output=True, text=True, check=True, timeout=self.remaining(30))
            packages = json.loads(cp.stdout)["packages"]
        names = source_install_packages(all_packages())
        names += [name for name in all_packages() if get_package(name).default]
        target = current_target()
        store = Path(env["HERMES_RUNTIME_DIR"])
        for package in walk(names):
            pin = packages.get(package.name, {})
            artifacts = pin.get("artifacts", {})
            plan = artifacts.get(target, artifacts.get("any", []))
            for artifact in plan if isinstance(plan, list) else [plan]:
                url, digest = artifact["url"], artifact["sha256"]
                print(f"Preparing pinned {package.name} archive: {url}", flush=True)
                archive = self.artifact(mirror_url(digest), digest)
                entry = store / f"fetch-{digest}"
                entry.mkdir(parents=True, exist_ok=True)
                destination = entry / url.rsplit("/", 1)[-1]
                for previous in entry.iterdir():
                    if previous != destination:
                        previous.unlink()
                shutil.copy2(archive, destination)
                # The unmodified shell bootstrap checks this exact pinned uv slot before
                # PM can consume its fetch cache. Do its normal archive extraction here.
                if package.name == "uv":
                    uv_entry = store / f"uv-{pin['version']}-{target}"
                    if not uv_entry.exists():
                        extract(destination, uv_entry)
                        flatten_single_dir(uv_entry)

    def environment(self, env: dict[str, str], *, managed_uv: str | None = None) -> dict[str, str]:
        home = Path(env["HERMES_HOME"])
        uv_cache = self.downloads.get("UV_CACHE_DIR", self.root / "uv")
        runtime = home / "tools"
        python = self.downloads.get("UV_PYTHON_INSTALL_DIR", self.root / "python")
        npm = self.downloads.get("npm_config_cache", self.root / "npm")
        for path in (uv_cache, runtime, python, npm):
            path.mkdir(parents=True, exist_ok=True)
        # PM deliberately uses the machine's default Hermes cache, not UV_CACHE_DIR. The old
        # fixture only set the latter, so every HEAD takeover downloaded its own wheel set.
        cache = Path(env["HOME"]) / ".hermes" / "cache" / "uv"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            cache.symlink_to(uv_cache, target_is_directory=True)
        assert cache.resolve() == uv_cache.resolve(), f"unexpected pre-existing cache: {cache}"
        if managed_uv is not None:
            binary = home / "bin" / "uv"
            binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(managed_uv, binary)
        return {**env, "UV_CACHE_DIR": str(uv_cache), "UV_PYTHON_INSTALL_DIR": str(python),
                "HERMES_RUNTIME_DIR": str(runtime), "npm_config_cache": str(npm), "npm_config_audit": "false",
                "UV_HTTP_TIMEOUT": "30", "npm_config_fetch_retries": "0", "npm_config_fetch_timeout": "30000"}
