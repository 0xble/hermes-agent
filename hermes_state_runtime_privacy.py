"""Compatibility exports for the moved authority privacy helpers."""
from persistence.sqlite.authority.runtime_privacy import *  # noqa: F401,F403
from importlib import import_module as _import_module
_TARGET = "persistence.sqlite.authority.runtime_privacy"
def __getattr__(name):
    return getattr(_import_module(_TARGET), name)

