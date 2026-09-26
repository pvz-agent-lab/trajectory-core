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

import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ._paths import require_within, resolve_under
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
        self._directories = {
            str(node["key"]): resolve_under(legacy.directory, str(node["path"])) for node in legacy.nodes
        }

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

    def node_directory(self, node: dict[str, Any]) -> Path:
        """The validated, resolved directory of an indexed node."""
        key = str(node["key"])
        directory = self._directories.get(key)
        if directory is None:
            raise EvidenceError(f"tree node is not part of the validated index: {key}")
        return directory

    def validate(self) -> dict[str, Any]:
        """Fully re-derive the tree once; later calls reuse the verified result."""
        if self._summary is None:
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
    manifest_path = tree.node_directory(node) / MANIFEST_FILE
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
    """Read ``tree.json``, validate the whole index, and only then allow node reads."""
    legacy = _legacy_tree.EvidenceTree.load(path)
    record = legacy.record
    if not isinstance(record, dict) or record.get("schema") != TREE_SCHEMA:
        schema = record.get("schema") if isinstance(record, dict) else None
        raise UnsupportedSchemaError(f"unsupported tree schema {schema!r}; this package reads {TREE_SCHEMA!r}")
    _validate_index(legacy.directory, record)
    identities = [str(node["trajectory_id"]) for node in legacy.nodes]
    if len(set(identities)) != len(identities):
        raise EvidenceError("the tree indexes the same trajectory identity more than once")
    return EvidenceTree(legacy)


def _validate_index(directory: Path, record: Any) -> list[dict[str, Any]]:
    """Enforce the public path contract, then the legacy index rules.

    This runs before any node manifest is opened: an index path that is
    absolute, drive/UNC prefixed, traversing or a resolved symlink escape is
    rejected here, and the legacy validator still proves keys, parents and
    directories exactly as before.
    """
    declared = record.get("nodes") if isinstance(record, dict) else None
    if isinstance(declared, list):
        for node in declared:
            if isinstance(node, dict) and isinstance(node.get("path"), str):
                resolve_under(directory, node["path"])
    try:
        nodes = _legacy_tree._index_entries(directory, record)
    except TypeError as error:
        raise EvidenceError(f"evidence tree node entry is malformed: {error}") from error
    return list(nodes)


def _boundary_key(boundary: Any) -> tuple[int, int, int] | None:
    if not isinstance(boundary, dict) or set(boundary) != {"epoch", "tick", "revision"}:
        return None
    values = (boundary["epoch"], boundary["tick"], boundary["revision"])
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return values


def _reached_boundaries(manifest: dict[str, Any]) -> set[tuple[int, int, int]]:
    """Every boundary a recording can be departed from.

    The set covers the initial boundary, every request end, and every
    intermediate audit boundary a multi-tick request actually passed through
    (action revisions and per-tick post-step versions). Legacy branch
    coordinates inside a multi-tick request are therefore not rejected just
    because the request only records its final version.
    """
    reached: set[tuple[int, int, int]] = set()
    current = _boundary_key(manifest["initial"]["observation"]["version"])
    if current is not None:
        reached.add(current)
    for step in manifest.get("steps") or []:
        request = step.get("request") or {}
        method = request.get("method")
        expect = _boundary_key(request.get("expect")) if isinstance(request, dict) else None
        if expect is not None:
            reached.add(expect)
            current = expect
        if method == "capture_frame":
            after = _boundary_key(step.get("after_version"))
            if after is not None:
                reached.add(after)
                current = after
            continue
        if method not in {"commit", "advance"}:
            continue
        result = step.get("result")
        if not isinstance(result, dict) or current is None:
            continue
        outcomes = result.get("action_results")
        if isinstance(outcomes, list):
            for ordinal in range(len(outcomes)):
                reached.add((current[0], current[1], current[2] + ordinal + 1))
        executed = result.get("executed_ticks")
        if type(executed) is int:
            for tick in range(1, executed + 1):
                reached.add((current[0], current[1] + tick, 0))
        end = _boundary_key(result.get("observation", {}).get("version"))
        if end is not None:
            reached.add(end)
            current = end
    return reached


def _verify_parent_boundaries(tree: EvidenceTree) -> None:
    """A child may only depart from a boundary its parent actually reached."""
    manifests = {str(node["key"]): read_json(tree.node_directory(node) / MANIFEST_FILE) for node in tree.nodes}
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


def package_tree(output_directory: str | Path, root: Any, *, branches: Iterable[Any] = ()) -> EvidenceTree:
    """Copy placed bundles into a new tree and fully validate it before returning.

    The legacy packager writes the directory and re-derives the legacy
    contract. This wrapper then applies the same checks the public reader uses
    (index path contract, duplicate identities and parent departure
    boundaries); a tree the reader would reject is never returned. An existing
    destination is never touched, and an output created by this call is
    removed when the public validation fails.
    """
    output = Path(output_directory)
    if output.exists():
        raise EvidenceError(f"package destination already exists: {output}")
    _legacy_tree.package_tree(output, root, branches=branches)
    try:
        tree = _load_index(output)
        tree.validate()
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return tree


def export_tree(path: str | Path, output: str | Path) -> Path:
    """Export a fully validated tree as a deterministic ZIP.

    Full public validation (including parent departure boundaries) completes
    before any output is created; an existing destination is never overwritten
    and a file this call started is removed when writing fails.
    """
    destination = Path(output)
    if destination.exists():
        raise EvidenceError(f"export destination already exists: {destination}")
    tree = load_tree(path)
    if destination.parent:
        destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_archive(tree.directory, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _write_archive(tree_directory: Path, destination: Path) -> None:
    """The deterministic ZIP layout the legacy exporter defined.

    Every archive member is proven to resolve inside the tree before the ZIP is
    opened, so a symlinked member cannot smuggle bytes from outside the tree.
    """
    root = tree_directory.resolve()
    files = []
    for candidate in tree_directory.rglob("*"):
        if candidate.is_file():
            require_within(root, candidate, label="tree archive member")
            files.append(candidate)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for candidate in sorted(files):
            relative = candidate.relative_to(tree_directory).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, candidate.read_bytes())
