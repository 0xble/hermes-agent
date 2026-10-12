"""Detect cron prompt capabilities that a restricted toolset must provide.

Cron prompt assembly can add operational steps after a job is authored (for example
Daily Brief findings and relay routing). Keep this check conservative: a restricted
allowlist should be warned about rather than silently running a prompt that cannot
perform one of its required steps.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional


# These are intentionally capability-oriented, not a list of every CLI in the
# world. Backtick commands and explicit cron-injected steps cover the failure mode
# while avoiding warnings for ordinary prose that merely mentions "shell".
_SHELL_REQUIREMENT_PATTERNS = (
    (re.compile(r"\bfinding\s+add\b", re.IGNORECASE), "Daily Brief `finding add` step"),
    (re.compile(r"`[^`\n]*\b(?:python3?|node|git|gh|slack|imsg|gog|ssh|psql|loops)\b[^`\n]*`", re.IGNORECASE),
     "shell command in backticks"),
    (re.compile(r"\b(?:run|execute)\s*:?[ \t]+`[^`\n]+`", re.IGNORECASE),
     "explicit Run command step"),
    (re.compile(r"```(?:bash|sh|zsh|shell)\s*\n", re.IGNORECASE), "shell command block"),
    (re.compile(r"\b(?:terminal|execute_code)\s*\(", re.IGNORECASE), "shell tool call"),
)

_RELAY_REQUIREMENT_PATTERNS = (
    (re.compile(r"\bpick\s+the\s+destination\b", re.IGNORECASE), "relay `Pick the Destination` step"),
    (re.compile(r"\brelay\s+(?:send|status|whoami)\b", re.IGNORECASE), "relay command step"),
    (re.compile(r"\bmcp__relay__", re.IGNORECASE), "relay MCP tool step"),
    (re.compile(r"\b(?:use|with|follow|load)\s+(?:the\s+)?`?relay`?\s+skill\b", re.IGNORECASE),
     "relay skill delivery step"),
)


def _plugin_skill_path(name: str):
    """Registered ``plugin:skill`` path, resolved the way ``skill_view`` does (without serving it)."""
    if ":" not in name:
        return None
    try:
        from hermes_cli.plugins import get_plugin_manager
        path = get_plugin_manager().find_plugin_skill(name)
    except Exception:
        return None
    return path if path is not None and path.exists() else None


def _attached_skill_content(name: str) -> str:
    """Read raw attached instructions; doctor must not run skill preprocessing/setup."""
    from tools.skills_tool import _locate_skill, _skill_lookup_path_error, _skill_search_dirs
    if _skill_lookup_path_error(name):
        return ""
    path = _plugin_skill_path(name)
    if path is None:
        roots, _ = _skill_search_dirs()
        error, _, path = _locate_skill(name, name.replace(":", "/") if ":" in name else None, roots)
        if error or path is None:
            return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _first_requirement(text: str, patterns: Iterable[tuple[re.Pattern[str], str]]) -> Optional[str]:
    for pattern, label in patterns:
        if pattern.search(text):
            return label
    return None


def _toolset_names(job: dict[str, Any]) -> list[str]:
    value = job.get("enabled_toolsets")
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


_LEGACY_TOOLSET_MAP = {
    "web_tools": ["web_search", "web_extract"],
    "terminal_tools": ["terminal"],
    "vision_tools": ["vision_analyze"],
    "image_tools": ["image_generate"],
    "skills_tools": ["skills_list", "skill_view", "skill_manage"],
    "browser_tools": ["browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll",
                      "browser_back", "browser_press", "browser_get_images", "browser_vision", "browser_console"],
    "cronjob_tools": ["cronjob_manage"],
    "file_tools": ["read_file", "write_file", "patch", "search_files"],
    "tts_tools": ["text_to_speech"],
}


def _static_tool_names(names: list[str]) -> set[str]:
    """Resolve built-in and legacy toolsets without importing runtime/plugin discovery."""
    from toolsets import TOOLSETS, TOOLSET_SESSION_PLATFORMS

    def resolve(name: str, visited: set[str] | None = None) -> set[str]:
        visited = set() if visited is None else visited
        if name in visited:
            return set()
        visited.add(name)
        if name in {"all", "*"}:
            return {
                tool
                for toolset_name in TOOLSETS
                if toolset_name not in TOOLSET_SESSION_PLATFORMS
                for tool in resolve(toolset_name, visited.copy())
            }
        definition = TOOLSETS.get(name)
        if not definition:
            return set()
        tools = set(definition.get("tools", []))
        for included in definition.get("includes", []):
            tools.update(resolve(included, visited.copy()))
        return tools

    tools: set[str] = set()
    for name in names:
        if name in _LEGACY_TOOLSET_MAP:
            tools.update(_LEGACY_TOOLSET_MAP[name])
        else:
            tools.update(resolve(name))
    return tools


def _static_disabled_tool_names(names: list[str]) -> set[str]:
    """Resolve the scheduler denylist without importing ``model_tools`` or plugin code."""
    return _static_tool_names(names)


def _disabled_toolset_names(cfg: Optional[dict]) -> list[str]:
    """Mirror cron's static denylist without importing the runtime scheduler."""
    root = cfg or {}
    cron_cfg = root.get("cron") or {}
    agent_cfg = root.get("agent") or {}
    disabled = ["messaging", "clarify"] if cron_cfg.get("allow_agent_scheduling") else ["cronjob", "messaging", "clarify"]
    from agent.skill_utils import parse_config_string_list
    for name in parse_config_string_list(agent_cfg.get("disabled_toolsets")):
        name = str(name).strip()
        if name and name not in disabled:
            disabled.append(name)
    return disabled


def _available_tools(names: list[str], cfg: Optional[dict]) -> set[str]:
    """Resolve built-in and legacy names, then apply the scheduler's denylist statically."""
    tools = _static_tool_names(names)
    tools.difference_update(_static_disabled_tool_names(_disabled_toolset_names(cfg)))
    return tools


def _allows_terminal(names: list[str], cfg: Optional[dict]) -> bool:
    return "terminal" in _available_tools(names, cfg)


def _configured_enabled_mcp_server_names(cfg: Optional[dict]) -> set[str]:
    """Read only native MCP declarations; portable plugin discovery is intentionally excluded."""
    from tools.mcp_tool_common import mcp_server_enabled
    servers = (cfg or {}).get("mcp_servers") or {}
    return {
        str(name) for name, server_cfg in servers.items()
        if isinstance(server_cfg, dict) and mcp_server_enabled(server_cfg)
    }


def _allows_relay(names: list[str], cfg: Optional[dict]) -> bool:
    """Relay may be a native toolset or a configured MCP server."""
    if any("relay" in tool.lower() for tool in _available_tools(names, cfg)):
        return True
    enabled_mcp = _configured_enabled_mcp_server_names(cfg)
    disabled = {name.lower() for name in _disabled_toolset_names(cfg)}
    for name in names:
        lowered = name.lower()
        if name in enabled_mcp and "relay" in lowered:
            aliases = {lowered, f"mcp-{lowered}", f"{lowered}-mcp"}
            if not aliases & disabled:
                return True
    return False


def _effective_toolsets(names: list[str], cfg: Optional[dict]) -> list[str]:
    """Mirror cron's MCP merge using only the native config declarations."""
    result = [name for name in names if name != "no_mcp"]
    if "no_mcp" in names:
        return result
    enabled_mcp = _configured_enabled_mcp_server_names(cfg)
    if set(result) & enabled_mcp:
        return result
    return result + sorted(enabled_mcp - set(result))


def job_tool_requirement_issues(
    job: dict[str, Any], *, skill_loader: Callable[[str], str] = _attached_skill_content,
    cfg: Optional[dict] = None,
) -> list[str]:
    """Return warnings for a restricted job whose prompt/skills need unavailable tools.

    An absent allowlist means the platform default and is deliberately ignored. A
    non-empty allowlist is checked fail-closed against its effective runtime value;
    malformed values produce no warning here because the job-store normalizer owns
    that validation.
    """
    stored = _toolset_names(job)
    if not stored or job.get("no_agent"):
        return []
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly()
        except Exception:
            cfg = {}
    names = _effective_toolsets(stored, cfg)

    texts = [("prompt", str(job.get("prompt") or ""))]
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if isinstance(skills, str):
        skills = [skills]
    for skill in skills if isinstance(skills, list) else []:
        name = str(skill).strip()
        if name:
            texts.append((f"attached skill '{name}'", skill_loader(name)))
    combined = "\n".join(text for _, text in texts)

    issues: list[str] = []
    shell_reason = _first_requirement(combined, _SHELL_REQUIREMENT_PATTERNS)
    if shell_reason and not _allows_terminal(names, cfg):
        issues.append(
            f"prompt requires {shell_reason}, but enabled_toolsets {stored!r} do not include terminal access"
        )
    relay_reason = _first_requirement(combined, _RELAY_REQUIREMENT_PATTERNS)
    if relay_reason and not (_allows_terminal(names, cfg) or _allows_relay(names, cfg)):
        issues.append(
            f"prompt requires {relay_reason}, but enabled_toolsets {stored!r} (effective: {names!r}) do not include terminal or a relay toolset"
        )
    return issues
