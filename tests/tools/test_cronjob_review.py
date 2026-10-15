"""Cron tool review opt-in shares the store validation and survives later edits."""
import json

import pytest

from cron.jobs import get_job, update_job
from tools.cronjob_tools import cronjob


def test_tool_can_create_update_and_clear_review(tmp_path, monkeypatch):
    for name, path in (("CRON_DIR", tmp_path / "cron"), ("JOBS_FILE", tmp_path / "cron/jobs.json"),
                       ("OUTPUT_DIR", tmp_path / "cron/output")):
        monkeypatch.setattr(f"cron.jobs.{name}", path)
    review = {"contract": "reports/stripe-brief.yaml", "subject": "Stripe Priorities", "max_reruns": 0}
    result = json.loads(cronjob(action="create", prompt="Report", schedule="every 1h", deliver="local",
                               review_json=json.dumps(review)))
    assert result["success"]
    job = result["job"]
    assert job["review"]["max_reruns"] == 0
    saved = get_job(job["job_id"])
    assert saved["output_local_only"] is True
    assert json.loads(cronjob(action="update", job_id=saved["id"], name="Updated"))["success"]
    assert get_job(saved["id"])["review"] == saved["review"]
    with pytest.raises(ValueError, match="requires deliver=local"):
        update_job(saved["id"], {"deliver": "telegram:chat:topic"})
    cleared = json.loads(cronjob(action="update", job_id=saved["id"], clear_review=True))
    assert cleared["success"]
    assert get_job(saved["id"])["review"] is None


@pytest.mark.parametrize("field, value", [("max_reruns", 2), ("max_reruns", 0.5),
                                        ("deadline_minutes", 0), ("rerun_deadline_minutes", float("inf"))])
def test_review_rejects_invalid_budgets(field, value):
    from cron.review import normalize_review
    with pytest.raises(ValueError):
        normalize_review({"contract": "x", "subject": "y", field: value})
