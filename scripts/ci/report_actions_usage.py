#!/usr/bin/env python3
"""Estimate current and local-first GitHub Actions costs from a usage summary."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import TypedDict, cast


RATES_USD = {"linux": 0.006, "windows": 0.010, "macos": 0.062}


@dataclass(frozen=True)
class Projection:
    baseline_cost_usd: float
    proposed_cost_usd: float
    reduction_percent: float
    proposed_minutes: dict[str, int]
    full_confirmations: int
    smoke_minutes_per_pr: int
    history_minutes_per_pr: int


class RiskReplay(TypedDict):
    risk_full_revision_count: int
    unmatched_summary_runs: int


class UsageSummary(TypedDict):
    rounded_minutes: dict[str, int]
    fork_ci_pull_request_runs: int
    fork_ci_push_runs: int
    trusted_policy_runs: int
    trusted_policy_linux_minutes: int
    fork_ci_pull_request_minutes: dict[str, int]
    other_linux_minutes: int
    ordinary_pr_rounded_minutes: dict[str, int]
    risk_replay: RiskReplay


def cost(minutes: dict[str, int]) -> float:
    return sum(minutes.get(os_name, 0) * rate for os_name, rate in RATES_USD.items())


def project(
    summary: UsageSummary,
    smoke_minutes: int | None = None,
    history_minutes: int | None = None,
    full_confirmations: int | None = None,
) -> Projection:
    baseline = summary["rounded_minutes"]
    pull_request_runs = summary["fork_ci_pull_request_runs"]
    confirmations = (
        # Classification runs on revisions, not once per PR. Without evidence
        # that unmatched runs avoided full checks, count those conservatively too.
        summary["risk_replay"]["risk_full_revision_count"]
        + summary["risk_replay"]["unmatched_summary_runs"]
        if full_confirmations is None
        else full_confirmations
    )
    ordinary = summary["ordinary_pr_rounded_minutes"]
    smoke = ordinary["smoke"] if smoke_minutes is None else smoke_minutes
    history = ordinary["history"] if history_minutes is None else history_minutes
    policy_minutes = summary["trusted_policy_linux_minutes"]
    dependency_minutes = summary.get("other_linux_minutes", 0)
    average_full_minutes = {
        os_name: math.ceil(minutes / pull_request_runs) * confirmations
        for os_name, minutes in summary["fork_ci_pull_request_minutes"].items()
    }

    proposed_minutes = {
        "linux": (
            pull_request_runs * (smoke + history)
            + policy_minutes
            + dependency_minutes
            + average_full_minutes.get("linux", 0)
        ),
        "windows": average_full_minutes.get("windows", 0),
        "macos": average_full_minutes.get("macos", 0),
    }
    proposed_cost = cost(proposed_minutes)
    baseline_cost = cost(baseline)
    reduction = 100.0 * (baseline_cost - proposed_cost) / baseline_cost
    return Projection(
        baseline_cost_usd=round(baseline_cost, 2),
        proposed_cost_usd=round(proposed_cost, 2),
        reduction_percent=round(reduction, 2),
        proposed_minutes=proposed_minutes,
        full_confirmations=confirmations,
        smoke_minutes_per_pr=smoke,
        history_minutes_per_pr=history,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument(
        "--smoke-minutes",
        type=int,
        help="override observed rounded Linux minutes for the smoke job",
    )
    parser.add_argument(
        "--history-minutes",
        type=int,
        help="override observed rounded Linux minutes for the separate history job",
    )
    parser.add_argument(
        "--full-confirmations",
        type=int,
        help="override observed risk revisions plus unmatched runs for an explicit scenario",
    )
    parser.add_argument("--minimum-reduction", type=float, default=80.0)
    args = parser.parse_args()

    summary = cast(UsageSummary, json.loads(args.summary.read_text(encoding="utf-8")))
    result = project(
        summary,
        smoke_minutes=args.smoke_minutes,
        history_minutes=args.history_minutes,
        full_confirmations=args.full_confirmations,
    )
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    if result.reduction_percent < args.minimum_reduction:
        print(
            f"projected reduction {result.reduction_percent:.2f}% is below "
            f"{args.minimum_reduction:.2f}%"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
