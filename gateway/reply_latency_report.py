"""Read local confirmed-delivery cohorts: python -m gateway.reply_latency_report --since 7d."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import re
import time

logger = logging.getLogger(__name__)
_IDENTITY = ("profile", "platform", "chat_id", "thread_id", "message_id")


def read_records(directory: Path | None = None) -> list[dict]:
    """Fold append-only observations into one record per immutable inbound identity.

    The process sink includes routed profiles, so profile is part of the key.
    Transport/authority turn IDs are diagnostic only. Replays never add a sample.
    """
    if directory is None:
        from hermes_constants import get_hermes_home
        directory = Path(get_hermes_home()) / "logs" / "reply-latency"
    records: dict[tuple, dict] = {}
    invalid_lines = unreadable_files = 0
    for path in sorted(directory.glob("*.jsonl")):
        try:
            with path.open("rb") as stream:
                for number, line in enumerate(stream, 1):
                    try:
                        observation = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        invalid_lines += 1
                        logger.warning("Ignoring invalid latency observation at %s:%s", path, number)
                        continue
                    _fold(records, observation)
        except OSError:
            unreadable_files += 1
            logger.warning("Ignoring unreadable latency file %s", path)
    if invalid_lines or unreadable_files:
        logger.warning("Skipped %s invalid lines and %s unreadable files", invalid_lines, unreadable_files)
    return list(records.values())


def _fold(records: dict, observation: dict) -> None:
    if not isinstance(observation, dict) or observation.get("message_id") is None:
        return
    stamp = observation.get("timestamp")
    if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
        return
    key = tuple(observation.get(name) for name in _IDENTITY)
    row = records.setdefault(key, dict(zip(_IDENTITY, key)))
    event = observation.get("event")
    if event in {"received", "admitted", "agent_start"}:
        row[event] = min(row.get(event, stamp), stamp)
    elif event in {"first_visible", "first_reply"} and observation.get("delivery_kind"):
        # Legacy text-only first_reply events did not observe progress. They cannot
        # establish first-visible latency and must remain missing, not bias p90.
        if stamp < row.get("first_visible", float("inf")):
            row.update(first_visible=stamp, first_visible_kind=observation["delivery_kind"])
    elif event == "final_delivery":
        row["final_delivery"] = max(row.get("final_delivery", stamp), stamp)


def _quantiles(values: list[float]) -> dict:
    values.sort()
    def percentile(p):
        if not values:
            return None
        index = (len(values) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 6)
    return {"n": len(values), "p50_s": percentile(.5), "p90_s": percentile(.9)}


def summarize(records: list[dict], *, since: float, until: float, platform: str = "telegram") -> dict:
    """Select by inbound receipt [since, until), even when delivery crosses midnight."""
    cohort = [r for r in records if r.get("platform") == platform
              and since <= r.get("received", float("-inf")) < until]
    result = {"platform": platform, "since": since, "until": until, "inbounds": len(cohort)}
    for field in ("admitted", "agent_start", "first_visible", "final_delivery"):
        values = [r[field] - r["received"] for r in cohort
                  if field in r and r[field] >= r["received"]]
        result[field] = _quantiles(values)
        result[f"missing_{field}"] = len(cohort) - len(values)
    return result


def _timestamp(value: str, anchor: float) -> float:
    if duration := re.fullmatch(r"(\d+)([dh])", value):
        return anchor - int(duration[1]) * {"d": 86400, "h": 3600}[duration[2]]
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="7d", help="lookback (7d, 24h) or ISO date/time, UTC if naive")
    parser.add_argument("--until", help="exclusive ISO date/time, defaults to now")
    parser.add_argument("--platform", default="telegram")
    parser.add_argument("--log-dir", type=Path, help="defaults to the active profile's logs/reply-latency")
    args = parser.parse_args(argv)
    try:
        until = _timestamp(args.until, time.time()) if args.until else time.time()
        since = _timestamp(args.since, until)
        if since >= until:
            raise ValueError("--since must precede --until")
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summarize(read_records(args.log_dir), since=since, until=until,
                               platform=args.platform), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
