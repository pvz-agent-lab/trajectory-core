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
    with pytest.raises(tc.PathContractError, match="traverse"):
        tc.validate_tree(tree)


def test_duplicate_trajectory_identity_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)
    shutil.copytree(tree / "nodes" / "left", tree / "nodes" / "duplicate")
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


@pytest.mark.parametrize(
    "reference",
    [
        "../outside",
        "C:/outside",
        "C:outside",
        "//server/share/outside",
        "\\\\server\\share\\outside",
        "/absolute/outside",
    ],
)
def test_escaped_index_paths_are_rejected_before_any_node_read(
    lab: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    tree = copy_tree(lab, tmp_path)
    shutil.copytree(lab["tree"] / "nodes" / "left", tmp_path / "outside")

    def escape(record):
        next(item for item in record["nodes"] if item["key"] == "left")["path"] = reference

    rewrite_index(tree, escape)
    reads: list[Path] = []
    real_read_json = tc.tree.read_json

    def spy(path):
        reads.append(Path(path))
        return real_read_json(path)

    monkeypatch.setattr(tc.tree, "read_json", spy)
    with pytest.raises(tc.PathContractError):
        tc.inspect_tree(tree)
    assert reads == []
    with pytest.raises(tc.PathContractError):
        tc.validate_tree(tree)
    assert reads == []
    destination = tmp_path / "escaped.zip"
    with pytest.raises(tc.PathContractError):
        tc.export_tree(tree, destination)
    assert reads == []
    assert not destination.exists()


def test_symlinked_node_path_escape_is_rejected(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)
    outside = tmp_path / "outside"
    shutil.copytree(lab["tree"] / "nodes" / "left", outside)
    shutil.rmtree(tree / "nodes" / "left")
    try:
        (tree / "nodes" / "left").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")

    def escape(record):
        next(item for item in record["nodes"] if item["key"] == "left")["path"] = "nodes/left"

    rewrite_index(tree, escape)
    with pytest.raises(tc.PathContractError, match="escapes its package root"):
        tc.inspect_tree(tree)


def _interior_bundles(tmp_path: Path, interior: dict) -> tuple[Path, Path]:
    source = tmp_path / "interior-source"
    trace = record_session(
        source,
        seed_actions=False,
        plan=lambda driver: driver.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=4),
    )
    plain = tmp_path / "interior-plain"
    tc.build_trajectory(trace, source / "audit", plain)
    root_bundle = tmp_path / "interior-root"
    tc.build_trajectory(trace, source / "audit", root_bundle, tree=tc.root_placement("main", identity_of(plain)))
    child_source = tmp_path / "interior-child-source"
    child_trace = record_session(child_source)
    child = tmp_path / "interior-child"
    tc.build_trajectory(
        child_trace,
        child_source / "audit",
        child,
        tree=tc.branch_placement(tc.load_trajectory(root_bundle), "side", at=interior),
    )
    return root_bundle, child


@pytest.mark.parametrize(
    "interior",
    [
        {"epoch": 1, "tick": 2, "revision": 0},
        {"epoch": 1, "tick": 0, "revision": 1},
        {"epoch": 1, "tick": 4, "revision": 0},
    ],
)
def test_child_may_depart_from_a_boundary_the_parent_reached(tmp_path: Path, interior: dict) -> None:
    root_bundle, child = _interior_bundles(tmp_path, interior)
    tree = tmp_path / "interior-tree"
    tc.package_tree(tree, tc.load_trajectory(root_bundle), branches=[tc.load_trajectory(child)])
    report = tc.validate_tree(tree)
    assert report["status"] == "valid"
    assert report["summary"]["branch_points"][0]["boundary"] == interior


def test_package_refuses_a_boundary_the_reader_rejects(tmp_path: Path) -> None:
    root_bundle, child = _interior_bundles(tmp_path, {"epoch": 9, "tick": 0, "revision": 0})
    output = tmp_path / "rejected-tree"
    with pytest.raises(tc.EvidenceError, match="never reached"):
        tc.package_tree(output, tc.load_trajectory(root_bundle), branches=[tc.load_trajectory(child)])
    assert not output.exists()


def test_export_refuses_a_boundary_the_reader_rejects(lab: dict, tmp_path: Path) -> None:
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
        tc.load_tree(tree)
    destination = tmp_path / "export" / "rejected.zip"
    with pytest.raises(tc.EvidenceError, match="never reached"):
        tc.export_tree(tree, destination)
    assert not destination.exists()
    assert not destination.parent.exists()


def test_export_removes_a_partial_output_it_created(lab: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "partial.zip"

    def failing(tree_directory, output):
        Path(output).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(tc.tree, "_write_archive", failing)
    with pytest.raises(OSError, match="disk full"):
        tc.export_tree(lab["tree"], destination)
    assert not destination.exists()


def test_export_refuses_a_symlinked_member_escaping_the_tree(lab: dict, tmp_path: Path) -> None:
    tree = copy_tree(lab, tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (tree / "extra.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    destination = tmp_path / "escaped.zip"
    with pytest.raises(tc.PathContractError, match="escapes"):
        tc.export_tree(tree, destination)
    assert not destination.exists()


def test_package_never_deletes_a_preexisting_destination(lab: dict, tmp_path: Path) -> None:
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "keep.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(tc.EvidenceError, match="already exists"):
        tc.package_tree(output, tc.load_trajectory(lab["trunk"]))
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_validate_tree_derives_each_node_once(lab: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    real_validate = tc.tree._legacy_tree.EvidenceTree.validate

    def spy(self):
        calls.append(self.directory)
        return real_validate(self)

    monkeypatch.setattr(tc.tree._legacy_tree.EvidenceTree, "validate", spy)
    assert tc.validate_tree(lab["tree"])["status"] == "valid"
    assert len(calls) == 1
    calls.clear()
    tree = tc.load_tree(lab["tree"])
    assert tree.verify()["status"] == "valid"
    tree.verify()
    assert len(calls) == 1
