"""Supported CLI for the Markdown skill-observation inbox."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "curate_skill_observations.py"
    spec = importlib.util.spec_from_file_location("hermes_observation_store", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load observation store: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_observations_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "observations", help="Index and disposition Markdown skill observations",
    )
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="observations_command", required=True)
    commands.add_parser("index", help="index top-level observation Markdown files")
    disposition = commands.add_parser("disposition", help="set an explicit disposition")
    disposition.add_argument("id")
    disposition.add_argument("disposition", choices=("pending", "accepted", "rejected", "deferred"))
    disposition.add_argument("--reason")
    archive = commands.add_parser("archive", help="archive dispositioned source files")
    archive.add_argument("--id")
    listing = commands.add_parser("list", help="list indexed observations as JSON")
    listing.add_argument("--disposition", choices=("pending", "accepted", "rejected", "deferred"))
    parser.set_defaults(func=cmd_observations)


def cmd_observations(args) -> int:
    module = _module()
    observations = args.observations or module.default_observations()
    db = args.db or module.default_db()
    if args.observations_command == "index":
        print(module.json.dumps(module.index_observations(observations, db).__dict__, sort_keys=True))
    elif args.observations_command == "disposition":
        if not module.set_disposition(db, args.id, args.disposition, reason=args.reason):
            print(f"unknown observation id: {args.id}")
            return 1
    elif args.observations_command == "archive":
        print(module.json.dumps(module.archive_dispositioned(observations, db, args.id).__dict__, sort_keys=True))
    else:
        with module._connect(db) as conn:
            query = "SELECT * FROM observations"
            params = ()
            if args.disposition:
                query += " WHERE disposition = ?"
                params = (args.disposition,)
            query += " ORDER BY imported_at, id"
            for row in conn.execute(query, params):
                print(module.json.dumps(dict(row), sort_keys=True))
    return 0