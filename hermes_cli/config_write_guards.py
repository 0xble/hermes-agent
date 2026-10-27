"""Semantic admission and revision guards for managed config persistence."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any

from hermes_cli.config_errors import ConfigValidationError


class ConfigWriteConflict(ValueError):
    """A read-modify-write used a config revision that is no longer current."""


class ConfigSnapshot(dict):
    """A mapping carrying its source revision out of band, never into YAML."""

    def __init__(self, data: dict, path: Path, revision):
        super().__init__(data)
        self.source_path = str(path.resolve())
        self.source_revision = revision

    def copy(self) -> ConfigSnapshot:
        return ConfigSnapshot(self, Path(self.source_path), self.source_revision)


def revision(path: Path):
    from utils import file_signature

    try:
        return file_signature(path.stat())
    except FileNotFoundError:
        return None


def snapshot(data: dict, path: Path, source_revision) -> ConfigSnapshot:
    return ConfigSnapshot(data, path, source_revision)


def assert_current(path: Path, expected) -> None:
    if revision(path) != expected:
        raise ConfigWriteConflict(f"Config changed since it was read: {path}. Read it again before saving.")


_holders = threading.local()


@contextmanager
def config_write_lock(path: Path, data: Any = None):
    """One lock per resolved config path. Preserve the existing RLock → flock order.

    Snapshots reject stale whole-document rollbacks even when a writer waited for the lock.
    Plain new mappings intentionally replace current state, but still serialize with all writers.
    """
    if path.name != "config.yaml":
        yield None
        return
    from hermes_cli import config
    from hermes_cli.auth import _file_lock
    from hermes_constants import mkdir_under_hermes_home

    path = Path(path).resolve()
    with config._CONFIG_LOCK:
        mkdir_under_hermes_home(path.parent)
        holders = getattr(_holders, "paths", None)
        if holders is None:
            holders = _holders.paths = {}
        holder = holders.setdefault(str(path), threading.local())
        with _file_lock(path.with_name(path.name + ".lock"), holder, 10.0,
                        f"Timed out waiting to write {path}"):
            if holder.depth == 1:
                holder.revision = revision(path)
            if isinstance(data, ConfigSnapshot) and data.source_path == str(path):
                assert_current(path, data.source_revision)
            yield holder.revision
            if isinstance(data, ConfigSnapshot) and data.source_path == str(path):
                # A caller may continue editing the exact object it just saved. Independent
                # copies retain the old revision and must still fail on their next save.
                data.source_revision = revision(path)


def validate_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve the complete disposable candidate through the runtime pipeline, without I/O writes.

    Presets expand before defaults merge, preserving the authored reference's identity. Returning
    the resolved candidate also lets last-good recovery use exactly the same admission checks.
    """
    from hermes_cli import config
    from hermes_cli.model_presets import expand_model_presets
    from hermes_cli.moa_config import apply_user_moa_presets

    if not isinstance(raw, dict):
        raise ConfigValidationError("config.yaml must contain a mapping")
    try:
        expanded_user = config._normalize_max_turns_config(expand_model_presets(copy.deepcopy(dict(raw))))
        merged = config._deep_merge(copy.deepcopy(config.DEFAULT_CONFIG), expanded_user)
        apply_user_moa_presets(merged, expanded_user)
        normalized = config._canonicalize_config(merged)
        expanded, _ = config._merge_managed_overlay(config._expand_env_vars(normalized))
        expanded = expand_model_presets(expanded)
        errors = [i.message for i in config.validate_config_structure(expanded) if i.severity == "error"]
        if expanded.get("mcp_servers") is not None and not isinstance(expanded["mcp_servers"], dict):
            errors.append("mcp_servers must be a mapping")
    except ConfigValidationError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigValidationError(str(exc)) from exc
    if errors:
        raise ConfigValidationError("; ".join(errors))
    return expanded


@contextmanager
def atomic_write_guard(path: Path):
    """Final serialized-byte admission and CAS immediately before atomic replacement."""
    if path.name != "config.yaml":
        yield lambda _: None
        return
    with config_write_lock(path) as expected:

        def admit(temp_path: Path) -> None:
            from utils import fast_safe_load

            with temp_path.open(encoding="utf-8-sig") as stream:
                candidate = fast_safe_load(stream)
            validate_candidate({} if candidate is None else candidate)
            assert_current(path, expected)

        yield admit
