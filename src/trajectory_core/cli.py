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
from .errors import EvidenceError, UnsupportedCapabilityError, UnsupportedSchemaError
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

    return parser


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
