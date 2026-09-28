"""Contract tests for opt-in generation isolation primitives."""
from __future__ import annotations

import json
from pathlib import Path

from gateway.generation import (
    GenerationCoordinator,
    GenerationIdentity,
    generation_paths,
    overlap_handover_enabled,
    remove_generation_files,
    write_generation_record,
)


def test_coordinator_registers_heartbeats_and_exposes_leased_generations(tmp_path: Path):
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="abc123", label="ai.hermes.gateway-a", pid=123)

    coordinator.register(identity)
    epoch = coordinator.acquire_lease("telegram:token", identity.id)
    coordinator.heartbeat(identity.id, state="ready")

    rows = coordinator.generations()
    assert rows[0]["id"] == identity.id
    assert rows[0]["state"] == "ready"
    assert coordinator.leases() == [{
        "resource": "telegram:token", "epoch": epoch,
        "generation_id": identity.id, "state": "active",
    }]


def test_generation_files_are_scoped_and_cleanup_is_fenced(tmp_path: Path):
    identity = GenerationIdentity.create(release_sha="abc", label="ai.hermes.gateway-b", pid=456,
                                         start_fingerprint="456:one")
    paths = generation_paths(tmp_path, identity)
    write_generation_record(paths["state"], identity, state="standby")
    assert json.loads(paths["state"].read_text())["id"] == identity.id

    other = GenerationIdentity.create(release_sha="def", label="ai.hermes.gateway-a", pid=456,
                                      start_fingerprint="456:two")
    other_paths = generation_paths(tmp_path, other)
    write_generation_record(other_paths["state"], other)
    remove_generation_files(tmp_path, identity)
    assert not paths["state"].exists()
    assert other_paths["state"].exists()


def test_overlap_gate_defaults_off_and_reads_nested_config(tmp_path, monkeypatch):
    from gateway.config import load_gateway_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not load_gateway_config().overlap_handover_enabled
    assert not overlap_handover_enabled({})
    (tmp_path / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
    assert load_gateway_config().overlap_handover_enabled
    assert overlap_handover_enabled({"gateway": {"overlap_handover": {"enabled": True}}})
    assert not overlap_handover_enabled(object())
