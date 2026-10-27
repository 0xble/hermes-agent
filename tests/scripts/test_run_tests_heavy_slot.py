"""Admission precedes activation and compilation, regardless of launch spelling."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(tmp_path):
    scripts = tmp_path / "checkout with spaces" / "scripts"
    scripts.mkdir(parents=True)
    entry = scripts / "run_tests.sh"
    shutil.copy2(ROOT / "scripts/run_tests.sh", entry)
    log = tmp_path / "events.jsonl"
    scripts.joinpath("_activation.sh").write_text(
        f"printf '%s\\n' '\"activation\"' >> '{log}'\n"
        'hermes_activation_current() { [ "${__HERMES_ACTIVATED:-}" = 1 ]; }\n'
    )
    scripts.joinpath("run-in-hermes-env").write_text(
        '#!/bin/bash\nexport __HERMES_ACTIVATED=1\n'
        'export __HERMES_TEST_PYTHON="$HERMES_PYTHON"\n'
        'exec "$@"\n'
    )
    scripts.joinpath("run-in-hermes-env").chmod(0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    python = commands / "python"
    python.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "stage = 'probe' if sys.argv[1] == '-c' else 'compile' if sys.argv[1] == '-m' else 'runner'\n"
        f"with open({str(log)!r}, 'a') as out:\n"
        "    out.write(json.dumps({'stage': stage, 'args': sys.argv[1:], 'held': os.environ.get('HEAVY_SLOT_HELD'), 'workers': os.environ.get('HERMES_TEST_WORKERS')}) + '\\n')\n"
        "if stage == 'runner': sys.exit(23)\n"
    )
    python.chmod(0o755)
    # Supply only the shell tools used by the runner. In particular, host shell
    # functions and the installed heavy-slot cannot affect the absent-tool case.
    for name in ("bash", "dirname", "env", "sed", "head"):
        commands.joinpath(name).symlink_to(shutil.which(name))
    commands.joinpath("git").write_text("#!/bin/bash\nexit 0\n")
    commands.joinpath("git").chmod(0o755)
    env = {"PATH": str(commands), "HOME": str(tmp_path), "HERMES_PYTHON": str(python)}
    return entry, commands, log, env


def install_slot(commands, log, mode):
    commands.joinpath("heavy-slot").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"with open({str(log)!r}, 'a') as out: out.write(json.dumps('slot') + '\\n')\n"
        "assert sys.argv[1] == '--label'\n"
        "assert sys.argv[3] == '--'\n"
        f"if {mode!r} == 'denied': sys.exit(75)\n"
        f"if {mode!r} == 'held': os.environ['HEAVY_SLOT_HELD'] = '0'\n"
        "os.execvp(sys.argv[4], sys.argv[4:])\n"
    )
    commands.joinpath("heavy-slot").chmod(0o755)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("launch", ["direct", "bash", "absolute", "timeout"])
@pytest.mark.parametrize("args", [[], ["tests/"], ["-k", "canary"], ["tests/canary.py"]])
def test_admission_denial_starts_no_activation_or_python(runner, launch, args):
    entry, commands, log, env = runner
    install_slot(commands, log, "denied")
    if launch == "direct":
        command = ["./scripts/run_tests.sh"]
    elif launch == "bash":
        command = ["bash", "scripts/run_tests.sh"]
    elif launch == "absolute":
        command = [str(entry)]
    else:
        timeout = shutil.which("timeout") or shutil.which("gtimeout")
        if timeout is None:
            pytest.skip("timeout executable unavailable")
        command = [timeout, "10", "scripts/run_tests.sh"]
    result = subprocess.run(command + args, cwd=entry.parent.parent, env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 75, result.stdout + result.stderr
    assert [json.loads(line) for line in log.read_text().splitlines()] == ["slot"]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", ["held", "reuse", "disabled", "absent", "activation"])
def test_admission_reexec_preserves_arguments_budget_and_failure(runner, mode):
    entry, commands, log, env = runner
    if mode != "absent":
        install_slot(commands, log, "held" if mode == "activation" else mode)
    if mode == "reuse":
        env["HEAVY_SLOT_HELD"] = "0"
    if mode == "activation":
        # A stale activation skips the explicit-interpreter shortcut on first entry.
        env["__HERMES_ACTIVATED"] = "stale"
    args = ["--paths", "tests/a directory", "-k", "two words", "--", "-q"]
    result = subprocess.run(["bash", str(entry), *args], env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 23, result.stdout + result.stderr
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert events.count("slot") == (0 if mode in {"reuse", "absent"} else 1)
    if mode not in {"reuse", "absent"}:
        assert events[0] == "slot"
    compilation, final = [event for event in events if isinstance(event, dict) and event["stage"] != "probe"]
    assert compilation["args"][:5] == ["-m", "compileall", "-q", "-j", "4"]
    assert final["args"][1:] == args
    assert final["workers"] == "4"
    assert final["held"] == ("0" if mode in {"held", "reuse", "activation"} else None)
