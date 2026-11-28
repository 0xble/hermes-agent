"""Compatibility exports for the moved SQLite publication helpers."""
from persistence.sqlite.admin.publication import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite.admin.publication"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

