"""Seal verification and deterministic export contracts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import trajectory_core as tc
from conftest import read_json, write_json


@pytest.fixture()
def staged(lab: dict, tmp_path: Path) -> dict:
    stage = tmp_path / "stage"
    shutil.copytree(lab["tree"], stage / "tree")
    (stage / "work").mkdir()
    (stage / "work" / "report.json").write_text('{"verdict": "synthetic"}\n', encoding="utf-8")
    (stage / "work" / "report.md").write_text("# synthetic\n", encoding="utf-8")
    summary = tc.load_tree(stage / "tree").summary()
    record = {
        "schema": "lvz.issue99-shovel-fork-seal.v1",
        "tree": "tree",
        "tree_id": summary.tree_id,
        "reports": [
            {"path": "work/report.json", "sha256": tc.file_hash(stage / "work" / "report.json")},
            {"path": "work/report.md", "sha256": tc.file_hash(stage / "work" / "report.md")},
        ],
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
    return {"stage": stage, "seal": seal, "record": record}


def test_verify_seal_matches_everything_it_binds(staged: dict) -> None:
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is True
    assert report["verification"] == "full"
    assert report["problems"] == []
    assert report["tree_id"] == staged["record"]["tree_id"]
    assert report["nodes"] == 3
    assert all(check["trajectory_verification"] == "full" for check in report["node_checks"])
    assert [item["match"] for item in report["reports"]] == [True, True]


def test_windows_style_references_resolve_through_source_root(staged: dict) -> None:
    record = read_json(staged["seal"])
    record["tree"] = record["tree"].replace("/", "\\")
    record["reports"][0]["path"] = record["reports"][0]["path"].replace("/", "\\")
    write_json(staged["seal"], record)
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is True


def test_structure_only_mode_never_claims_full_verification(staged: dict) -> None:
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"], verify_nodes=False)
    assert report["matches"] is True
    assert report["verification"] == "structure_only"
    assert all(check["trajectory_verification"] == "digest_only" for check in report["node_checks"])


def test_tampered_report_is_named(staged: dict) -> None:
    (staged["stage"] / "work" / "report.md").write_text("# replaced\n", encoding="utf-8")
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is False
    assert any(problem["what"] == "report.sha256" for problem in report["problems"])
    assert report["verification"] == "full"


def test_tampered_node_manifest_digest_is_named(staged: dict) -> None:
    manifest = staged["stage"] / "tree" / "nodes" / "left" / "trajectory.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is False
    assert any(problem["what"] == "node.manifest_sha256" for problem in report["problems"])


def test_broken_node_degrades_verification_and_attributes_it(staged: dict) -> None:
    manifest_path = staged["stage"] / "tree" / "nodes" / "left" / "trajectory.json"
    manifest = read_json(manifest_path)
    manifest["scope"] = "rewritten after sealing"
    write_json(manifest_path, manifest)
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is False
    assert report["verification"] == "failed"
    failed = [check for check in report["node_checks"] if check["trajectory_verification"] == "failed"]
    assert [check["key"] for check in failed] == ["left"]
    assert any(problem["what"] == "tree.validate" for problem in report["problems"])


def test_seal_path_escape_is_rejected(staged: dict) -> None:
    record = read_json(staged["seal"])
    record["tree"] = "../outside"
    write_json(staged["seal"], record)
    with pytest.raises(tc.PathContractError):
        tc.verify_seal(staged["seal"], source_root=staged["stage"])
    record["tree"] = "tree"
    record["reports"][0]["path"] = "C:/absolute/report.json"
    write_json(staged["seal"], record)
    with pytest.raises(tc.PathContractError):
        tc.verify_seal(staged["seal"], source_root=staged["stage"])


def test_unknown_seal_schema_fails_closed(staged: dict) -> None:
    record = read_json(staged["seal"])
    record["schema"] = "lvz.issue99-shovel-fork-seal.v2"
    write_json(staged["seal"], record)
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.verify_seal(staged["seal"], source_root=staged["stage"])


def test_seal_binding_a_foreign_node_is_reported(staged: dict) -> None:
    record = read_json(staged["seal"])
    record["nodes"].append(
        {
            "key": "ghost",
            "branch_id": "ghost",
            "trunk": False,
            "parent_key": "root",
            "trajectory_id": "f" * 64,
            "manifest_sha256": "e" * 64,
        }
    )
    write_json(staged["seal"], record)
    report = tc.verify_seal(staged["seal"], source_root=staged["stage"])
    assert report["matches"] is False
    assert any(problem.get("key") == "ghost" for problem in report["problems"])


def test_non_object_record_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "seal.json"
    path.write_text(json.dumps([1, 2, 3]) + "\n", encoding="utf-8")
    with pytest.raises(tc.SealError):
        tc.read_seal(path)
