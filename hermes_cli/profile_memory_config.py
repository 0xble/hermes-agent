"""Carry the ACTIVE memory provider's own config into a ``--clone`` (#120115).

``--clone`` copies ``config.yaml`` — and with it ``memory.provider: hindsight`` — but the
provider keeps its settings outside config.yaml, so the clone booted with the provider
selected and silently unavailable. Providers store per-home config by convention (the same
convention ``hermes_cli.web_routers.memory_providers`` reads): a ``<home>/<provider>/``
directory (hindsight) or a flat ``<home>/<provider>.json`` (mem0, honcho, supermemory). Copying
by convention keeps this free of plugin imports: the provider may live in the catalog, not in
tree, so a hook the plugin must implement could not fix the reported case.
"""

import contextlib
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Optional

# A provider name is a bare directory/file stem; anything else (path separators, ``..``, spaces)
# would let a hand-edited config.yaml aim the copy outside the source profile.
_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def active_memory_provider(config: Optional[dict]) -> Optional[str]:
    """The external ``memory.provider`` named in a parsed config.yaml, or None for the built-in
    store or an unsafe name."""
    from agent.memory_provider import is_core_memory_provider

    memory = (config or {}).get("memory")
    name = memory.get("provider") if isinstance(memory, dict) else None
    if not isinstance(name, str) or is_core_memory_provider(name):
        return None
    name = name.strip()
    if name in {".", ".."} or not _PROVIDER_NAME_RE.match(name):
        return None
    return name


def _is_regular(path: Path, want: int) -> bool:
    """True when *path* itself (not a symlink target) has file type *want*."""
    try:
        return stat.S_IFMT(os.lstat(path).st_mode) == want
    except OSError:
        return False


def _copy_owner_only(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst, follow_symlinks=False)
    with contextlib.suppress(OSError):
        os.chmod(str(dst), 0o600)


def clone_memory_provider_config(source_dir: Path, profile_dir: Path, provider: Optional[str]) -> bool:
    """Copy ``<provider>/`` and/or ``<provider>.json`` from *source_dir* into *profile_dir* when
    present. Files land owner-only like ``.env``: they can hold an API key. Returns True when
    anything was copied.

    Only real files and directories inside the source profile travel. A symlink is never
    followed: it could point anywhere, and following it would turn a config clone into a copy
    of unrelated data. Sockets, FIFOs and other special files are skipped rather than aborting
    the clone."""
    if not provider:
        return False
    copied = False
    src_dir = source_dir / provider
    if _is_regular(src_dir, stat.S_IFDIR):
        for root, dirs, files in os.walk(src_dir, followlinks=False):
            rel = Path(root).relative_to(src_dir)
            dirs[:] = [d for d in dirs if _is_regular(Path(root) / d, stat.S_IFDIR)]
            (profile_dir / provider / rel).mkdir(parents=True, exist_ok=True)
            for filename in files:
                src = Path(root) / filename
                if _is_regular(src, stat.S_IFREG):
                    _copy_owner_only(src, profile_dir / provider / rel / filename)
        copied = True
    src_file = source_dir / f"{provider}.json"
    if _is_regular(src_file, stat.S_IFREG):
        _copy_owner_only(src_file, profile_dir / f"{provider}.json")
        copied = True
    return copied


def cloned_memory_provider(profile_dir: Path) -> Optional[str]:
    """Name of the external provider whose config *profile_dir* now carries, for the CLI notice."""
    from hermes_cli.profiles import _load_yaml_dict

    provider = active_memory_provider(_load_yaml_dict(profile_dir / "config.yaml"))
    if provider and ((profile_dir / provider).is_dir() or (profile_dir / f"{provider}.json").is_file()):
        return provider
    return None
