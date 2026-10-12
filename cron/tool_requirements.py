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
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        discover_plugins()  # idempotent
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
    except OSError:
        return ""


def _first_requirement(text: str, patterns: Iterable[tuple[re.Pattern[str], str]]) -> Optional[str]:
    for pattern, label in patterns:
        if pattern.search(text):
            return label
    return None


def _toolset_names(job: dict[str, Any]) -> list[str]:
    value = job.get("enabled_toolsets")
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _available_tools(names: list[str], cfg: Optional[dict]) -> set[str]:
    """Resolve the same native and legacy tool names the runtime selects, then apply its denylist."""
    from model_tools import _select_tool_names
    from cron.scheduler import _resolve_cron_disabled_toolsets

    return _select_tool_names(
        list(names), _resolve_cron_disabled_toolsets(cfg or {}), quiet_mode=True
    )


def _allows_terminal(names: list[str], cfg: Optional[dict]) -> bool:
    return "terminal" in _available_tools(names, cfg)


def _allows_relay(names: list[str], cfg: Optional[dict]) -> bool:
    """Relay may be a native toolset or a configured MCP server."""
    if any("relay" in tool.lower() for tool in _available_tools(names, cfg)):
        return True
    from hermes_cli.tools_config import enabled_mcp_server_names
    enabled_mcp = enabled_mcp_server_names(cfg or {})
    return any(name in enabled_mcp and "relay" in name.lower() for name in names)


def _effective_toolsets(names: list[str], cfg: Optional[dict]) -> list[str]:
    """The allowlist the scheduler actually runs with: enabled MCP servers are merged onto a
    non-empty per-job list (honoring ``no_mcp`` and explicit servers). Reads local config only."""
    from cron.scheduler import _merge_mcp_into_per_job_toolsets
    try:
        return _merge_mcp_into_per_job_toolsets(list(names), cfg or {})
    except Exception:
        return [name for name in names if name != "no_mcp"]


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
