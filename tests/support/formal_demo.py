"""Build the synthetic two-branch + two-rerun formal closure demo package.

This is a test-only fixture, shared by the pytest suite and the public example
``examples/formal_closure_two_branches.py``.  It writes:

* a sealed four-node tree: the control baseline (trunk), the control re-run,
  the intervention branch and the intervention re-run;
* two rerun reports produced by the legacy audit comparator
  (``compare_audits``) over the synthetic audit streams;
* one ``trajectory-core.outcome.v2`` document per node, adapted from
  synthetic legacy plan/case documents that reproduce the historical #97
  semantics (``flags_to_complete`` counted rounds; ``full_cycle`` is not goal
  completion);
* a producer receipt explicitly marked ``synthetic: true`` -- this is a
  fixture, never a live producer's runtime proof.

No game, network or socket is involved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import trajectory_core as tc
from support.synthetic import record_session
from trajectory_core.legacy_lvz.audit_compare import AuditLog, compare_audits

CONTROL_BASELINE = "root"
CONTROL_RERUN = "issue3-control-b"
INTERVENTION_BASELINE = "issue3-intervention-a"
INTERVENTION_RERUN = "issue3-intervention-b"
PAIR_CONTROL = "control-rerun"
PAIR_INTERVENTION = "intervention-rerun"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _identity(bundle: Path) -> dict[str, Any]:
    manifest = tc.read_manifest(bundle)
    initial = manifest["initial"]
    return {
        "game": initial["identity"]["game"],
        "artifacts": initial["identity"]["artifacts"],
        "init_recipe": initial["initialization"],
    }


def _record_and_build(workspace: Path, name: str, output: str, *, tree: Any = None, terminal_tick: int = 5) -> Path:
    source = workspace / f"{name}-source"
    trace = record_session(source, terminal_tick=terminal_tick)
    bundle = workspace / output
    tc.build_trajectory(trace, source / "audit", bundle, tree=tree)
    return bundle


def _legacy_outcome(
    key: str,
    *,
    plan_rounds_alias: int,
    outcome: str,
    goal_reached: bool,
) -> tc.Outcome:
    """One synthetic legacy plan/case pair adapted to the public outcome schema.

    ``plan_rounds_alias`` is written with the historical ``flags_to_complete``
    field name on purpose: the adapter must read it as rounds, not flags.
    """
    plan = {"schema": "lvz.evaluation-plan.v2", "flags_to_complete": plan_rounds_alias}
    case = {
        "status": "completed",
        "outcome": outcome,
        "full_cycle": True,
        "rounds_completed": 1,
        "maximum_wave": 20,
        "final_observation": {"scene": 3},
    }
    adapted = tc.outcome_from_legacy(
        plan,
        case,
        source=f"synthetic legacy plan/case fixture for node {key!r}",
        expected_scene=3,
        evidence_scope="synthetic fixture values only; not a live game observation",
    )
    expected_extent = "complete" if outcome == "full_cycle_completed" else "partial"
    assert adapted.goal_reached is goal_reached, adapted.document
    assert adapted.execution_extent == expected_extent, adapted.document
    return adapted


def build_formal_demo(workspace: str | Path) -> dict[str, Any]:
    """Build the demo package under ``workspace/stage`` and return its paths."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    stage = workspace / "stage"

    plain = _record_and_build(workspace, "plain", "plain-bundle")
    control_baseline = _record_and_build(
        workspace,
        "control-baseline",
        "control-baseline-bundle",
        tree=tc.root_placement(CONTROL_BASELINE, _identity(plain)),
    )
    baseline_end = tc.end_boundary(tc.read_manifest(control_baseline))
    control_rerun = _record_and_build(
        workspace,
        "control-rerun",
        "control-rerun-bundle",
        tree=tc.branch_placement(tc.load_trajectory(control_baseline), CONTROL_RERUN, at=baseline_end),
    )
    intervention_baseline = _record_and_build(
        workspace,
        "intervention-baseline",
        "intervention-baseline-bundle",
        tree=tc.branch_placement(tc.load_trajectory(control_baseline), INTERVENTION_BASELINE, at=baseline_end),
    )
    intervention_baseline_end = tc.end_boundary(tc.read_manifest(intervention_baseline))
    intervention_rerun = _record_and_build(
        workspace,
        "intervention-rerun",
        "intervention-rerun-bundle",
        tree=tc.branch_placement(
            tc.load_trajectory(intervention_baseline), INTERVENTION_RERUN, at=intervention_baseline_end
        ),
    )

    tree = tc.package_tree(
        stage / "tree",
        tc.load_trajectory(control_baseline),
        branches=[
            tc.load_trajectory(control_rerun),
            tc.load_trajectory(intervention_baseline),
            tc.load_trajectory(intervention_rerun),
        ],
    )
    summary = tree.summary()
    node_ids = {node.key: node.trajectory_id for node in summary.nodes}
    assert set(node_ids) == {CONTROL_BASELINE, CONTROL_RERUN, INTERVENTION_BASELINE, INTERVENTION_RERUN}

    reports = {
        PAIR_CONTROL: (CONTROL_BASELINE, CONTROL_RERUN),
        PAIR_INTERVENTION: (INTERVENTION_BASELINE, INTERVENTION_RERUN),
    }
    for pair_id, (baseline, rerun) in reports.items():
        comparison = compare_audits(
            AuditLog(_bundle_directory(workspace, baseline) / "audit", require_closed=True),
            AuditLog(_bundle_directory(workspace, rerun) / "audit", require_closed=True),
        )
        _write_json(
            stage / "reports" / f"{pair_id}.json",
            {
                "schema": tc.RERUN_REPORT_SCHEMA,
                "baseline": {"key": baseline, "trajectory_id": node_ids[baseline]},
                "rerun": {"key": rerun, "trajectory_id": node_ids[rerun]},
                "verdict": "equal" if comparison["equal"] is True else "different",
                "synthetic": True,
                "detail": comparison,
            },
        )

    outcomes = {
        CONTROL_BASELINE: _legacy_outcome(
            CONTROL_BASELINE, plan_rounds_alias=1, outcome="full_cycle_completed", goal_reached=True
        ),
        CONTROL_RERUN: _legacy_outcome(
            CONTROL_RERUN, plan_rounds_alias=1, outcome="full_cycle_completed", goal_reached=True
        ),
        # The #97 acceptance case: a full cycle completed while the declared
        # two-round target was not reached.  The adapted document must report
        # cycle.completed=true and goal.reached=false.
        INTERVENTION_BASELINE: _legacy_outcome(
            INTERVENTION_BASELINE, plan_rounds_alias=2, outcome="terminal_before_complete", goal_reached=False
        ),
        INTERVENTION_RERUN: _legacy_outcome(
            INTERVENTION_RERUN, plan_rounds_alias=2, outcome="terminal_before_complete", goal_reached=False
        ),
    }
    outcome_paths: dict[str, str] = {}
    for key, outcome in outcomes.items():
        _write_json(stage / "outcomes" / f"{key}.json", outcome.to_dict())
        outcome_paths[key] = f"outcomes/{key}.json"

    receipt_artifacts = [
        {"path": path.relative_to(stage).as_posix(), "sha256": tc.file_hash(path)}
        for path in sorted(stage.rglob("*"))
        if path.is_file()
    ]
    receipt = {
        "schema": tc.PRODUCER_RECEIPT_SCHEMA,
        "producer": {"name": "synthetic fixture (tests/support/formal_demo.py)", "version": "issue3-demo"},
        "synthetic": True,
        "attestation": "no_active_writers",
        "statement": (
            "Synthetic fixture receipt: all fixture writers are closed before sealing. "
            "This is NOT a live producer's runtime proof."
        ),
        "method": "single-threaded fixture construction; every file handle is closed before sealing",
        "scope": {
            "tree_id": summary.tree_id,
            "nodes": [
                {
                    "key": node.key,
                    "trajectory_id": node.trajectory_id,
                    "manifest_sha256": node.manifest_sha256,
                }
                for node in summary.nodes
            ],
            "artifacts": receipt_artifacts,
        },
    }
    _write_json(stage / "receipt.json", receipt)

    closure = tc.seal_formal_closure(
        stage,
        stage / "closure.json",
        tree="tree",
        outcomes=outcome_paths,
        pairs=[
            {
                "id": PAIR_CONTROL,
                "baseline": CONTROL_BASELINE,
                "rerun": CONTROL_RERUN,
                "report": f"reports/{PAIR_CONTROL}.json",
            },
            {
                "id": PAIR_INTERVENTION,
                "baseline": INTERVENTION_BASELINE,
                "rerun": INTERVENTION_RERUN,
                "report": f"reports/{PAIR_INTERVENTION}.json",
            },
        ],
        receipt="receipt.json",
    )
    return {
        "workspace": workspace,
        "stage": stage,
        "closure_path": stage / "closure.json",
        "closure": closure,
        "outcomes": outcomes,
        "tree_id": summary.tree_id,
    }


def _bundle_directory(workspace: Path, key: str) -> Path:
    mapping = {
        CONTROL_BASELINE: "control-baseline-bundle",
        CONTROL_RERUN: "control-rerun-bundle",
        INTERVENTION_BASELINE: "intervention-baseline-bundle",
        INTERVENTION_RERUN: "intervention-rerun-bundle",
    }
    return workspace / mapping[key]
