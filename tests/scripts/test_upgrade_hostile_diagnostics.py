"""Hostile E2E watchdogs report owned files without querying a stuck child."""
from tests.e2e.core.upgrade.git import _hostile_diagnostics as D


def test_watchdog_reports_phase_and_only_the_bounded_owned_log_tail(tmp_path, capsys):
    log = tmp_path / "update.log"
    log.write_text("old bytes not in the tail\n" + "x" * 5000 + "\nnew completion progress\n", encoding="utf-8")
    D.phase("namespace CLI start", log)
    D.snapshot()
    out = capsys.readouterr().err
    assert "namespace CLI start" in out
    assert ("bwrap" if D.H.BWRAP_OK else "no-bwrap CI fallback") in out
    assert "new completion progress" in out
    assert "old bytes not in the tail" not in out
    assert len(out) < 5000


def test_watchdog_reports_an_unavailable_owned_log_without_masking_other_logs(tmp_path, capsys):
    absent = tmp_path / "ns-agent.stderr"
    receipt = tmp_path / "latest.json"
    receipt.write_text('{"outcome": "running"}\n', encoding="utf-8")
    D.phase("module install start", absent, receipt)
    D.snapshot()
    out = capsys.readouterr().err
    assert str(absent) in out and "FileNotFoundError" in out
    assert '"outcome": "running"' in out


def test_diagnostics_fixture_uses_original_stderr_and_closes_its_watchdog(request):
    # Exercise the real fixture lifetime without waiting for its 180s watchdog.
    diagnostics = getattr(D.hostile_diagnostics, "__wrapped__")(request)
    next(diagnostics)
    stream = D._STREAM
    try:
        assert stream is not None and not stream.closed
        D.phase("diagnostics fixture smoke")
    finally:
        diagnostics.close()
    assert stream.closed
    assert D._STREAM is None
