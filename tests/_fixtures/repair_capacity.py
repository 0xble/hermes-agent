"""Opt-in capacity observations for functional repair fixtures, including real children."""

from contextlib import contextmanager
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _ample_disk_usage(_path):
    # Observe capacity only. Real backup, SQLite, scratch validation and refusal
    # guards still execute. Nested low-space tests replace this observation.
    size = 1024 ** 4
    return SimpleNamespace(total=size, used=0, free=size)


@contextmanager
def ample_repair_capacity():
    """Use the same observation in a real repair subprocess."""
    with patch.object(shutil, "disk_usage", _ample_disk_usage):
        yield


@pytest.fixture
def adequate_repair_capacity(monkeypatch):
    """Avoid making functional repairs depend on the host volume's free space."""
    # Use the native undo stack so nested monkeypatches cannot restore the
    # synthetic observation after a separately-owned context has ended.
    monkeypatch.setattr(shutil, "disk_usage", _ample_disk_usage)
