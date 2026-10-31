"""Agent output goes through ``_safe_print``, which prints nothing to raw stdout while private.

A gateway redirects stdout to gateway.log, which the privacy log filter never sees, so a raw
write on a turn path can put a private prompt, response, tool argument, tool result or provider
body in a log file. This scans every module under ``agent/`` and ``run_agent.py`` for raw output:
``print`` calls, ``print`` used as a value (an alias or a fallback callable), ``builtins.print``,
``from builtins import print`` and ``sys.stdout`` / ``sys.stderr`` writes. Each allowed site is
named by file, enclosing function and the source of its first argument, so a new raw write in an
allowed function still fails.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}

# (file, enclosing function) -> (why it cannot carry private turn content, the allowed sites'
# first-argument source, truncated). Allow a site only when it is CLI-only, startup or content-free.
ALLOWED = {
    ('agent/agent_init.py', '_emit_compression_summary'): ('startup, non-quiet only: token counts', frozenset({
        "f'📊 Context limit: {_cc.context_length:,} tokens",
    })),
    ('agent/agent_init.py', '_init_anthropic_client'): ('startup, non-quiet only: model and region', frozenset({
        "f'🤖 AI Agent initialized with model: {agent.mode",
    })),
    ('agent/agent_init.py', '_init_bedrock_client'): ('startup, non-quiet only: model and region', frozenset({
        "f'🤖 AI Agent initialized with model: {agent.mode",
    })),
    ('agent/agent_init.py', '_init_moa_client'): ('startup, non-quiet only: preset name', frozenset({
        "f'🤖 AI Agent initialized with MoA preset: {agent",
    })),
    ('agent/agent_init.py', '_init_openai_client'): ('startup, non-quiet only: model and base URL', frozenset({
        "f'🔗 Using custom base URL: {base_url}'",
        "f'🤖 AI Agent initialized with MoA preset: {agent",
        "f'🤖 AI Agent initialized with model: {agent.mode",
    })),
    ('agent/agent_init.py', '_load_tools'): ('startup, non-quiet only: tool and toolset names', frozenset({
        "'📝 Trajectory saving enabled'",
        "'🛠️  No tools loaded (all tools filtered out or ",
        'f"   ✅ Enabled toolsets: {\', \'.join(enabled_tool',
        'f"   ❌ Disabled toolsets: {\', \'.join(disabled_to',
        'f"🛠️  Loaded {len(agent.tools)} tools: {\', \'.joi',
        "f'💾 Prompt caching: ENABLED ({source}, {agent._c",
    })),
    ('agent/agent_init.py', '_print_key_banner'): ('startup, non-quiet only: masked credential label', frozenset({
        "'⚠️  Warning: API key appears invalid or missing",
        "f'🔑 Using credentials: {source_label}'",
        "f'🔑 Using {label}: {key[:8]}...{key[-4:]}'",
    })),
    ('agent/agent_init.py', '_warn_invalid_config_int'): ('startup config validation: key and value', frozenset({
        "f'\\n⚠ Invalid {what}: {value!r}\\n  {requirement[",
    })),
    ('agent/agent_init_fallback.py', '_init_fallback_chain'): ('startup, non-quiet only: model labels', frozenset({
        "f'🔄 Fallback chain ({len(chain)} providers): ' +",
        "f'🔄 Fallback model: {labels[0]}'",
    })),
    ('agent/agent_runtime_helpers.py', 'dump_api_request_debug'): ('returns before any output while private; needs HERMES_DUMP_REQUEST_STDOUT', frozenset({
        'json.dumps(_redacted_payload, ensure_ascii=False',
    })),
    ('agent/anthropic_credentials.py', 'run_hermes_oauth_login_pure'): ('interactive CLI OAuth login', frozenset({
        '"\\nAfter authorizing, you\'ll see a code. Paste i',
        "'  (Browser opened automatically)'",
        "'No access token in response.'",
        "'No code entered.'",
        "'\\n'.join(['', 'Authorize Hermes with your Claud",
        "f'Token exchange failed: {e}'",
    })),
    ('agent/legacy_cli.py', 'main'): ('CLI entry point', frozenset({
        "'\\nNo query given: pass one with --query (or run",
    })),
    ('agent/lsp/cli.py', '_cmd_install'): ('`hermes lsp` CLI', frozenset({
        "f'installed: {bin_path}\\n'",
        "f'installing {server_id} (pkg={pkg}) ...\\n'",
        "f'{server_id} already installed\\n'",
        "f'{server_id}: this server requires a manual ins",
    })),
    ('agent/lsp/cli.py', '_cmd_install_all'): ('`hermes lsp` CLI', frozenset({
        "f'  installing {s.server_id} (pkg={pkg}) ... '",
        "f'  {s.server_id:24s} already installed\\n'",
        "f'ok ({path})\\n' if path else 'FAILED\\n'",
    })),
    ('agent/lsp/cli.py', '_cmd_list'): ('`hermes lsp` CLI', frozenset({
        'f"{s.server_id:24s} [{status:11s}] {\',\'.join(s.e',
    })),
    ('agent/lsp/cli.py', '_cmd_restart'): ('`hermes lsp` CLI', frozenset({
        "'LSP service shut down. Next edit will respawn c",
    })),
    ('agent/lsp/cli.py', '_cmd_status'): ('`hermes lsp` CLI', frozenset({
        "'\\n'.join(out) + '\\n'",
        "json.dumps({'service': info, 'registry': registr",
    })),
    ('agent/lsp/cli.py', '_cmd_which'): ('`hermes lsp` CLI', frozenset({
        "f'{server_id}: not installed\\n'",
        "resolved + '\\n'",
    })),
    ('agent/lsp/cli.py', 'run_lsp_command'): ('`hermes lsp` CLI', frozenset({
        "f'unknown lsp subcommand: {sub}\\n'",
    })),
    ('agent/secret_sources/command.py', '_log'): ('secret helper status: errno, exit code or timeout only', frozenset({
        "f'[secrets:command] {message}'",
    })),
    ('agent/shell_hooks.py', '_prompt_and_record'): ('interactive TTY consent prompt', frozenset({
        '',
        "f'\\n⚠ Hermes is about to register a shell hook t",
    })),
    ('agent/status_output.py', '_safe_print'): ('the guard itself: prints only when public or rendered', frozenset({
        'print value',
    })),
    ('agent/transports/hermes_tools_mcp_server.py', 'main'): ('separate MCP server process, startup errors', frozenset({
        "f'hermes-tools MCP server cannot start: {exc}\\n'",
        "f'hermes-tools MCP server error: {exc}\\n'",
    })),
    ('run_agent.py', '_parse_toolset_arg'): ('`python run_agent.py` CLI', frozenset({
        "f'{label}: {names}'",
    })),
    ('run_agent.py', '_print_tool_listing'): ('`python run_agent.py --list_tools`', frozenset({
        "'-' * 40",
        "'-' * 50",
        "'\\n🎭 Scenario-Specific Toolsets:'",
        "'\\n🎯 Predefined Toolsets (New System):'",
        "'\\n📂 Composite Toolsets (built from other toolse",
        "'\\n📌 Basic Toolsets:'",
        "'\\n📦 Legacy Toolsets (for backward compatibility",
        "'📋 Available Tools & Toolsets:'",
        '_LIST_TOOLS_USAGE',
        'f"    Includes: {(\', \'.join(info[\'includes\']) if',
        'f"    Requirements: {\', \'.join(info[\'requirement',
        'f"    Tools: {(\', \'.join(info[\'resolved_tools\'])',
        'f"    Total tools: {info[\'tool_count\']}"',
        'f"  {(\'✅\' if info[\'available\'] else \'❌\')} {name}',
        'f"  • {name:15} - {info[\'description\']}"',
        'f"  • {name:20} - {info[\'description\']}"',
        "f'  📌 {tool_name} (from {get_toolset_for_tool(to",
        "f'\\n🔧 Individual Tools ({len(all_tools)} availab",
    })),
    ('run_agent.py', '_save_sample_trajectory'): ('`python run_agent.py --save_sample`', frozenset({
        "f'\\n⚠️ Failed to save sample: {e}'",
        "f'\\n💾 Sample trajectory saved to: {sample_filena",
    })),
    ('run_agent.py', 'main'): ('`python run_agent.py` CLI entry point', frozenset({
        "'   - Failed conversations → failed_trajectories",
        "'   - Successful conversations → trajectory_samp",
        "'=' * 50",
        "'\\n' + '=' * 50",
        "'\\n' + '=' * 50 + '\\n📋 CONVERSATION SUMMARY\\n' +",
        "'\\n🎯 FINAL RESPONSE:\\n' + '-' * 30 + '\\n' + rend",
        "'\\n👋 Agent execution completed!'",
        "'💾 Trajectory saving: ENABLED'",
        "'🤖 AI Agent with Tool Calling'",
        'f"✅ Completed: {result[\'completed\']}\\n📞 API Call',
        "f'\\n📝 User Query: {user_query}'",
        "f'❌ Failed to initialize agent: {e}'",
    })),
}


def _stream_write(node):
    if not (isinstance(node, ast.Attribute) and node.attr in {"write", "writelines"}):
        return False
    stream = node.value.value if isinstance(node.value, ast.Attribute) and node.value.attr == "buffer" else node.value
    return (isinstance(stream, ast.Attribute) and stream.attr in STREAMS
            and isinstance(stream.value, ast.Name) and stream.value.id == "sys")


def _first_argument(call):
    if not isinstance(call, ast.Call):
        return ""
    return ast.unparse(call.args[0])[:48] if call.args else ""


def output_sites():
    """Yield (file, function, kind, first argument source, line) for every raw output site."""
    for path in sorted((ROOT / "agent").rglob("*.py")) + [ROOT / "run_agent.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
        stack = []
        rel = path.relative_to(ROOT).as_posix()

        def visit(node):
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if named:
                stack.append(node.name)
            kind = None
            if isinstance(node, ast.Name) and node.id == "print" and isinstance(node.ctx, ast.Load):
                kind = "print" if id(node) in calls else "print value"
            elif (isinstance(node, ast.Attribute) and node.attr == "print"
                  and isinstance(node.value, ast.Name) and node.value.id == "builtins"):
                kind = "builtins.print"
            elif _stream_write(node):
                kind = "stream write"
            elif isinstance(node, ast.ImportFrom) and node.module == "builtins" and any(
                    alias.name == "print" for alias in node.names):
                kind = "import print"
            if kind:
                text = _first_argument(calls.get(id(node))) if kind in {"print", "builtins.print", "stream write"} else kind
                yield rel, (stack[0] if stack else "<module>"), kind, text, node.lineno
            for child in ast.iter_child_nodes(node):
                yield from visit(child)
            if named:
                stack.pop()

        yield from visit(tree)


def test_agent_output_uses_safe_print():
    unexpected = [
        f"{path}:{line} {kind} in {func}: {text}"
        for path, func, kind, text, line in output_sites()
        if text not in ALLOWED.get((path, func), ("", frozenset()))[1]
    ]
    assert not unexpected, (
        "Raw output in agent code bypasses the private-turn stdout guard. Use the agent's "
        "_safe_print, or allow a CLI-only or startup site in ALLOWED with its reason:\n  "
        + "\n  ".join(unexpected))
