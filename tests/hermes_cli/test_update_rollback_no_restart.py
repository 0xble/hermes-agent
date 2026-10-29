"""Rollback must not silently restart a fleet when explicitly forbidden."""

import argparse
from pathlib import Path

import pytest

from hermes_cli import update_cmd, update_receipt
from hermes_cli.subcommands.update import build_update_parser


def test_rollback_no_gateway_restart_refused_before_state_change(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    args = parser.parse_args(["update", "--rollback", "--no-gateway-restart"])
    assert args.rollback and args.no_gateway_restart

    home = tmp_path / "home"
    home.mkdir()
    releases = home / "releases"
    previous = releases / "old"
    current = releases / "new"
    previous.mkdir(parents=True)
    current.mkdir()
    (home / "current").symlink_to(current)
    (home / "previous").symlink_to(previous)
    journal = home / "release-layout.json"
    journal.write_text('{"source":"/unrelated/source"}', encoding="utf-8")
    before = {path.name: path.read_bytes() for path in (journal,)}
    before_links = [(home / name).readlink() for name in ("current", "previous")]
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update",
                        lambda *_args, **_kwargs: pytest.fail("fleet restarted"))
    with update_receipt.update_receipt_scope():
        with pytest.raises((SystemExit, ValueError, RuntimeError), match="rollback.*no-gateway-restart"):
            update_cmd._cmd_update_impl(args, gateway_mode=False)
    assert [(home / name).readlink() for name in ("current", "previous")] == before_links
    assert journal.read_bytes() == before[journal.name]
    assert not (home / "logs").exists()
