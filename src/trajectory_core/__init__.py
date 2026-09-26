"""Public API of ``trajectory-core``.

The package reads and validates the sealed ``lvz.*`` evidence formats without a
game, a Windows runtime, a socket or a network.  Start with:

* :func:`load_trajectory` / :func:`validate_trajectory` -- one sealed bundle;
* :func:`load_tree` / :func:`validate_tree` / :func:`inspect_tree` -- a
  packaged tree of bundles;
* :func:`verify_seal` -- a seal record bound to a tree and its reports.

The old format identity is kept: manifests, canonical JSON and content hashes
are defined exactly once, in the adapted legacy closure under
``trajectory_core.legacy_lvz``.
"""

from __future__ import annotations

from .errors import (
    EvidenceError,
    IncompleteEvidenceError,
    PathContractError,
    SealError,
    TrajectoryCoreError,
    UnsupportedCapabilityError,
    UnsupportedSchemaError,
)
from .identity import AUDIT_SCHEMA, TRAJECTORY_SCHEMA, TREE_SCHEMA, chain_sha256, content_identity, end_boundary
from .jsonio import canonical, decode, file_hash, read_json
from .seal import SEAL_REPORT_SCHEMA, SUPPORTED_SEAL_SCHEMAS, read_seal, verify_seal
from .trajectory import (
    StepSummary,
    SummaryTermination,
    Trajectory,
    TrajectorySummary,
    build_trajectory,
    load_trajectory,
    read_manifest,
    validate_trajectory,
)
from .tree import (
    EvidenceTree,
    TreeNodeSummary,
    TreePlacement,
    TreeSummary,
    attach_tree,
    branch_placement,
    export_tree,
    inspect_tree,
    load_tree,
    package_tree,
    root_placement,
    validate_tree,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # errors
    "TrajectoryCoreError",
    "EvidenceError",
    "UnsupportedSchemaError",
    "IncompleteEvidenceError",
    "PathContractError",
    "SealError",
    "UnsupportedCapabilityError",
    # schemas and identity
    "TRAJECTORY_SCHEMA",
    "TREE_SCHEMA",
    "AUDIT_SCHEMA",
    "content_identity",
    "chain_sha256",
    "end_boundary",
    # json helpers
    "canonical",
    "decode",
    "read_json",
    "file_hash",
    # trajectory
    "Trajectory",
    "TrajectorySummary",
    "StepSummary",
    "SummaryTermination",
    "load_trajectory",
    "validate_trajectory",
    "build_trajectory",
    "read_manifest",
    # tree
    "EvidenceTree",
    "TreeSummary",
    "TreeNodeSummary",
    "TreePlacement",
    "load_tree",
    "validate_tree",
    "inspect_tree",
    "root_placement",
    "branch_placement",
    "attach_tree",
    "package_tree",
    "export_tree",
    # seal
    "SEAL_REPORT_SCHEMA",
    "SUPPORTED_SEAL_SCHEMAS",
    "read_seal",
    "verify_seal",
]
