"""Opaque Bearer diagnostics are scrubbed at source, not only on egress."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


def _run_isolated(enabled, script):
    """Exercise import-time opt-out with no live profile or credential inheritance."""
    with tempfile.TemporaryDirectory(prefix="terminal-bearer-") as directory:
        env = {
            "PATH": os.defpath,
            "HOME": directory,
            "HERMES_HOME": str(Path(directory) / "home"),
            "HERMES_RUNTIME_DIR": str(Path(directory) / "runtime"),
            "TMPDIR": directory,
            "HERMES_REDACT_SECRETS": "true" if enabled else "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestTerminalDiagnosticBearer(unittest.TestCase):
    def test_diagnostic_bytes_quotes_escapes_and_line_endings(self):
        _run_isolated(True, r'''
            import ast
            from agent.redact import redact_terminal_output

            token = "syntheticOpaqueFixture000000000000000000000000"
            # The first form is a real bytes repr ending in escaped LF.
            values = [repr(("Bearer " + token + "\n").encode()), "Bearer " + token,
                      'b"bEaReR ' + token + r'\r\n"']
            values += [
                prefix + quote + "Bearer " + token + suffix + quote
                for prefix in ("b", "", "B")
                for quote in ("'", '"')
                for suffix in (r"\n", r"\r\n", r"\t", r"\\", r"\'", r'\"', "")
            ]
            for command in ("python synthetic_fixture.py", "pytest -q", None):
                for force in (False, True):
                    for ending in ("", "\n", "\r\n", "\r"):
                        for prefix in (
                            "httpx.LocalProtocolError: Invalid header value ",
                            "ValueError: Invalid header value ",
                            "E       ValueError: Invalid header value ",
                            "  E   header = ",
                        ):
                            for value in values:
                                # Keep pytest source/traceback context byte-for-byte.
                                context = "    header = b'Bearer " + token + r"\n'" + "\n"
                                raw = context + prefix + value + ending
                                expected = raw.replace(
                                    prefix + value,
                                    prefix + value.replace(token, "[redacted]").replace("bEaReR ", "Bearer "),
                                )
                                actual = redact_terminal_output(raw, command=command, force=force)
                                assert actual == expected, (command, force, prefix, value, ending)
            masked = redact_terminal_output(
                "ValueError: Invalid header value " + values[0], force=True,
            ).split("Invalid header value ", 1)[1]
            assert ast.literal_eval(masked) == b"Bearer [redacted]\n"
        ''')

    def test_preserves_source_unrelated_output_and_disabled_default(self):
        _run_isolated(False, r'''
            from agent.redact import redact_terminal_output

            token = "syntheticOpaqueFixture000000000000000000000000"
            value = repr(("Bearer " + token + "\n").encode())
            diagnostic = "httpx.LocalProtocolError: Invalid header value " + value + "\r\n"
            preserved = [
                "header = " + value + "\n",
                "    raise ValueError(" + value + ")\r\n",
                "    header = " + value + "\n>   assert header == expected\n",
                "print(\"ValueError: Invalid header value \" + header)\n",
                "INFO: Invalid header value " + value + "\n",
                "ordinary Bearer " + token + "\n",
                "ValueError: the bearer of bad news\r\n",
                "ValueError: Bearer $SYNTHETIC_TOKEN\n",
                "ValueError: Bearer {token}\n",
                "ValueError: Bearer short\n",
                "ValueError: useBearer " + token + "\n",
                "ValueError: {'token_count': 'synthetic_count', 'retry': True}\n",
            ]
            for command in ("cat synthetic_fixture.py", "python synthetic_fixture.py", "pytest -q", None):
                assert redact_terminal_output(diagnostic, command=command) == diagnostic
                expected = diagnostic.replace(token, "[redacted]")
                assert redact_terminal_output(diagnostic, command=command, force=True) == expected
                assert redact_terminal_output(expected, command=command, force=True) == expected
                for raw in preserved:
                    assert redact_terminal_output(raw, command=command) == raw
                    assert redact_terminal_output(raw, command=command, force=True) == raw
            # Existing keyed repr masking must still run before the Bearer sweep.
            raw = "ValueError: {'API_KEY': '" + token + "'} " + value
            expected = "ValueError: {'API_KEY': '***'} " + value.replace(token, "[redacted]")
            assert redact_terminal_output(raw, force=True) == expected
            assert redact_terminal_output("", force=True) == ""
        ''')
