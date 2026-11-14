"""Non-ACP inventory readers retain models.dev cold discovery and stale refresh."""

import time

import pytest

from agent import models_dev as md
from hermes_cli import model_switch_providers as msp


@pytest.mark.parametrize("cache_state", ["cold", "stale"])
def test_nonblocking_provider_catalogs_keep_registry_discovery(monkeypatch, cache_state):
    registry = {"deepseek": {"name": "Registry name", "env": ["DEEPSEEK_API_KEY"]}}
    monkeypatch.setattr(md, "_models_dev_cache", registry if cache_state == "stale" else {})
    monkeypatch.setattr(md, "_models_dev_cache_time", time.time() - md._MODELS_DEV_CACHE_TTL - 1)
    monkeypatch.setattr(md, "_models_dev_retry_after", 0)
    monkeypatch.setattr(md, "_load_disk_cache", lambda: {})
    monkeypatch.setattr(md, "_disk_cache_age_seconds", lambda: None)
    fetched = []
    refreshed = []
    monkeypatch.setattr(md, "get_provider_info", lambda *a, **kw: None)
    monkeypatch.setattr(md, "_refresh_locked", lambda where: fetched.append(where) or registry)
    monkeypatch.setattr(md, "_start_background_refresh_models_dev", lambda: refreshed.append(True))
    monkeypatch.setattr(msp, "_build_curated_lists", lambda *a, **kw: {"deepseek": ["curated"]})
    monkeypatch.setattr(msp, "_live_or_curated_ids", lambda *a, **kw: ["curated"])
    monkeypatch.setattr(msp, "_lap_overlay_rows", lambda *a: None)
    monkeypatch.setattr(msp, "_lap_canonical_rows", lambda *a: None)
    monkeypatch.setattr(msp, "_lap_lmstudio_row", lambda *a: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")

    rows = msp.list_authenticated_providers(non_blocking_catalogs=True)

    if cache_state == "cold":
        assert fetched == ["foreground"], "non-ACP cold registry discovery was disabled"
    else:
        assert refreshed, "non-ACP stale registry no longer schedules its SWR refresh"
    assert any(row["slug"] == "deepseek" for row in rows)
