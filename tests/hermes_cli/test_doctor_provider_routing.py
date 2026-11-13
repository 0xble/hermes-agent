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

def test_check_certificates_exercises_real_tls_policy(monkeypatch, capsys):
    """install_truststore True: the check exercises the actual policy
    (agent.ssl_verify platform trust store), reports TLS as configured, and
    never presents context construction as certificate verification."""
    monkeypatch.setattr("agent.ssl_verify.install_truststore", lambda: True)

    doctor_platform.check_certificates()

    out = _tls_out_normalized(capsys.readouterr().out)
    assert "✓" in out
    assert "tls" in out
    assert "skipped" not in out
    assert "verification active" not in out  # configuration, not proof of validation
    assert "platform trust" in out or "configured" in out

def test_check_certificates_fallback_names_openssl_without_platform_trust_claim(monkeypatch, capsys):
    """install_truststore False + a constructible context is an OpenSSL fallback:
    the output names the actual fallback, stays available, and no ✓ row claims
    the platform trust store is active."""
    monkeypatch.setattr("agent.ssl_verify.install_truststore", lambda: False)

    doctor_platform.check_certificates()

    raw = capsys.readouterr().out
    out = _tls_out_normalized(raw)
    assert "⚠" in raw
    assert "trust store" in out
    assert "openssl" in out  # names the actual fallback
    assert "verification active" not in out  # construction proves nothing verified
    assert "available" in out  # status is context available, not verification proven
    assert not any(
        line.startswith("✓") and "platform trust" in line
        for line in raw.splitlines()
    )

class TestDoctorPlatformHints:
    def test_sqlite_upgrade_hint_recreates_docker_containers(self, monkeypatch):
        monkeypatch.setattr(config_mod, "detect_install_method", lambda _root: "docker")

        hint = doctor_platform._sqlite_upgrade_hint()

        assert "docker pull nousresearch/hermes-agent:latest" in hint
        assert "hermes update" not in hint


    def test_sqlite_upgrade_hint_apt_stamp_names_termux_external_update(self):
        # The "apt" install method is Termux APT by contract (config.py
        # _UPDATE_COMMAND_BY_METHOD). Deployment kinds are first-class: doctor
        # reports the correct external update command instead of routing an
        # out-of-place method through the in-place `hermes update` path.
        hint = doctor_platform._sqlite_upgrade_hint("apt")

        assert "run `pkg upgrade hermes-agent`" in hint
        assert "hermes update" not in hint

    def test_sqlite_upgrade_hint_preserves_nix_guidance_as_prose(self):
        from hermes_cli.config import recommended_update_command_for_method

        guidance = recommended_update_command_for_method("nix")
        hint = doctor_platform._sqlite_upgrade_hint("nix")

        assert guidance in hint
        assert f"run `{guidance}`" not in hint
        assert "hermes update" not in hint

class TestProviderEnvDetection:
    def test_detects_openai_api_key(self):
        content = "OPENAI_BASE_URL=http://localhost:1234/v1\nOPENAI_API_KEY=***"
        assert _has_provider_env_config(content)


    def test_returns_false_when_no_provider_settings(self):
        content = "TERMINAL_ENV=local\n"
        assert not _has_provider_env_config(content)

class TestDoctorToolAvailabilitySummary:
    def test_missing_api_key_summary_ignores_disabled_toolsets(self, monkeypatch):
        unavailable = [
            {"name": "rl", "missing_vars": ["TINKER_API_KEY"]},
            {"name": "web", "missing_vars": ["EXA_API_KEY"]},
        ]
        monkeypatch.setattr(doctor_tools, "_enabled_cli_toolsets_for_doctor", lambda: {"web"})

        filtered = doctor_tools._missing_api_key_toolsets_for_summary(unavailable)

        assert [item["name"] for item in filtered] == ["web"]

    def test_image_gen_without_provider_reports_setup_hint_not_system_dependency(self, monkeypatch):
        """image_gen declares no single env var (FAL / managed Nous / plugin providers); an
        unconfigured backend is a setup problem and must say so, and it counts toward the
        'run hermes setup' summary like any missing key (#9516)."""
        unavailable = [{"name": "image_gen", "env_vars": [], "tools": ["image_generate"]},
                       {"name": "homeassistant", "env_vars": [], "tools": []}]
        monkeypatch.setattr(doctor_tools, "_enabled_cli_toolsets_for_doctor", lambda: {"image_gen"})
        monkeypatch.setattr(doctor_tools, "_apply_doctor_tool_availability_overrides", lambda a, u: (a, u))
        monkeypatch.setattr(doctor_tools, "_doctor_web_capability_rows", list)
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda: ([], unavailable),
            TOOLSET_REQUIREMENTS={"image_gen": {"name": "image_gen"}, "homeassistant": {"name": "homeassistant"}},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f = doctor_tools._check_tool_availability(False)
        out = buf.getvalue()

        image_line = next(line for line in out.splitlines() if "image_gen" in line)
        assert "hermes tools" in image_line and "system dependency" not in image_line and "unavailable" in image_line
        assert "system dependency not met" in next(line for line in out.splitlines() if "homeassistant" in line)
        assert any("hermes setup" in issue for issue in f.issues)

    def test_web_capability_rows_warn_when_selected_provider_not_ready(self, monkeypatch):
        """#78412: selected firecrawl with is_available=False must warn."""
        class _Unavailable:
            name = "firecrawl"

            def is_available(self):
                return False

        unavailable = _Unavailable()
        monkeypatch.setattr(
            "agent.web_search_registry.get_active_search_provider",
            lambda: unavailable,
        )
        monkeypatch.setattr(
            "agent.web_search_registry.get_active_extract_provider",
            lambda: unavailable,
        )

        rows = doctor_tools._doctor_web_capability_rows()
        assert rows
        assert all(status == "warn" for status, _, _ in rows)

    def test_web_capability_rows_ok_when_provider_ready(self, monkeypatch):
        class _Ready:
            name = "ddgs"

            def is_available(self):
                return True

        ready = _Ready()
        monkeypatch.setattr(
            "agent.web_search_registry.get_active_search_provider",
            lambda: ready,
        )
        monkeypatch.setattr(
            "agent.web_search_registry.get_active_extract_provider",
            lambda: ready,
        )

        rows = doctor_tools._doctor_web_capability_rows()
        assert rows and all(status == "ok" for status, _, _ in rows)

class TestDoctorEnvFileEncoding:
    """Regression for #18637 (bug 3): `hermes doctor` crashed on Windows
    Chinese locale (GBK) because `.env` was read with Path.read_text(encoding="utf-8") which
    defaults to the system locale encoding, not UTF-8."""

    def test_doctor_reads_env_as_utf8_even_when_locale_is_not_utf8(
        self, monkeypatch, tmp_path
    ):
        import pathlib

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        # Write a UTF-8 .env containing an em dash (U+2014 = e2 80 94). The
        # 0x94 byte is exactly the one the issue reporter hit: it's invalid
        # as a GBK trailing byte in this position, so locale-default reads
        # raise UnicodeDecodeError on Chinese Windows.
        env_path = hermes_home / ".env"
        env_path.write_text(
            "OPENAI_API_KEY=sk-test  # em-dash here — should not crash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)

        orig_read_text = pathlib.Path.read_text

        def gbk_like_read_text(self, encoding=None, errors=None, **kwargs):
            # Simulate a GBK locale: refuse to decode this specific UTF-8
            # .env unless the caller pins a UTF-8 codec. utf-8-sig is the
            # sanctioned read encoding (Windows tools BOM-prefix files it
            # touches); it decodes BOM-less UTF-8 identically.
            if self == env_path and encoding not in ("utf-8", "utf-8-sig"):
                raise UnicodeDecodeError(
                    "gbk", b"\x94", 0, 1, "illegal multibyte sequence"
                )
            return orig_read_text(self, encoding=encoding, errors=errors, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", gbk_like_read_text)

        # Short-circuit the expensive tool-availability probe — we only
        # need doctor to reach the .env read without crashing.
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        # Run doctor. If the .env read still uses locale encoding, this
        # raises UnicodeDecodeError and the test fails.
        with pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=False))


    def test_doctor_reads_invalid_utf8_env_via_latin1_fallback(
        self, monkeypatch, tmp_path
    ):
        """cp1252/latin-1 .env with ASCII provider hints must not abort doctor."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        env_path = hermes_home / ".env"
        # 0xff is invalid UTF-8; latin-1 decodes it. Keep an ASCII provider key
        # so the scan still reports a configured endpoint/key.
        env_path.write_bytes(b"OPENAI_API_KEY=sk-test\xff\n")

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        with pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=False))

class TestDoctorToolAvailabilityOverrides:


    def test_marks_kanban_available_only_when_missing_worker_env_gate(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        available, unavailable = doctor_tools._apply_doctor_tool_availability_overrides(
            [],
            [{"name": "kanban", "env_vars": [], "tools": ["kanban_show"]}],
        )

        assert available == ["kanban"]
        assert unavailable == []

    def test_leaves_kanban_unavailable_when_worker_env_is_set(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "probe")
        kanban_entry = {"name": "kanban", "env_vars": [], "tools": ["kanban_show"]}

        available, unavailable = doctor_tools._apply_doctor_tool_availability_overrides(
            [],
            [kanban_entry],
        )

        assert available == []
        assert unavailable == [kanban_entry]

@pytest.mark.parametrize("running_in_container", (False, True))
def test_doctor_reports_vercel_backend_diagnostics(monkeypatch, tmp_path, running_in_container):
    monkeypatch.setattr(hermes_constants, "is_container", lambda: running_in_container)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_VERCEL_RUNTIME", "python3.13")
    monkeypatch.setenv("TERMINAL_CONTAINER_DISK", "2048")
    monkeypatch.setenv("VERCEL_TOKEN", "super-secret-value")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_ID", raising=False)
    monkeypatch.setenv("VERCEL_TEAM_ID", "team")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "vercel" else None)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "VERCEL_PROJECT_ID" in out  # names the missing auth var
    assert "super-secret-value" not in out

def test_run_doctor_accepts_named_provider_from_providers_section(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)

    import hermes_yaml as yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "volcengine-plan",
                    "default": "doubao-seed-2.0-code",
                },
                "providers": {
                    "volcengine-plan": {
                        "name": "volcengine-plan",
                        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
                        "default_model": "doubao-seed-2.0-code",
                        "models": {"doubao-seed-2.0-code": {}},
                    }
                },
            }
        )
    , encoding="utf-8")

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'volcengine-plan' is not a recognised provider" not in out

def test_run_doctor_accepts_stable_key_when_provider_name_differs(
    monkeypatch, tmp_path
):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:local-127.0.0.1:11434\n"
        "  default: qwen3.5:9b\n"
        "providers:\n"
        "  local-127.0.0.1:11434:\n"
        "    name: Local Ollama\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    default_model: qwen3.5:9b\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert (
        "model.provider 'custom:local-127.0.0.1:11434' is not a recognised provider"
        not in out
    )
    assert "model.provider 'custom:local-127.0.0.1:11434' is unknown" not in out

def test_run_doctor_accepts_bare_custom_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: local-model\n"
        "  base_url: http://localhost:8000/v1\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'custom' is not a recognised provider" not in out

def test_run_doctor_flags_missing_credentials_for_active_openrouter_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-4.1-mini\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    try:
        from hermes_cli import auth as _auth_mod

        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_gemini_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'openrouter' is set but no API key is configured" in out

@pytest.mark.parametrize(
    ("provider", "default_model"),
    [
        ("ai-gateway", "anthropic/claude-sonnet-4.6"),
        ("opencode-zen", "anthropic/claude-sonnet-4.6"),
        ("kilocode", "anthropic/claude-sonnet-4.6"),
        ("kimi-coding", "kimi-k2"),
        ("nvidia", "qwen/qwen3.5-122b-a10b"),
        ("moa", "anthropic/claude-sonnet-4.6"),
    ],
)
def test_run_doctor_accepts_hermes_provider_ids_that_catalog_aliases(
    monkeypatch, tmp_path, provider, default_model
):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        f"  provider: {provider}\n"
        f"  default: {default_model}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert f"model.provider '{provider}' is not a recognised provider" not in out
    assert f"model.provider '{provider}' is unknown" not in out
    if provider in {"ai-gateway", "opencode-zen", "kilocode", "nvidia"}:
        assert (
            f"model.default '{default_model}' uses a vendor/model slug but provider is '{provider}'"
            not in out
        )

def test_run_doctor_accepts_vendor_slugs_for_named_custom_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:hpc-ai\n"
        "  default: deepseek/deepseek-v4-flash\n"
        "custom_providers:\n"
        "  - name: hpc-ai\n"
        "    base_url: https://hpc-ai.example/v1\n"
        "    api_key: test-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'custom:hpc-ai' is not a recognised provider" not in out
    assert "model.provider 'custom:hpc-ai' is unknown" not in out
    assert (
        "model.default 'deepseek/deepseek-v4-flash' uses a vendor/model slug but provider is "
        "'custom:hpc-ai'"
        not in out
    )
    assert "Either set model.provider to 'openrouter', or drop the vendor prefix." not in out

@pytest.mark.parametrize(
    ("base_url", "expects_warning"),
    [
        ("http://localhost:20128/v1", False),
        ("https://api.openai.com/v1", True),
    ],
)
def test_run_doctor_vendor_slug_policy_for_openai_api_endpoint(
    monkeypatch, tmp_path, base_url, expects_warning
):
    """openai-api behind a custom router owns a vendor/model namespace (#69912); the real
    OpenAI endpoint keeps the warning."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openai-api\n"
        "  default: nvidia/z-ai/glm-5.2\n"
        f"  base_url: {base_url}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    warning = (
        "model.default 'nvidia/z-ai/glm-5.2' uses a vendor/model slug "
        "but provider is 'openai-api'"
    )
    assert (warning in buf.getvalue()) is expects_warning

def test_run_doctor_accepts_kimi_coding_cn_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("KIMI_CN_API_KEY=***\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: kimi-coding-cn\n"
        "  default: kimi-k2.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status_local", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_auth_status", lambda provider: {"logged_in": True})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'kimi-coding-cn' is not a recognised provider" not in out

def _doctor_env_for_browser(monkeypatch, tmp_path):
    """Shared fixture setup for the Node + Chromium rows of run_doctor."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setenv("PREFIX", "/usr")
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd: "/usr/bin/node" if cmd in {"node", "npm"} else None,
    )

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

def test_run_doctor_reports_installed_chromium(monkeypatch, tmp_path):
    _doctor_env_for_browser(monkeypatch, tmp_path)

    monkeypatch.setattr("hermes_cli.browser_runtime.chromium_executable", lambda **_kw: "/pm/chromium/chrome")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Chromium" in out
    assert "/pm/chromium/chrome" in out
    assert "Chromium not installed" not in out

def test_doctor_fix_does_not_claim_success_without_published_binary(monkeypatch, tmp_path):
    from hermes_cli import doctor_tools
    _doctor_env_for_browser(monkeypatch, tmp_path)

    monkeypatch.setattr("hermes_cli.browser_runtime.chromium_executable", lambda **_kw: None)
    monkeypatch.setattr("hermes_cli.doctor_tools._is_termux", lambda: False)
    monkeypatch.setattr("pm.ensure", lambda *a, **kw: None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_tools._check_chromium(True)
    out = buf.getvalue()

    assert "Chromium not installed" in out
    assert "hermes pm install chromium" in out

def test_doctor_fix_reports_pm_install_failure(monkeypatch, tmp_path):
    from hermes_cli import doctor_tools
    import pm
    _doctor_env_for_browser(monkeypatch, tmp_path)

    def refuse(*args, **kwargs):
        raise pm.InstallError("chromium", "offline")

    monkeypatch.setattr("hermes_cli.browser_runtime.chromium_executable", lambda **_kw: None)
    monkeypatch.setattr("hermes_cli.doctor_tools._is_termux", lambda: False)
    monkeypatch.setattr(pm, "ensure", refuse)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_tools._check_chromium(True)
    out = buf.getvalue()

    assert "Chromium install failed" in out
    assert "offline" in out

@pytest.mark.parametrize("browser_yaml,reason", [
    ("browser:\n  cdp_url: http://127.0.0.1:9222\n", "CDP override"),
    ("browser:\n  engine: lightpanda\n", "lightpanda"),
    ("browser:\n  cloud_provider: browserbase\n", "browserbase"),
])
def test_missing_chromium_is_info_when_config_needs_none(monkeypatch, tmp_path, browser_yaml, reason):
    """A CDP override, Lightpanda or a cloud provider runs no local Chromium: no warning row."""
    from hermes_cli import doctor_tools
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(browser_yaml, encoding="utf-8")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr("hermes_cli.browser_runtime.chromium_executable", lambda **_kw: None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_tools._check_chromium(False)
    out = buf.getvalue()

    assert "not needed" in out and reason in out
    assert "hermes pm install chromium" not in out

def test_run_doctor_kimi_cn_env_is_detected_and_probe_is_null_safe(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("KIMI_CN_API_KEY=sk-test\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("KIMI_CN_API_KEY", "sk-test")

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except Exception:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return types.SimpleNamespace(status_code=200)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "API key or custom endpoint configured" in out
    assert "Kimi / Moonshot (China)" in out
    assert "str expected, not NoneType" not in out
    assert any(url == "https://api.moonshot.cn/v1/models" for url, _, _ in calls)

def test_run_doctor_dashscope_retries_china_endpoint_after_intl_unauthorized(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("DASHSCOPE_API_KEY=sk-test\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except ImportError:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        status = 200 if "dashscope.aliyuncs.com" in url else 401
        return types.SimpleNamespace(status_code=status)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Alibaba/DashScope" in out
    assert "invalid API key" not in out
    assert any(
        url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models"
        for url, _, _ in calls
    )
    assert any(
        url == "https://dashscope.aliyuncs.com/compatible-mode/v1/models"
        for url, _, _ in calls
    )

@pytest.mark.parametrize("base_url", [None, "https://opencode.ai/zen/go/v1"])
def test_run_doctor_opencode_go_skips_invalid_models_probe(monkeypatch, tmp_path, base_url):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("OPENCODE_GO_API_KEY=***\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    if base_url:
        monkeypatch.setenv("OPENCODE_GO_BASE_URL", base_url)
    else:
        monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", dict)
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", dict)
    except ImportError:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return types.SimpleNamespace(status_code=200)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert any(
        "OpenCode Go" in line and "(key configured)" in line
        for line in out.splitlines()
    )
    assert not any(url == "https://opencode.ai/zen/go/v1/models" for url, _, _ in calls)
    assert not any("opencode" in url.lower() and "models" in url.lower() for url, _, _ in calls)

