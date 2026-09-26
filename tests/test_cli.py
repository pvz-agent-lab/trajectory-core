"""The CLI must work from outside the repository and report precise exit codes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import trajectory_core as tc

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["TRAJECTORY_CORE_FNV"] = "python"
    return subprocess.run(
        [sys.executable, "-m", "trajectory_core", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_summary_and_validation_from_outside_the_repository(lab: dict, tmp_path: Path) -> None:
    result = run_cli(["summary", str(lab["plain"])], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["trajectory_id"] == tc.load_trajectory(lab["plain"]).trajectory_id
    assert summary["steps"] == 2

    tree = run_cli(["summary", "--tree", str(lab["tree"])], cwd=tmp_path)
    assert tree.returncode == 0, tree.stderr
    assert json.loads(tree.stdout)["tree_id"] == tc.load_tree(lab["tree"]).tree_id

    validated = run_cli(["validate-tree", str(lab["tree"])], cwd=tmp_path)
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["status"] == "valid"


def test_verify_seal_cli(lab: dict, tmp_path: Path) -> None:
    import shutil

    from conftest import write_json

    stage = tmp_path / "stage"
    (stage / "work").mkdir(parents=True)
    shutil.copytree(lab["tree"], stage / "tree")
    (stage / "work" / "report.json").write_text("{}\n", encoding="utf-8")
    summary = tc.load_tree(stage / "tree").summary()
    record = {
        "schema": "lvz.issue99-shovel-fork-seal.v1",
        "tree": "tree",
        "tree_id": summary.tree_id,
        "reports": [{"path": "work/report.json", "sha256": tc.file_hash(stage / "work" / "report.json")}],
        "nodes": [
            {
                "key": node.key,
                "branch_id": node.branch_id,
                "trunk": node.trunk,
                "parent_key": node.parent_key,
                "trajectory_id": node.trajectory_id,
                "manifest_sha256": node.manifest_sha256,
            }
            for node in summary.nodes
        ],
    }
    seal = stage / "seal.json"
    write_json(seal, record)
    result = run_cli(["verify-seal", str(seal), "--source-root", str(stage), "--structure-only"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["matches"] is True
    assert report["verification"] == "structure_only"


def test_adapt_outcome_cli(formal_lab: dict, tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    case = tmp_path / "case.json"
    plan.write_text('{"schema": "lvz.evaluation-plan.v2", "flags_to_complete": 2}\n', encoding="utf-8")
    case.write_text(
        '{"status": "completed", "outcome": "terminal_before_complete", "full_cycle": true,\n'
        ' "rounds_completed": 1, "maximum_wave": 20, "final_observation": {"scene": 3}}\n',
        encoding="utf-8",
    )
    result = run_cli(["adapt-outcome", str(plan), str(case), "--expected-scene", "3"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["plan"] == {"unit": "round", "rounds": 2}
    assert outcome["cycle"]["completed"] is True
    assert outcome["goal"]["reached"] is False
    assert outcome["run"]["execution_extent"] == "partial"
    assert outcome["provenance"]["kind"] == "legacy-adapted"

    plan.write_text('{"schema": "lvz.evaluation-plan.v99"}\n', encoding="utf-8")
    unsupported = run_cli(["adapt-outcome", str(plan), str(case), "--expected-scene", "3"], cwd=tmp_path)
    assert unsupported.returncode == 3


def test_verify_closure_cli(formal_lab: dict, tmp_path: Path) -> None:
    result = run_cli(["verify-closure", str(formal_lab["closure_path"])], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "valid"
    assert report["content_integrity"]["status"] == "verified"
    assert report["producer_attested_closure"]["attested"] is False
    assert report["producer_attested_closure"]["synthetic"] is True
    assert report["producer_attested_closure"]["scope"] == {
        "tree_id": report["tree_id"],
        "nodes": 4,
        "producer_artifacts": 31,
    }
    assert report["counts"] == {"nodes": 4, "outcomes": 4, "pairs": 2, "inputs": 32, "producer_artifacts": 31}


def test_verify_closure_cli_rejects_tampering(formal_lab: dict, tmp_path: Path) -> None:
    import shutil

    package = tmp_path / "package"
    shutil.copytree(formal_lab["stage"], package)
    outcome = package / "outcomes" / "root.json"
    outcome.write_bytes(outcome.read_bytes() + b"\n")
    result = run_cli(["verify-closure", str(package / "closure.json")], cwd=tmp_path)
    assert result.returncode == 1
    assert "invalid evidence" in result.stderr


def test_seal_closure_cli_round_trip(formal_lab: dict, tmp_path: Path) -> None:
    import shutil

    package = tmp_path / "package"
    shutil.copytree(formal_lab["stage"], package)
    (package / "closure.json").unlink()
    args = [
        "seal-closure",
        str(package),
        str(package / "fresh.json"),
        "--tree",
        "tree",
        "--receipt",
        "receipt.json",
        "--outcome",
        "root=outcomes/root.json",
        "--outcome",
        "issue3-control-b=outcomes/issue3-control-b.json",
        "--outcome",
        "issue3-intervention-a=outcomes/issue3-intervention-a.json",
        "--outcome",
        "issue3-intervention-b=outcomes/issue3-intervention-b.json",
        "--pair",
        "control-rerun",
        "root",
        "issue3-control-b",
        "reports/control-rerun.json",
        "--pair",
        "intervention-rerun",
        "issue3-intervention-a",
        "issue3-intervention-b",
        "reports/intervention-rerun.json",
    ]
    sealed = run_cli(args, cwd=tmp_path)
    assert sealed.returncode == 0, sealed.stderr
    assert json.loads(sealed.stdout)["status"] == "valid"
    verified = run_cli(["verify-closure", str(package / "fresh.json")], cwd=tmp_path)
    assert verified.returncode == 0, verified.stderr
    duplicate = run_cli(args, cwd=tmp_path)
    assert duplicate.returncode == 1
    assert "already exists" in duplicate.stderr


def test_seal_closure_cli_usage_error(tmp_path: Path) -> None:
    result = run_cli(
        [
            "seal-closure",
            str(tmp_path),
            str(tmp_path / "closure.json"),
            "--tree",
            "tree",
            "--receipt",
            "r.json",
            "--outcome",
            "broken",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "usage error" in result.stderr


def test_unknown_schema_exits_unsupported(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle = tmp_path / "bundle"
    (bundle / "audit").mkdir(parents=True)
    (bundle / "trajectory.json").write_text(json.dumps({"schema": "lvz.engine-replay.v99"}), encoding="utf-8")
    result = run_cli(["validate-trajectory", str(bundle)], cwd=tmp_path)
    assert result.returncode == 3
    assert "unsupported" in result.stderr


def test_missing_manifest_exits_invalid(tmp_path: Path) -> None:
    result = run_cli(["validate-trajectory", str(tmp_path / "nowhere")], cwd=tmp_path)
    assert result.returncode == 1


@pytest.mark.parametrize("args", [["summary"], ["unknown-command"]])
def test_usage_errors(args: list[str], tmp_path: Path) -> None:
    result = run_cli(args, cwd=tmp_path)
    assert result.returncode == 2
