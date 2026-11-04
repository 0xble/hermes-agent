"""Download caches for the expensive historical-install scenarios.

Only content-addressed downloads are shared. Each scenario still creates its own checkout,
install identity, environment, selection records and user data through the real installer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping
import shutil


class InstallCache:
    def __init__(self, root: Path, download_env: Mapping[str, str] | None = None):
        self.root = root
        # The runner explicitly supplies isolated content caches. Do not discover a cache by
        # probing the user's HOME, and never inherit a venv, selection record or user config.
        source = download_env or {}
        self.downloads = {key: Path(source[key]) for key in
                          ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache")
                          if source.get(key) and Path(source[key]).is_absolute()}

    def environment(self, env: dict[str, str], *, managed_uv: str | None = None) -> dict[str, str]:
        home = Path(env["HERMES_HOME"])
        uv_cache = self.downloads.get("UV_CACHE_DIR", self.root / "uv")
        runtime = self.root / "runtime"
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
                "HERMES_RUNTIME_DIR": str(runtime), "npm_config_cache": str(npm), "npm_config_audit": "false"}
