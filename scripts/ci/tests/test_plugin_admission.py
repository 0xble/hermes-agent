"""Admission boundaries exercised with real local Git repos, without network."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "check_plugin_admission.py"
spec = importlib.util.spec_from_file_location("plugin_admission", SCRIPT)
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


class PluginAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="plugin-admission-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.catalog = self.root / "catalog"
        self.clones = self.root / "clones"
        self.clones.mkdir()
        self.env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for repo in (self.source, self.catalog):
            repo.mkdir()
            self.git(repo, "init", "-q")
            self.git(repo, "config", "user.name", "CI fixture")
            self.git(repo, "config", "user.email", "fixture@example.invalid")
            self.git(repo, "config", "commit.gpgsign", "false")
        self.git(self.root, "config", "--global", f"url.{self.source.as_uri()}.insteadOf", "https://example.invalid/plugin")
        self.git(self.root, "config", "--global", "protocol.file.allow", "always")
        (self.source / "plugin.yaml").write_text("name: fixture\n", encoding="utf-8")
        (self.source / "version.txt").write_text("pinned", encoding="utf-8")
        self.pin = self.commit(self.source)
        (self.source / "version.txt").write_text("newer branch tip", encoding="utf-8")
        self.commit(self.source)
        self.record = self.root / "validated.json"
        self.validator = self.root / "validator.py"
        self.validator.write_text(
            "import json, pathlib, subprocess, sys\n"
            "plugin = pathlib.Path(sys.argv[-1])\n"
            "record = {'args': sys.argv[2:], 'path': str(plugin), "
            "'version': (plugin / 'version.txt').read_text(encoding='utf-8'), "
            "'sha': subprocess.check_output(['git', '-C', str(plugin), 'rev-parse', 'HEAD'], text=True, encoding='utf-8', errors='replace').strip()}\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(record), encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.command = [sys.executable, str(self.validator), str(self.record)]

    def git(self, root, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=root, text=True, encoding="utf-8", errors="replace", stderr=subprocess.PIPE).strip()

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "fixture")
        return self.git(root, "rev-parse", "HEAD")

    def entry(self, **overrides):
        return {
            "name": "fixture", "repo": "https://example.invalid/plugin",
            "sha": self.pin, "description": "Fixture plugin", "maintainer": "Fixture",
            **overrides,
        }

    def admit(self, entry, **options):
        return admission.admit(entry, validator=self.command, temporary_parent=self.clones, **options)

    def test_only_changed_entries_are_validated_at_exact_head_and_pin(self):
        directory = self.catalog / "plugin-catalog"
        directory.mkdir()
        for name in ("unchanged.yaml", "deleted.yaml"):
            (directory / name).write_text(yaml.safe_dump(self.entry(sha="f" * 40)), encoding="utf-8")
        base = self.commit(self.catalog)
        (directory / "deleted.yaml").unlink()
        (directory / "removed.yaml").write_text("removed: []\n", encoding="utf-8")
        (directory / "added.yaml").write_text(yaml.safe_dump(self.entry()), encoding="utf-8")
        head = self.commit(self.catalog)
        # An unrelated dirty catalog edit must not replace the selected head.
        (directory / "added.yaml").write_text("invalid dirty work\n", encoding="utf-8")
        actual, files = admission.changed_entries(self.catalog, base, head)
        self.assertEqual((actual, files), (head, ["plugin-catalog/added.yaml"]))
        self.assertEqual(admission.check(self.catalog, base, head, validator=self.command), 0)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(record["sha"], self.pin)
        self.assertEqual(record["version"], "pinned")
        self.assertEqual(record["args"][:-1], ["plugins", "validate", "--install-deps"])
        self.assertFalse(Path(record["path"]).exists())
        self.assertEqual(admission.check(self.catalog, head, head, validator=self.command), 0)
        with self.assertRaises(RuntimeError):
            admission.changed_entries(self.catalog, "missing-base", head)

    def test_invalid_sources_fail_and_owned_clones_are_always_cleaned(self):
        nested = self.source / "plugins" / "with space"
        nested.mkdir(parents=True)
        (nested / "plugin.yaml").write_text("name: nested\n", encoding="utf-8")
        (nested / "version.txt").write_text("nested plugin", encoding="utf-8")
        nested_pin = self.commit(self.source)
        self.admit(self.entry(sha=nested_pin, subdir="plugins/with space"))
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["version"], "nested plugin")
        self.assertEqual(list(self.clones.iterdir()), [])
        for overrides, error in [
            ({"sha": "main"}, "40 lowercase"),
            ({"sha": "f" * 40}, "git exited"),
            ({"subdir": "../source"}, "inside"),
            ({"subdir": "C:/outside"}, "inside"),
            ({"subdir": "/outside"}, "inside"),
            ({"subdir": "missing"}, "directory"),
        ]:
            with self.subTest(overrides=overrides), self.assertRaisesRegex((ValueError, RuntimeError), error):
                self.admit(self.entry(**overrides))
            self.assertEqual(list(self.clones.iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, "exited 7"):
            admission.admit(self.entry(), validator=[sys.executable, "-c", "raise SystemExit(7)"], temporary_parent=self.clones)
        self.assertEqual(list(self.clones.iterdir()), [])
        with self.assertRaises(subprocess.TimeoutExpired):
            admission.admit(self.entry(), validator=[sys.executable, "-c", "import time; time.sleep(30)"], temporary_parent=self.clones, validation_timeout=0.1)
        self.assertEqual(list(self.clones.iterdir()), [])
        (self.source / "updater.ts").write_text("fetch('https://x/releases/latest');\nwriteFile('plugin', 'new');\n", encoding="utf-8")
        updater_pin = self.commit(self.source)
        with self.assertRaisesRegex(ValueError, "self-updating"):
            self.admit(self.entry(sha=updater_pin))
        self.assertEqual(list(self.clones.iterdir()), [])
        (self.source / "updater.ts").unlink()
        (self.source / "plugin.yaml").unlink()
        missing_manifest_pin = self.commit(self.source)
        with self.assertRaisesRegex(ValueError, "manifest missing"):
            self.admit(self.entry(sha=missing_manifest_pin))
        self.assertEqual(list(self.clones.iterdir()), [])
        if os.name == "posix":
            (self.source / "plugin.yaml").symlink_to(self.validator)
            symlink_pin = self.commit(self.source)
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.admit(self.entry(sha=symlink_pin))
            self.assertEqual(list(self.clones.iterdir()), [])

    def test_entries_identical_to_the_accepted_release_are_not_readmitted(self):
        directory = self.catalog / "plugin-catalog"
        directory.mkdir()
        (directory / "keep.yaml").write_text(yaml.safe_dump(self.entry(sha="f" * 40)), encoding="utf-8")
        base = self.commit(self.catalog)
        (directory / "upstream.yaml").write_text(yaml.safe_dump(self.entry(sha="e" * 40)), encoding="utf-8")
        (directory / "forked.yaml").write_text(yaml.safe_dump(self.entry(sha="d" * 40)), encoding="utf-8")
        release = self.commit(self.catalog)
        (directory / "forked.yaml").write_text(yaml.safe_dump(self.entry()), encoding="utf-8")
        (self.catalog / "MAINTENANCE.md").write_text(
            f"Accepted release baseline:\n`vTEST`, `{release}`.\n", encoding="utf-8")
        head = self.commit(self.catalog)
        self.assertEqual(admission.changed_entries(self.catalog, base, head),
                         (head, ["plugin-catalog/forked.yaml"]))
        (self.catalog / "MAINTENANCE.md").write_text("no baseline\n", encoding="utf-8")
        unrecorded = self.commit(self.catalog)
        self.assertEqual(admission.changed_entries(self.catalog, base, unrecorded)[1],
                         ["plugin-catalog/forked.yaml", "plugin-catalog/upstream.yaml"])

    def test_working_tree_checks_modified_and_untracked_entries(self):
        directory = self.catalog / "plugin-catalog"
        directory.mkdir()
        for name in ("modified.yaml", "deleted.yaml", "unchanged.yaml"):
            (directory / name).write_text(yaml.safe_dump(self.entry()), encoding="utf-8")
        base = self.commit(self.catalog)
        (directory / "modified.yaml").write_text(yaml.safe_dump(self.entry(description="Edited locally")), encoding="utf-8")
        (directory / "new.yaml").write_text(yaml.safe_dump(self.entry()), encoding="utf-8")
        (directory / "deleted.yaml").unlink()
        (directory / "removed.yaml").write_text("removed: []\n", encoding="utf-8")
        scope, files = admission.changed_entries(self.catalog, base, None)
        self.assertIsNone(scope)
        self.assertEqual(files, ["plugin-catalog/modified.yaml", "plugin-catalog/new.yaml"])
        self.assertEqual(admission.check(self.catalog, base, None, validator=self.command), 0)
        (directory / "modified.yaml").write_text("invalid dirty entry\n", encoding="utf-8")
        self.assertEqual(admission.check(self.catalog, base, None, validator=self.command), 1)
        self.assertEqual(admission.check(self.catalog, base, base, validator=self.command), 0)
        if os.name == "posix":
            (directory / "modified.yaml").unlink()
            (directory / "modified.yaml").symlink_to(self.root / "outside.yaml")
            (self.root / "outside.yaml").write_text(yaml.safe_dump(self.entry()), encoding="utf-8")
            self.assertEqual(admission.check(self.catalog, base, None, validator=self.command), 1)


if __name__ == "__main__":
    unittest.main()
