"""Tests for BaseEnvironment unified execution model.

Tests _wrap_command(), _extract_cwd_from_output(), _embed_stdin_heredoc(),
init_session() failure handling, and the CWD marker contract.
"""

from concurrent.futures import ThreadPoolExecutor, wait
from unittest.mock import MagicMock
import os
import threading

import pytest

from tools import terminal_tool_sudo
from tools.environments import base_output
import tools.environments.base as base_environment
from tools.environments.base import BaseEnvironment, _load_json_store
from tools.environments.base_output import _BoundedOutputCollector


def test_snapshot_store_reads_dict_and_tolerates_missing_or_damaged_file(tmp_path):
    path = tmp_path / "snapshots.json"
    assert _load_json_store(path) == {}
    path.write_text('{"task": "snapshot"}', encoding="utf-8")
    assert _load_json_store(path) == {"task": "snapshot"}
    path.write_text('["not a mapping"]', encoding="utf-8")
    assert _load_json_store(path) == {}
    path.write_text("{bad json", encoding="utf-8")
    assert _load_json_store(path) == {}


class _TestableEnv(BaseEnvironment):
    """Concrete subclass for testing base class methods."""

    _passwordless_sudo_probe_supported = True

    def __init__(self, cwd="/tmp", timeout=10):
        super().__init__(cwd=cwd, timeout=timeout)

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        raise NotImplementedError("Use mock")

    def cleanup(self):
        pass


def test_prepare_command_uses_selected_environment_for_nopasswd(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    env = _TestableEnv()
    monkeypatch.setattr(env, "_sudo_nopasswd_works", lambda: True)

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for NOPASSWD")

    monkeypatch.setattr(terminal_tool_sudo, "_prompt_for_sudo_password", _fail_prompt)

    assert env._prepare_command("sudo true") == ("sudo true", None)


@pytest.mark.parametrize(
    ("supported", "returncode", "expected", "probed"),
    [(True, 0, True, True), (True, 1, False, True), (False, 0, False, False)],
)
def test_nopasswd_probe_runs_sudo_n_inside_backend_only_when_supported(
    monkeypatch, supported, returncode, expected, probed
):
    env = _TestableEnv()
    env._passwordless_sudo_probe_supported = supported
    run = MagicMock(return_value=object())
    monkeypatch.setattr(env, "_run_bash", run)
    monkeypatch.setattr(env, "_wait_for_process", MagicMock(return_value={"returncode": returncode}))

    assert env._sudo_nopasswd_works() is expected
    assert run.called is probed
    if probed:
        assert run.call_args.args[0] == "sudo -n true"


def test_execute_explains_host_exit_and_does_not_retry(monkeypatch):
    env = _TestableEnv()
    env._snapshot_ready = True
    run_bash = MagicMock()
    monkeypatch.setattr(env, "_run_bash", run_bash)
    monkeypatch.setattr(base_environment, "_enter_foreground_spawn", lambda: False)

    result = env.execute("printf 'must not run'")

    assert result["returncode"] == 130
    assert "[host is exiting: command not started]" in result["output"]
    assert "Hermes is restarting or shutting down" in result["output"]
    assert "command was NOT run" in result["output"]
    assert "work will resume after restart" in result["output"]
    assert "Do not retry this command in this turn" in result["output"]
    run_bash.assert_not_called()


class TestBoundedOutputCollector:
    def test_large_stream_retains_bounded_head_and_tail(self):
        collector = _BoundedOutputCollector(1_000)
        collector.append("HEAD-SENTINEL\n")
        for _ in range(2_000):
            collector.append("x" * 4_096)
        collector.append("\nTAIL-SENTINEL")

        rendered = collector.render()

        assert collector.total_chars > 8_000_000
        assert collector.buffered_chars <= 1_000
        assert len(rendered) <= 1_000
        assert rendered.startswith("HEAD-SENTINEL")
        assert rendered.endswith("TAIL-SENTINEL")
        assert "[OUTPUT TRUNCATED" in rendered


    def test_required_status_suffix_stays_inside_limit(self):
        collector = _BoundedOutputCollector(120)
        collector.append("A" * 10_000)

        rendered = collector.render(suffix="\n[Command timed out after 1s]")

        assert len(rendered) <= 120
        assert rendered.endswith("[Command timed out after 1s]")
        assert "[OUTPUT TRUNCATED" in rendered


class TestSpillPruning:
    """Regression: every bounded terminal call used to enumerate and stat the
    whole spill directory on the calling thread (a live gateway had 15 stacks
    doing this concurrently over ~6,400 files)."""

    @pytest.fixture(autouse=True)
    def _no_inherited_prune(self):
        # A prune started by an earlier test must not make this one's caller skip.
        self._join_prune()

    @staticmethod
    def _setup(tmp_path, monkeypatch, scandir_hook=None):
        spill_dir = tmp_path / "cache" / "terminal-output"
        spill_dir.mkdir(parents=True)
        monkeypatch.setattr(base_output, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(base_output, "_spill_prune_last", {}, raising=False)
        original_scandir = os.scandir
        scans = []
        scans_lock = threading.Lock()

        def counting_scandir(path=None):
            # pathlib scans "<dir>/"; normalize so any scan of the directory counts.
            if path is not None and os.path.normpath(os.fspath(path)) == os.path.normpath(spill_dir):
                with scans_lock:
                    scans.append(path)
                if scandir_hook is not None:
                    scandir_hook()
            return original_scandir(path) if path is not None else original_scandir()

        monkeypatch.setattr(os, "scandir", counting_scandir)
        return spill_dir, scans

    @staticmethod
    def _join_prune():
        thread = getattr(base_output, "_spill_prune_thread", None)
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()

    def test_concurrent_collectors_trigger_at_most_one_prune_and_never_wait(
        self, tmp_path, monkeypatch
    ):
        scan_started = threading.Event()
        release_scan = threading.Event()

        def block():
            scan_started.set()
            release_scan.wait(timeout=10)

        _, scans = self._setup(tmp_path, monkeypatch, scandir_hook=block)
        # Rate limiting off: only single-flight can keep this at one scan.
        monkeypatch.setattr(base_output, "_SPILL_PRUNE_INTERVAL_S", 0.0, raising=False)
        try:
            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = [
                    pool.submit(base_output._new_output_collector, object(), True)
                    for _ in range(20)
                ]
                done, _ = wait(futures, timeout=5)
                # Collector creation must not wait on the in-flight scan.
                assert len(done) == 20
                assert scan_started.wait(timeout=5)
                assert len(scans) == 1
        finally:
            release_scan.set()
            self._join_prune()
        assert len(scans) == 1

    def test_second_collector_within_interval_skips_scan_then_scans_again(
        self, tmp_path, monkeypatch
    ):
        _, scans = self._setup(tmp_path, monkeypatch)
        clock = [100.0]
        monkeypatch.setattr(base_output, "_monotonic", lambda: clock[0], raising=False)

        base_output._new_output_collector(object(), True)
        self._join_prune()
        clock[0] += 1.0
        base_output._new_output_collector(object(), True)
        self._join_prune()
        assert len(scans) == 1

        clock[0] += base_output._SPILL_PRUNE_INTERVAL_S
        base_output._new_output_collector(object(), True)
        self._join_prune()
        assert len(scans) == 2

    @pytest.mark.platforms("linux", "macos")
    def test_fork_while_gate_held_leaves_child_able_to_prune(self):
        gate = base_output._spill_prune_gate
        assert gate.acquire(blocking=False)
        try:
            pid = os.fork()
            if pid == 0:  # child: only os._exit, never return into pytest
                os._exit(0 if base_output._spill_prune_gate.acquire(blocking=False) else 1)
        finally:
            gate.release()
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, "child inherited a held prune gate"

    def test_old_spills_are_still_pruned_and_fresh_ones_kept(self, tmp_path, monkeypatch):
        spill_dir, _ = self._setup(tmp_path, monkeypatch)
        stale = spill_dir / "out-1-2-dead.log"
        fresh = spill_dir / "out-2-3-beef.log"
        unrelated = spill_dir / "keep-me.txt"
        for path in (stale, fresh, unrelated):
            path.write_text("x")
        os.utime(stale, (1, 1))
        os.utime(unrelated, (1, 1))

        base_output._new_output_collector(object(), True)
        self._join_prune()

        assert not stale.exists()
        assert fresh.exists()
        assert unrelated.exists()


class TestAtomicSnapshotWrite:
    """Regression for #38249: concurrent terminal calls in one session both
    source AND rewrite the shared env snapshot. A non-atomic ``export -p >
    snap`` truncates-then-writes in place, so a concurrent ``source snap`` can
    read a half-written file and embed ``declare -x``/``export`` fragments into
    PATH, breaking ``ls``/``git``/``tr`` with command-not-found. The write must
    assemble in a temp file and ``mv -f`` it into place (mv is atomic on POSIX
    same-fs), so a reader sees the old-or-new complete file, never a torn one.
    """

    def test_wrap_command_uses_atomic_temp_then_mv(self):
        env = _TestableEnv()
        env._snapshot_ready = True
        wrapped = env._wrap_command("echo hi", "/tmp")
        # Env dump goes to a temp file, not directly over the live snapshot.
        assert "export -p" in wrapped and "> " in wrapped
        assert ".tmp." in wrapped
        # Then an atomic rename onto the real snapshot path.
        assert "mv -f " in wrapped
        # The env-dump must NOT write the live snapshot in place (the bug).
        snap = env._snapshot_path
        assert f"> {snap} " not in wrapped
        assert f"> '{snap}'" not in wrapped
        assert f"> {snap}\n" not in wrapped

    def test_temp_path_uses_mktemp_not_pid_variables(self):
        """The temp name MUST be allocated by ``mktemp`` — never ``$$`` (in
        ``&``-launched concurrent subshells it stays the parent shell's PID, so
        two writers would pick the same temp name and publish a torn file) and
        never ``$BASHPID`` (macOS ships bash 3.2, which lacks it — the name
        expands empty, collapsing every writer onto one temp path and
        reopening the #38249 race).  Regression for PR #54314."""
        env = _TestableEnv()
        env._snapshot_ready = True
        wrapped = env._wrap_command("echo hi", "/tmp")
        assert "mktemp " in wrapped
        assert ".tmp.XXXXXXXXXX" in wrapped
        assert "$BASHPID" not in wrapped
        # The bare $$ temp form must be gone.
        assert ".tmp.$$" not in wrapped


    def test_init_session_bootstrap_also_atomic_and_mktemp(self):
        """The init_session bootstrap (first snapshot write) is the same shared
        file a concurrent command could source — it must be atomic and use
        ``mktemp`` too (no ``$BASHPID``: absent on macOS bash 3.2)."""
        env = _TestableEnv()
        captured = {}

        def fake_run_bash(cmd_string, *, login=False, timeout=120, stdin_data=None):
            captured.setdefault("cmd", cmd_string)  # only the bootstrap; ignore the failure-path probe
            raise RuntimeError("stop after capture")

        env._run_bash = fake_run_bash  # type: ignore[assignment]
        try:
            env.init_session()
        except Exception:
            pass
        boot = captured.get("cmd", "")
        assert ".tmp." in boot and "mv -f " in boot, boot
        assert "mktemp " in boot
        assert "$BASHPID" not in boot
        assert ".tmp.$$" not in boot






class TestSnapshotFileModes:
    """Snapshot metadata files are private without changing user command umask."""

    def test_snapshot_and_cwd_files_are_0600(self, tmp_path):
        import os
        from pathlib import Path
        import shutil
        import stat
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            import pytest
            pytest.skip("bash required")

        class ExecutableEnv(BaseEnvironment):
            def __init__(self, temp_dir):
                self._temp_dir = str(temp_dir)
                super().__init__(cwd=str(temp_dir), timeout=10)

            def get_temp_dir(self):
                return self._temp_dir

            def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
                proc = subprocess.Popen(
                    [bash, "-lc", cmd_string],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    cwd=self.cwd,
                )
                proc.communicate(timeout=timeout)
                return proc

            def cleanup(self):
                pass

        old_umask = os.umask(0o022)
        try:
            env = ExecutableEnv(tmp_path)
            env.init_session()

            user_file = tmp_path / "user-created.txt"
            env.execute(f"touch {user_file}")

            assert stat.S_IMODE(user_file.stat().st_mode) == 0o644
            assert stat.S_IMODE(Path(env._snapshot_path).stat().st_mode) == 0o600
            # The cwd temp file is no longer written (cwd travels via the
            # stdout marker for every backend) — nothing to leak on disk.
            assert not Path(env._cwd_file).exists()
        finally:
            os.umask(old_umask)


class TestExtractCwdFromOutput:
    def test_happy_path(self):
        env = _TestableEnv()
        marker = env._cwd_marker
        result = {
            "output": f"hello\n{marker}/home/user{marker}\n",
        }
        env._extract_cwd_from_output(result)

        assert env.cwd == "/home/user"
        assert marker not in result["output"]




class TestEmbedStdinHeredoc:

    def test_unique_delimiter_each_call(self):
        r1 = BaseEnvironment._embed_stdin_heredoc("cat", "data")
        r2 = BaseEnvironment._embed_stdin_heredoc("cat", "data")

        # Extract delimiters
        d1 = r1.split("'")[1]
        d2 = r2.split("'")[1]
        assert d1 != d2  # UUID-based, should be unique

    def test_compound_command_receives_stdin_as_a_group(self):
        import shutil
        import subprocess

        import pytest

        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash required")

        command = BaseEnvironment._embed_stdin_heredoc(
            'IFS= read -r first; IFS= read -r second; '
            'printf \'<%s|%s>\' "$first" "$second"',
            "alpha\nbeta",
        )

        result = subprocess.run(
            [bash, "-c", command],
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout == "<alpha|beta>"


class TestInitSessionFailure:
    def test_snapshot_ready_false_on_failure(self):
        env = _TestableEnv()

        def failing_run_bash(*args, **kwargs):
            raise RuntimeError("bash not found")

        env._run_bash = failing_run_bash
        env.init_session()

        assert env._snapshot_ready is False


    def test_prefer_nonlogin_when_login_bash_is_dead(self):
        """Login snapshot failure + working non-login probe → don't use bash -l."""
        env = _TestableEnv()

        def mock_run_bash(cmd, *, login=False, timeout=120, stdin_data=None):
            mock = MagicMock()
            mock.poll.return_value = 0
            mock.stdout = iter([])
            if login:
                mock.returncode = 1
            else:
                mock.returncode = 0
            return mock

        env._run_bash = mock_run_bash
        env.init_session()

        assert env._snapshot_ready is False
        assert env._prefer_nonlogin is True

        calls = []

        def track_run_bash(cmd, *, login=False, timeout=120, stdin_data=None):
            calls.append({"login": login})
            mock = MagicMock()
            mock.poll.return_value = 0
            mock.returncode = 0
            mock.stdout = iter([])
            return mock

        env._run_bash = track_run_bash
        env.execute("echo test")

        assert calls[0]["login"] is False


class TestCwdMarker:

    def test_unique_per_instance(self):
        env1 = _TestableEnv()
        env2 = _TestableEnv()
        assert env1._cwd_marker != env2._cwd_marker


class TestSanitizeTaskIdForPath:
    """sanitize_task_id_for_path must yield mountable, collision-free segments.

    A raw task id like ``session:agent:main:telegram:dm:12345`` used as a
    sandbox directory name made docker -v split the bind-mount on the embedded
    colons and the daemon rejected it with "invalid mode" / exit 125 (#92414).
    The helper is shared by every backend that builds host paths from task_id
    (docker persistent sandboxes, singularity overlays), fixing the class once.
    """

    def test_docker_unsafe_characters_are_replaced(self):
        from tools.environments.path_utils import sanitize_task_id_for_path

        out = sanitize_task_id_for_path("session:agent:main:telegram:dm:12345")
        assert ":" not in out
        assert "/" not in out and "\\" not in out

    def test_safe_ids_pass_through_verbatim(self):
        """Existing sandboxes keep resolving to their current directory."""
        from tools.environments.path_utils import sanitize_task_id_for_path

        for value in ("default", "task-01.abc_def", "astropy__astropy-12907"):
            assert sanitize_task_id_for_path(value) == value

    def test_deterministic_and_collision_free_for_distinct_inputs(self):
        from tools.environments.path_utils import sanitize_task_id_for_path

        assert sanitize_task_id_for_path("a:b") == sanitize_task_id_for_path("a:b")
        # substitution alone is not injective — the digest must disambiguate
        assert sanitize_task_id_for_path("a:b") != sanitize_task_id_for_path("a_b")
        assert sanitize_task_id_for_path("!!!") != sanitize_task_id_for_path("@@@")

    def test_empty_and_traversal_inputs_are_neutralized(self):
        from tools.environments.path_utils import sanitize_task_id_for_path

        assert sanitize_task_id_for_path("") == "default"
        for value in (".", "..", "../../etc", "..\\..\\escape"):
            out = sanitize_task_id_for_path(value)
            assert out not in {".", ".."}
            assert "/" not in out and "\\" not in out

    def test_oversized_input_truncates_with_unique_digest(self):
        from tools.environments.path_utils import (
            _SANDBOX_DIR_MAX_LEN,
            sanitize_task_id_for_path,
        )

        long_a = "a" * 300 + ":1"
        long_b = "a" * 300 + ":2"
        out_a = sanitize_task_id_for_path(long_a)
        out_b = sanitize_task_id_for_path(long_b)
        assert len(out_a) <= _SANDBOX_DIR_MAX_LEN
        assert ":" not in out_a
        assert out_a != out_b

    def test_sanitized_dir_is_creatable(self, tmp_path):
        from tools.environments.path_utils import sanitize_task_id_for_path

        target = tmp_path / "docker" / sanitize_task_id_for_path(
            "session:agent:main:telegram:dm:12345"
        )
        target.mkdir(parents=True)
        assert target.is_dir()
