"""Bounded one-shot rerun child for a reviewed report; never performs delivery."""
from __future__ import annotations

import json
import sys


def main():
    from cron.worker_bootstrap import finish_worker_boot
    finish_worker_boot()
    from cron.scheduler import run_job
    job = json.loads(sys.stdin.read())
    job.pop("review", None)
    success, _output, text, error = run_job(job)
    print("HERMES_REVIEW_RESULT=" + json.dumps([success, text, error]))


if __name__ == "__main__":
    main()
