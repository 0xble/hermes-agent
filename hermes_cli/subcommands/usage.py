"""``hermes usage`` — the account-limits block of the REPL ``/usage`` without starting a session.

Script-friendly Codex / Anthropic / OpenRouter quota view (issue #33094): same fetch and renderer as
``/usage`` (``agent.account_usage``), same credential resolution as a session with no live agent, plus
``--json`` for cron jobs and shell loops. Slim redo of #81819 (@himanusia).
"""

from __future__ import annotations

import argparse
import json
import sys


def usage_snapshot_document(snapshot) -> dict:
    """``hermes usage --json`` document. Schema is documented in website/docs/reference/cli-commands.md —
    keep the keys stable; extend only by adding keys."""
    return {
        "provider": snapshot.provider,
        "source": snapshot.source,
        "title": snapshot.title,
        "plan": snapshot.plan,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "windows": [
            {
                "label": window.label,
                "used_percent": window.used_percent,
                "resets_at": window.reset_at.isoformat() if window.reset_at else None,
                "detail": window.detail,
            }
            for window in snapshot.windows
        ],
        "details": list(snapshot.details),
        "unavailable_reason": snapshot.unavailable_reason,
    }


def cmd_ledger(args: argparse.Namespace) -> int:
    """Read local size/route-only provider request rows; no provider calls are made."""
    from hermes_cli.observability.request_usage_ledger import request_usage_rows

    rows = request_usage_rows(
        source=getattr(args, "source", None), model=getattr(args, "model", None),
        cron_job_id=getattr(args, "cron_job_id", None), since=getattr(args, "since", None),
        limit=getattr(args, "limit", 1000),
    )
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Print the configured (or ``--provider``) account's usage windows; exit 1 when nothing could be fetched."""
    if getattr(args, "usage_command", None) == "ledger":
        return cmd_ledger(args)
    from agent.account_usage import fetch_account_usage, render_account_usage_lines
    from hermes_cli.runtime_provider import resolve_requested_provider

    provider = resolve_requested_provider(getattr(args, "provider", None))
    # No explicit key: the fetcher resolves the credential exactly as a session without a live agent
    # would (singleton store, then credential pool) — it never adopts or refreshes anything else.
    snapshot = fetch_account_usage(provider)
    if snapshot is None:
        print(
            f"No account usage available for provider '{provider}': no credential is configured for it, "
            "the provider has no usage endpoint, or the fetch failed.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "json", False):
        print(json.dumps(usage_snapshot_document(snapshot), indent=2))
    else:
        print("\n".join(render_account_usage_lines(snapshot)))
    return 0


def build_usage_parser(subparsers) -> None:
    """Attach the ``usage`` subcommand to ``subparsers``."""
    usage_parser = subparsers.add_parser(
        "usage", help="Show account rate-limit windows (the /usage block) without starting a session",
        description="Fetch the configured provider's account limits (Codex 5h/weekly windows, plan, banked "
                    "resets; Anthropic OAuth windows; OpenRouter credits) — the same block the /usage slash "
                    "command prints — and exit. Exit code 1 when no credential is configured or the fetch fails.",
    )
    usage_parser.add_argument(
        "--provider", default=None, help="Provider to query (default: the configured model provider)")
    usage_parser.add_argument(
        "--json", action="store_true", help="Print one JSON document instead of the human-readable block")
    usage_parser.set_defaults(func=cmd_usage)
    ledger_parser = usage_parser.add_subparsers(dest="usage_command")
    ledger = ledger_parser.add_parser(
        "ledger", help="Read local size/route-only provider request telemetry")
    ledger.add_argument("--source", default=None)
    ledger.add_argument("--model", default=None)
    ledger.add_argument("--cron-job-id", default=None)
    ledger.add_argument("--since", default=None, help="Only rows at or after this ISO-8601 timestamp")
    ledger.add_argument("--limit", type=int, default=1000, help="Maximum rows to print (default: 1000)")
    # Keep the parent --json value when it appears before ``ledger`` while also accepting
    # the conventional ``usage ledger --json`` spelling.
    ledger.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Print a JSON array")
    ledger.set_defaults(func=cmd_usage, usage_command="ledger")
