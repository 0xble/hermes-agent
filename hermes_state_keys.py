"""Compatibility exports for the moved SQLite state vocabulary."""
from persistence.sqlite._state.keys import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite._state.keys"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

