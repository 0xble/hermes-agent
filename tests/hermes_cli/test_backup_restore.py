"""Tests for hermes backup and import commands."""

import os
import sqlite3
import stat
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from tests.hermes_cli.backup_support import (
    _break_member,
    _count_rows,
    _no_real_gateway_service as _no_real_gateway_service,
    _restore_backup_datetime as _restore_backup_datetime,
    _write_session_db,
)


class TestImportAtomicWrites:
    """`hermes import` must never leave a user's file truncated.

    The pre-fix code did ``open(target, "wb")`` then ``dst.write(src.read())``,
    which zeroes the existing file *before* any replacement bytes exist. These
    tests pin the invariant for both restore branches: the HERMES_HOME branch
    and the ``_external/`` branch that writes into third-party configs under
    the user's home.
    """

    def _zip(self, zip_path: Path, files: dict) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)

    def test_failed_member_leaves_existing_file_intact(self, tmp_path, monkeypatch):
        """A dying member must not destroy the file it was replacing."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        original = "model: original\napi_key: keep-me\n"
        (hermes_home / "config.yaml").write_text(original)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {"config.yaml": "model: replacement\n", "state.db": ""})
        _break_member(monkeypatch, "config.yaml")

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        # Pre-fix this file is 0 bytes: the truncate landed, the write did not.
        assert (hermes_home / "config.yaml").read_text() == original
        # And the aborted write must not litter the directory it staged in.
        assert list(hermes_home.glob(".config.yaml.*")) == []

    def test_failed_external_member_leaves_existing_file_intact(self, tmp_path, monkeypatch):
        """Same invariant on the `_external/` branch, which writes outside HERMES_HOME."""
        dst_home = tmp_path / "dst"
        dst_home.mkdir()
        hermes_home = dst_home / ".hermes"
        hermes_home.mkdir()
        honcho = dst_home / ".honcho"
        honcho.mkdir()
        original = '{"peer":"original"}'
        (honcho / "config.json").write_text(original)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {
            "config.yaml": "model: {}\n",
            "_external/.honcho/config.json": '{"peer":"replacement"}',
        })

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: dst_home)
        _break_member(monkeypatch, "_external/.honcho/config.json")

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert (honcho / "config.json").read_text() == original
        assert list(honcho.glob(".config.json.*")) == []

    @pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
    def test_symlinked_target_keeps_its_symlink(self, tmp_path, monkeypatch):
        """A symlinked target is written through, not replaced by a regular file.

        Guards the atomic rewrite against a naive ``os.replace``, which would
        detach dotfiles-managed deployments (GitHub #16743). ``atomic_replace``
        resolves the link first.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        store = hermes_home / "store"
        store.mkdir()
        real = store / "config.yaml"
        real.write_text("model: original\n")
        link = hermes_home / "config.yaml"
        link.symlink_to(real)

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {"config.yaml": "model: restored\n", "state.db": ""})

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert link.is_symlink(), "import replaced the symlink with a regular file"
        assert real.read_text() == "model: restored\n"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
    def test_symlinked_external_target_keeps_its_symlink(self, tmp_path, monkeypatch):
        """Same guard on the `_external/` branch — the realistic dotfiles case."""
        dst_home = tmp_path / "dst"
        dst_home.mkdir()
        hermes_home = dst_home / ".hermes"
        hermes_home.mkdir()
        dotfiles = dst_home / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "honcho.json"
        real.write_text('{"peer":"original"}')
        honcho = dst_home / ".honcho"
        honcho.mkdir()
        link = honcho / "config.json"
        link.symlink_to(real)

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: dst_home)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {
            "config.yaml": "model: {}\n",
            "_external/.honcho/config.json": '{"peer":"restored"}',
        })

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert link.is_symlink(), "import replaced the symlink with a regular file"
        assert real.read_text() == '{"peer":"restored"}'

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_restore_preserves_existing_file_mode(self, tmp_path, monkeypatch):
        """Staging through mkstemp must not silently tighten restored files to 0600.

        ``tempfile.mkstemp`` creates at 0600; the mode of the file being
        replaced has to survive the publish, or Docker/NAS installs that rely
        on broader permissions break on restore.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        target = hermes_home / "config.yaml"
        target.write_text("model: original\n")
        os.chmod(target, 0o644)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {"config.yaml": "model: restored\n", "state.db": ""})

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert target.read_text() == "model: restored\n"
        assert (target.stat().st_mode & 0o777) == 0o644

    @pytest.mark.platforms("posix")
    def test_restore_preserves_existing_file_owner(self, tmp_path, monkeypatch):
        """A root-run import must not re-own the user's files to root.

        ``os.replace`` swaps in a temp file owned by the *writing* user, so a
        ``sudo hermes import`` onto a user-owned (or Docker/NAS volume-owned)
        HERMES_HOME would hand every restored file to root. The uid/gid is
        forced so the assertion does not require running as root.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        target = hermes_home / "config.yaml"
        target.write_text("model: original\n")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {"config.yaml": "model: restored\n", "state.db": ""})

        chown_calls: list[tuple[Path, int, int]] = []
        monkeypatch.setattr(
            "hermes_cli.backup_restore._preserve_file_owner",
            lambda p: (123, 456) if Path(p).exists() else None,
        )
        monkeypatch.setattr(
            "utils.os.chown",
            lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
        )

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert target.read_text() == "model: restored\n"
        # config.yaml pre-existed, so its owner is captured and re-applied;
        # state.db is newly created, so there is no prior owner to restore.
        assert chown_calls == [(target, 123, 456)]

    def test_mode_is_applied_before_the_replace(self, tmp_path, monkeypatch):
        """Publish the correct native permission bits with no permissive window."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        target = hermes_home / "config.yaml"
        target.write_text("model: original\n")
        os.chmod(target, 0o644)
        expected_mode = target.stat().st_mode & 0o777
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(zip_path, {"config.yaml": "model: restored\n", "state.db": ""})

        import hermes_cli.backup_restore as backup_mod

        real_replace = backup_mod.atomic_replace
        staged_modes: list[int] = []

        def spying_replace(tmp, dst):
            if Path(dst).name == "config.yaml":
                staged_modes.append(os.stat(tmp).st_mode & 0o777)
            return real_replace(tmp, dst)

        monkeypatch.setattr(backup_mod, "atomic_replace", spying_replace)

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        assert staged_modes == [expected_mode]
        assert (target.stat().st_mode & 0o777) == expected_mode

    @pytest.mark.platforms("posix")
    def test_restore_does_not_carry_setuid_onto_archive_content(
        self, tmp_path, monkeypatch
    ):
        """An imported member must not inherit a privileged target's identity.

        ``_preserve_file_mode`` returns ``stat.S_IMODE``, i.e. all twelve bits,
        so a target sitting at 0o6755 hands setuid/setgid straight back to a
        file whose contents now come from the zip.  Whoever produced the
        archive would then get whatever that file executes as.  The other
        ``utils`` writers can preserve the full mode safely because they
        re-serialize content this process produced; ``hermes import`` is the
        one write path where the bytes are untrusted, and it is also the path
        that documents ``sudo`` use for owner preservation.

        The sibling assertions in this class mask with ``& 0o777``, which
        discards exactly the bits at issue, so this failure is invisible to
        them.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        target = hermes_home / "helper.sh"
        target.write_text("#!/bin/sh\necho original\n")
        os.chmod(target, 0o6755)
        if stat.S_IMODE(target.stat().st_mode) != 0o6755:
            pytest.skip("filesystem refuses setuid/setgid on a user-owned file")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        zip_path = tmp_path / "backup.zip"
        self._zip(
            zip_path,
            {"helper.sh": "#!/bin/sh\necho attacker\n", "state.db": ""},
        )

        import hermes_cli.backup_restore as backup_mod

        real_replace = backup_mod.atomic_replace
        staged_modes: list[int] = []

        def spying_replace(tmp, dst):
            if Path(dst).name == "helper.sh":
                staged_modes.append(stat.S_IMODE(os.stat(tmp).st_mode))
            return real_replace(tmp, dst)

        monkeypatch.setattr(backup_mod, "atomic_replace", spying_replace)

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        published = stat.S_IMODE(target.stat().st_mode)
        assert target.read_text() == "#!/bin/sh\necho attacker\n"
        assert not published & stat.S_ISUID, (
            f"archive content kept the target's setuid bit (mode 0o{published:o})"
        )
        assert not published & stat.S_ISGID, (
            f"archive content kept the target's setgid bit (mode 0o{published:o})"
        )
        # The ordinary permission bits are still preserved — this drops the
        # elevated bits, it does not fall back to mkstemp's 0600.
        assert published == 0o755
        # And there must be no transient elevation either: the temp file is
        # chmod'd before the replace, so it must never carry the bits.
        assert staged_modes == [0o755], (
            f"the staged temp file was elevated before publish: {staged_modes}"
        )

class TestProfileRestoration:
    def _make_backup_zip(self, zip_path: Path, files: dict[str, str | bytes]) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)


    def test_import_skips_profile_dirs_without_config(self, tmp_path, monkeypatch):
        """Import doesn't create wrappers for profile dirs without config."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        wrapper_dir = tmp_path / ".local" / "bin"
        wrapper_dir.mkdir(parents=True)

        zip_path = tmp_path / "backup.zip"
        self._make_backup_zip(zip_path, {
            "config.yaml": "model: test\n",
            "profiles/valid/config.yaml": "model: test\n",
            "profiles/empty/readme.txt": "nothing here\n",
        })

        args = Namespace(zipfile=str(zip_path), force=True)

        from hermes_cli.backup import run_import
        run_import(args)

        # Only valid profile should get a wrapper
        assert (wrapper_dir / "valid").exists()
        assert not (wrapper_dir / "empty").exists()

class TestMemoryProviderExternalPaths:
    def _make_min_tree(self, hermes_home: Path) -> None:
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        (hermes_home / ".env").write_text("OPENROUTER_API_KEY=sk-test\n")
        (hermes_home / "state.db").write_bytes(b"x")


    def test_backup_skips_external_paths_outside_home(self, tmp_path, monkeypatch):
        """A declared path outside the home dir is not portable and must be
        skipped, never archived."""
        hermes_home = tmp_path / ".hermes"
        self._make_min_tree(hermes_home)
        outside = tmp_path.parent / "outside-home-secret"
        outside.mkdir(exist_ok=True)
        (outside / "leak.json").write_text('{"secret":1}')

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import hermes_cli.backup as backup_mod
        monkeypatch.setattr(
            backup_mod, "_collect_memory_provider_external_paths", lambda: [outside]
        )

        out_zip = tmp_path / "backup.zip"
        backup_mod.run_backup(Namespace(output=str(out_zip)))

        with zipfile.ZipFile(out_zip) as zf:
            names = set(zf.namelist())
        assert not any(n.startswith("_external/") for n in names)
        assert not any("leak.json" in n for n in names)
        (outside / "leak.json").unlink()
        outside.rmdir()

    def test_import_restores_external_to_home_relative_location(self, tmp_path, monkeypatch):
        """_external/ members restore to ~/<relpath>, not under HERMES_HOME,
        and credential-shaped files get 0600."""
        dst_home = tmp_path / "dst"
        dst_home.mkdir()
        hermes_home = dst_home / ".hermes"
        hermes_home.mkdir()

        zip_path = tmp_path / "backup.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("config.yaml", "model: {}\n")
            zf.writestr(".env", "X=1\n")
            zf.writestr("state.db", "")
            zf.writestr("_external/.honcho/config.json", '{"peer":"bob"}')

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: dst_home)

        from hermes_cli.backup import run_import
        run_import(Namespace(zipfile=str(zip_path), force=True))

        restored = dst_home / ".honcho" / "config.json"
        assert restored.exists()
        assert restored.read_text() == '{"peer":"bob"}'
        # Credential-shaped file tightened.
        assert (restored.stat().st_mode & 0o777) == 0o600
        # External state did NOT leak into HERMES_HOME.
        assert not (hermes_home / "_external").exists()

class TestImportHonorsHermesHomeOverride:
    """`hermes import` must restore into the home the command runs under.

    Resolving the target through get_default_hermes_root() maps a profile
    home (<root>/profiles/<name>) back to <root>: the import then overwrites
    the live root's config.yaml while the profile directory stays empty —
    exactly what "Target:" printed it would NOT do.
    """

    def _make_backup_zip(self, tmp_path):
        import zipfile

        src_root = tmp_path / "src-home"
        src_root.mkdir()
        (src_root / "config.yaml").write_text("model:\n  provider: anthropic\n")
        (src_root / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n")
        zip_path = tmp_path / "backup.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(src_root / "config.yaml", "config.yaml")
            zf.write(src_root / ".env", ".env")
        return zip_path

    def test_import_targets_named_profile_home(self, tmp_path, monkeypatch):
        """HERMES_HOME=<root>/profiles/<name> must restore INTO the profile,
        not into <root> (which would clobber the live root config)."""
        root = tmp_path / "hermes-root"
        profile = root / "profiles" / "coder"
        profile.mkdir(parents=True)
        # Live root config that must survive untouched.
        (root / "config.yaml").write_text("model:\n  provider: openai\n")

        monkeypatch.setenv("HERMES_HOME", str(profile))
        from hermes_constants import get_hermes_home

        assert get_hermes_home() == profile

        zip_path = self._make_backup_zip(tmp_path)

        import argparse

        from hermes_cli.backup import run_import

        args = argparse.Namespace(zipfile=str(zip_path), force=True)
        run_import(args)

        assert (profile / "config.yaml").read_text() == (
            "model:\n  provider: anthropic\n"
        )
        assert (root / "config.yaml").read_text() == "model:\n  provider: openai\n"

    def test_import_skips_gateway_install_for_non_default_home(
        self, tmp_path, monkeypatch
    ):
        """A restore into a sandbox must not silently start a second gateway
        pointed at it — the profile/sandbox gateway would shadow the default
        service installed by the primary install."""
        native_default = tmp_path / "native-default"
        sandbox = tmp_path / "sandbox-home"
        sandbox.mkdir(parents=True)
        # Live default install markers.
        native_default.mkdir()
        (native_default / "config.yaml").write_text("model:\n  provider: openai\n")

        monkeypatch.setenv("HERMES_HOME", str(sandbox))

        import argparse

        from hermes_cli import backup as backup_mod

        monkeypatch.setattr(
            backup_mod,
            "_get_platform_default_hermes_home",
            lambda: native_default,
        )

        calls = []
        monkeypatch.setattr(
            "hermes_cli.gateway_setup_service.ensure_gateway_service",
            lambda *a, **kw: calls.append(kw),
        )
        monkeypatch.setattr(
            "hermes_cli.gateway._is_service_running",
            lambda: False,
        )

        zip_path = self._make_backup_zip(tmp_path)
        args = argparse.Namespace(zipfile=str(zip_path), force=True)
        backup_mod.run_import(args)

        assert calls == [], "gateway must not be auto-installed for a sandbox restore"

    def test_import_installs_gateway_when_default_home_is_target(
        self, tmp_path, monkeypatch
    ):
        """Restoring into the default home keeps the auto-install behavior."""
        native_default = tmp_path / "native-default"
        native_default.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(native_default))

        import argparse

        from hermes_cli import backup as backup_mod

        monkeypatch.setattr(
            backup_mod,
            "_get_platform_default_hermes_home",
            lambda: native_default,
        )

        calls = []
        monkeypatch.setattr(
            "hermes_cli.gateway_setup_service.ensure_gateway_service",
            lambda *a, **kw: calls.append(kw),
        )
        monkeypatch.setattr(
            "hermes_cli.gateway._is_service_running",
            lambda: False,
        )

        zip_path = self._make_backup_zip(tmp_path)
        args = argparse.Namespace(zipfile=str(zip_path), force=True)
        backup_mod.run_import(args)

        assert calls and calls[0].get("context") == "import"

class TestImportLiveSessionDatabase:
    """`hermes import` must not swap the inode of a database Hermes holds open.

    Publishing state.db with a rename leaves any live gateway/dashboard/WebUI
    connection reading and writing the unlinked inode, so its sessions vanish
    from the database everyone else opens and nothing is logged (#100960).
    """

    def _zip_with_db(self, zip_path: Path, db_path: Path) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(db_path, "state.db")

    def _prepare(self, tmp_path, monkeypatch, live=(3, 4), backup=(2, 2)):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        live_db = home / "state.db"
        _write_session_db(live_db, *live)

        staged = tmp_path / "backup-state.db"
        _write_session_db(staged, *backup)
        zip_path = tmp_path / "backup.zip"
        self._zip_with_db(zip_path, staged)
        return home, live_db, zip_path

    def test_live_holder_sees_imported_rows(self, tmp_path, monkeypatch):
        """A connection open across the import converges on the imported data."""
        from hermes_cli.backup import run_import

        _home, live_db, zip_path = self._prepare(tmp_path, monkeypatch)

        holder = sqlite3.connect(str(live_db))
        # Read first so the connection has cached pages of the pre-import file.
        assert holder.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12
        inode_before = os.stat(live_db).st_ino

        try:
            run_import(Namespace(zipfile=str(zip_path), force=True))
            assert holder.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        finally:
            holder.close()

        assert os.stat(live_db).st_ino == inode_before
        assert _count_rows(live_db) == (2, 4)

    def test_refused_restore_is_reported_and_leaves_db_intact(
        self, tmp_path, monkeypatch, capsys
    ):
        """A refused live-safe restore is a warning, not a counted success."""
        import hermes_cli.backup as backup_mod
        # _import_db_member (the run_import .db publish path) lives in
        # hermes_cli.backup_restore and resolves _safe_restore_db there.
        import hermes_cli.backup_restore as backup_restore_mod

        _home, live_db, zip_path = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(backup_restore_mod, "_safe_restore_db", lambda src, dst: False)

        assert backup_mod.run_import(Namespace(zipfile=str(zip_path), force=True)) == 1

        out = capsys.readouterr().out
        assert "files skipped" in out
        assert "state.db" in out
        assert "has been restored" not in out

        # The pre-import database is still the one on disk.
        assert _count_rows(live_db) == (3, 12)

    def test_sidecar_members_are_not_installed_beside_a_restored_db(
        self, tmp_path, monkeypatch
    ):
        """A `state.db-wal` member from an old/hand-built archive must not be
        os.replace'd next to the page-restored database: it describes a
        different image and SQLite would replay it on the next open."""
        from hermes_cli.backup import run_import

        _home, live_db, zip_path = self._prepare(tmp_path, monkeypatch)
        with zipfile.ZipFile(zip_path, "a") as zf:
            zf.writestr("state.db-wal", b"foreign-wal-from-archive")
            zf.writestr("state.db-shm", b"foreign-shm")
            zf.writestr("state.db-journal", b"foreign-journal")

        run_import(Namespace(zipfile=str(zip_path), force=True))

        for suffix, payload in (
            ("-wal", b"foreign-wal-from-archive"),
            ("-shm", b"foreign-shm"),
            ("-journal", b"foreign-journal"),
        ):
            sidecar = live_db.with_name("state.db" + suffix)
            assert not sidecar.exists() or sidecar.read_bytes() != payload, suffix
        assert _count_rows(live_db) == (2, 4)

    def test_missing_target_takes_the_plain_publish(self, tmp_path, monkeypatch):
        """A fresh install has no inode to preserve; the member still lands."""
        from hermes_cli.backup import run_import

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        staged = tmp_path / "backup-state.db"
        _write_session_db(staged, 2, 3)
        zip_path = tmp_path / "backup.zip"
        self._zip_with_db(zip_path, staged)

        run_import(Namespace(zipfile=str(zip_path), force=True))
        assert _count_rows(home / "state.db") == (2, 6)
