"""Only the live release (or the updater) may write config into a release-managed home.

A Hermes home on immutable releases runs whatever ``<root>/current`` names. Any other code
tree, such as a dev worktree, an older release, or a source checkout, carries its own config
schema. A config write from one of those trees can stamp ``_config_version`` past the live release
and block the next ``hermes update`` (``config ... is newer than this release``). Every
``hermes_cli.config`` writer passes through ``_write_config_state``, which calls
:func:`ensure_release_owns_config_write` before touching the file.

``hermes update`` legitimately writes from code that is not yet ``current``: the
source-checkout updater, and the post-swap child that migrates config from the staged
candidate before promotion. Those entrypoints run inside :func:`updater_owns_config_writes`.
That is a process-local ContextVar, not an environment variable, so a separate ``hermes
config set`` process never inherits it.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_UPDATER_OWNS_WRITES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_updater_owns_config_writes", default=False)


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
        raise ForeignBuildConfigWriteError(
            f"refusing to write {config_path}: this hermes runs from {running}, but {root} is "
            f"managed by release {release}. Use ~/.hermes/current/.venv/bin/hermes "
            f"(or {root / 'current' / '.venv' / 'bin' / 'hermes'} for this home).")
