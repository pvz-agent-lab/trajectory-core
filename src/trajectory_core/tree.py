"""Read and validate one sealed ``lvz.evidence-tree.v1`` package.

A packaged tree adds exactly one manifest section (``tree``) to a trajectory:
where it sits, which branch it belongs to and how its content identity binds
into the parent-to-child hash chain.  Verification re-derives the root identity
and every chain from the root down; a lone child cannot prove its recorded root
digest, so ``validate_tree`` is the authoritative check.

``inspect_tree`` is the deliberate partial mode: it reads the index and every
node manifest but does not re-derive the chains.  Its report says so, so a
partial read can never be mistaken for a verified one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import EvidenceError, UnsupportedSchemaError
from .identity import MANIFEST_FILE, TREE_SCHEMA, end_boundary
from .jsonio import file_sha256, read_json
from .legacy_lvz import evidence_tree as _legacy_tree
from .legacy_lvz.evidence_tree import TreePlacement

__all__ = [
    "EvidenceTree",
    "TreeSummary",
    "TreeNodeSummary",
    "load_tree",
    "validate_tree",
    "inspect_tree",
    "summary_of",
    "TreePlacement",
    "root_placement",
    "branch_placement",
    "attach_tree",
    "package_tree",
    "export_tree",
]

root_placement = _legacy_tree.root_placement
branch_placement = _legacy_tree.branch_placement
attach_tree = _legacy_tree.attach_tree
package_tree = _legacy_tree.package_tree
export_tree = _legacy_tree.export_tree


@dataclass(frozen=True)
class TreeNodeSummary:
    """One tree node as it is indexed, plus its recorded boundaries."""

    key: str
    path: str
    trajectory_id: str
    branch_id: str
    trunk: bool
    parent_key: str | None
    parent_boundary: dict[str, int] | None
    steps: int
    initial_version: dict[str, int]
    end_boundary: dict[str, int]
    manifest_sha256: str
    verification: str


@dataclass(frozen=True)
class TreeSummary:
    """The public schema of a packaged tree."""

    schema: str
    tree_id: str
    root: str
    nodes: tuple[TreeNodeSummary, ...]
    branches: tuple[str, ...]
    trunk_nodes: tuple[str, ...]
    branch_points: tuple[dict[str, Any], ...]
    scope: str
    verification: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["nodes"] = [asdict(node) for node in self.nodes]
        return value


class EvidenceTree:
    """A loaded, fully re-verified tree package."""

    def __init__(self, legacy: _legacy_tree.EvidenceTree) -> None:
        self._legacy = legacy
        self._record = legacy.record
        self._summary: dict[str, Any] | None = None

    @property
    def directory(self) -> Path:
        return self._legacy.directory

    @property
    def record(self) -> dict[str, Any]:
        return self._record

    @property
    def tree_id(self) -> str:
        return str(self._record["tree_id"])

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return list(self._legacy.nodes)

    def validate(self) -> dict[str, Any]:
        """Fully re-derive the tree; returns the legacy-shaped summary."""
        self._summary = self._legacy.validate()
        _verify_parent_boundaries(self)
        return self._summary

    def verify(self) -> dict[str, Any]:
        summary = self.validate()
        return {
            "schema": "trajectory-core.tree-report.v1",
            "path": str(self.directory),
            "status": "valid",
            "summary": summary,
            "verification": "full",
        }

    def summary(self) -> TreeSummary:
        """The typed projection of the already-validated tree."""
        if self._summary is None:
            self.validate()
        assert self._summary is not None  # noqa: S101 - set directly above
        return summary_of(self, self._summary)


def _node_summary(tree: EvidenceTree, node: dict[str, Any], *, verification: str) -> TreeNodeSummary:
    manifest_path = tree.directory / node["path"] / MANIFEST_FILE
    manifest = read_json(manifest_path)
    section = manifest.get("tree") or {}
    parent = section.get("parent") if isinstance(section, dict) else None
    return TreeNodeSummary(
        key=str(node["key"]),
        path=str(node["path"]),
        trajectory_id=str(node["trajectory_id"]),
        branch_id=str(node["branch_id"]),
        trunk=bool(node["trunk"]),
        parent_key=node["parent_key"],
        parent_boundary=dict(parent["boundary"]) if isinstance(parent, dict) else None,
        steps=len(manifest.get("steps") or []),
        initial_version=dict(manifest["initial"]["observation"]["version"]),
        end_boundary=dict(end_boundary(manifest)),
        manifest_sha256=file_sha256(manifest_path),
        verification=verification,
    )


def summary_of(tree: EvidenceTree, verified: dict[str, Any] | None = None) -> TreeSummary:
    """Project a tree index into typed nodes; ``verified`` is the full summary."""
    record = tree.record
    nodes = [_node_summary(tree, node, verification="full" if verified else "index_only") for node in tree.nodes]
    if verified is not None:
        branches = tuple(verified["branches"])
        trunk_nodes = tuple(verified["trunk_nodes"])
        branch_points = tuple(verified["branch_points"])
        scope = str(verified["scope"])
    else:
        branches = tuple(sorted({node.branch_id for node in nodes}))
        trunk_nodes = tuple(sorted(node.key for node in nodes if node.trunk))
        branch_points = tuple(
            {"key": node.key, "parent": node.parent_key, "branch_id": node.branch_id, "boundary": node.parent_boundary}
            for node in nodes
            if node.parent_key is not None
        )
        scope = (
            "index-only projection: node manifests and boundaries were read; the root identity "
            "and parent-to-child chains were not re-derived"
        )
    return TreeSummary(
        schema=str(record["schema"]),
        tree_id=str(record["tree_id"]),
        root=str(record["root"]),
        nodes=tuple(nodes),
        branches=branches,
        trunk_nodes=trunk_nodes,
        branch_points=branch_points,
        scope=scope,
        verification="full" if verified is not None else "index_only",
    )


def _load_index(path: str | Path) -> EvidenceTree:
    """Read ``tree.json`` and check its schema; nodes are not read yet."""
    legacy = _legacy_tree.EvidenceTree.load(path)
    record = legacy.record
    if not isinstance(record, dict) or record.get("schema") != TREE_SCHEMA:
        schema = record.get("schema") if isinstance(record, dict) else None
        raise UnsupportedSchemaError(f"unsupported tree schema {schema!r}; this package reads {TREE_SCHEMA!r}")
    identities = [str(node["trajectory_id"]) for node in legacy.nodes]
    if len(set(identities)) != len(identities):
        raise EvidenceError("the tree indexes the same trajectory identity more than once")
    return EvidenceTree(legacy)


def _boundary_key(boundary: Any) -> tuple[int, int, int] | None:
    if not isinstance(boundary, dict) or set(boundary) != {"epoch", "tick", "revision"}:
        return None
    values = (boundary["epoch"], boundary["tick"], boundary["revision"])
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return values


def _reached_boundaries(manifest: dict[str, Any]) -> set[tuple[int, int, int]]:
    """Every boundary a recording can be departed from: its initial plus step ends."""
    reached = {_boundary_key(manifest["initial"]["observation"]["version"])}
    for step in manifest.get("steps") or []:
        if step["request"]["method"] == "capture_frame":
            version = step.get("after_version")
        else:
            version = step.get("result", {}).get("observation", {}).get("version")
        reached.add(_boundary_key(version) if isinstance(version, dict) else None)
    return {item for item in reached if item is not None}


def _verify_parent_boundaries(tree: EvidenceTree) -> None:
    """A child may only depart from a boundary its parent actually reached."""
    manifests = {str(node["key"]): read_json(tree.directory / str(node["path"]) / MANIFEST_FILE) for node in tree.nodes}
    for node in tree.nodes:
        parent_key = node["parent_key"]
        if parent_key is None:
            continue
        section = manifests[str(node["key"])]["tree"]
        parent = section.get("parent")
        if not isinstance(parent, dict):
            raise EvidenceError(f"child node has no parent reference: {node['key']}")
        boundary = _boundary_key(parent.get("boundary"))
        if boundary is None or boundary not in _reached_boundaries(manifests[str(parent_key)]):
            raise EvidenceError(
                f"child node {node['key']} departs from a boundary its parent never reached: {parent.get('boundary')!r}"
            )


def load_tree(path: str | Path) -> EvidenceTree:
    """Load a packaged tree and re-derive every node chain and parent boundary."""
    tree = _load_index(path)
    tree.validate()
    return tree


def validate_tree(path: str | Path) -> dict[str, Any]:
    """Validate a packaged tree and return the machine-readable report."""
    return load_tree(path).verify()


def inspect_tree(path: str | Path) -> TreeSummary:
    """Read the index and node manifests without re-deriving the chains."""
    return summary_of(_load_index(path))
