"""Compatibility exports for the moved prune-pacing helpers."""
from persistence.sqlite.admin.prune_pacing import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite.admin.prune_pacing"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

