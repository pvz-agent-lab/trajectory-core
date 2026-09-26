"""Command line entry point for offline verification.

Every command is read-only except ``export``, which refuses an existing
destination.  Exit codes: 0 valid, 1 evidence invalid, 2 usage error,
3 unsupported schema/capability.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .closure import seal_formal_closure, validate_formal_closure
from .errors import EvidenceError, UnsupportedCapabilityError, UnsupportedSchemaError
from .jsonio import read_json
from .outcome import outcome_from_legacy
from .seal import verify_seal
from .trajectory import load_trajectory
from .tree import export_tree, inspect_tree, load_tree


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trajectory-core", description="Offline lvz trajectory and evidence-tree reader"
    )
    parser.add_argument("--version", action="version", version=f"trajectory-core {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    summary = commands.add_parser("summary", help="fully verify and summarize a trajectory or tree")
    summary.add_argument("path", type=Path)
    summary.add_argument("--tree", action="store_true", help="treat the path as a packaged tree")

    validate_trajectory = commands.add_parser("validate-trajectory", help="fully validate one trajectory")
    validate_trajectory.add_argument("path", type=Path)

    validate_tree = commands.add_parser("validate-tree", help="fully validate one packaged tree")
    validate_tree.add_argument("path", type=Path)

    inspect = commands.add_parser("inspect-tree", help="index-only tree projection (chains not re-derived)")
    inspect.add_argument("path", type=Path)

    seal = commands.add_parser("verify-seal", help="re-derive a seal record and everything it binds")
    seal.add_argument("seal", type=Path)
    seal.add_argument("--source-root", type=Path, required=True)
    seal.add_argument(
        "--structure-only", action="store_true", help="compare digests without fully re-reading every node"
    )

    export = commands.add_parser("export", help="deterministically export a validated tree to a new ZIP")
    export.add_argument("tree", type=Path)
    export.add_argument("output", type=Path)

    verify_closure = commands.add_parser("verify-closure", help="fully re-derive a formal producer closure")
    verify_closure.add_argument("closure", type=Path)
    verify_closure.add_argument(
        "--root", type=Path, default=None, help="package root (default: the closure file's directory)"
    )

    seal_closure = commands.add_parser(
        "seal-closure", help="write a formal closure over an existing package (never overwrites)"
    )
    seal_closure.add_argument("root", type=Path)
    seal_closure.add_argument("destination", type=Path)
    seal_closure.add_argument("--tree", required=True, metavar="PATH", help="tree directory relative to ROOT")
    seal_closure.add_argument("--receipt", required=True, metavar="PATH", help="producer receipt relative to ROOT")
    seal_closure.add_argument(
        "--outcome", action="append", default=[], metavar="KEY=PATH", help="outcome document per tree node"
    )
    seal_closure.add_argument(
        "--pair",
        action="append",
        nargs=4,
        default=[],
        metavar=("ID", "BASELINE", "RERUN", "REPORT"),
        help="one baseline/rerun pair and its report, relative to ROOT",
    )

    adapt = commands.add_parser(
        "adapt-outcome", help="adapt a legacy evaluation plan/case pair to trajectory-core.outcome.v1"
    )
    adapt.add_argument("plan", type=Path, help="legacy plan document, or - when no plan is available")
    adapt.add_argument("case", type=Path)
    adapt.add_argument("--expected-scene", type=int, default=None, help="the declared final scene the plan expects")

    return parser


def _outcome_arguments(values: list[str]) -> dict[str, str] | None:
    outcomes: dict[str, str] = {}
    for item in values:
        key, separator, path = item.partition("=")
        if not separator or not key or not path:
            print(f"usage error: --outcome requires KEY=PATH, got {item!r}", file=sys.stderr)
            return None
        if key in outcomes:
            print(f"usage error: --outcome binds {key!r} more than once", file=sys.stderr)
            return None
        outcomes[key] = path
    return outcomes


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "summary":
            if args.tree:
                _dump(load_tree(args.path).summary().to_dict())
            else:
                _dump(load_trajectory(args.path).summary().to_dict())
            return 0
        if args.command == "validate-trajectory":
            report = load_trajectory(args.path).verify()
            _dump(report)
            return 0
        if args.command == "validate-tree":
            report = load_tree(args.path).verify()
            _dump(report)
            return 0
        if args.command == "inspect-tree":
            _dump(inspect_tree(args.path).to_dict())
            return 0
        if args.command == "verify-seal":
            report = verify_seal(args.seal, source_root=args.source_root, verify_nodes=not args.structure_only)
            _dump(report)
            return 0 if report["matches"] else 1
        if args.command == "export":
            path = export_tree(args.tree, args.output)
            _dump(
                {
                    "schema": "trajectory-core.export-report.v1",
                    "path": str(path),
                    "sha256": load_tree(args.tree).tree_id,
                }
            )
            return 0
        if args.command == "verify-closure":
            _dump(validate_formal_closure(args.closure, root=args.root))
            return 0
        if args.command == "seal-closure":
            outcomes = _outcome_arguments(args.outcome)
            if outcomes is None:
                return 2
            pairs = [
                {"id": pair_id, "baseline": baseline, "rerun": rerun, "report": report}
                for pair_id, baseline, rerun, report in args.pair
            ]
            closure = seal_formal_closure(
                args.root, args.destination, tree=args.tree, outcomes=outcomes, pairs=pairs, receipt=args.receipt
            )
            _dump(closure.report())
            return 0
        if args.command == "adapt-outcome":
            plan = None if str(args.plan) == "-" else read_json(args.plan)
            case = read_json(args.case)
            outcome = outcome_from_legacy(
                plan,
                case,
                source=f"{args.plan} + {args.case}",
                expected_scene=args.expected_scene,
            )
            _dump(outcome.to_dict())
            return 0
    except UnsupportedSchemaError as error:
        print(f"unsupported: {error}", file=sys.stderr)
        return 3
    except UnsupportedCapabilityError as error:
        print(f"unsupported: {error}", file=sys.stderr)
        return 3
    except EvidenceError as error:
        print(f"invalid evidence: {error}", file=sys.stderr)
        return 1
    except FileExistsError as error:
        print(f"refusing to overwrite: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
