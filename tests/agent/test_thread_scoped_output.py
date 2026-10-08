"""Tests for agent.thread_scoped_output.thread_scoped_silence.

Behaviour contract: a thread inside ``thread_scoped_silence()`` has its
stdout/stderr routed to devnull, while every OTHER thread keeps writing to the
real stream — even concurrently, while the first thread is still inside the
context.  This is the property the old process-global
``contextlib.redirect_stdout(devnull)`` violated (issue #55769 / #55925).
"""

import contextlib
import io
import sys
import threading
import time

import pytest

import agent.thread_scoped_output as thread_output
from agent.thread_scoped_output import thread_scoped_silence


def _run_with_real_stream(fn):
    """Bind a StringIO as the real stdout, run fn, return what reached it."""
    real_out = io.StringIO()
    orig = sys.stdout
    sys.stdout = real_out
    try:
        fn()
    finally:
        sys.stdout = orig
    return real_out.getvalue()






def test_stderr_is_also_routed_per_thread():
    real_err = io.StringIO()
    orig = sys.stderr
    sys.stderr = real_err
    try:
        with thread_scoped_silence():
            sys.stderr.write("err-dropped\n")
        sys.stderr.write("err-kept\n")
    finally:
        sys.stderr = orig
    out = real_err.getvalue()
    assert "err-dropped" not in out
    assert "err-kept" in out






def test_many_concurrent_silenced_and_loud_threads():
    """Stress: interleaved silenced/loud threads keep their respective fates."""
    start = threading.Event()
    results_lock = threading.Lock()

    def silenced(i):
        start.wait(timeout=2.0)
        with thread_scoped_silence():
            print(f"S{i}")
            time.sleep(0.05)

    def loud(i):
        start.wait(timeout=2.0)
        time.sleep(0.02)
        print(f"L{i}")

    def body():
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=silenced, args=(i,)))
            threads.append(threading.Thread(target=loud, args=(i,)))
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=15.0)
        assert not any(t.is_alive() for t in threads), "straggler thread would truncate captured output"

    captured = _run_with_real_stream(body)
    for i in range(5):
        assert f"S{i}" not in captured, f"silenced S{i} leaked"
        assert f"L{i}" in captured, f"loud L{i} swallowed"


def test_repeated_contexts_never_write_to_a_closed_sink():
    """The installed proxy must survive later silenced workers."""
    original = sys.stdout
    try:
        for _ in range(3):
            with thread_scoped_silence():
                sys.stdout.write("hidden\n")
            sys.stdout.fileno()
    finally:
        sys.stdout = original


def test_temporary_global_redirects_do_not_allocate_new_sinks(monkeypatch):
    """A displaced proxy is temporary, not a reason to leak another FD pair."""
    opened_sinks = []

    def fake_open(*_args, **_kwargs):
        sink = io.StringIO()
        opened_sinks.append(sink)
        return sink

    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_sinks", {}, raising=False)
    monkeypatch.setattr(thread_output, "open", fake_open, raising=False)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        with thread_scoped_silence():
            pass
        assert len(opened_sinks) == 2
        original_proxies = dict(thread_output._installed)

        for _ in range(20):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with thread_scoped_silence():
                    print("hidden")

        with thread_scoped_silence():
            pass
        assert len(opened_sinks) == 2
        assert thread_output._installed == original_proxies
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr


def test_silence_survives_redirect_restoring_an_older_proxy(monkeypatch):
    """Silencing is stream-wide, even when a redirect swaps proxy generations."""
    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_sinks", {}, raising=False)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    passthrough = io.StringIO()
    sys.stdout = passthrough
    entered = threading.Event()
    release = threading.Event()

    try:
        with thread_scoped_silence():
            pass

        def worker():
            with thread_scoped_silence():
                entered.set()
                assert release.wait(timeout=10)
                print("must-stay-silenced")

        redirected = io.StringIO()
        with contextlib.redirect_stdout(redirected):
            thread = threading.Thread(target=worker)
            thread.start()
            assert entered.wait(timeout=10)

        release.set()
        thread.join(timeout=10)

        assert not thread.is_alive()
        assert "must-stay-silenced" not in passthrough.getvalue()
        assert "must-stay-silenced" not in redirected.getvalue()
    finally:
        release.set()
        sys.stdout, sys.stderr = original_stdout, original_stderr



def _chain_length(stream):
    """Wrapper layers above the real stream, walked without invoking any ``__getattr__``."""
    seen = []
    while id(stream) not in map(id, seen):
        seen.append(stream)
        state = getattr(stream, "__dict__", {})
        if "_passthrough" in state:
            stream = state["_passthrough"]
        elif type(stream).__name__ == "_SafeWriter":
            stream = object.__getattribute__(stream, "_inner")
        else:
            return len(seen) - 1
    raise AssertionError("stdout wrapper chain loops back on itself")


def _isolate_routing_state(monkeypatch):
    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_sinks", {}, raising=False)
    monkeypatch.setattr(thread_output, "_routing_states", {}, raising=False)


def test_agent_builds_and_silenced_workers_never_grow_the_stdio_chain(monkeypatch):
    """Every agent build re-runs ``_install_safe_stdio`` and every background review re-installs the
    routing proxy. Interleaved, they stacked two wrapper layers per cycle until attribute lookup
    through ``sys.stdout`` hit the recursion limit and failed the gateway turn."""
    from agent.process_bootstrap import _install_safe_stdio

    _isolate_routing_state(monkeypatch)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    real = io.StringIO()
    sys.stdout, sys.stderr = real, io.StringIO()
    try:
        for _ in range(sys.getrecursionlimit()):
            _install_safe_stdio()
            with thread_scoped_silence():
                print("hidden")

        assert _chain_length(sys.stdout) <= 2
        assert sys.stdout.line_buffering is False  # hermes_logging._line_buffer_piped_stdout's probe
        with pytest.raises(AttributeError):
            sys.stdout.attribute_no_stream_has
        print("visible")
        assert real.getvalue() == "visible\n"
    finally:
        for sink in thread_output._sinks.values():
            sink.close()
        sys.stdout, sys.stderr = original_stdout, original_stderr


def test_a_wrapper_cycle_fails_attribute_lookup_cleanly_and_is_repaired_on_install(monkeypatch):
    """A cycle between the two wrappers must raise AttributeError, not RecursionError, and the next
    install must unwrap it back to a real stream."""
    from agent.process_bootstrap import _SafeWriter, _install_safe_stdio

    _isolate_routing_state(monkeypatch)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    real = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", real)
    try:
        proxy = thread_output._ThreadRoutingStream(io.StringIO(), thread_output._RoutingState(io.StringIO()))
        writer = _SafeWriter(proxy)
        proxy._passthrough = writer  # close the cycle: proxy -> writer -> proxy
        sys.stdout = writer

        with pytest.raises(AttributeError):
            writer.line_buffering
        with pytest.raises(AttributeError):
            proxy.line_buffering

        _install_safe_stdio()
        with thread_scoped_silence():
            pass
        _chain_length(sys.stdout)
        print("repaired")
        assert real.getvalue() == "repaired\n"
    finally:
        for sink in thread_output._sinks.values():
            sink.close()
        sys.stdout, sys.stderr = original_stdout, original_stderr


def _legacy_chain(real, depth):
    """Alternating proxy/_SafeWriter layers as a long-running gateway accumulated them before the
    fix: built without the constructors, which now refuse to wrap another wrapper."""
    from agent.process_bootstrap import _SafeWriter

    state = thread_output._RoutingState(io.StringIO())
    stream = real
    for _ in range(depth):
        proxy = object.__new__(thread_output._ThreadRoutingStream)
        proxy.__dict__.update(_passthrough=stream, _state=state)
        writer = object.__new__(_SafeWriter)
        object.__setattr__(writer, "_inner", proxy)
        stream = writer
    return stream


@pytest.mark.parametrize("installer", ["agent_build", "silence"])
def test_an_existing_long_chain_collapses_on_the_next_install(monkeypatch, installer):
    """A process that already holds a chain deeper than the recursion limit must be repaired by
    the next install, not just stop growing: writes through it recursed and were silently lost."""
    from agent.process_bootstrap import _install_safe_stdio

    _isolate_routing_state(monkeypatch)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    real = io.StringIO()
    try:
        sys.stdout = _legacy_chain(real, sys.getrecursionlimit())
        if installer == "agent_build":
            _install_safe_stdio()
        else:
            with thread_scoped_silence():
                pass

        assert _chain_length(sys.stdout) <= 1
        print("after-repair")
        assert real.getvalue() == "after-repair\n"
    finally:
        for sink in thread_output._sinks.values():
            sink.close()
        sys.stdout, sys.stderr = original_stdout, original_stderr


def _wrapper_depth(stream) -> int:
    from agent.process_bootstrap import _SafeWriter

    depth = 0
    while True:
        if isinstance(stream, _SafeWriter):
            stream = object.__getattribute__(stream, "_inner")
        elif isinstance(stream, thread_output._ThreadRoutingStream):
            stream = stream._passthrough
        else:
            return depth
        depth += 1


def test_safe_stdio_and_silence_do_not_grow_a_wrapper_chain(monkeypatch):
    """Every agent init installs safe stdio and background work installs the routing proxy.
    Alternating them must stay bounded: an unbounded __getattr__ chain hit the recursion
    limit in a long-lived gateway (review_candidate, subagent construction)."""
    from agent.process_bootstrap import _install_safe_stdio

    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_routing_states", {})
    real = io.StringIO()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = real, io.StringIO()
    try:
        depths = []
        for _ in range(50):
            _install_safe_stdio()
            with thread_scoped_silence():
                sys.stdout.write("hidden\n")
            sys.stdout.write("kept\n")
            depths.append(_wrapper_depth(sys.stdout))
        assert max(depths) <= 2
        assert depths[-1] == depths[1]
        sys.stdout.flush  # attribute delegation resolves without recursion
        assert "hidden" not in real.getvalue()
        assert real.getvalue().count("kept") == 50
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
