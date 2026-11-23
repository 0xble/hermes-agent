"""Compatibility imports for the moved legacy-local migration module."""

from persistence.sqlite.authority._local_migration import bind_legacy_target
from persistence.sqlite.conversation._store.local_migration import LEGACY_PREFIX, legacy_lineage_root
from session_rules.legacy_local import validate_legacy_row

__all__ = ["LEGACY_PREFIX", "bind_legacy_target", "legacy_lineage_root", "validate_legacy_row"]
