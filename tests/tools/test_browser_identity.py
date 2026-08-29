"""Regression contract for named real-profile browser identities."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest


def _browser_cfg(*, required: bool = False, default: str = "personal") -> dict:
    return {
        "real_profile_identities": {
            "personal": {"browser": "chrome", "source_profile": "Default"},
            "lpg": {"browser": "chrome", "source_profile": "Profile 1"},
        },
        "default_identity": default,
        "require_identity": required,
    }


class TestIdentityResolution:
    def test_explicit_identity_overrides_default(self):
        from hermes_cli.browser_identity import resolve_browser_identity

        resolved = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert resolved.alias == "lpg"
        assert resolved.browser == "chrome"
        assert resolved.source_profile == "Profile 1"

    def test_omitted_identity_uses_default(self):
        from hermes_cli.browser_identity import resolve_browser_identity

        assert (
            resolve_browser_identity(None, browser_cfg=_browser_cfg()).alias
            == "personal"
        )

    def test_strict_mode_rejects_omission_even_with_default(self):
        from hermes_cli.browser_identity import (
            BrowserIdentityError,
            resolve_browser_identity,
        )

        with pytest.raises(BrowserIdentityError, match="required"):
            resolve_browser_identity(None, browser_cfg=_browser_cfg(required=True))

    def test_unknown_identity_never_falls_back(self):
        from hermes_cli.browser_identity import (
            BrowserIdentityError,
            resolve_browser_identity,
        )

        with pytest.raises(
            BrowserIdentityError, match="unknown browser identity 'meridian'"
        ):
            resolve_browser_identity("meridian", browser_cfg=_browser_cfg())

    def test_unconfigured_install_preserves_legacy_behavior(self):
        from hermes_cli.browser_identity import resolve_browser_identity

        assert resolve_browser_identity(None, browser_cfg={}) is None

    @pytest.mark.parametrize(
        "source_profile", ["../Default", "a/b", r"a\\b", ".", "..", "Guest Profile"]
    )
    def test_invalid_source_profiles_fail_closed(self, source_profile):
        from hermes_cli.browser_identity import (
            BrowserIdentityError,
            resolve_browser_identity,
        )

        cfg = _browser_cfg()
        cfg["real_profile_identities"]["personal"]["source_profile"] = source_profile
        with pytest.raises(BrowserIdentityError, match="source_profile"):
            resolve_browser_identity("personal", browser_cfg=cfg)

    def test_runtime_keys_are_stable_and_scoped(self):
        from hermes_cli.browser_identity import browser_identity_runtime_key

        assert browser_identity_runtime_key(
            "personal", "chrome"
        ) == browser_identity_runtime_key("personal", "chrome")
        assert browser_identity_runtime_key(
            "personal", "chrome"
        ) != browser_identity_runtime_key("lpg", "chrome")
        assert browser_identity_runtime_key(
            "personal", "chrome"
        ) != browser_identity_runtime_key("personal", "edge")

    @pytest.mark.parametrize(
        "change,match",
        [
            ({"real_profile_identities": "personal"}, "must be a mapping"),
            ({"require_identity": "yes"}, "must be true or false"),
        ],
    )
    def test_malformed_configuration_never_falls_back(self, change, match):
        from hermes_cli.browser_identity import (
            BrowserIdentityError,
            resolve_browser_identity,
        )

        cfg = _browser_cfg()
        cfg.update(change)
        with pytest.raises(BrowserIdentityError, match=match):
            resolve_browser_identity(None, browser_cfg=cfg)

    def test_runtime_scope_key_changes_with_hermes_home(self, tmp_path, monkeypatch):
        from hermes_cli.browser_identity import browser_identity_scope_key

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
        first = browser_identity_scope_key("runtime")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
        second = browser_identity_scope_key("runtime")
        assert first != second


class TestBrowserIdentityProcessLock:
    def test_same_snapshot_lock_is_exclusive_across_processes(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.browser_identity import (
            BrowserIdentityError,
            BrowserIdentityProcessLock,
        )

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parents[2])
        code = """
from hermes_cli.browser_identity import BrowserIdentityProcessLock
import time
with BrowserIdentityProcessLock('runtime', timeout=2):
    print('locked', flush=True)
    time.sleep(2)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert (
                proc.stdout is not None and proc.stdout.readline().strip() == "locked"
            )
            with pytest.raises(BrowserIdentityError, match="timed out"):
                with BrowserIdentityProcessLock("runtime", timeout=0.1):
                    pass
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        lock_file = home / "browser-profile" / "locks" / "runtime.lock"
        if os.name != "nt":
            assert lock_file.stat().st_mode & 0o077 == 0


class TestIdentitySnapshots:
    @staticmethod
    def _source(root: Path) -> Path:
        for profile, marker in (("Default", "personal"), ("Profile 1", "lpg")):
            profile_dir = root / profile
            (profile_dir / "Network").mkdir(parents=True)
            (profile_dir / "Cache").mkdir()
            (profile_dir / "Preferences").write_text(marker)
            (profile_dir / "Network" / "Cookies").write_text(f"{marker}-cookies")
            (profile_dir / "Cache" / "discard").write_text("cache")
        (root / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 1"}})
        )
        return root

    def test_exact_profiles_use_distinct_snapshot_dirs_without_last_used(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc

        source = self._source(tmp_path / "source")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "home")
        with patch.object(
            bc,
            "_last_used_profile",
            side_effect=AssertionError("must not consult last_used"),
        ):
            personal, personal_err = bc.snapshot_real_profile(
                "chrome", src=str(source), source_profile="Default", identity="personal"
            )
            lpg, lpg_err = bc.snapshot_real_profile(
                "chrome", src=str(source), source_profile="Profile 1", identity="lpg"
            )

        assert personal_err is None and lpg_err is None
        assert personal != lpg
        assert Path(personal).parts[-3] == "identities"
        assert (Path(personal) / "Default" / "Preferences").read_text() == "personal"
        assert (Path(lpg) / "Default" / "Preferences").read_text() == "lpg"
        assert not (Path(personal) / "Default" / "Cache").exists()
        assert not (Path(lpg) / "Default" / "Cache").exists()

    def test_snapshot_local_state_carries_the_selected_profile_identity(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc

        source = self._source(tmp_path / "source")
        (source / "Local State").write_text(
            json.dumps(
                {
                    "profile": {
                        "last_used": "Profile 1",
                        "last_active_profiles": ["Profile 1"],
                        "info_cache": {
                            "Default": {
                                "name": "Personal",
                                "user_name": "personal@example.test",
                            },
                            "Profile 1": {
                                "name": "LPG",
                                "user_name": "lpg@example.test",
                            },
                        },
                    }
                }
            )
        )
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "home")

        snapshot, err = bc.snapshot_real_profile(
            "chrome",
            src=str(source),
            source_profile="Profile 1",
            identity="lpg",
        )

        assert err is None and snapshot is not None
        state = json.loads((Path(snapshot) / "Local State").read_text())
        profile = state["profile"]
        assert profile["last_used"] == "Default"
        assert profile["last_active_profiles"] == ["Default"]
        assert profile["info_cache"] == {
            "Default": {
                "name": "LPG",
                "user_name": "lpg@example.test",
            }
        }

    def test_missing_exact_profile_fails_without_default_fallback(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc

        source = self._source(tmp_path / "source")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "home")
        dst, err = bc.snapshot_real_profile(
            "chrome", src=str(source), source_profile="Profile 99", identity="missing"
        )
        assert dst is None
        assert err and "Profile 99" in err and "not found" in err

    def test_source_profile_change_rebuilds_identity_snapshot(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc

        source = self._source(tmp_path / "source")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "home")
        first, err = bc.snapshot_real_profile(
            "chrome", src=str(source), source_profile="Default", identity="personal"
        )
        assert err is None
        (Path(first) / "Default" / "History").write_text("stale-personal-history")

        second, err = bc.snapshot_real_profile(
            "chrome", src=str(source), source_profile="Profile 1", identity="personal"
        )
        assert err is None and second != first
        assert (Path(second) / "Default" / "Preferences").read_text() == "lpg"
        assert not (Path(second) / "Default" / "History").exists()

    def test_named_snapshot_secures_every_credential_store_ancestor(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc

        source = self._source(tmp_path / "source")
        home = tmp_path / "home"
        secured: list[str] = []
        monkeypatch.setattr(bc, "get_hermes_home", lambda: home)
        monkeypatch.setattr(bc, "_secure_snapshot_root", secured.append)

        dst, err = bc.snapshot_real_profile(
            "chrome",
            src=str(source),
            source_profile="Default",
            identity="personal",
        )

        assert err is None and dst is not None
        root = home / "browser-profile"
        assert str(root) in secured
        assert str(root / "identities") in secured
        assert str(Path(dst).parent) in secured
        assert str(Path(dst)) in secured


class TestBrowserUseIdentityRouting:
    @pytest.fixture(autouse=True)
    def _clear_bindings(self):
        import tools.browser_use_cli as bu

        bu._browser_exec_identity_bindings.clear()
        bu._browser_exec_identity_daemons.clear()
        bu._browser_exec_identity_daemon_homes.clear()
        yield
        bu._browser_exec_identity_bindings.clear()
        bu._browser_exec_identity_daemons.clear()
        bu._browser_exec_identity_daemon_homes.clear()

    def test_schema_exposes_aliases_and_strict_requirement(self):
        import tools.browser_use_cli as bu

        with (
            patch.object(bu, "_real_profile_consented", return_value=True),
            patch(
                "hermes_cli.browser_identity.read_browser_identity_config",
                return_value=_browser_cfg(required=True),
            ),
        ):
            params = bu._dynamic_schema_overrides()["parameters"]
        assert params["properties"]["identity"]["enum"] == ["lpg", "personal"]
        assert params["required"] == ["code", "identity"]

    def test_identity_namespaces_daemon_and_is_audited(self, tmp_path, monkeypatch):
        import tools.browser_use_cli as bu

        cli = tmp_path / "browser-use"
        cli.write_text(
            '#!/bin/sh\ncat > /dev/null\necho "name:$BU_NAME cdp:$BU_CDP_URL"\n'
        )
        cli.chmod(0o755)
        monkeypatch.setattr(bu, "_find_cli", lambda: [str(cli)])
        monkeypatch.setattr(bu, "_real_profile_consented", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )

        def route(env, force_local, identity=None):
            assert identity.alias == "lpg"
            assert bu._browser_exec_identity_daemons == {}
            env["BU_CDP_URL"] = "http://127.0.0.1:9229"
            return None

        monkeypatch.setattr(bu, "_resolve_real_profile_cdp", route)
        monkeypatch.setattr(bu, "_resolve_backend_cdp", lambda *args, **kwargs: None)
        result = json.loads(
            bu.browser_exec("print(1)", session="openrouter", identity="lpg")
        )
        assert result["success"] is True
        assert result["identity"] == "lpg"
        assert result["session"] == "openrouter"
        assert "name:openrouter" not in result["output"]
        assert "cdp:http://127.0.0.1:9229" in result["output"]

    def test_named_session_cannot_switch_identity(self, tmp_path, monkeypatch):
        import tools.browser_use_cli as bu

        cli = tmp_path / "browser-use"
        cli.write_text("#!/bin/sh\ncat > /dev/null\n")
        cli.chmod(0o755)
        monkeypatch.setattr(bu, "_find_cli", lambda: [str(cli)])
        monkeypatch.setattr(bu, "_real_profile_consented", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )
        monkeypatch.setattr(
            bu, "_resolve_real_profile_cdp", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(bu, "_resolve_backend_cdp", lambda *args, **kwargs: None)

        assert json.loads(bu.browser_exec("print(1)", session="s1", identity="lpg"))[
            "success"
        ]
        switched = json.loads(
            bu.browser_exec("print(1)", session="s1", identity="personal")
        )
        assert "already bound" in switched["error"]
        assert "lpg" not in switched["error"]

    @pytest.mark.parametrize("legacy_first", [True, False])
    def test_session_rejects_legacy_named_transitions(
        self, tmp_path, monkeypatch, legacy_first
    ):
        import tools.browser_use_cli as bu

        cli = tmp_path / "browser-use"
        cli.write_text("#!/bin/sh\ncat > /dev/null\n")
        cli.chmod(0o755)
        cfg = {} if legacy_first else _browser_cfg()
        monkeypatch.setattr(bu, "_find_cli", lambda: [str(cli)])
        monkeypatch.setattr(bu, "_real_profile_consented", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: cfg,
        )
        monkeypatch.setattr(
            bu, "_resolve_real_profile_cdp", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(bu, "_resolve_backend_cdp", lambda *args, **kwargs: None)

        first_identity = "" if legacy_first else "lpg"
        assert json.loads(
            bu.browser_exec("print(1)", session="stable", identity=first_identity)
        )["success"]
        cfg = _browser_cfg() if legacy_first else {}
        second_identity = "lpg" if legacy_first else ""
        switched = json.loads(
            bu.browser_exec("print(1)", session="stable", identity=second_identity)
        )
        assert "already bound" in switched["error"]

    def test_unknown_identity_fails_before_cli_launch(self, monkeypatch):
        import tools.browser_use_cli as bu

        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )
        monkeypatch.setattr(
            bu,
            "_find_cli",
            lambda: (_ for _ in ()).throw(AssertionError("must not launch")),
        )
        result = json.loads(bu.browser_exec("print(1)", identity="meridian"))
        assert "unknown browser identity" in result["error"]

    def test_identity_rejects_operator_cdp_override(self, monkeypatch):
        from hermes_cli.browser_identity import resolve_browser_identity
        import tools.browser_use_cli as bu

        identity = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        monkeypatch.setattr(bu, "_real_profile_consented", lambda: True)
        monkeypatch.setattr(
            "tools.browser_tool._get_cdp_override_raw", lambda: "ws://other"
        )
        err = bu._resolve_real_profile_cdp({}, force_local=True, identity=identity)
        assert "incompatible" in err

    def test_runtime_reload_stops_only_its_browser_use_daemons(self, monkeypatch):
        import tools.browser_use_cli as bu

        bu._browser_exec_identity_daemons.update({
            "rp_a": "runtime-a",
            "rp_b": "runtime-b",
        })
        calls = []
        monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
        monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
        monkeypatch.setattr(
            bu.subprocess,
            "run",
            lambda argv, **kwargs: calls.append((argv, kwargs["env"]["BU_NAME"])),
        )

        bu._reload_browser_exec_daemons_for_runtime("runtime-a")

        assert calls == [(["browser-use", "--reload"], "rp_a")]
        assert bu._browser_exec_identity_daemons == {"rp_b": "runtime-b"}

    def test_failed_daemon_reload_remains_registered_for_retry(self):
        import tools.browser_use_cli as bu

        bu._browser_exec_identity_daemons["rp_bad"] = "runtime-a"
        with (
            patch.object(bu, "_find_cli", return_value=["browser-use"]),
            patch.object(
                bu.subprocess,
                "run",
                return_value=Mock(returncode=1, stdout="", stderr="reload failed"),
            ),
        ):
            assert bu._reload_browser_exec_daemons_for_runtime("runtime-a") is False
        assert bu._browser_exec_identity_daemons == {"rp_bad": "runtime-a"}


class TestBuiltInIdentityRouting:
    def test_local_session_passes_identity_to_real_profile(self):
        import tools.browser_tool as bt

        with (
            patch.object(
                bt, "_real_profile_cdp", return_value=("http://127.0.0.1:9230", None)
            ) as cdp,
            patch.object(bt, "_resolve_cdp_override", side_effect=lambda url: url),
            patch.object(bt, "_use_real_profile", return_value=True),
            patch(
                "hermes_cli.browser_identity.read_browser_identity_config",
                return_value=_browser_cfg(),
            ),
        ):
            info = bt._create_local_session("task", identity="lpg")
        cdp.assert_called_once_with("lpg")
        assert info["browser_identity"] == "lpg"
        assert info["features"]["real_profile"] is True

    def test_non_navigation_creation_resolves_and_stamps_default_identity(self):
        import tools.browser_tool as bt

        created = []

        def fake_create(task_id, allow_real_profile=True, identity=None):
            created.append((task_id, allow_real_profile, identity))
            return {
                "session_name": "rp_default",
                "cdp_url": "http://127.0.0.1:9230",
                "browser_identity": identity,
                "browser_identity_key": "key",
                "features": {"local": True, "real_profile": True},
            }

        try:
            with (
                patch(
                    "hermes_cli.browser_identity.read_browser_identity_config",
                    return_value=_browser_cfg(),
                ),
                patch.object(bt, "_create_local_session", side_effect=fake_create),
                patch.object(bt, "_get_cloud_provider", return_value=None),
                patch.object(bt, "_get_cdp_override_raw", return_value=None),
                patch.object(bt, "_start_browser_cleanup_thread"),
                patch.object(bt, "_update_session_activity"),
            ):
                info = bt._get_session_info("default-first-command")
            assert created == [("default-first-command", True, "personal")]
            assert info["browser_identity"] == "personal"
        finally:
            bt._active_sessions.pop("default-first-command", None)

    def test_non_navigation_creation_obeys_strict_identity_mode(self):
        import tools.browser_tool as bt

        with (
            patch(
                "hermes_cli.browser_identity.read_browser_identity_config",
                return_value=_browser_cfg(required=True),
            ),
            patch.object(bt, "_create_local_session") as create,
            pytest.raises(RuntimeError, match="identity is required"),
        ):
            bt._get_session_info("strict-first-command")
        create.assert_not_called()

    def test_identity_local_session_rejects_consent_off(self):
        import tools.browser_tool as bt

        with (
            patch(
                "hermes_cli.browser_identity.read_browser_identity_config",
                return_value=_browser_cfg(),
            ),
            patch.object(bt, "_use_real_profile", return_value=False),
            pytest.raises(RuntimeError, match="use_real_profile"),
        ):
            bt._create_local_session("consent-off", identity="personal")

    def test_cached_task_session_rejects_identity_switch(self):
        from hermes_cli.browser_identity import (
            browser_identity_scope_key,
            resolve_browser_identity,
        )
        import tools.browser_tool as bt

        lpg = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert lpg is not None
        bt._active_sessions["identity-switch-test"] = {
            "session_name": "rp_test",
            "cdp_url": "http://127.0.0.1:9231",
            "browser_identity": "lpg",
            "browser_identity_key": browser_identity_scope_key(lpg.runtime_key),
            "browser_identity_home": __import__("hermes_constants").hermes_home_key(),
            "features": {"local": True, "real_profile": True},
        }
        try:
            with (
                patch(
                    "hermes_cli.browser_identity.read_browser_identity_config",
                    return_value=_browser_cfg(),
                ),
                pytest.raises(RuntimeError, match="already bound"),
            ):
                bt._get_session_info("identity-switch-test", identity="personal")
        finally:
            bt._active_sessions.pop("identity-switch-test", None)

    def test_cached_task_rejects_same_alias_remapped_to_another_profile(self):
        from hermes_cli.browser_identity import (
            browser_identity_scope_key,
            resolve_browser_identity,
        )
        import tools.browser_tool as bt

        original = _browser_cfg()
        lpg = resolve_browser_identity("lpg", browser_cfg=original)
        assert lpg is not None
        bt._active_sessions["identity-remap-test"] = {
            "session_name": "rp_test",
            "cdp_url": "http://127.0.0.1:9232",
            "browser_identity": "lpg",
            "browser_identity_key": browser_identity_scope_key(lpg.runtime_key),
            "browser_identity_home": __import__("hermes_constants").hermes_home_key(),
            "features": {"local": True, "real_profile": True},
        }
        changed = _browser_cfg()
        changed["real_profile_identities"]["lpg"]["source_profile"] = "Profile 2"
        try:
            with (
                patch(
                    "hermes_cli.browser_identity.read_browser_identity_config",
                    return_value=changed,
                ),
                pytest.raises(RuntimeError, match="already bound"),
            ):
                bt._get_session_info("identity-remap-test", identity="lpg")
        finally:
            bt._active_sessions.pop("identity-remap-test", None)

    def test_named_task_cannot_be_reused_from_another_hermes_home(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.browser_identity import (
            browser_identity_scope_key,
            resolve_browser_identity,
        )
        from hermes_constants import hermes_home_key
        import tools.browser_tool as bt

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
        lpg = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert lpg is not None
        bt._active_sessions["cross-home-task"] = {
            "session_name": "rp_test",
            "cdp_url": "http://127.0.0.1:9232",
            "browser_identity": "lpg",
            "browser_identity_key": browser_identity_scope_key(lpg.runtime_key),
            "browser_identity_home": hermes_home_key(),
            "features": {"local": True, "real_profile": True},
        }
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
        try:
            with pytest.raises(RuntimeError, match="another Hermes profile"):
                bt._get_session_info("cross-home-task")
        finally:
            bt._active_sessions.pop("cross-home-task", None)

    def test_navigate_schema_exposes_identity_at_session_entry(self):
        import tools.browser_tool as bt

        with patch(
            "hermes_cli.browser_identity.read_browser_identity_config",
            return_value=_browser_cfg(required=False),
        ):
            params = bt._browser_navigate_schema_overrides()["parameters"]
        assert params["properties"]["identity"]["enum"] == ["lpg", "personal"]
        assert params["required"] == ["url"]

    def test_strict_navigate_rejects_omitted_identity_before_browser_use(self):
        import tools.browser_tool as bt

        with (
            patch(
                "hermes_cli.browser_identity.read_browser_identity_config",
                return_value=_browser_cfg(required=True),
            ),
            patch.object(bt, "_get_session_info") as session,
        ):
            result = json.loads(
                bt.browser_navigate("https://example.com", task_id="strict")
            )
        assert "identity is required" in result["error"]
        session.assert_not_called()


class TestNamedRealProfileProcesses:
    @pytest.fixture(autouse=True)
    def _clear_runtime_tracking(self, monkeypatch):
        import tools.browser_tool as bt

        next_port = iter(range(9301, 9400))
        real_popen = subprocess.Popen

        def fake_popen(argv, **_kwargs):
            data_arg = next(
                (arg for arg in argv if arg.startswith("--user-data-dir=")), None
            )
            if data_arg is None:
                return real_popen(argv, **_kwargs)
            data_dir = data_arg.split("=", 1)[1]
            os.makedirs(data_dir, exist_ok=True)
            port = next(next_port)
            with open(os.path.join(data_dir, "DevToolsActivePort"), "w") as handle:
                handle.write(f"{port}\n/devtools/browser/hermes\n")
            proc = Mock(pid=port)
            proc.poll.return_value = None
            return proc

        bt._real_profile_cdp_cache.clear()
        bt._real_profile_session_names.clear()
        bt._real_profile_session_homes.clear()
        bt._real_profile_browser_processes.clear()
        monkeypatch.setattr(
            "hermes_cli.browser_connect.chromium_executable",
            lambda _browser: "/opt/chrome",
        )
        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(bt, "_cdp_http_ready", lambda _url: True)
        yield
        bt._real_profile_cdp_cache.clear()
        bt._real_profile_session_names.clear()
        bt._real_profile_session_homes.clear()
        bt._real_profile_browser_processes.clear()

    def test_runtime_resources_are_scoped_to_active_hermes_home(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.browser_identity import resolve_browser_identity
        import tools.browser_tool as bt

        identity = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert identity is not None
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
        first = bt._real_profile_runtime_resources(identity)
        legacy_first = bt._real_profile_runtime_resources(None)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
        second = bt._real_profile_runtime_resources(identity)
        legacy_second = bt._real_profile_runtime_resources(None)
        assert first[0] != second[0]
        assert first[2] != second[2]
        assert legacy_first[0] != legacy_second[0]
        assert legacy_first[2] != legacy_second[2]

    def test_profile_scoped_cleanup_does_not_close_another_home(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.browser_identity import resolve_browser_identity
        import tools.browser_tool as bt
        import tools.browser_use_cli as bu

        identity = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert identity is not None
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
        session_a, _lock_a, key_a = bt._real_profile_runtime_resources(identity)
        bt._real_profile_cdp_cache[key_a] = "http://127.0.0.1:9300"
        bt._track_real_profile_session(key_a, session_a)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
        session_b, _lock_b, key_b = bt._real_profile_runtime_resources(identity)
        bt._real_profile_cdp_cache[key_b] = "http://127.0.0.1:9301"
        bt._track_real_profile_session(key_b, session_b)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
        closed = []
        monkeypatch.setattr(
            bu, "_close_all_browser_exec_identity_daemons", lambda **_kwargs: None
        )
        monkeypatch.setattr(
            bt,
            "_agent_browser_close_session",
            lambda name, **_kwargs: closed.append(name),
        )

        bt._close_all_real_profile_runtimes()

        assert closed == [session_a]
        assert key_a not in bt._real_profile_cdp_cache
        assert bt._real_profile_cdp_cache[key_b] == "http://127.0.0.1:9301"
        assert bt._real_profile_session_names == {key_b: session_b}

    def test_cached_cdp_is_rejected_when_live_process_does_not_own_snapshot(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.browser_identity import resolve_browser_identity
        import hermes_cli.browser_connect as bc
        import tools.browser_tool as bt

        home = tmp_path / "home"
        source = tmp_path / "source"
        snapshot = home / "browser-profile" / "identities" / "opaque" / "chrome"
        source.mkdir()
        snapshot.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        identity = resolve_browser_identity("lpg", browser_cfg=_browser_cfg())
        assert identity is not None
        session_name, _lock, cache_key = bt._real_profile_runtime_resources(identity)
        stale = "http://127.0.0.1:9300"
        fresh = "http://127.0.0.1:9301"
        bt._real_profile_cdp_cache[cache_key] = stale
        closed = []
        cdp_results = iter([None, fresh])

        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )
        monkeypatch.setattr(bt, "_use_real_profile", lambda: True)
        monkeypatch.setattr(bc, "real_profile_data_dir", lambda _browser: str(source))
        monkeypatch.setattr(
            bc, "real_profile_copy_dir", lambda *_args, **_kwargs: str(snapshot)
        )
        monkeypatch.setattr(
            bc,
            "snapshot_real_profile",
            lambda *_args, **_kwargs: (str(snapshot), None),
        )
        monkeypatch.setattr(
            bt,
            "_cdp_owned_by_data_dir",
            lambda endpoint, _path: endpoint == fresh,
        )
        monkeypatch.setattr(bt, "_reload_browser_use_runtime", lambda _key: True)
        monkeypatch.setattr(
            bt, "_agent_browser_get_cdp", lambda _name: next(cdp_results)
        )
        monkeypatch.setattr(
            bt,
            "_agent_browser_close_session",
            lambda name, **_kwargs: closed.append(name),
        )
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/usr/bin/agent-browser")
        monkeypatch.setattr(
            bt.subprocess,
            "run",
            lambda *_args, **_kwargs: Mock(returncode=0, stdout="", stderr=""),
        )

        result, err = bt._real_profile_cdp("lpg")
        assert err is None and result == fresh
        assert closed == [session_name]
        assert bt._real_profile_cdp_cache[cache_key] == fresh

    def test_recovers_direct_browser_before_snapshot_overlay(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc
        import tools.browser_tool as bt

        home = tmp_path / "home"
        source = tmp_path / "source"
        snapshot = home / "browser-profile" / "identities" / "opaque" / "chrome"
        source.mkdir()
        snapshot.mkdir(parents=True)
        (snapshot / "DevToolsActivePort").write_text(
            "9355\n/devtools/browser/recovered\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        attached = []

        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )
        monkeypatch.setattr(bt, "_use_real_profile", lambda: True)
        monkeypatch.setattr(bc, "real_profile_data_dir", lambda _browser: str(source))
        monkeypatch.setattr(
            bc, "real_profile_copy_dir", lambda *_args, **_kwargs: str(snapshot)
        )
        monkeypatch.setattr(
            bc,
            "snapshot_real_profile",
            lambda *_args, **_kwargs: pytest.fail(
                "must not overlay a snapshot while its browser is live"
            ),
        )
        monkeypatch.setattr(bt, "_agent_browser_get_cdp", lambda _name: None)
        monkeypatch.setattr(bt, "_cdp_http_ready", lambda endpoint: endpoint.endswith("9355"))
        monkeypatch.setattr(bt, "_cdp_owned_by_data_dir", lambda *_args: True)
        monkeypatch.setattr(bt, "_reload_browser_use_runtime", lambda _key: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/usr/bin/agent-browser")

        def attach(argv, **_kwargs):
            attached.append(argv)
            return Mock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(bt.subprocess, "run", attach)
        monkeypatch.setattr(
            bt.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("must not launch a second browser"),
        )

        result, err = bt._real_profile_cdp("lpg")

        assert err is None
        assert result == "http://127.0.0.1:9355"
        assert len(attached) == 1
        assert "--cdp" in attached[0]
        assert "9355" in attached[0]

    def test_exact_identities_get_distinct_snapshot_process_and_cache_resources(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.browser_connect as bc
        import tools.browser_tool as bt

        source = TestIdentitySnapshots()._source(tmp_path / "source")
        home = tmp_path / "home"
        launched: dict[str, str] = {}
        snapshots: list[dict] = []

        def get_cdp(session_name):
            return launched.get(session_name)

        def run(argv, **kwargs):
            session_name = argv[argv.index("--session") + 1]
            launched[session_name] = f"http://127.0.0.1:{9300 + len(launched)}"
            return Mock(returncode=0, stdout="", stderr="")

        original_snapshot = bc.snapshot_real_profile

        def snapshot(*args, **kwargs):
            snapshots.append(dict(kwargs))
            return original_snapshot(*args, **kwargs)

        bt._real_profile_cdp_cache.clear()
        monkeypatch.setattr(bc, "get_hermes_home", lambda: home)
        monkeypatch.setattr(bc, "real_profile_data_dir", lambda browser: str(source))
        monkeypatch.setattr(bc, "snapshot_real_profile", snapshot)
        monkeypatch.setattr(bt, "_use_real_profile", lambda: True)
        monkeypatch.setattr(bt, "_agent_browser_get_cdp", get_cdp)
        monkeypatch.setattr(bt, "_cdp_http_ready", lambda value: bool(value))
        monkeypatch.setattr(bt, "_cdp_owned_by_data_dir", lambda *_args: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/usr/bin/agent-browser")
        monkeypatch.setattr(bt.subprocess, "run", run)
        monkeypatch.setattr(bt, "_is_headed_mode", lambda: False)
        monkeypatch.setattr(
            "hermes_cli.browser_identity.read_browser_identity_config",
            lambda: _browser_cfg(),
        )

        lpg_cdp, lpg_error = bt._real_profile_cdp("lpg")
        personal_cdp, personal_error = bt._real_profile_cdp("personal")
        lpg_again, repeat_error = bt._real_profile_cdp("lpg")

        assert (lpg_error, personal_error, repeat_error) == (None, None, None)
        assert lpg_cdp == lpg_again
        assert lpg_cdp != personal_cdp
        assert len(launched) == 2
        assert len(set(launched)) == 2
        assert [call["source_profile"] for call in snapshots] == [
            "Profile 1",
            "Default",
        ]
        assert [call["identity"] for call in snapshots] == ["lpg", "personal"]
        assert len({call["identity"] for call in snapshots}) == 2
        assert len(list((home / "browser-profile" / "identities").iterdir())) == 2
        bt._real_profile_cdp_cache.clear()
