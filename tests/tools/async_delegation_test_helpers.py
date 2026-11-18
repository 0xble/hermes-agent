"""Shared helpers for async-delegation tests."""

from __future__ import annotations

import time

import pytest


def wait_for_queued(ad, delegation_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = next((item for item in ad.list_async_delegations()
                     if item["delegation_id"] == delegation_id), None)
        if item is not None and item["status"] == "queued":
            return
        time.sleep(0.01)
    pytest.fail(f"delegation {delegation_id} did not remain queued")
