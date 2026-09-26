"""Re-derive one legacy seal record and everything it binds (offline).

This tool exists so a real sealed tree from the old repository can be checked
with the public ``trajectory_core`` API from outside that repository:

    python tools/verify_legacy_seal.py --seal work/issue99-fc2-seal.json \
        --source-root <old-checkout> --report work/core-seal-report.json

``--source-root`` is where the seal's relative Windows-style references are
interpreted; the tool never hard-codes a checkout path and never writes into
the sealed tree.  ``--structure-only`` compares node digests without fully
re-reading every trajectory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trajectory_core import EvidenceError, UnsupportedSchemaError, verify_seal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--structure-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_seal(args.seal, source_root=args.source_root, verify_nodes=not args.structure_only)
    except UnsupportedSchemaError as error:
        print(f"unsupported seal: {error}", file=sys.stderr)
        return 3
    except EvidenceError as error:
        print(f"invalid seal evidence: {error}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    print(f"tree_id={report['tree_id']}")
    print(f"nodes={report['nodes']} verification={report['verification']}")
    for check in report["node_checks"]:
        print(f"  {check['key']}: {check['trajectory_verification']} problems={len(check['problems'])}")
    for item in report["reports"]:
        print(f"  report {item['path']}: match={item['match']}")
    print(f"matches={report['matches']} problems={len(report['problems'])}")
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
