"""Tests for hermes_cli.doctor."""

import importlib.util
import os
import subprocess
import sys
import types
import io
import contextlib
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_constants
from hermes_cli import doctor
import hermes_constants
from hermes_cli import config as config_mod
import hermes_cli.gateway as gateway_cli
from hermes_cli import doctor as doctor_mod
from hermes_cli.doctor_config import _has_provider_env_config
from hermes_cli.doctor_report import Finding
import shutil
from hermes_cli import doctor_tools
from hermes_cli import doctor_state
from hermes_cli import doctor_platform
from hermes_cli import doctor_config


@pytest.fixture(autouse=True)
def _no_browser_downloads(monkeypatch):
    """Unrelated doctor --fix tests must not start an installer worker.

    Browser acquisition/readback is exercised with real temporary PM facts in
    test_browser_pm; tests here may replace this boundary deliberately.
    """
    def refuse(*args, **kwargs):
        raise RuntimeError("PM downloads disabled in doctor unit tests")

    monkeypatch.setattr("pm.client._request", refuse)


def _tls_out_normalized(out: str) -> str:
    """Doctor print matcher for TLS rows: key on words, not spacing."""
    return " ".join(out.lower().split())

class TestDoctorMemoryProviderSection:
    """The ◆ Memory Provider section should respect memory.provider config."""

    def _make_hermes_home(self, tmp_path, provider="", memory_config=None):
        """Create a minimal HERMES_HOME with config.yaml."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        import hermes_yaml as yaml
        config = dict(memory_config or {})
        if provider:
            config["provider"] = provider
        config = {"memory": config}
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        return home

    def _run_doctor_and_capture(
        self,
        monkeypatch,
        tmp_path,
        provider="",
        *,
        memory_config=None,
        stale_builtin_files=False,
    ):
        """Run doctor and capture stdout."""
        home = self._make_hermes_home(tmp_path, provider, memory_config)
        if stale_builtin_files:
            memories = home / "memories"
            memories.mkdir()
            (memories / "MEMORY.md").write_text("stale memory", encoding="utf-8")
            (memories / "USER.md").write_text("stale user", encoding="utf-8")
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        (tmp_path / "project").mkdir(exist_ok=True)

        # Stub tool availability (returns empty) so doctor runs past it
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        # Stub auth checks to avoid real API calls
        try:
            from hermes_cli import auth as _auth_mod
            monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
            monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
        except Exception:
            pass

        # Keep doctor from probing the AMBIENT gh CLI. A PATH lookup that
        # resolves (any dev box has gh) makes doctor shell out to
        # `gh auth status`, which both leaks the runner's real auth state
        # into the assertion surface and -- on Windows -- dies with WinError 5
        # when gh resolves to a Store/MSIX reparse-point shim. The gh-
        # specific doctor behaviors have their own dedicated tests below,
        # which mock gh explicitly.
        real_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda cmd, **kwargs: None if cmd == "gh" else real_which(cmd, **kwargs),
        )

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue()

    def test_no_provider_shows_builtin_ok(self, monkeypatch, tmp_path):
        out = self._run_doctor_and_capture(monkeypatch, tmp_path, provider="")
        assert "Memory Provider" in out
        assert "Built-in memory active" in out
        # Should NOT mention Honcho or Mem0 errors
        assert "Honcho API key" not in out
        assert "Mem0" not in out


    def test_catalog_provider_not_installed_names_install_command(self, monkeypatch, tmp_path):
        # mem0 left core for the plugin catalog: a home still configured for it gets the exact command.
        out = self._run_doctor_and_capture(monkeypatch, tmp_path, provider="mem0")
        section = out.split("Memory Provider", 1)[1][:600]
        assert "Built-in memory active" not in out
        assert "mem0 plugin not found" in section and "plugins install mem0" in section

    @pytest.mark.parametrize("memory_enabled", [False, True])
    def test_stale_builtin_files_reported_only_when_store_enabled(
        self, monkeypatch, tmp_path, memory_enabled
    ):
        # #100668: disabled built-in stores must not surface stale files as active.
        out = self._run_doctor_and_capture(
            monkeypatch,
            tmp_path,
            provider="mnemosyne",
            memory_config={
                "memory_enabled": memory_enabled,
                "user_profile_enabled": False,
            },
            stale_builtin_files=True,
        )

        assert ("MEMORY.md exists" in out) is memory_enabled
        assert "USER.md exists" not in out
        assert ("Built-in memory files disabled by config" in out) is not memory_enabled

class TestGitHubTokenCheck:
    """Tests for GitHub token / gh auth detection in doctor."""






    def test_gh_authenticated_on_gh_without_authenticated_json_field(self, monkeypatch):
        """gh 2.98+ dropped the `authenticated` field from `gh auth status --json`,
        so that invocation exits 1 even for a logged-in user. A logged-in user on
        such a gh must still be reported as authenticated."""
        from hermes_cli import doctor_state
        def gh_2_98(cmd, **kwargs):
            assert cmd == ["gh", "auth", "status"], cmd
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"Logged in to github.com")

        monkeypatch.setattr(subprocess, "run", gh_2_98)
        monkeypatch.setattr(doctor_tools, "_safe_which", lambda cmd: "/usr/bin/gh" if cmd == "gh" else None)
        assert doctor_state._gh_authenticated() is True

def _run_doctor_with_healthy_oauth_fallback(
    monkeypatch,
    tmp_path,
    *,
    env_key: str,
    bad_key: str,
    failing_host: str,
    minimax_oauth_status: dict,
    xai_oauth_status: dict | None = None,
) -> str:
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: nous\n"
        "  default: moonshotai/kimi-k2.6\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv(env_key, bad_key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_CN_API_KEY", raising=False)
    monkeypatch.setenv(env_key, bad_key)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    from hermes_cli import auth as _auth_mod

    monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", lambda: {"logged_in": True})
    monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
    monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: minimax_oauth_status)
    _xai_status = xai_oauth_status if xai_oauth_status is not None else {}
    monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: _xai_status)

    def fake_get(url, headers=None, timeout=None):
        status = 401 if failing_host in url else 200
        return types.SimpleNamespace(status_code=status)

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    return buf.getvalue()

def test_run_doctor_ignores_invalid_direct_keys_when_oauth_fallback_is_healthy(
    monkeypatch,
    tmp_path,
    env_key,
    bad_key,
    failing_host,
    minimax_oauth_status,
    xai_oauth_status,
    unexpected_issue,
):
    out = _run_doctor_with_healthy_oauth_fallback(
        monkeypatch,
        tmp_path,
        env_key=env_key,
        bad_key=bad_key,
        failing_host=failing_host,
        minimax_oauth_status=minimax_oauth_status,
        xai_oauth_status=xai_oauth_status,
    )

    assert "invalid API key" in out
    assert unexpected_issue not in out

class TestDoctorXaiOAuthStatus:
    """The ◆ Auth Providers section must show xAI OAuth login state.

    xAI OAuth is checked in a *separate* try/except block so that an import
    failure (or runtime exception) cannot silence the Nous / Codex / Gemini /
    MiniMax rows that were already printed above it.
    """

    def _run(self, monkeypatch, tmp_path, *, xai_auth_fn) -> str:
        """Run doctor with a controlled xAI auth callable; return stdout."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", xai_auth_fn)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue()


    def test_logged_in_does_not_emit_not_logged_in_on_xai_line(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": True},
        )
        assert "xAI OAuth" in out
        # The xAI OAuth line itself must say "(logged in)", not "(not logged in)".
        xai_line = next(l for l in out.splitlines() if "xAI OAuth" in l)
        assert "(logged in)" in xai_line
        assert "(not logged in)" not in xai_line


    def test_import_failure_does_not_affect_other_providers(self, monkeypatch, tmp_path):
        """Nous / Codex / Gemini / MiniMax rows must survive an xAI import failure."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", lambda: {"logged_in": True})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.delattr(_auth_mod, "get_xai_oauth_auth_status", raising=False)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        out = buf.getvalue()
        assert "Nous Portal auth" in out
        assert "logged in" in out

    def test_function_raises_does_not_crash_doctor(self, monkeypatch, tmp_path):
        """A runtime exception from get_xai_oauth_auth_status must be swallowed."""
        def _raise():
            raise RuntimeError("simulated xAI status failure")

        out = self._run(monkeypatch, tmp_path, xai_auth_fn=_raise)
        assert "Auth Providers" in out

class TestDoctorStaleMaxIterationsDrift:
    """Regression for #17534: a stale HERMES_MAX_ITERATIONS in .env shadows
    agent.max_turns in config.yaml. The repro symptom is config.yaml saying
    400 while the gateway activity line reads N/90. Doctor must detect the
    drift, and `--fix` must remove the .env ghost (config.yaml wins).

    The detector reads the .env FILE directly, NOT os.environ — the gateway
    startup bridge can already have overridden os.environ to the config value,
    so the ghost is only visible in the file.
    """

    def _run_config_section(self, monkeypatch, tmp_path, *, fix, ghost, cfg_turns,
                            os_environ_value=None):
        import contextlib
        import io
        from argparse import Namespace

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(
            f"agent:\n  max_turns: {cfg_turns}\n", encoding="utf-8"
        )
        env_lines = ["OPENAI_API_KEY=sk-test\n"]
        if ghost is not None:
            env_lines.append(f"HERMES_MAX_ITERATIONS={ghost}\n")
        (hermes_home / ".env").write_text("".join(env_lines), encoding="utf-8")

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
        monkeypatch.setattr(doctor_mod, "get_hermes_home", lambda: hermes_home)
        # Point the config helpers at the temp home.
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        if os_environ_value is not None:
            # Simulate the gateway bridge having already overridden os.environ.
            monkeypatch.setenv("HERMES_MAX_ITERATIONS", str(os_environ_value))
        else:
            monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)

        # Short-circuit at the Tool Availability stage — the drift check runs
        # well before it in the Configuration Files section.
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=fix))
        return buf.getvalue(), hermes_home

    def test_detects_drift_warn_only(self, monkeypatch, tmp_path):
        out, hermes_home = self._run_config_section(
            monkeypatch, tmp_path, fix=False, ghost=90, cfg_turns=400,
            os_environ_value=400,  # bridge contaminated os.environ
        )
        assert "HERMES_MAX_ITERATIONS=90" in out
        assert "shadows" in out
        # Warn-only must NOT mutate .env.
        assert "HERMES_MAX_ITERATIONS=90" in (hermes_home / ".env").read_text(encoding="utf-8")

    def test_fix_removes_ghost(self, monkeypatch, tmp_path):
        out, hermes_home = self._run_config_section(
            monkeypatch, tmp_path, fix=True, ghost=90, cfg_turns=400,
            os_environ_value=400,
        )
        assert "Removed stale HERMES_MAX_ITERATIONS" in out
        env_after = (hermes_home / ".env").read_text(encoding="utf-8")
        assert "HERMES_MAX_ITERATIONS" not in env_after
        assert "OPENAI_API_KEY=sk-test" in env_after  # other keys preserved


    def test_no_drift_when_ghost_absent(self, monkeypatch, tmp_path):
        out, _ = self._run_config_section(
            monkeypatch, tmp_path, fix=False, ghost=None, cfg_turns=400,
        )
        assert "shadows" not in out

class TestDoctorLegacyCustomProvidersResidue:
    """A legacy ``custom_providers`` list entry without a ``providers:`` twin lives on in the retired list
    store; doctor must name it and point at the move. Twins (URL modulo trailing slash /
    case) and non-list values are not this step's business."""

    def _run(self, tmp_path, yaml_text):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml_text, encoding="utf-8")
        finding = doctor_config.Finding()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_config._drift_legacy_custom_providers(finding, False, cfg)
        return buf.getvalue(), finding

    def test_orphan_entry_is_flagged_with_repair_instruction(self, tmp_path):
        out, finding = self._run(tmp_path, (
            "custom_providers:\n  - name: Local (8283)\n    base_url: http://127.0.0.1:8283/v1\n"
            "providers:\n  other:\n    api: http://127.0.0.1:8290/v1\n"))
        assert "Local (8283)" in out
        assert finding.manual_issues and "providers.<key>.api: http://127.0.0.1:8283/v1" in finding.manual_issues[0]
        assert finding.fixed == 0 and finding.issues == []  # warn-only: no --fix rewrite of config.yaml

    def test_twin_and_scalar_are_silent(self, tmp_path):
        out, finding = self._run(tmp_path, (
            "custom_providers:\n  - name: Local\n    base_url: http://127.0.0.1:8283/V1/\n"
            "providers:\n  local:\n    api: http://127.0.0.1:8283/v1\n"))
        assert out == "" and finding.manual_issues == []
        out, finding = self._run(tmp_path, "custom_providers: oops\n")
        assert out == "" and finding.manual_issues == []

class TestDoctorDeprecatedConfigAndEnv:
    """Doctor must surface deprecated/legacy config keys and env vars with
    modern replacements as non-failing warnings — without auto-migrating.
    """



    def test_collect_deprecated_env_vars_ignores_empty(self):
        assert doctor_config.collect_deprecated_env_vars({"TERMINAL_CWD": "  "}) == []
        assert doctor_config.collect_deprecated_env_vars({}) == []
        assert doctor_config.collect_deprecated_env_vars(None) == []

class TestStagedRuntimeVenv:
    """doctor_platform._check_python_environment distinguishes staged
    dependencies (pm's Venv().venv_dir() resolved state) from the active
    interpreter: under no-boot-through-venv bundled installs
    sys.prefix == sys.base_prefix by design, so "not active" must not read
    as "missing dependencies" when pm has staged the runtime venv."""

    @staticmethod
    def _stub_venv(monkeypatch, venv_dir):
        class _StubVenv:
            def venv_dir(self):
                return venv_dir

        monkeypatch.setattr("pm.packages.Venv", _StubVenv)

    # --- _staged_venv_dir: pm authority + provisioned-venv marker ---


    def test_resolved_path_without_venv_marker_is_not_staged(self, tmp_path, monkeypatch):
        empty = tmp_path / "venv"
        empty.mkdir()
        self._stub_venv(monkeypatch, empty)

        assert doctor_platform._staged_venv_dir() is None

    def test_unreadable_pm_degrades_to_none(self, monkeypatch):
        class _BrokenVenv:
            def venv_dir(self):
                raise RuntimeError("pm unavailable")

        monkeypatch.setattr("pm.packages.Venv", _BrokenVenv)

        assert doctor_platform._staged_venv_dir() is None

    # --- the check's staged-vs-active rows ---


    def test_nothing_staged_keeps_legacy_interpreter_probe(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_platform, "_staged_venv_dir", lambda: None)
        monkeypatch.setattr(doctor_platform.sys, "prefix", "/usr")
        monkeypatch.setattr(doctor_platform.sys, "base_prefix", "/usr")

        doctor_platform._check_python_environment(False)

        out = capsys.readouterr().out
        assert "Runtime venv staged" not in out
        assert "Not in virtual environment" in out

def test_run_doctor_reports_shadowed_lightpanda_engine(monkeypatch, tmp_path):
    helper = TestDoctorMemoryProviderSection()

    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: True, raising=False)
    monkeypatch.setattr(
        "tools.browser_tool_lightpanda_fallback.lightpanda_engine_status",
        lambda: (False, "cloud provider Browserbase is selected"), raising=False,
    )
    out = helper._run_doctor_and_capture(monkeypatch, tmp_path)
    assert "browser.engine=lightpanda is shadowed" in out
    assert "Browserbase" in out

def test_run_doctor_warns_when_lightpanda_binary_missing(monkeypatch, tmp_path):
    helper = TestDoctorMemoryProviderSection()

    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: True, raising=False)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback.lightpanda_engine_status",
                        lambda: (True, "Hermes spawns `lightpanda serve`"), raising=False)
    monkeypatch.setattr("tools.browser_lightpanda.find_lightpanda_binary", lambda: None)
    out = helper._run_doctor_and_capture(monkeypatch, tmp_path)
    assert "Lightpanda selected but binary not found" in out

def test_docker_daemon_probe_uses_version_not_info(monkeypatch):
    """`docker info` needs the /info endpoint, which socket proxies commonly block, so doctor reported
    "daemon not running" against a working DOCKER_HOST (#72927). `docker version` (/version) is what the
    backend itself probes with."""
    calls: list = []
    monkeypatch.setattr(doctor_tools, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(doctor_tools, "_run_ok", lambda cmd, timeout, **kw: calls.append(cmd) or True)
    monkeypatch.setattr(doctor_tools, "_require", lambda *a, **k: None)

    doctor_tools._check_docker_backend("docker", False, [])

    assert calls == [["/usr/bin/docker", "version"]]

def test_doctor_reports_auxiliary_blocks_that_do_not_resolve(tmp_path, monkeypatch):
    """A routed auxiliary.<task> block that the runtime resolver rejects is a doctor finding, not a
    silent fall-back to the main model (#116055); a resolvable one is not flagged."""
    import hermes_yaml as yaml
    from hermes_cli import doctor_config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"auxiliary": {
        "background_review": {"provider": "no-such-provider", "model": "m"},
        "compression": {"provider": "openai", "model": "gpt-x", "base_url": "https://gateway.example/v1", "api_key": "gw"},
    }}))
    issues = []
    doctor_config._validate_auxiliary_config(cfg_file, issues)
    assert len(issues) == 1 and "auxiliary.background_review" in issues[0] and "no-such-provider" in issues[0]

class TestMacOSTCCGrants:
    """macOS TCC grant persistence check (#86385): a cdhash-pinned DR (pre-#73681
    local builds) silently resets Screen Recording/Accessibility grants on every
    rebuild while the Settings toggle stays ON."""

    @staticmethod
    def _darwin_bundle(monkeypatch, tmp_path, dr):
        monkeypatch.setattr(doctor_platform, "_desktop_app_bundle", lambda: tmp_path / "Hermes.app")
        if dr is not ...:
            monkeypatch.setattr(doctor_platform, "_macos_desktop_dr", lambda app: dr)

    def test_silent_without_desktop_bundle(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_platform, "_desktop_app_bundle", lambda: None)
        doctor_platform.check_macos_tcc_grants()
        assert capsys.readouterr().out == ""

    def test_warns_on_cdhash_pinned_dr(self, monkeypatch, capsys, tmp_path):
        self._darwin_bundle(
            monkeypatch, tmp_path,
            'designated => identifier "com.nousresearch.hermes" and cdhash H"97e692f3890f781fa0ad5ad6cb9d769cfaf42628"',
        )
        doctor_platform.check_macos_tcc_grants()
        out = capsys.readouterr().out
        assert "TCC grants will reset after every update" in out
        assert "hermes update" in out
        assert "signing identity is stable" not in out

    def test_identifier_dr_is_stable_with_upgrade_hint_and_repair_info(self, monkeypatch, capsys, tmp_path):
        self._darwin_bundle(monkeypatch, tmp_path, 'designated => identifier "com.nousresearch.hermes"')
        doctor_platform.check_macos_tcc_grants()
        out = capsys.readouterr().out
        assert "TCC signing identity is stable" in out
        assert "--setup-tcc-identity" in out
        assert "tccutil reset ScreenCapture com.nousresearch.hermes" in out

    def test_certificate_anchored_dr_is_stable_without_upgrade_hint(self, monkeypatch, capsys, tmp_path):
        self._darwin_bundle(
            monkeypatch, tmp_path,
            'designated => identifier "com.nousresearch.hermes" and certificate root = H"aabbcc"',
        )
        doctor_platform.check_macos_tcc_grants()
        out = capsys.readouterr().out
        assert "TCC signing identity is stable" in out
        assert "--setup-tcc-identity" not in out
        assert "tccutil reset ScreenCapture com.nousresearch.hermes" in out

    @pytest.mark.parametrize("failure", ["none", "empty", "timeout", "no_codesign"])
    def test_unreadable_dr_warns_and_never_claims_stable(self, monkeypatch, capsys, tmp_path, failure):
        """codesign failing, hanging, missing or printing nothing degrades to a
        warning; an empty DR must not false-positive as a stable identity."""
        if failure in ("none", "empty"):
            self._darwin_bundle(monkeypatch, tmp_path, None if failure == "none" else "")
        else:
            self._darwin_bundle(monkeypatch, tmp_path, ...)
            if failure == "timeout":
                monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/codesign")

                def _timeout(*args, **kwargs):
                    raise subprocess.TimeoutExpired(cmd=["codesign"], timeout=15)

                monkeypatch.setattr(subprocess, "run", _timeout)
            else:
                monkeypatch.setattr(shutil, "which", lambda _name: None)
        doctor_platform.check_macos_tcc_grants()
        out = capsys.readouterr().out
        assert "could not read code-signing requirement" in out
        assert "stable" not in out

def test_cron_store_check_reports_writability_and_low_space(
        monkeypatch, tmp_path, capsys, probe_error, free, root, expected, issues):
    from cron import store_health

    (tmp_path / "cron").mkdir()
    monkeypatch.setattr(doctor, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(store_health, "probe_store", lambda _d: probe_error)
    monkeypatch.setattr(store_health.shutil, "disk_usage", lambda _d: SimpleNamespace(free=free))
    if hasattr(os, "geteuid"):  # rows run as the user they name, whoever runs the suite
        monkeypatch.setattr(os, "geteuid", lambda: 0 if root else 1000)
        monkeypatch.setattr(os, "statvfs", lambda _d: SimpleNamespace(f_bfree=1 << 20, f_frsize=4096))
    finding = doctor_state._check_cron_store(False)
    assert expected in capsys.readouterr().out
    assert len(finding.issues) == len(issues) and all(any(s in i for i in finding.issues) for s in issues)

def test_profile_alias_status_follows_the_wrapper_the_profile_really_has(monkeypatch, tmp_path):
    """#131753: doctor said "no alias" for every Windows profile (``.bat`` wrapper) and for any
    profile reachable only through a custom alias name; a bare (pre-``.bat`` on Windows) wrapper counts."""
    import hermes_cli.gateway_migrate as gm
    from hermes_cli import profiles

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    monkeypatch.setattr(gm, "duplicate_credential_findings", list)
    for name in ("dev", "ops", "work", "old"):
        (tmp_path / ".hermes" / "profiles" / name).mkdir(parents=True)
        (tmp_path / ".hermes" / "profiles" / name / "config.yaml").write_text("model: m\n", encoding="utf-8")
    profiles.create_wrapper_script("dev")
    profiles.create_wrapper_script("w", target="work")
    (tmp_path / ".local" / "bin" / "old").write_text('#!/bin/sh\nexec hermes -p old "$@"\n', encoding="utf-8")
    for foreign in ("ops.bat", "old.bat"):  # same-named non-Hermes commands
        (tmp_path / ".local" / "bin" / foreign).write_text("@echo off\r\nnotepad %*\r\n", encoding="utf-8")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        doctor_state._check_profiles(False)
    lines = out.getvalue().splitlines()
    status = {name: next(ln for ln in lines if f" {name}:" in ln) for name in ("dev", "ops", "work", "old")}

    assert not any("no alias" in status[n] for n in ("dev", "work", "old"))
    assert "no alias" in status["ops"]  # a foreign ops.bat is not ops's alias
    old = next(p for p in profiles.list_profiles() if p.name == "old")
    assert old.alias_path == tmp_path / ".local" / "bin" / "old"
