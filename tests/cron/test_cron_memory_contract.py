"""Cron memory contract adapted to the intentional maintained-fork policy HERMES-036.

Upstream #91384 disabled persistent memory; #91447 enabled it by default.
This fork deliberately retains external-provider suppression and permits only
explicit per-job local MEMORY.md/USER.md opt-in. See MAINTENANCE.md HERMES-036
and tests/agent/test_skip_memory_store_65429.py for the real store/provider
boundary. A job cannot change skip_memory directly, and a profile denylist
still overrides a local-memory request.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

from cron.scheduler import run_job


@contextlib.contextmanager
def _run_job_patches(tmp_path):
    """Patch bundle so run_job runs offline; yields (fake_db, mock_agent_cls).

    Mirrors tests/cron/test_scheduler.py::_run_job_patches — every patch is
    entered via one ExitStack so none can be silently dropped.
    ``cron.scheduler._hermes_home`` is pointed at ``tmp_path`` so run_job's
    config load reads ``tmp_path/config.yaml`` (write one to exercise config
    toggles).
    """
    fake_db = MagicMock()
    fake_db.get_compression_tip.side_effect = lambda session_id: session_id
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "ok"}
    base = [
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler_delivery._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state_registry.acquire", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", return_value=mock_agent),
    ]
    with contextlib.ExitStack() as stack:
        entered = [stack.enter_context(cm) for cm in base]
        yield fake_db, entered[-1]


class TestCronMemoryContractOn:
    """Only an explicit job toolset request enables local memory."""

    def test_resolver_requires_explicit_local_memory_opt_in(self):
        from cron.scheduler import _resolve_cron_disabled_toolsets

        assert "memory" in _resolve_cron_disabled_toolsets({})
        assert "memory" in _resolve_cron_disabled_toolsets(
            {"cron": {"allow_agent_scheduling": True}}
        )
        assert "memory" not in _resolve_cron_disabled_toolsets(
            {"enabled_toolsets": ["memory", "file"]}, {}
        )


class TestCronMemoryContractOff:
    """Direction (b): the supported OFF switch stays off."""

    def test_config_disabled_toolsets_denies_memory(self, tmp_path):
        """agent.disabled_toolsets: [memory] in config.yaml denies the toolset.

        This is the intended user-level off-switch after #91447: the user
        denylist layers onto cron's base denylist (#25752), so the memory
        tool is denied AND agent_init treats a denylisted toolset as
        not-requested. A per-job enabled_toolsets cannot widen past it.
        """
        (tmp_path / "config.yaml").write_text(
            "agent:\n  disabled_toolsets:\n    - memory\n"
        )
        job = {
            "id": "mem-contract-off",
            "name": "t",
            "prompt": "hi",
            "enabled_toolsets": ["memory", "file"],
        }
        with _run_job_patches(tmp_path) as (_db, agent_cls):
            run_job(job)
        kwargs = agent_cls.call_args.kwargs
        assert "memory" in (kwargs["disabled_toolsets"] or []), (
            "config.yaml agent.disabled_toolsets must propagate 'memory' into "
            "the cron agent's denylist — the OFF direction of the contract"
        )

    def test_skip_memory_is_not_a_per_job_knob(self, tmp_path):
        """An unsupported job field cannot enable external memory providers."""
        job = {
            "id": "mem-contract-noknob",
            "name": "t",
            "prompt": "hi",
            "skip_memory": False,  # not a supported job field; must be ignored
        }
        with _run_job_patches(tmp_path) as (_db, agent_cls):
            run_job(job)
        kwargs = agent_cls.call_args.kwargs
        assert kwargs["skip_memory"] is True
