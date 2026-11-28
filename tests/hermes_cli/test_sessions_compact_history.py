from argparse import Namespace

from hermes_cli.sessions_cmd import _cmd_compact_history


class _FakeDB:
    def __init__(self):
        self.days = None

    def compacted_history_retention(self, days, **kwargs):
        self.days = days
        return {"deleted": 0, "rows": 0, "archive": str(kwargs["archive_path"])}


def _args(older_than):
    return Namespace(
        older_than=older_than,
        archive=None,
        dry_run=True,
        batch_size=500,
        yes=False,
        force=False,
    )


def test_compact_history_accepts_duration_ages(capsys):
    db = _FakeDB()

    assert _cmd_compact_history(db, _args("14d")) == 0

    assert db.days == 14.0
    assert '"rows": 0' in capsys.readouterr().out


def test_compact_history_rejects_invalid_age(capsys):
    db = _FakeDB()

    assert _cmd_compact_history(db, _args("fourteen days")) == 1

    assert "must be a duration" in capsys.readouterr().out


def test_compact_history_requires_confirmation(monkeypatch, capsys):
    db = _FakeDB()
    args = _args("14d")
    args.dry_run = False
    args.yes = False
    monkeypatch.setattr("hermes_cli.sessions_cmd._confirm_or_exit_code", lambda *a, **k: 1)

    assert _cmd_compact_history(db, args) == 1
    assert db.days is None
    assert capsys.readouterr().out == ""


def test_compact_history_yes_skips_confirmation(monkeypatch):
    db = _FakeDB()
    args = _args("14d")
    args.dry_run = False
    args.yes = True
    monkeypatch.setattr("hermes_cli.sessions_cmd._confirm_or_exit_code", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))

    assert _cmd_compact_history(db, args) == 0
    assert db.days == 14.0
