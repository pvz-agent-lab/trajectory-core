"""Seal verification and deterministic export contracts.

A *seal* is a small record that binds one packaged tree to the artifacts that
explain it: a tree identity, every node's manifest digest and the digest of each
report.  The record is written by the legacy issue #99 tooling and stores paths
with Windows separators relative to the old repository root.  This module reads
it with an explicit ``source_root`` -- the only place a legacy path convention
is interpreted -- and re-derives every field.  It never rewrites the record or
any artifact it binds.

Every declaration is validated before it is compared.  Duplicate node keys,
duplicate report references (including normalized aliases), non-object entries
and entries missing required fields are ``SealError``s instead of being
silently overwritten or dropped.  An empty ``reports`` list stays valid: a seal
may bind a tree without binding any report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._paths import normalize_reference, resolve_under
from .errors import EvidenceError, SealError, UnsupportedSchemaError
from .jsonio import file_sha256, read_json
from .trajectory import load_trajectory
from .tree import EvidenceTree, _load_index, export_tree

SEAL_REPORT_SCHEMA = "trajectory-core.seal-report.v1"
SUPPORTED_SEAL_SCHEMAS = frozenset({"lvz.issue99-shovel-fork-seal.v1"})

__all__ = ["SUPPORTED_SEAL_SCHEMAS", "SEAL_REPORT_SCHEMA", "read_seal", "verify_seal", "export_tree"]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NODE_FIELDS = ("key", "branch_id", "trunk", "parent_key", "trajectory_id", "manifest_sha256")
_REPORT_FIELDS = ("path", "sha256")


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


def _validate_node_bindings(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate every declared node binding, rejecting duplicates and omissions."""
    declared = record.get("nodes")
    if not isinstance(declared, list):
        raise SealError("seal record nodes must be a list")
    bindings: dict[str, dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, dict):
            raise SealError(f"seal node binding must be an object: {entry!r}")
        missing = [name for name in _NODE_FIELDS if name not in entry]
        if missing:
            raise SealError(f"seal node binding is missing required fields: {', '.join(missing)}")
        key = entry["key"]
        if not isinstance(key, str) or not key:
            raise SealError("seal node binding key must be a nonempty string")
        if key in bindings:
            raise SealError(f"seal record binds node {key!r} more than once")
        if not isinstance(entry["branch_id"], str):
            raise SealError(f"seal node binding {key!r} branch_id must be a string")
        if type(entry["trunk"]) is not bool:
            raise SealError(f"seal node binding {key!r} trunk must be boolean")
        if entry["parent_key"] is not None and not isinstance(entry["parent_key"], str):
            raise SealError(f"seal node binding {key!r} parent_key must be a string or null")
        for name in ("trajectory_id", "manifest_sha256"):
            value = entry[name]
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise SealError(f"seal node binding {key!r} {name} must be a lowercase SHA-256 value")
        bindings[key] = entry
    return bindings


def _validate_report_bindings(root: Path, record: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
    """Validate and resolve every declared report binding once.

    Returns ``(entry, resolved_path)`` pairs.  Duplicates are detected on the
    normalized reference and on the resolved path, so ``a\\b.json``,
    ``a/b.json`` and ``./a/b.json`` cannot all be bound separately, and neither
    can two references that resolve to the same file through a symlink.
    """
    declared = record.get("reports")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise SealError("seal record reports must be a list")
    resolved_entries: list[tuple[dict[str, Any], Path]] = []
    seen_references: set[str] = set()
    seen_paths: set[Path] = set()
    for entry in declared:
        if not isinstance(entry, dict):
            raise SealError(f"seal report binding must be an object: {entry!r}")
        missing = [name for name in _REPORT_FIELDS if name not in entry]
        if missing:
            raise SealError(f"seal report binding is missing required fields: {', '.join(missing)}")
        reference = entry["path"]
        if not isinstance(reference, str) or not reference.strip():
            raise SealError("seal report path must be a nonempty string")
        checksum = entry["sha256"]
        if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
            raise SealError(f"seal report {reference!r} sha256 must be a lowercase SHA-256 value")
        normalized = normalize_reference(reference, legacy_windows=True)
        resolved = resolve_under(root, reference, legacy_windows=True)
        if normalized in seen_references or resolved in seen_paths:
            raise SealError(f"seal record binds report {reference!r} more than once")
        seen_references.add(normalized)
        seen_paths.add(resolved)
        resolved_entries.append((entry, resolved))
    return resolved_entries


def _attribute_node_failures(tree: EvidenceTree, nodes: Any) -> dict[str, str]:
    """When full tree validation fails, load each validated node to name the broken ones."""
    outcome: dict[str, str] = {}
    for node in nodes:
        try:
            loaded = load_trajectory(tree.node_manifest(node))
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

    Structural problems in the record itself -- duplicate node keys, duplicate
    report references, non-object entries or missing required fields -- raise
    ``SealError``.  Mismatches between a well-formed record and the artifacts
    stay in ``problems`` so a caller can print all of them at once.
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

    declared_nodes = _validate_node_bindings(record)
    declared_reports = _validate_report_bindings(root, record)

    verified: dict[str, Any] | None = None
    node_verification = "digest_only"
    attribution: dict[str, str] = {}
    if verify_nodes:
        try:
            verified = tree.validate()
            node_verification = "full"
        except EvidenceError as error:
            node_verification = "failed"
            problems.append({"what": "tree.validate", "detail": f"{type(error).__name__}: {error}"})
            attribution = _attribute_node_failures(tree, tree.nodes)

    node_checks: list[NodeCheck] = []
    for node in tree.nodes:
        key = str(node["key"])
        declared = declared_nodes.pop(key, None)
        manifest_path = tree.node_manifest(node)
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
    for key in sorted(declared_nodes):
        problems.append({"what": "node", "key": key, "detail": "the seal binds a node the tree does not have"})

    reports = []
    for entry, resolved in declared_reports:
        actual_sha256 = file_sha256(resolved) if resolved.is_file() else None
        match = actual_sha256 is not None and actual_sha256 == entry["sha256"]
        reports.append(
            {
                "path": entry["path"],
                "resolved": str(resolved),
                "declared_sha256": entry["sha256"],
                "actual_sha256": actual_sha256,
                "match": match,
            }
        )
        if not match:
            problems.append(
                {
                    "what": "report.sha256",
                    "path": entry["path"],
                    "declared": entry["sha256"],
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
