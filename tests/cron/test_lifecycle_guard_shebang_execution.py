"""Kernel-rejected shebangs must not turn shell fallback into a lenient walk (#547)."""

import shlex
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.platforms("posix")
def test_overlong_shebang_shell_fallback_fails_closed(tmp_path):
    child = tmp_path / "child.sh"
    child.write_text("#!/bin/sh\n" + "# pad\n" * 200_000 + "printf 'child-ran\\n'\n")
    child.chmod(0o755)
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b"#!/" + b"a" * 600 + b"/python3\n" + f"{shlex.quote(str(child))}\n".encode())
    wrapper.chmod(0o755)

    # Nothing restarts: the child only emits a sentinel proving /bin/sh ran the body.
    result = subprocess.run(["/bin/sh", "-c", shlex.quote(str(wrapper))], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "child-ran\n"
    unsafe, refusal = scan_gateway_lifecycle(str(wrapper))
    assert unsafe is True
    assert refusal is not None and str(child) in refusal


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("case", [
    "crlf", "crlf-direct", "trailing-space", "trailing-space-direct", "relative", "missing",
    "non-executable", "nested", "env-nested", "shebangless-interpreter", "shell-symlink",
])
def test_ambiguous_shebangs_keep_the_strict_walk(tmp_path_factory, case):
    # A short fixture root keeps the real interpreter paths portable across kernels.
    root = tmp_path_factory.mktemp("lg")
    child = root / "child.sh"
    child.write_text("#!/bin/sh\n" + "# pad\n" * 200_000 + "printf 'child-ran\\n'\n")
    child.chmod(0o755)
    interpreter = root / "python3"
    if case == "shell-symlink":
        interpreter.symlink_to("/bin/sh")
    elif case == "non-executable":
        import shutil
        import sys

        shutil.copyfile(sys.executable, interpreter)
        interpreter.chmod(0o644)
    else:
        prefix = "" if case == "shebangless-interpreter" else "#!/bin/sh\n"
        interpreter.write_text(prefix + '/bin/sh "$1"\n')
        interpreter.chmod(0o755)
    shebangs = {
        "crlf": b"#!/usr/bin/env python3\r\n",
        "crlf-direct": b"#!/usr/bin/python3\r\n",
        "trailing-space": b"#!/usr/bin/env python3 \n",
        "trailing-space-direct": b"#!/usr/bin/python3 \n",
        "relative": b"#!python3\n",
        "missing": f"#!{root}/missing/python3\n".encode(),
        "non-executable": f"#!{interpreter}\n".encode(),
        "nested": f"#!{interpreter}\n".encode(),
        "env-nested": f"#!/usr/bin/env {interpreter}\n".encode(),
        "shebangless-interpreter": f"#!{interpreter}\n".encode(),
        "shell-symlink": f"#!{interpreter}\n".encode(),
    }
    shebang = shebangs[case]
    wrapper = root / "wrapper"
    wrapper.write_bytes(shebang + f"{shlex.quote(str(child))}\n".encode())
    wrapper.chmod(0o755)

    result = subprocess.run(["/bin/sh", "-c", shlex.quote(str(wrapper))], capture_output=True, text=True, timeout=10)
    if case in {"nested", "env-nested", "shebangless-interpreter", "shell-symlink"}:
        # Linux can recurse into a script interpreter; kernels that reject it can fall back to
        # shell. Either way this allowlisted basename actually executes the shell-only child.
        assert result.returncode == 0, result.stderr
        assert result.stdout == "child-ran\n"
    else:
        assert result.stdout == "", result.stderr
    unsafe, refusal = scan_gateway_lifecycle(str(wrapper))
    assert unsafe is True
    assert refusal is not None and str(child) in refusal


@pytest.mark.platforms("posix")
def test_remote_text_cannot_prove_interpreter_executability():
    source = "#!/usr/bin/env python3\n/remote/child.sh\n"
    child = "#!/bin/sh\n" + "# pad\n" * 200_000
    texts = {"/remote/wrapper": source, "/remote/child.sh": child}
    unsafe, refusal = scan_gateway_lifecycle("/remote/wrapper", read_remote_script=texts.get)
    assert unsafe is True
    assert refusal is not None and "/remote/child.sh" in refusal