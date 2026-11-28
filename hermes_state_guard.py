"""Compatibility exports for the moved SQLite guard helpers."""
from persistence.sqlite._state.guard import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite._state.guard"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

