"""Shared-metrics ownership for finite CLI sessions."""

from hermes_cli import lifecycle
from hermes_cli.observability import relay_shared_metrics
from tests.hermes_cli.test_relay_shared_metrics_runtime import (
    direct_runtime as direct_runtime,
    _finish_desktop_task,
    _join_export_workers,
    _stored_values,
)


def _cli_turn(session_id: str, parent_session_id: str = "") -> None:
    hook = dict(session_id=session_id, task_id=f"{session_id}-t", platform="cli")
    lifecycle.invoke_hook("pre_llm_call", **hook, parent_session_id=parent_session_id)
    relay_shared_metrics.finish_task_run(**hook, result={"completed": True})


def test_finite_cli_runs_report_their_own_entrypoint(direct_runtime, tmp_path, monkeypatch):
    """`hermes -z` / `chat -q` / `-Q` runs (HERMES_SINGLE_QUERY_SESSION) report `one_shot`, a
    dispatcher-spawned one (kanban worker, A2A forward) `background`; the REPL stays `interactive`,
    and a delegated child or a non-CLI surface keeps its own entrypoint inside a finite run."""
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    _cli_turn("repl")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    _cli_turn("oneshot")
    _cli_turn("child", parent_session_id="oneshot")
    _finish_desktop_task("desktop", "desktop-t")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    _cli_turn("kanban")
    for session_id in ("repl", "oneshot", "child", "desktop", "kanban"):
        lifecycle.finalize_session(session_id=session_id)
    _join_export_workers()

    started = sorted((d["entrypoint"], d["execution_surface"], v)
                     for d, v in _stored_values(tmp_path, "hermes.task_run.started"))
    assert started == [
        ("background", "cli", 1), ("delegated", "cli", 1), ("interactive", "cli", 1),
        ("interactive", "desktop", 1), ("one_shot", "cli", 1),
    ]
    finished = {d["entrypoint"] for d, _ in _stored_values(tmp_path, "hermes.task_run.finished")}
    assert finished == {"background", "delegated", "interactive", "one_shot"}
    # Delegated children are not sessions of their own.
    sessions = sorted((d["entrypoint"], d["execution_surface"], d["turn_count_bucket"])
                      for d, _ in _stored_values(tmp_path, "hermes.session.count"))
    assert sessions == [
        ("background", "cli", "1"), ("interactive", "cli", "1"), ("interactive", "desktop", "1"),
        ("one_shot", "cli", "1"),
    ]


def test_oneshot_hard_exit_cleanup_records_the_session_summary(direct_runtime, tmp_path, monkeypatch):
    """`hermes -z` leaves through os._exit, past the atexit hook that closes the metrics session; its
    pre-exit cleanup must write the run's session.count itself."""
    from hermes_cli import main as hermes_main

    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(hermes_main, "_oneshot_cleanup_done", False)
    _cli_turn("z-run")
    hermes_main._cleanup_oneshot_runtime()
    _join_export_workers()

    sessions = _stored_values(tmp_path, "hermes.session.count")
    assert [(d["entrypoint"], d["turn_count_bucket"], v) for d, v in sessions] == [("one_shot", "1", 1)]
