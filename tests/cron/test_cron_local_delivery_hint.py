"""The cron DELIVERY hint must match the job's actual delivery lane.

``deliver: local`` jobs are only saved to local output, so their prompt must not claim the
response is automatically delivered or forbid self-relay that the job's own instructions require.
"""

import pytest

from cron.scheduler_prompt import _CRON_HINT, _build_job_prompt

_AUTO = "automatically delivered"
_LOCAL = "saved to local output only and is not sent anywhere"
_SHARED_TAIL = _CRON_HINT[_CRON_HINT.index("SILENT:"):]


def _prompt(**job):
    return _build_job_prompt({"id": "hint-test", "prompt": "relay the result", **job})


@pytest.mark.parametrize("deliver", ["local", None, "", ["local"], "local,local", " local "])
def test_local_only_jobs_are_told_output_is_saved_not_delivered(deliver):
    job = {} if deliver is None else {"deliver": deliver}
    prompt = _prompt(**job)
    assert _LOCAL in prompt
    assert "do that yourself with the tool they name" in prompt
    assert _AUTO not in prompt
    assert "do NOT use send_message" not in prompt
    # SILENT / FAILURE / RECURSION guidance is unchanged.
    assert _SHARED_TAIL in prompt
    assert prompt.endswith("relay the result")


@pytest.mark.parametrize("deliver", ["origin", "telegram", "local,origin", "origin,local", "all"])
def test_jobs_with_a_delivery_target_keep_the_existing_hint(deliver):
    prompt = _prompt(deliver=deliver)
    assert prompt.startswith(_CRON_HINT)
    assert _LOCAL not in prompt
