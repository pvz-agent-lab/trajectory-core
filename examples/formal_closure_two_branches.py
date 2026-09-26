"""Public example: seal and verify a synthetic two-branch + two-rerun family.

Run from an installed package (or from the repository with ``src`` on the
path).  The synthetic recorder lives in the repository's ``tests/support``
package, so pass ``--support <repo>/tests`` when running outside a checkout;
the formal-closure API itself is the installed public ``trajectory_core``.

    python examples/formal_closure_two_branches.py WORKSPACE --support <repo>/tests

The example writes a four-node tree (control baseline, control re-run,
intervention baseline, intervention re-run) with two rerun reports, adapts
synthetic legacy plan/case documents to the versioned outcome contract, seals
the package formally, then loads it again and proves the read-only inventory is
unchanged.  The producer receipt is explicitly synthetic: the report must show
``producer_attested_closure.attested == false`` and ``synthetic == true``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import trajectory_core as tc


def _inventory(root: Path) -> dict[str, str]:
    import hashlib

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="writable directory for the demo package")
    parser.add_argument(
        "--support",
        type=Path,
        default=None,
        help="directory containing the test-only 'support' package (for example <repo>/tests)",
    )
    parser.add_argument("--json", action="store_true", help="print the full closure report as JSON")
    args = parser.parse_args(argv)

    if args.support is not None:
        sys.path.insert(0, str(args.support))
    from support.formal_demo import build_formal_demo  # noqa: E402 (test-only recorder, after the sys.path setup)

    demo = build_formal_demo(args.workspace)
    stage = demo["stage"]
    before = _inventory(stage)
    closure = tc.load_formal_closure(demo["closure_path"])
    report = closure.report()
    after = _inventory(stage)
    if before != after:
        raise SystemExit("read-only verification changed the source inventory")

    intervention = demo["outcomes"]["issue3-intervention-a"]
    if intervention.cycle_completed is not True or intervention.goal_reached is not False:
        raise SystemExit("the demo's full-cycle-without-goal outcome was not reproduced")
    if report["producer_attested_closure"]["attested"] is not False:
        raise SystemExit("a synthetic receipt was presented as producer-attested closure")
    if report["producer_attested_closure"]["synthetic"] is not True:
        raise SystemExit("the synthetic receipt was not marked synthetic")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"closure_id={closure.closure_id}")
        print(f"tree_id={closure.tree_id}")
        print(f"content_integrity={report['content_integrity']['status']}")
        print(f"producer_attested={report['producer_attested_closure']['attested']}")
        print(f"synthetic_receipt={report['producer_attested_closure']['synthetic']}")
        print(f"read_only_inventory_stable={before == after}")
        print(f"counts={json.dumps(report['counts'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
