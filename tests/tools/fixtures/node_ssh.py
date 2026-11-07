"""A synthetic SSH endpoint for adapter integration tests, never a live desktop.

Run as an executable named ssh. It implements the versioned CuaDriver result
shapes through actual MCP JSON-RPC stdio, retaining session and capture tokens
only for this child lifetime. The test controls observations in state.json.
"""
import json
import os
from pathlib import Path
import plistlib
import sys
import threading

root = Path(__file__).parent
host, command = sys.argv[-2:]
state = json.loads((root / "state.json").read_text(encoding="utf-8-sig"))
settings = state.get(host, {})
log = root / "calls.jsonl"


def record(name, args=None):
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"host": host, "pid": os.getpid(), "name": name, "args": args or {}}) + "\n")


if "__HERMES_NODE_PROBE__" in command:
    record("probe")
    if settings.get("offline"):
        sys.stderr.write("ssh: connect to host: Connection refused\n")
        sys.exit(255)
    if settings.get("denied"):
        sys.stderr.write("Permission denied (publickey).\n")
        sys.exit(255)
    if settings.get("stall_probe"):
        threading.Event().wait()
    parts = {
        "version": "cua-driver 0.32.0",
        "status": ("Cua Driver daemon is running\n  permission mode: " + settings.get("permission_mode", "unrestricted") + " (fixture)")
                  if settings.get("daemon_running", True) else "Cua Driver daemon is not running",
        "permissions": json.dumps({"accessibility": settings.get("accessibility", True),
                                   "screen_recording": settings.get("screen_recording", True),
                                   "source": {"attribution": settings.get("attribution", "driver-daemon")}}),
        "console": plistlib.dumps({"IOConsoleLocked": settings.get("locked", False)}).decode(),
        "power": plistlib.dumps([{"System Capabilities": settings.get("capabilities", 15)}]).decode(),
    }
    for name, value in parts.items():
        if name not in settings.get("omit", []):
            print(f"\n__HERMES_NODE_PROBE__{name}\n{value}", flush=True)
    sys.exit(0)

record("mcp_spawn", {"argv": sys.argv[1:], "home": os.environ.get("HERMES_HOME")})
if settings.get("race_standard"):
    settings["permission_mode"] = "standard"
    state[host] = settings
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
if settings.get("stall_handshake"):
    threading.Event().wait()
session = None
captured = False
names = ["start_session", "end_session", "set_config", "get_config", "list_windows", "list_apps",
         "get_window_state", "get_desktop_state", "screenshot", "click", "type_text", "press_key", "scroll", "drag",
         "set_value"]
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "synthetic-cua-driver", "version": "0.32.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "description": "fixture",
                             "inputSchema": {"type": "object", "properties": {
                                 "session": {"type": "string"}, "element_token": {"type": "string"},
                                 "capture_id": {"type": "string"},
                                 "include_screenshot": {"type": "boolean"}}}} for name in names]}
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments", {})
        record(name, args)
        result = {"isError": False, "content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": True}}
        if name == "start_session":
            session = args["session"]
        elif args.get("session") != session:
            result.update(isError=True, content=[{"type": "text", "text": "session has ended; call start_session"}])
        elif name == "list_windows":
            result["structuredContent"] = {"windows": [{"app_name": "TextEdit", "pid": 41, "window_id": 42,
                                                       "title": "Fixture", "on_screen": True, "z_index": 5}]}
        elif name == "list_apps":
            result["structuredContent"] = {"apps": [{"name": "TextEdit", "pid": 41}]}
        elif name in ("get_window_state", "get_desktop_state", "screenshot"):
            captured = True
            result["content"] = [{"type": "text", "text": 'AXWindow "Fixture"\n  [1] AXButton "Close"'},
                                 {"type": "image", "data": state["image"], "mimeType": "image/png"}]
            result["structuredContent"] = {"capture_id": "capture-fixture", "elements": [
                {"element_index": 1, "role": "AXButton", "label": "Close", "frame": {"x": 0, "y": 0, "w": 8, "h": 8},
                 "element_token": "fixture-token"}]}
        elif name in ("click", "type_text", "press_key", "scroll", "set_value"):
            if settings.get("stall_write"):
                threading.Event().wait()
            if settings.get("drop_write"):
                sys.exit(0)  # The write was observed, but its response was lost. Never replay it.
            if not captured or args.get("capture_id") != "capture-fixture":
                result.update(isError=True, content=[{"type": "text", "text": "capture required"}])
        elif name == "end_session":
            session = None
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
record("mcp_exit")
