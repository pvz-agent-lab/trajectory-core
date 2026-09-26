"""Behavior contract of the public tree reader."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

import trajectory_core as tc
from conftest import identity_of, read_json, write_json
from support.synthetic import record_session


def copy_tree(lab: dict, tmp_path: Path) -> Path:
    target = tmp_path / "tree-copy"
    shutil.copytree(lab["tree"], target)
    return target


def rewrite_node(tree: Path, key: str, change) -> None:
    path = tree / "nodes" / key / "trajectory.json"
    manifest = read_json(path)
    change(manifest)
    write_json(path, manifest)


def rewrite_index(tree: Path, change) -> None:
    path = tree / "tree.json"
    record = read_json(path)
    change(record)
    write_json(path, record)


def test_valid_tree_rederives_every_chain(lab: dict) -> None:
    tree = tc.load_tree(lab["tree"])
    report = tree.verify()
    assert report["status"] == "valid"
    summary = report["summary"]
    assert summary["tree_id"] == tree.tree_id
    assert summary["nodes"] == 3
    assert summary["branches"] == ["left", "main", "right"]
    assert summary["trunk_nodes"] == ["root"]
    assert {point["parent"] for point in summary["branch_points"]} == {"root"}
    assert [point["boundary"] for point in summary["branch_points"]] == [lab["end"], lab["end"]]
    typed = tree.summary()
    assert typed.verification == "full"
    assert typed.root == "root"
    assert all(node.verification == "full" for node in typed.nodes)
    assert typed.nodes[0].branch_id == "main" and typed.nodes[0].trunk is True


def test_inspect_tree_is_explicitly_index_only(lab: dict) -> None:
    summary = tc.inspect_tree(lab["tree"])
    assert summary.verification == "index_only"
    assert "not re-derived" in summary.scope
    assert [node.key for node in summary.nodes] == ["root", "left", "right"]
    assert summary.tree_id == tc.load_tree(lab["tree"]).tree_id


def test_manifest_tamper_breaks_the_node_load(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)
    rewrite_node(tree, "left", lambda manifest: manifest.update(scope="rewritten after sealing"))
    with pytest.raises(tc.EvidenceError):
        tc.validate_tree(tree)


def test_child_chain_must_continue_the_actual_parent(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def reanchor(manifest):
        fake = "a" * 64
        section = manifest["tree"]
        section["parent"]["chain_sha256"] = fake
        section["chain"]["parent_sha256"] = fake
        section["chain"]["sha256"] = tc.chain_sha256(
            trajectory_id=manifest["trajectory_id"],
            branch=section["branch_id"],
            trunk=section["trunk"],
            parent=section["parent"],
            parent_sha256=fake,
            tree_id=section["root"]["sha256"],
        )

    rewrite_node(tree, "left", reanchor)
    with pytest.raises(tc.EvidenceError, match="does not continue its actual parent|hash chain is broken"):
        tc.validate_tree(tree)


def test_index_parent_must_match_the_embedded_placement(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def reparent(record):
        node = next(item for item in record["nodes"] if item["key"] == "left")
        node["parent_key"] = "right"

    rewrite_index(tree, reparent)
    with pytest.raises(tc.EvidenceError):
        tc.validate_tree(tree)


def test_branch_node_cannot_claim_the_real_continuation_baseline(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def claim_trunk(manifest):
        section = manifest["tree"]
        section["trunk"] = True
        section["chain"]["sha256"] = tc.chain_sha256(
            trajectory_id=manifest["trajectory_id"],
            branch=section["branch_id"],
            trunk=True,
            parent=section["parent"],
            parent_sha256=section["parent"]["chain_sha256"],
            tree_id=section["root"]["sha256"],
        )

    rewrite_node(tree, "left", claim_trunk)

    def index_trunk(record):
        next(item for item in record["nodes"] if item["key"] == "left")["trunk"] = True

    rewrite_index(tree, index_trunk)
    with pytest.raises(tc.EvidenceError, match="real continuation baseline"):
        tc.validate_tree(tree)


def test_duplicate_branch_id_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def rename_to_left(manifest):
        section = manifest["tree"]
        section["branch_id"] = "left"
        section["chain"]["sha256"] = tc.chain_sha256(
            trajectory_id=manifest["trajectory_id"],
            branch="left",
            trunk=section["trunk"],
            parent=section["parent"],
            parent_sha256=section["parent"]["chain_sha256"],
            tree_id=section["root"]["sha256"],
        )

    rewrite_node(tree, "right", rename_to_left)

    def index_branch(record):
        next(item for item in record["nodes"] if item["key"] == "right")["branch_id"] = "left"

    rewrite_index(tree, index_branch)
    with pytest.raises(tc.EvidenceError, match="separate paths"):
        tc.validate_tree(tree)


def test_missing_parent_node_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def ghost(record):
        next(item for item in record["nodes"] if item["key"] == "left")["parent_key"] = "ghost"

    rewrite_index(tree, ghost)
    with pytest.raises(tc.EvidenceError, match="parent node is missing"):
        tc.validate_tree(tree)


def test_index_path_outside_the_tree_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def escape(record):
        next(item for item in record["nodes"] if item["key"] == "left")["path"] = "../outside"

    rewrite_index(tree, escape)
    with pytest.raises(tc.EvidenceError, match="escapes the tree"):
        tc.validate_tree(tree)


def test_duplicate_trajectory_identity_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)
    record = read_json(tree / "tree.json")
    record["nodes"].append(
        {
            "key": "duplicate",
            "path": "nodes/duplicate",
            "trajectory_id": record["nodes"][0]["trajectory_id"],
            "branch_id": "duplicate",
            "trunk": False,
            "parent_key": "root",
        }
    )
    write_json(tree / "tree.json", record)
    with pytest.raises(tc.EvidenceError, match="same trajectory identity"):
        tc.load_tree(tree)


def test_cross_root_link_is_rejected(lab: dict, tmp_path: Path) -> None:
    other_source = tmp_path / "other-source"
    record_session(other_source)
    other_plain = tmp_path / "other-plain"
    other_trace = other_source / "session.jsonl"
    tc.build_trajectory(other_trace, other_source / "audit", other_plain)
    other_root = tmp_path / "other-bundle"
    tc.build_trajectory(
        other_trace, other_source / "audit", other_root, tree=tc.root_placement("main", identity_of(other_plain))
    )
    assert tc.load_tree(lab["tree"]).tree_id != tc.load_trajectory(other_root).summary().tree_id
    foreign = {
        key: value
        for key, value in read_json(other_root / "trajectory.json")["tree"]["root"].items()
        if key in {"trajectory_id", "chain_sha256", "sha256"}
    }

    tree = copy_tree(lab, tmp_path)

    def relink(manifest):
        section = manifest["tree"]
        section["root"] = dict(foreign)
        section["tree_id"] = foreign["sha256"]
        section["chain"]["sha256"] = tc.chain_sha256(
            trajectory_id=manifest["trajectory_id"],
            branch=section["branch_id"],
            trunk=section["trunk"],
            parent=section["parent"],
            parent_sha256=section["parent"]["chain_sha256"],
            tree_id=foreign["sha256"],
        )

    rewrite_node(tree, "left", relink)
    with pytest.raises(tc.EvidenceError, match="foreign"):
        tc.validate_tree(tree)


def test_child_parent_boundary_must_exist_in_the_parent(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)

    def move_boundary(manifest):
        section = manifest["tree"]
        section["parent"]["boundary"] = {"epoch": 99, "tick": 999, "revision": 0}
        section["chain"]["sha256"] = tc.chain_sha256(
            trajectory_id=manifest["trajectory_id"],
            branch=section["branch_id"],
            trunk=section["trunk"],
            parent=section["parent"],
            parent_sha256=section["parent"]["chain_sha256"],
            tree_id=section["root"]["sha256"],
        )

    rewrite_node(tree, "left", move_boundary)
    with pytest.raises(tc.EvidenceError, match="never reached"):
        tc.validate_tree(tree)


def test_export_is_deterministic_and_never_overwrites(lab: dict, tmp_path: Path) -> None:
    first = tc.export_tree(lab["tree"], tmp_path / "first.zip")
    second = tc.export_tree(lab["tree"], tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert "tree.json" in names
        assert "nodes/root/trajectory.json" in names
    with pytest.raises(tc.EvidenceError, match="already exists"):
        tc.export_tree(lab["tree"], first)
