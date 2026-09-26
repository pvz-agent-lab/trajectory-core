"""Shared fixtures: fully synthetic, game-free recordings built by public API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trajectory_core as tc
from support.formal_demo import build_formal_demo
from support.synthetic import record_session


def build_bundle(
    root: Path, name: str, *, tree: tc.TreePlacement | None = None, terminal_tick: int | None = None
) -> Path:
    source = root / f"{name}-source"
    bundle = root / f"{name}-bundle"
    trace = record_session(source, terminal_tick=terminal_tick)
    tc.build_trajectory(trace, source / "audit", bundle, tree=tree)
    return bundle


def identity_of(bundle: str | Path) -> dict:
    manifest = tc.read_manifest(bundle)
    initial = manifest["initial"]
    return {
        "game": initial["identity"]["game"],
        "artifacts": initial["identity"]["artifacts"],
        "init_recipe": initial["initialization"],
    }


@pytest.fixture(scope="session")
def lab(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One plain bundle plus a root/child tree, all built from synthetic runs."""
    root = tmp_path_factory.mktemp("trajectory-core-lab")
    source = root / "plain-source"
    trace = record_session(source)
    plain = root / "plain-bundle"
    tc.build_trajectory(trace, source / "audit", plain)
    trunk = root / "trunk-bundle"
    tc.build_trajectory(trace, source / "audit", trunk, tree=tc.root_placement("main", identity_of(plain)))
    end = tc.end_boundary(tc.read_manifest(trunk))
    left = build_bundle(root, "left", tree=tc.branch_placement(tc.load_trajectory(trunk), "left", at=end))
    right = build_bundle(root, "right", tree=tc.branch_placement(tc.load_trajectory(trunk), "right", at=end))
    tree = root / "tree"
    tc.package_tree(tree, tc.load_trajectory(trunk), branches=[tc.load_trajectory(left), tc.load_trajectory(right)])
    trunk_manifest = tc.read_manifest(trunk)
    root_identity = {
        "game": trunk_manifest["initial"]["identity"]["game"],
        "artifacts": trunk_manifest["initial"]["identity"]["artifacts"],
        "init_recipe": trunk_manifest["initial"]["initialization"],
    }
    return {
        "root": root,
        "plain": plain,
        "trunk": trunk,
        "left": left,
        "right": right,
        "tree": tree,
        "end": end,
        "identity": root_identity,
    }


@pytest.fixture(scope="session")
def formal_lab(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One synthetic two-branch + two-rerun formal closure package."""
    workspace = tmp_path_factory.mktemp("formal-closure-lab")
    return build_formal_demo(workspace)


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: dict) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def rewrite_manifest(bundle: str | Path, change) -> None:
    """Edit a manifest and re-apply the public content identity."""
    path = Path(bundle) / "trajectory.json"
    manifest = read_json(path)
    change(manifest)
    manifest["trajectory_id"] = tc.content_identity(manifest)
    write_json(path, manifest)


def replace_tree_link(bundle: str | Path, *, root: dict, parent: dict | None = None) -> None:
    """Recompute a node's own chain after changing what it points at."""
    path = Path(bundle) / "trajectory.json"
    manifest = read_json(path)
    section = manifest["tree"]
    section["root"] = dict(root)
    if parent is not None:
        section["parent"] = dict(parent)
    section["chain"]["parent_sha256"] = section["parent"]["chain_sha256"] if section["parent"] else None
    section["chain"]["sha256"] = tc.chain_sha256(
        trajectory_id=manifest["trajectory_id"],
        branch=section["branch_id"],
        trunk=section["trunk"],
        parent=section["parent"],
        parent_sha256=section["chain"]["parent_sha256"],
        tree_id=section["root"]["sha256"],
    )
    section["tree_id"] = section["root"]["sha256"]
    write_json(path, manifest)
