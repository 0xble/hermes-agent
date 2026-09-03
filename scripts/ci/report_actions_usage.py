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


class UsageSummary(TypedDict):
    rounded_minutes: dict[str, int]
    fork_ci_pull_request_runs: int
    fork_ci_push_runs: int
    trusted_policy_runs: int
    trusted_policy_linux_minutes: int
    fork_ci_pull_request_minutes: dict[str, int]
    other_linux_minutes: int


def cost(minutes: dict[str, int]) -> float:
    return sum(minutes.get(os_name, 0) * rate for os_name, rate in RATES_USD.items())


def project(summary: UsageSummary, smoke_minutes: int, full_confirmations: int) -> Projection:
    baseline = summary["rounded_minutes"]
    pull_request_runs = summary["fork_ci_pull_request_runs"]
    policy_minutes = summary["trusted_policy_linux_minutes"]
    dependency_minutes = summary.get("other_linux_minutes", 0)
    average_full_minutes = {
        os_name: math.ceil(minutes / pull_request_runs) * full_confirmations
        for os_name, minutes in summary["fork_ci_pull_request_minutes"].items()
    }

    proposed_minutes = {
        "linux": (
            pull_request_runs * smoke_minutes
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
        round(baseline_cost, 2),
        round(proposed_cost, 2),
        round(reduction, 2),
        proposed_minutes,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--smoke-minutes", type=int, default=3)
    parser.add_argument("--full-confirmations", type=int, default=1)
    parser.add_argument("--minimum-reduction", type=float, default=80.0)
    args = parser.parse_args()

    summary = cast(UsageSummary, json.loads(args.summary.read_text(encoding="utf-8")))
    result = project(summary, args.smoke_minutes, args.full_confirmations)
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
