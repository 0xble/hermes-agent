"""Only builds whose config schema is not newer than the live release may write its config.

A Hermes home on immutable releases runs whatever ``<root>/current`` names. A dev worktree or
sync candidate can carry a newer config schema; its config writes stamp ``_config_version`` past
the live release and block the next ``hermes update`` (``config ... is newer than this release``).
Hermes's own ``config.yaml`` writers (config set/unset, ``save_config``, ``migrate_config``,
``import-agent``) go through utils' ``_atomic_write``, which calls
:func:`ensure_release_owns_config_write` first. The agent file tool writes text a model authored,
never a schema stamp, so it is deliberately outside this guard.

A previously-live release still running after promotion (an open CLI, a gateway awaiting restart,
a pinned worker) has an older or equal schema and keeps writing: ``migrate_config`` only stamps
upward to its own latest version, so it cannot raise the stamp.

``hermes update`` legitimately writes from code that is not yet ``current``: the
source-checkout updater, and the post-swap child that migrates config from the staged
candidate before promotion. Those entrypoints run inside :func:`updater_owns_config_writes`.
That is a process-local ContextVar, not an environment variable, so a separate ``hermes
config set`` process never inherits it.
"""
from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_UPDATER_OWNS_WRITES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_updater_owns_config_writes", default=False)
_SCHEMA_RE = re.compile(r'^\s*"_config_version":\s*(\d+)\s*,', re.MULTILINE)


class ForeignBuildConfigWriteError(RuntimeError):
    """A non-live code tree tried to write a release-managed home's config."""


@contextmanager
def updater_owns_config_writes() -> Iterator[None]:
    token = _UPDATER_OWNS_WRITES.set(True)
    try:
        yield
    finally:
        _UPDATER_OWNS_WRITES.reset(token)


def in_updater_context() -> bool:
    return _UPDATER_OWNS_WRITES.get()


def _running_code_root() -> Path:
    from hermes_cli.immutable_releases import _LOADED_CODE_ROOT
    return _LOADED_CODE_ROOT


def _running_schema_version() -> int:
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    return int(DEFAULT_CONFIG.get("_config_version", 1))


def _release_schema_version(release: Path) -> Optional[int]:
    """Read a release's config schema without importing code from that release."""
    try:
        text = (release / "hermes_cli" / "config_defaults.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SCHEMA_RE.search(text)
    return int(match.group(1)) if match else None


def _release_root_candidates(home: Path) -> list[Path]:
    # A named profile lives at <root>/profiles/<name>; ``current`` lives at <root>.
    return [home, home.parent.parent] if home.parent.name == "profiles" else [home]


def ensure_release_owns_config_write(config_path: Path) -> None:
    """Raise before writing *config_path* when its home runs a different release."""
    if in_updater_context():
        return
    from hermes_cli.immutable_releases import resolved_release

    home = Path(config_path).expanduser().absolute().parent
    for root in _release_root_candidates(home):
        release = resolved_release(root)
        if release is None:
            continue
        running = Path(_running_code_root()).resolve()
        if running == release:
            return
        live_schema = _release_schema_version(release)
        running_schema = _running_schema_version()
        if live_schema is not None and running_schema <= live_schema:
            return
        live_label = f"schema {live_schema}" if live_schema is not None else "an unreadable schema"
        raise ForeignBuildConfigWriteError(
            f"refusing to write {config_path}: this hermes runs from {running} (config schema "
            f"{running_schema}), but {root} is managed by release {release} ({live_label}). A newer "
            f"schema stamp would block the next `hermes update`. Run the hermes installed in "
            f"{root / 'current'} for this home.")
