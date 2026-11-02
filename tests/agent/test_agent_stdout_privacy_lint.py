"""Agent output goes through ``_safe_print``, which prints nothing to raw stdout while private.

A gateway redirects stdout to gateway.log, which the privacy log filter never sees, so a bare
``print`` on a turn path can put a private prompt, response, tool argument, tool result or
provider body in a log file. Only the CLI-only and startup functions below may print directly.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (file, enclosing function) -> why a bare print cannot carry private turn content there.
ALLOWED = {
    ("agent/agent_init.py", "_warn_invalid_config_int"): "startup config validation, content-free",
    ("agent/agent_init.py", "_print_key_banner"): "startup, non-quiet only, masked credential label",
    ("agent/agent_init.py", "_init_anthropic_client"): "startup, non-quiet only, model and region",
    ("agent/agent_init.py", "_init_moa_client"): "startup, non-quiet only, preset name",
    ("agent/agent_init.py", "_init_bedrock_client"): "startup, non-quiet only, model and region",
    ("agent/agent_init.py", "_init_openai_client"): "startup, non-quiet only, model and base URL",
    ("agent/agent_init.py", "_load_tools"): "startup, non-quiet only, tool and toolset names",
    ("agent/agent_init.py", "_emit_compression_summary"): "startup, non-quiet only, token counts",
    ("agent/agent_init_fallback.py", "_init_fallback_chain"): "startup, non-quiet only, model labels",
    ("agent/agent_runtime_helpers.py", "dump_api_request_debug"):
        "returns before any output while private; stdout copy needs HERMES_DUMP_REQUEST_STDOUT",
    ("agent/anthropic_credentials.py", "run_hermes_oauth_login_pure"): "interactive CLI OAuth login",
    ("agent/legacy_cli.py", "main"): "CLI entry point",
    ("agent/shell_hooks.py", "_prompt_and_record"): "interactive TTY consent prompt",
    ("run_agent.py", "_print_tool_listing"): "`python run_agent.py --list_tools`",
    ("run_agent.py", "_parse_toolset_arg"): "`python run_agent.py` argument echo",
    ("run_agent.py", "_save_sample_trajectory"): "`python run_agent.py --save_sample`",
    ("run_agent.py", "main"): "`python run_agent.py` CLI entry point",
}


def bare_prints():
    for path in sorted((ROOT / "agent").glob("*.py")) + [ROOT / "run_agent.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stack = []

        def visit(node):
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if named:
                stack.append(node.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                yield path.relative_to(ROOT).as_posix(), (stack[0] if stack else "<module>"), node.lineno
            for child in ast.iter_child_nodes(node):
                yield from visit(child)
            if named:
                stack.pop()

        yield from visit(tree)


def test_turn_path_output_uses_safe_print():
    found = list(bare_prints())
    unexpected = [f"{path}:{line} in {func}" for path, func, line in found if (path, func) not in ALLOWED]
    assert not unexpected, (
        "Bare print() in agent code bypasses the private-turn stdout guard. Use the agent's "
        "_safe_print, or add a CLI-only or startup function to ALLOWED with its reason:\n  "
        + "\n  ".join(unexpected))
