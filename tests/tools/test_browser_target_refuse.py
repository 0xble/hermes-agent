import json
from unittest.mock import Mock

import pytest

from tools import browser_camofox
from tools import browser_task_cdp
from tools import browser_tool as bt
from tools import browser_tool_cdp
from tools import browser_tool_cloud as cloud
from tools import browser_tool_session as session
from tools import browser_use_cli

CDP_TARGET = "http://127.0.0.1:9222"
CAMOFOX_TARGET = "http://127.0.0.1:9377"

# Browser defaults before browser.target / browser.fallback were added (base ec949b9eb).
BASE_BROWSER_DEFAULTS = {
    "backend": "",
    "snapshot_threshold": 15000,
    "record_sessions": False,
    "allow_unsafe_evaluate": False,
    "restrict_evaluate": False,
    "inactivity_timeout": 120,
    "command_timeout": 30,
    "headed": False,
    "allow_private_urls": False,
    "engine": "auto",
    "executable_path": "",
    "user_data_dir": "",
    "chromium_args": [],
    "auto_local_for_private_urls": True,
    "cdp_url": "",
    "use_real_profile": False,
    "real_profile_autoclose": False,
    "real_profile_pin": "",
    "dialog_policy": "must_respond",
    "dialog_timeout_s": 300,
    "camofox": {
        "managed_persistence": False,
        "user_id": "",
        "session_key": "",
        "adopt_existing_tab": False,
        "uploads_dir": "",
        "rewrite_loopback_urls": False,
        "loopback_host_alias": "host.docker.internal",
    },
    "extension_control": {"enabled": False, "developer_mode": False},
}

_UNSET = object()


def _config(monkeypatch, *, target=CDP_TARGET, fallback: object = "refuse", provider=None, **extra):
    """Drive every ``browser.*`` read (policy, target, cloud_provider, cdp_url) from one raw config."""
    browser = {"target": target, "cloud_provider": provider, **extra}
    if fallback is not _UNSET:
        browser["fallback"] = fallback
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": browser})


def _no_alternative(monkeypatch):
    """Every non-pinned selection path fails the test if it is reached."""
    monkeypatch.setattr(session, "_create_local_session", lambda *a, **k: pytest.fail("local fallback"))
    monkeypatch.setattr(session, "_create_cloud_session_or_fallback", lambda *a, **k: pytest.fail("cloud fallback"))
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: pytest.fail("cloud provider lookup"))
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: pytest.fail("CDP alternative"))


def _isolate_sessions(monkeypatch):
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle._update_session_activity", lambda t: None)


def test_refuse_pins_session_before_cloud_local_or_cdp(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(cloud, "pinned_browser_target_or_raise", lambda: CDP_TARGET)
    monkeypatch.setattr(session._cdp, "_resolve_cdp_override", lambda target: target)
    _no_alternative(monkeypatch)

    result = session._create_session_for_key("task", force_local=True)

    assert result["cdp_url"] == CDP_TARGET
    assert result["pinned_target"] == CDP_TARGET


def test_default_policy_keeps_local_selection(monkeypatch):
    _config(monkeypatch, target="", fallback="allow")
    local = {"session_name": "local", "features": {"local": True}}
    monkeypatch.setattr(session, "_create_local_session", lambda *args, **kwargs: local)
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: None)

    assert session._create_session_for_key("task", force_local=False) is local


def test_absent_fallback_key_keeps_legacy_selection(monkeypatch):
    _config(monkeypatch, target="", fallback=_UNSET)
    local = {"session_name": "local", "features": {"local": True}}
    monkeypatch.setattr(session, "_create_local_session", lambda *args, **kwargs: local)
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: None)

    assert cloud.browser_fallback_policy() == "allow"
    assert session._create_session_for_key("task", force_local=False) is local


# --- P1.1: refuse with an empty/unset target fails closed on new sessions -----------------------

@pytest.mark.parametrize("target", ["", None])
def test_refuse_with_empty_target_fails_closed_on_new_session(monkeypatch, target):
    _config(monkeypatch, target=target)
    _no_alternative(monkeypatch)

    with pytest.raises(cloud.BrowserTargetUnavailable) as excinfo:
        session._create_session_for_key("task", force_local=False)

    assert "no pinned browser target configured" in excinfo.value.reason


def test_refuse_with_empty_target_fails_closed_through_get_session_info(monkeypatch):
    _config(monkeypatch, target="")
    _isolate_sessions(monkeypatch)
    _no_alternative(monkeypatch)

    with pytest.raises(cloud.BrowserTargetUnavailable):
        session._get_session_info("task")
    assert bt._active_sessions == {}


def test_refuse_with_empty_target_is_structured_for_browser_commands(monkeypatch):
    _config(monkeypatch, target="")
    _isolate_sessions(monkeypatch)
    _no_alternative(monkeypatch)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})

    result = session._run_browser_command("task", "snapshot")

    assert result["success"] is False
    assert result["error_type"] == "browser_target_unavailable"


def test_pinned_target_or_raise_raises_on_empty_target(monkeypatch):
    _config(monkeypatch, target="")
    with pytest.raises(cloud.BrowserTargetUnavailable):
        cloud.pinned_browser_target_or_raise()


# --- P1.2: lookup failures and unrecognised values fail closed ------------------------------------

@pytest.mark.parametrize("value", ["refues", "Refuse ", "deny", "none", True, 0, None, "allow-all"])
def test_unrecognised_fallback_value_refuses(monkeypatch, value):
    _config(monkeypatch, fallback=value)
    expected = "refuse"
    assert cloud.browser_fallback_policy() == expected
    assert cloud.pinned_browser_target_refuses() is True


@pytest.mark.parametrize("value", ["allow", "ALLOW", " Allow "])
def test_explicit_allow_is_the_only_value_that_allows(monkeypatch, value):
    _config(monkeypatch, fallback=value)
    assert cloud.browser_fallback_policy() == "allow"
    assert cloud.pinned_browser_target_refuses() is False


def test_config_read_exception_refuses(monkeypatch):
    def boom():
        raise OSError("config unavailable")

    monkeypatch.setattr("hermes_cli.config.read_raw_config", boom)
    assert cloud.browser_fallback_policy() == "refuse"
    assert cloud.pinned_browser_target_refuses() is True


def test_failed_config_read_refuses(monkeypatch):
    from hermes_cli.config_read_errors import FailedConfigRead

    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: FailedConfigRead(error=OSError("EIO")))
    assert cloud.browser_fallback_policy() == "refuse"


def test_non_mapping_browser_section_refuses(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": "oops"})
    assert cloud.browser_fallback_policy() == "refuse"


def test_lookup_failure_refuses_new_sessions(monkeypatch):
    def boom():
        raise OSError("config unavailable")

    monkeypatch.setattr("hermes_cli.config.read_raw_config", boom)
    _no_alternative(monkeypatch)

    with pytest.raises(cloud.BrowserTargetUnavailable):
        session._create_session_for_key("task", force_local=False)


def test_browser_use_cli_policy_lookup_failure_refuses(monkeypatch):
    monkeypatch.setattr(cloud, "pinned_browser_target_refuses", Mock(side_effect=RuntimeError("lookup failed")))
    assert browser_use_cli._cloud_refuses_pinned_target() is True


def test_browser_use_cli_policy_lookup_failure_refuses_with_import_error(monkeypatch):
    monkeypatch.setattr(browser_use_cli.importlib, "import_module", Mock(side_effect=ImportError("gone")))
    assert browser_use_cli._cloud_refuses_pinned_target() is True


def test_browser_use_cli_unrecognised_value_refuses(monkeypatch):
    _config(monkeypatch, fallback="refues", target="")
    error = browser_use_cli._resolve_backend_cdp({}, "task")
    assert error is not None
    assert json.loads(error)["error_type"] == "browser_target_unavailable"


# --- P1.3: Lightpanda -> Chrome fallback never runs under refuse ----------------------------------

@pytest.mark.parametrize(
    "command, result",
    [
        ("snapshot", {"success": True, "data": {"snapshot": ""}}),
        ("screenshot", {"success": True, "data": {"path": "/nonexistent/shot.png"}}),
    ],
)
def test_lightpanda_chrome_fallback_is_skipped_under_refuse(monkeypatch, command, result):
    _config(monkeypatch)
    _isolate_sessions(monkeypatch)
    info = {"session_name": "s", "cdp_url": CDP_TARGET, "pinned_target": CDP_TARGET}
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda task_id: info)
    monkeypatch.setattr(session, "run_fenced_pair", lambda session_info, fn: fn())
    monkeypatch.setattr(session, "_dispatch_browser_command", lambda *a, **k: ("lightpanda", dict(result)))
    monkeypatch.setattr(session._lp, "_run_chrome_fallback_command",
                        lambda *a, **k: pytest.fail("Chrome fallback command ran under refuse"))
    monkeypatch.setattr(session._lp, "_chrome_fallback_screenshot",
                        lambda *a, **k: pytest.fail("Chrome fallback screenshot ran under refuse"))

    assert session._run_browser_command("task", command) == result


def test_lightpanda_chrome_fallback_still_runs_under_allow(monkeypatch):
    _config(monkeypatch, target="", fallback="allow")
    _isolate_sessions(monkeypatch)
    info = {"session_name": "s"}
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda task_id: info)
    monkeypatch.setattr(session, "run_fenced_pair", lambda session_info, fn: fn())
    monkeypatch.setattr(session, "_dispatch_browser_command",
                        lambda *a, **k: ("lightpanda", {"success": True, "data": {"snapshot": ""}}))
    chrome = Mock(return_value={"success": True, "data": {"snapshot": "x" * 40}})
    monkeypatch.setattr(session._lp, "_run_chrome_fallback_command", chrome)

    result = session._run_browser_command("task", "snapshot")

    chrome.assert_called_once()
    assert result["browser_engine"] == "chrome"


def test_pinned_session_never_selects_a_non_auto_engine(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(cloud, "_get_browser_engine", lambda: "lightpanda")
    captured = {}

    def fake_spawn(task_id, session_info, cmd_parts, command, engine, timeout, stdin_payload):
        captured["cmd"] = cmd_parts
        captured["engine"] = engine
        return {"success": True, "data": {}}

    monkeypatch.setattr(session, "_spawn_and_collect", fake_spawn)
    monkeypatch.setattr(session, "_agent_browser_argv", lambda cmd: [cmd])
    monkeypatch.setattr(session, "_ensure_cdp_supervisor", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)
    info = {"session_name": "s", "cdp_url": CDP_TARGET, "pinned_target": CDP_TARGET}

    engine, _ = session._dispatch_browser_command("task", info, "agent-browser", "snapshot", [], 5, "lightpanda")

    assert engine == "auto"
    assert "--engine" not in captured["cmd"]


# --- P1.4: Camofox target ignores every CDP override ----------------------------------------------

@pytest.fixture
def cdp_overrides(monkeypatch):
    """Every operator CDP source set at once: live /browser connect, BROWSER_CDP_URL, browser.cdp_url."""
    monkeypatch.setenv("BROWSER_CDP_URL", "http://env-browser:9222")
    browser_tool_cdp.set_live_cdp_override("http://live-browser:9222")
    yield
    browser_tool_cdp.set_live_cdp_override(None)


def test_camofox_target_ignores_live_env_and_config_cdp_overrides(monkeypatch, cdp_overrides):
    _config(monkeypatch, target=CAMOFOX_TARGET, provider="camofox", cdp_url="http://config-browser:9222")

    assert browser_tool_cdp.live_cdp_override() == "http://live-browser:9222"
    assert browser_tool_cdp._get_cdp_override_raw() == ""
    assert browser_tool_cdp._get_cdp_override() == ""
    assert browser_camofox.is_camofox_mode() is True
    assert browser_camofox.get_camofox_url() == CAMOFOX_TARGET
    assert browser_task_cdp.use_camofox("task") is True


def test_session_cdp_override_raw_ignores_overrides_under_refuse(monkeypatch, cdp_overrides):
    from tools import browser_session_cdp

    _config(monkeypatch, target=CAMOFOX_TARGET, provider="camofox", cdp_url="http://config-browser:9222")
    assert browser_session_cdp.get_cdp_override_raw() == ""

    _config(monkeypatch, target=CDP_TARGET, cdp_url="http://config-browser:9222")
    assert browser_session_cdp.get_cdp_override_raw() == CDP_TARGET


def test_cdp_target_ignores_live_env_and_config_cdp_overrides(monkeypatch, cdp_overrides):
    _config(monkeypatch, target=CDP_TARGET, cdp_url="http://config-browser:9222")

    assert browser_tool_cdp._get_cdp_override_raw() == CDP_TARGET


def test_unset_target_under_refuse_ignores_cdp_overrides(monkeypatch, cdp_overrides):
    _config(monkeypatch, target="", cdp_url="http://config-browser:9222")

    assert browser_tool_cdp._get_cdp_override_raw() == ""


def test_allow_policy_still_honours_cdp_overrides(monkeypatch, cdp_overrides):
    _config(monkeypatch, target="", fallback="allow", cdp_url="http://config-browser:9222")

    assert browser_tool_cdp._get_cdp_override_raw() == "http://live-browser:9222"


@pytest.mark.parametrize("frame_id", [None, "frame-1"])
def test_browser_cdp_returns_target_unavailable_for_camofox_target(monkeypatch, cdp_overrides, frame_id):
    from tools import browser_cdp_tool

    _config(monkeypatch, target=CAMOFOX_TARGET, provider="camofox")
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_via_supervisor",
                        lambda **k: pytest.fail("supervisor lane reached"))
    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", lambda *a, **k: pytest.fail("CDP call reached"))

    payload = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", frame_id=frame_id, task_id="task"))

    assert payload["error_type"] == "browser_target_unavailable"
    assert payload["target"] == CAMOFOX_TARGET


def test_browser_cdp_returns_target_unavailable_for_unset_target(monkeypatch, cdp_overrides):
    from tools import browser_cdp_tool

    _config(monkeypatch, target="")
    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", lambda *a, **k: pytest.fail("CDP call reached"))

    payload = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", task_id="task"))

    assert payload["error_type"] == "browser_target_unavailable"


# --- P1.5: a CDP-pinned profile never routes to Camofox -----------------------------------------

@pytest.fixture
def camofox_env(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox-env:9377")


def test_cdp_target_with_camofox_url_never_routes_to_camofox(monkeypatch, camofox_env):
    _config(monkeypatch, target=CDP_TARGET)

    assert browser_camofox.get_camofox_url() == ""
    assert browser_camofox.is_camofox_mode() is False
    assert bt._is_camofox_mode() is False
    assert browser_task_cdp.use_camofox("task") is False
    assert browser_task_cdp.use_camofox(None) is False


def test_cdp_target_ignores_stored_camofox_selection(monkeypatch, camofox_env):
    _config(monkeypatch, target=CDP_TARGET)
    monkeypatch.setattr("tools.tool_backend_helpers.read_selection", lambda name: "camofox")

    assert browser_camofox.is_camofox_mode() is False
    assert browser_task_cdp.use_camofox("task") is False


def test_unset_target_under_refuse_never_routes_to_camofox(monkeypatch, camofox_env):
    _config(monkeypatch, target="")
    monkeypatch.setattr("tools.tool_backend_helpers.read_selection", lambda name: "camofox")

    assert browser_camofox.get_camofox_url() == ""
    assert browser_camofox.is_camofox_mode() is False
    assert browser_task_cdp.use_camofox("task") is False


def test_camofox_target_does_not_use_camofox_url_env(monkeypatch, camofox_env):
    _config(monkeypatch, target=CAMOFOX_TARGET, provider="camofox")

    assert browser_camofox.get_camofox_url() == CAMOFOX_TARGET
    assert browser_task_cdp.use_camofox("task") is True


def test_allow_policy_still_honours_camofox_url(monkeypatch, camofox_env):
    _config(monkeypatch, target="", fallback="allow")
    monkeypatch.setattr("tools.tool_backend_helpers.read_selection", lambda name: None)

    assert browser_camofox.get_camofox_url() == "http://camofox-env:9377"
    assert browser_camofox.is_camofox_mode() is True


def test_browser_navigate_on_cdp_target_does_not_call_camofox(monkeypatch, camofox_env):
    _config(monkeypatch, target=CDP_TARGET)
    _isolate_sessions(monkeypatch)
    monkeypatch.setattr(bt, "_url_policy_error", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_camofox", lambda *a, **k: pytest.fail("Camofox lane reached"))
    monkeypatch.setattr(session, "_get_session_info", lambda task_id: {"_first_nav": False})
    monkeypatch.setattr(session, "_run_browser_command",
                        lambda *a, **k: {"success": False, "error": "stop here"})

    result = json.loads(bt.browser_navigate("https://example.com", task_id="task"))

    assert result["success"] is False


# --- P2: browser_navigate structured error ---------------------------------------------------------

@pytest.mark.parametrize("target", ["/tmp/browser.sock", ""])
def test_browser_navigate_returns_structured_target_error(monkeypatch, target):
    _config(monkeypatch, target=target)
    _isolate_sessions(monkeypatch)
    _no_alternative(monkeypatch)
    monkeypatch.setattr(bt, "_url_policy_error", lambda *a, **k: None)
    monkeypatch.setattr(session._cdp, "_resolve_cdp_override", lambda t: pytest.fail("unix socket resolved"))

    result = json.loads(bt.browser_navigate("https://example.com", task_id="task"))

    assert result["success"] is False
    assert result["error_type"] == "browser_target_unavailable"
    assert result["code"] == "browser_target_unavailable"


# --- unix-socket rejection --------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "/tmp/browser.sock",
    "unix:///tmp/browser.sock",
    "file:///tmp/browser.sock",
    "ftp://127.0.0.1:9222",
    "127.0.0.1:9222",
    "http://",
])
def test_pinned_target_session_rejects_non_cdp_targets(monkeypatch, target):
    _config(monkeypatch, target=target)
    monkeypatch.setattr(session._cdp, "_resolve_cdp_override", lambda t: pytest.fail("target resolved"))
    monkeypatch.setattr(session, "_create_cdp_session", lambda *a, **k: pytest.fail("session created"))

    with pytest.raises(cloud.BrowserTargetUnavailable) as excinfo:
        session._create_pinned_target_session("task")

    assert excinfo.value.target == target
    assert "Unix-socket" in excinfo.value.reason or "invalid target" in excinfo.value.reason


# --- browser_use_cli ordering ---------------------------------------------------------------------

def test_refuse_browser_use_route_replaces_operator_endpoint(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(browser_use_cli, "_cloud_refuses_pinned_target", lambda: True)
    monkeypatch.setattr(cloud, "pinned_browser_target_or_raise", lambda: "http://127.0.0.1:9444")

    env = {"BU_CDP_URL": "http://another-browser:9222"}
    assert browser_use_cli._resolve_backend_cdp(env, "task") is None
    assert env == {"BU_CDP_URL": "http://127.0.0.1:9444"}


def test_refuse_invalid_target_is_structured(monkeypatch):
    _config(monkeypatch, target="/tmp/browser.sock")
    monkeypatch.setattr(browser_use_cli, "_cloud_refuses_pinned_target", lambda: True)

    error = browser_use_cli._resolve_backend_cdp({}, "task")
    assert error is not None
    payload = json.loads(error)

    assert payload["success"] is False
    assert payload["error_type"] == "browser_target_unavailable"
    assert payload["target"] == "/tmp/browser.sock"
    assert "loopback HTTP(S) proxy" in payload["reason"]


@pytest.mark.parametrize("local", [False, True])
def test_refuse_guard_runs_before_real_profile_resolution(monkeypatch, local):
    _config(monkeypatch)
    monkeypatch.setattr(browser_use_cli, "_resolve_real_profile_cdp",
                        lambda *a, **k: pytest.fail("real-profile resolved before the refuse guard"))
    monkeypatch.setattr(browser_use_cli, "_real_profile_consented", lambda: True)

    env: dict = {}
    assert browser_use_cli._route_backend(env, "sess", "task", local) is None
    assert CDP_TARGET in env.values()


def test_refuse_with_empty_target_is_structured_for_browser_exec_route(monkeypatch):
    _config(monkeypatch, target="")
    monkeypatch.setattr(browser_use_cli, "_resolve_real_profile_cdp",
                        lambda *a, **k: pytest.fail("real-profile resolved before the refuse guard"))

    error = browser_use_cli._route_backend({}, "sess", "task", local=True)

    assert error is not None
    assert json.loads(error)["error_type"] == "browser_target_unavailable"


# --- P2: model-visible payloads are redacted --------------------------------------------------------

def test_target_error_redacts_credentials_in_model_visible_payload():
    secret = "super-secret-token-123456"
    target = f"ftp://user:{secret}@host:9222/path?access_token={secret}"

    payload = cloud.browser_target_error(
        cloud.BrowserTargetUnavailable(target, f"invalid target {target}"))

    assert secret not in json.dumps(payload)
    assert payload["success"] is False
    assert payload["error_type"] == "browser_target_unavailable"


def test_target_error_redacts_generic_exception_text(monkeypatch):
    secret = "super-secret-token-123456"
    _config(monkeypatch, target=f"https://cdp.example/json/version?access_token={secret}")

    payload = cloud.browser_target_error(
        RuntimeError(f"upstream rejected https://cdp.example/json/version?access_token={secret}"))

    assert secret not in json.dumps(payload)


# --- P2: DEFAULT_CONFIG browser section --------------------------------------------------------------

def test_default_browser_config_equals_base_apart_from_new_keys():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    browser = DEFAULT_CONFIG["browser"]
    new_keys = {"target", "fallback"}

    assert browser["target"] == ""
    assert browser["fallback"] == "allow"
    assert {k: v for k, v in browser.items() if k not in new_keys} == BASE_BROWSER_DEFAULTS
    assert set(browser) == set(BASE_BROWSER_DEFAULTS) | new_keys


def test_default_browser_config_is_a_fresh_copy_each_time():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.config_defaults_browser import default_browser_config

    first, second = default_browser_config(), default_browser_config()
    assert first == second
    assert first is not second
    assert first["camofox"] is not second["camofox"]
    assert first["chromium_args"] is not second["chromium_args"]
    assert DEFAULT_CONFIG["browser"] is not first
    assert DEFAULT_CONFIG["browser"]["camofox"] is not first["camofox"]

    first["camofox"]["user_id"] = "mutated"
    first["chromium_args"].append("--mutated")
    assert default_browser_config()["camofox"]["user_id"] == ""
    assert default_browser_config()["chromium_args"] == []
    assert DEFAULT_CONFIG["browser"]["camofox"]["user_id"] == ""
