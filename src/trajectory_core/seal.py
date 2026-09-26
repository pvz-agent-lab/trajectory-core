"""Seal verification and deterministic export contracts.

A *seal* is a small record that binds one packaged tree to the artifacts that
explain it: a tree identity, every node's manifest digest and the digest of each
report.  The record is written by the legacy issue #99 tooling and stores paths
with Windows separators relative to the old repository root.  This module reads
it with an explicit ``source_root`` -- the only place a legacy path convention
is interpreted -- and re-derives every field.  It never rewrites the record or
any artifact it binds.

``export_tree`` is the only writer here: it validates first and refuses an
existing destination, so a seal can never be silently replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._paths import resolve_under
from .errors import EvidenceError, SealError, UnsupportedSchemaError
from .identity import MANIFEST_FILE
from .jsonio import file_sha256, read_json
from .trajectory import load_trajectory
from .tree import _load_index
from .tree import export_tree as _legacy_export_tree

SEAL_REPORT_SCHEMA = "trajectory-core.seal-report.v1"
SUPPORTED_SEAL_SCHEMAS = frozenset({"lvz.issue99-shovel-fork-seal.v1"})

__all__ = ["SUPPORTED_SEAL_SCHEMAS", "SEAL_REPORT_SCHEMA", "read_seal", "verify_seal", "export_tree"]

export_tree = _legacy_export_tree


@dataclass
class NodeCheck:
    """One seal node compared against the tree and, optionally, re-read."""

    key: str
    declared: dict[str, Any]
    actual: dict[str, Any]
    manifest_sha256_declared: str | None
    manifest_sha256_actual: str | None
    trajectory_verification: str
    problems: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "declared": self.declared,
            "actual": self.actual,
            "manifest_sha256_declared": self.manifest_sha256_declared,
            "manifest_sha256_actual": self.manifest_sha256_actual,
            "trajectory_verification": self.trajectory_verification,
            "problems": self.problems,
        }


def read_seal(path: str | Path) -> dict[str, Any]:
    """Read a seal record and fail closed on an unknown seal schema."""
    seal_path = Path(path)
    if not seal_path.is_file():
        raise SealError(f"seal record is missing: {seal_path}")
    record = read_json(seal_path)
    if not isinstance(record, dict):
        raise SealError(f"seal record must be an object: {seal_path}")
    schema = record.get("schema")
    if schema not in SUPPORTED_SEAL_SCHEMAS:
        raise UnsupportedSchemaError(f"unsupported seal schema {schema!r}; supported: {sorted(SUPPORTED_SEAL_SCHEMAS)}")
    return record


def _compare_node(declared: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    for name in ("branch_id", "trunk", "parent_key", "trajectory_id"):
        if declared.get(name) != actual.get(name):
            problems.append(
                {
                    "what": f"node.{name}",
                    "key": actual["key"],
                    "declared": declared.get(name),
                    "actual": actual.get(name),
                }
            )
    return problems


def _attribute_node_failures(tree_directory: Path, nodes: Any) -> dict[str, str]:
    """When full tree validation fails, load each node to name the broken ones."""
    outcome: dict[str, str] = {}
    for node in nodes:
        try:
            loaded = load_trajectory(tree_directory / str(node["path"]))
            outcome[str(node["key"])] = "full"
            if loaded.trajectory_id != node["trajectory_id"]:
                outcome[str(node["key"])] = "identity_mismatch"
        except EvidenceError:
            outcome[str(node["key"])] = "failed"
    return outcome


def verify_seal(seal_path: str | Path, *, source_root: str | Path, verify_nodes: bool = True) -> dict[str, Any]:
    """Re-derive a seal against its tree, reports and (optionally) every node.

    ``source_root`` is the directory the record's relative, possibly
    backslash-separated paths are interpreted against.  Path traversal,
    absolute paths and drive/UNC prefixes are rejected before any file is
    opened.  With ``verify_nodes=True`` the tree is fully re-derived (every
    node is loaded through the public trajectory reader and the whole chain is
    checked); a comparison of manifest digests alone is never presented as
    full verification.
    """
    root = Path(source_root)
    if not root.is_dir():
        raise SealError(f"source_root is not a directory: {root}")
    record = read_seal(seal_path)
    problems: list[dict[str, Any]] = []

    tree_reference = record.get("tree")
    if not isinstance(tree_reference, str):
        raise SealError("seal record has no tree reference")
    tree_directory = resolve_under(root, tree_reference, legacy_windows=True)
    if not tree_directory.is_dir():
        raise SealError(f"sealed tree directory is missing: {tree_directory}")
    tree = _load_index(tree_directory)
    tree_id = str(tree.tree_id)
    if record.get("tree_id") != tree_id:
        problems.append({"what": "tree_id", "declared": record.get("tree_id"), "actual": tree_id})

    verified: dict[str, Any] | None = None
    node_verification = "digest_only"
    if verify_nodes:
        try:
            verified = tree.validate()
            node_verification = "full"
        except EvidenceError as error:
            node_verification = "failed"
            problems.append({"what": "tree.validate", "detail": f"{type(error).__name__}: {error}"})
            attribution = _attribute_node_failures(tree_directory, tree.nodes)
        else:
            attribution = {}
    else:
        attribution = {}

    declared_nodes = list(record.get("nodes") or [])
    by_key = {str(item.get("key")): item for item in declared_nodes if isinstance(item, dict)}
    node_checks: list[NodeCheck] = []
    for node in tree.nodes:
        key = str(node["key"])
        declared = by_key.pop(key, None)
        manifest_path = tree_directory / str(node["path"]) / MANIFEST_FILE
        actual = {
            "key": key,
            "branch_id": node["branch_id"],
            "trunk": node["trunk"],
            "parent_key": node["parent_key"],
            "trajectory_id": node["trajectory_id"],
        }
        per_node = attribution.get(key, node_verification)
        if declared is None:
            check = NodeCheck(
                key=key,
                declared={},
                actual=actual,
                manifest_sha256_declared=None,
                manifest_sha256_actual=file_sha256(manifest_path),
                trajectory_verification="not_bound",
            )
            check.problems.append({"what": "node", "key": key, "detail": "the seal does not bind this node"})
            problems.extend(check.problems)
            node_checks.append(check)
            continue
        check = NodeCheck(
            key=key,
            declared=dict(declared),
            actual=actual,
            manifest_sha256_declared=declared.get("manifest_sha256"),
            manifest_sha256_actual=file_sha256(manifest_path),
            trajectory_verification=per_node,
        )
        check.problems.extend(_compare_node(declared, actual))
        if check.manifest_sha256_declared != check.manifest_sha256_actual:
            check.problems.append(
                {
                    "what": "node.manifest_sha256",
                    "key": key,
                    "declared": check.manifest_sha256_declared,
                    "actual": check.manifest_sha256_actual,
                }
            )
        problems.extend(check.problems)
        node_checks.append(check)
    for key in sorted(by_key):
        problems.append({"what": "node", "key": key, "detail": "the seal binds a node the tree does not have"})

    reports = []
    for item in record.get("reports") or []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            problems.append({"what": "report", "detail": "report entry is malformed", "entry": item})
            continue
        resolved = resolve_under(root, item["path"], legacy_windows=True)
        actual_sha256 = file_sha256(resolved) if resolved.is_file() else None
        match = actual_sha256 is not None and actual_sha256 == item.get("sha256")
        reports.append(
            {
                "path": item["path"],
                "resolved": str(resolved),
                "declared_sha256": item.get("sha256"),
                "actual_sha256": actual_sha256,
                "match": match,
            }
        )
        if not match:
            problems.append(
                {
                    "what": "report.sha256",
                    "path": item["path"],
                    "declared": item.get("sha256"),
                    "actual": actual_sha256,
                }
            )

    verification = ("full" if verified is not None else "failed") if verify_nodes else "structure_only"
    return {
        "schema": SEAL_REPORT_SCHEMA,
        "seal": str(Path(seal_path)),
        "seal_schema": record.get("schema"),
        "source_root": str(root),
        "declared_tree": tree_reference,
        "tree": str(tree_directory),
        "tree_id": tree_id,
        "nodes": len(node_checks),
        "node_checks": [check.to_dict() for check in node_checks],
        "reports": reports,
        "matches": not problems,
        "verification": verification,
        "summary": verified,
        "problems": problems,
    }
