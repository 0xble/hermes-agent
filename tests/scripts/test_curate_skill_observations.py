from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "curate_skill_observations.py"


def load_module():
    spec = importlib.util.spec_from_file_location("curate_skill_observations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_index_preserves_markdown_and_records_stable_hashes(tmp_path):
    module = load_module()
    observations = tmp_path / "observations"
    observations.mkdir()
    payload = "# Improve deploy\n\nKeep the rollback step.\n"
    source = observations / "deploy.md"
    source.write_text(payload, encoding="utf-8")
    db = tmp_path / "state" / "skill-observations.sqlite3"

    result = module.index_observations(observations, db)
    again = module.index_observations(observations, db)

    assert result.imported == 1
    assert again.imported == 0
    assert again.existing == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT id, skill, payload, content_hash, file_hash, disposition FROM observations"
        ).fetchone()
    assert row[0] == module.observation_id("deploy", payload)
    assert row[1:] == (
        "deploy",
        payload,
        module.sha256_text(payload),
        module.sha256_bytes(payload.encode("utf-8")),
        "pending",
    )


def test_disposition_is_explicit_and_archive_keeps_payload(tmp_path):
    module = load_module()
    observations = tmp_path / "observations"
    observations.mkdir()
    payload = "---\nprovenance: session-42\n---\n\nA markdown observation with provenance.\n"
    source = observations / "review.md"
    source.write_text(payload, encoding="utf-8")
    db = tmp_path / "observations.sqlite3"
    module.index_observations(observations, db)
    record_id = module.observation_id("review", payload)

    assert module.set_disposition(db, record_id, "accepted", reason="verified")
    archive_result = module.archive_dispositioned(observations, db, record_id)

    assert archive_result.archived == 1
    assert not source.exists()
    archived = next((observations / "archive").rglob("*.md"))
    assert archived.read_text(encoding="utf-8") == payload
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT disposition, disposition_reason, provenance, archive_path, archived_at FROM observations WHERE id = ?",
            (record_id,),
        ).fetchone()
    assert row[0:3] == ("accepted", "verified", "session-42")
    assert Path(row[3]) == archived
    assert row[4]


def test_pending_observation_cannot_be_archived(tmp_path):
    module = load_module()
    observations = tmp_path / "observations"
    observations.mkdir()
    (observations / "review.md").write_text("Keep the evidence.\n", encoding="utf-8")
    db = tmp_path / "observations.sqlite3"
    module.index_observations(observations, db)

    result = module.archive_dispositioned(observations, db)

    assert result.archived == 0
    assert result.pending == 1
    assert (observations / "review.md").exists()


def test_cli_has_no_legacy_publish_fallback(tmp_path, capsys):
    module = load_module()

    with pytest.raises(SystemExit):
        module.main(["--observations", str(tmp_path), "--publish"])
    assert "following arguments are required: command" in capsys.readouterr().err


def test_per_observation_files_disposition_and_archive_independently(tmp_path):
    """Writers add one ``<skill>@<suffix>.md`` file per observation; a newer
    observation for the same skill must not block archiving an older one."""
    module = load_module()
    observations = tmp_path / "observations"
    observations.mkdir()
    first = observations / "deploy@20260924T010000Z-aaaa.md"
    first.write_text("Keep the rollback step.\n", encoding="utf-8")
    db = tmp_path / "observations.sqlite3"
    module.index_observations(observations, db)
    first_id = module.observation_id("deploy", "Keep the rollback step.\n")

    second = observations / "deploy@20260924T020000Z-bbbb.md"
    second.write_text("Verify the health check.\n", encoding="utf-8")
    assert module.index_observations(observations, db).imported == 1
    assert module.set_disposition(db, first_id, "rejected", reason="duplicate")

    assert module.archive_dispositioned(observations, db).archived == 1
    assert not first.exists()
    assert second.exists()
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT skill, disposition FROM observations ORDER BY source_path").fetchall()
    assert rows == [("deploy", "rejected"), ("deploy", "pending")]
