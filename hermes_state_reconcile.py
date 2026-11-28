"""Compatibility exports for the moved SQLite reconciliation helpers."""
from persistence.sqlite.admin._reconcile import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite.admin._reconcile"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

