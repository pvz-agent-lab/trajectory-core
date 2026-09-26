"""Public error taxonomy for ``trajectory_core``.

The package keeps the old contract that *rejected evidence* is a data problem and
therefore a ``ValueError``; callers that already catch ``EvidenceError`` from the
pre-migration ``llm_vs_zombies`` readers keep working.  The subclasses separate
the failure classes the migration review asked to distinguish:

``EvidenceError``
    The bytes on disk are structurally valid JSON but the evidence inside is
    wrong: a broken hash chain, a mismatched digest, a tampered parent link.
``UnsupportedSchemaError``
    The record is readable, but this package does not know the schema/version.
    Unknown schemas fail closed instead of being silently upgraded.
``IncompleteEvidenceError``
    Something a complete sealed artifact must carry is absent: a missing file,
    a recording that was never closed, a declared receipt that does not exist.
``PathContractError``
    A reference tries to address bytes outside the package root: absolute
    paths, ``..`` traversal, drive/UNC prefixes or symlink escapes.
``SealError``
    A seal record or its bound artifacts cannot be re-derived.
``OutcomeContractError``
    A versioned outcome document is malformed, incomplete or contradicts
    itself; unknown facts may never be turned into success claims.
``ClosureError``
    A formal closure record, its producer receipt, its rerun reports or the
    package they bind are missing, duplicated, conflicting or changed.
``UnsupportedCapabilityError``
    The requested operation is deliberately not implemented (for example a
    native-runtime replay).  It is never a silent success.
"""

from __future__ import annotations


class TrajectoryCoreError(Exception):
    """Base class for every error this package raises on its own behalf."""


class EvidenceError(TrajectoryCoreError, ValueError):
    """Stored evidence is present but wrong or contradictory."""


class UnsupportedSchemaError(EvidenceError):
    """The record declares a schema or format version this package rejects."""


class IncompleteEvidenceError(EvidenceError):
    """A complete artifact requires evidence that is absent or still open."""


class PathContractError(EvidenceError):
    """A referenced path leaves its package root or depends on the cwd."""


class SealError(EvidenceError):
    """A seal record disagrees with the artifacts it binds."""


class OutcomeContractError(EvidenceError):
    """A versioned outcome document is malformed or self-contradictory."""


class ClosureError(EvidenceError):
    """A formal closure record cannot be closed over the package it binds."""


class UnsupportedCapabilityError(TrajectoryCoreError):
    """The requested capability is intentionally unavailable."""
