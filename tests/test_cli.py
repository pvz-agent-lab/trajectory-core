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


def test_unknown_schema_exits_unsupported(tmp_path: Path) -> None:
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
