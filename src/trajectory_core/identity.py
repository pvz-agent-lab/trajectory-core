"""Shared format constants and the single authoritative identity definitions.

The digest formulas in this module are not re-implemented: they re-export the
legacy reader's implementation at the adapted commit so old and new readers
cannot drift.  See ``NOTICES.md``.
"""

from __future__ import annotations

from typing import Any

from .legacy_lvz import evidence_tree as _legacy_tree

TRAJECTORY_SCHEMA = "lvz.engine-replay.v1"
TREE_SCHEMA = "lvz.evidence-tree.v1"
AUDIT_SCHEMA = "lvz.audit.v1"
TREE_FILE = "tree.json"
MANIFEST_FILE = "trajectory.json"

content_identity = _legacy_tree.content_identity
chain_sha256 = _legacy_tree.chain_sha256
digest = _legacy_tree.digest
end_boundary = _legacy_tree.end_boundary

__all__ = [
    "TRAJECTORY_SCHEMA",
    "TREE_SCHEMA",
    "AUDIT_SCHEMA",
    "TREE_FILE",
    "MANIFEST_FILE",
    "content_identity",
    "chain_sha256",
    "digest",
    "end_boundary",
    "identity_fields",
]


def identity_fields(manifest: Any) -> set[str]:
    """The manifest fields excluded from ``trajectory_id``."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return {"trajectory_id", "tree"}
