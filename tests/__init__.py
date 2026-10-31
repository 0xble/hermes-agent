"""Hermes test package.

``tests/conftest.py`` sandboxes ``HERMES_HOME`` and arms the live state.db guard, but only for a
pytest run. A direct ``python -`` probe that imports a test helper (``from tests.gateway.<module>
import <fixture helper>``) gets neither: no conftest runs and it has no pytest ancestor, so the
helper's ``SessionDB()`` opens the production store of whatever ``HERMES_HOME`` the shell exported.
A review probe driving a gateway fixture that way began a runtime epoch on the live gateway's
state.db and stalled every turn until a restart.

Importing this package therefore exports the guard's own isolation marker. A probe on a temporary
``HERMES_HOME`` runs as before; one that resolves a production store fails at its first
``SessionDB()`` instead of writing to it. Under pytest the conftest replaces the value with its
sandbox home.
"""
import os

os.environ.setdefault("HERMES_TEST_ISOLATION", os.environ.get("HERMES_HOME", "") or "1")
