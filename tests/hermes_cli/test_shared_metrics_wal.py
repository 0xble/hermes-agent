"""Shared-metrics journal-mode and connection-lifetime contracts."""

from __future__ import annotations

import gc
import sqlite3
import warnings

import pytest

from hermes_cli.observability import shared_metrics as shared_metrics_module
from hermes_cli.observability.shared_metrics import SharedMetricsStore


def _journal_mode(path):
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


@pytest.mark.requires_wal
def test_new_and_delete_mode_stores_end_in_wal(tmp_path):
    database_path = tmp_path / "metrics.sqlite3"
    outbox_directory = tmp_path / "outbox"

    SharedMetricsStore(database_path, outbox_directory)
    assert _journal_mode(database_path) == "wal"

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    finally:
        connection.close()
    assert _journal_mode(database_path) == "delete"

    SharedMetricsStore(database_path, outbox_directory)
    assert _journal_mode(database_path) == "wal"


def test_building_store_closes_every_connection(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        store = SharedMetricsStore(tmp_path / "metrics.sqlite3", tmp_path / "outbox")
        del store
        gc.collect()

    assert not [warning for warning in caught if issubclass(warning.category, ResourceWarning)]
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_reader_lock_keeps_store_usable_and_retries_wal_later(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("hermes_state_wal.is_sqlite_wal_reset_vulnerable", lambda **_: False)
    database_path = tmp_path / "metrics.sqlite3"
    outbox_directory = tmp_path / "outbox"
    SharedMetricsStore(database_path, outbox_directory)
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    finally:
        connection.close()

    reader = sqlite3.connect(database_path, timeout=0.05)
    real_open_db = shared_metrics_module.open_db

    def open_db_with_reader(path, **kwargs):
        if kwargs.get("wal"):
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM telemetry_state").fetchall()
            kwargs["busy_timeout_ms"] = 50
            kwargs["wal_lock_retries"] = 1
        return real_open_db(path, **kwargs)

    monkeypatch.setattr(shared_metrics_module, "open_db", open_db_with_reader)
    try:
        store = SharedMetricsStore(database_path, outbox_directory)
        assert store.counter_snapshot() == []
        assert _journal_mode(database_path) == "delete"
    finally:
        reader.rollback()
        reader.close()

    monkeypatch.setattr(shared_metrics_module, "open_db", real_open_db)
    SharedMetricsStore(database_path, outbox_directory)
    assert _journal_mode(database_path) == "wal"
