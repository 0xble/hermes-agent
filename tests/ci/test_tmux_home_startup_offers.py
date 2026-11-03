"""Scripted CLI homes must not depend on the host's free disk at resume."""
from unittest.mock import Mock

import pytest

from hermes_cli import prune_advisor
from tests.e2e.core.cli_tmux._classic import ClassicHome
from tests.fakes.fake_llm_provider import write_hermes_home


@pytest.mark.parametrize("classic", [True, False], ids=["classic-parity-and-live", "resize-scrollback"])
def test_scripted_home_does_not_ask_to_prune_on_a_low_disk(tmp_path, monkeypatch, classic):
    if classic:
        home = ClassicHome(tmp_path / "classic").write("http://127.0.0.1:9").hermes_home
    else:
        home = write_hermes_home(tmp_path / "resize", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    # A prior launch has populated the advice cache. On a low disk the next
    # launch would ask synchronously, before resume recap or the composer.
    snap = prune_advisor.DiskSnapshot(1024 * 1024, 6 * prune_advisor.GIB, 72 * prune_advisor.GIB)
    monkeypatch.setattr(prune_advisor, "disk_snapshot", lambda home=None: snap)
    prune_advisor._store_estimate(home, advice={
        "retention_days": 90, "prunable": 0, "total": 1,
        "db_bytes": snap.db_bytes, "reclaim_bytes": 0,
    })
    ask = Mock(return_value="")
    output = Mock()

    assert prune_advisor.offer_interactive(cprint=output, ask=ask) is None

    ask.assert_not_called()
    output.assert_not_called()
