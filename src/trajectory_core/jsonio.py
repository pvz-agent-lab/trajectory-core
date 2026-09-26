"""Strict JSON and hashing primitives, inherited from the legacy readers.

These are the exact rules the old ``llm_vs_zombies`` readers used, so a digest
computed here still identifies the same bytes:

* UTF-8, sorted keys, no insignificant whitespace, ``NaN`` refused;
* duplicate object keys are an error, not a last-one-wins;
* content identity covers every manifest field except ``trajectory_id`` and
  ``tree`` (a placement never changes what a recording *is*).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .legacy_lvz.audit_compare import canonical, decode, file_hash, read_json

__all__ = ["canonical", "decode", "read_json", "file_hash", "digest_sha256", "sha256_bytes"]


def digest_sha256(value: Any) -> str:
    """The lowercase SHA-256 of one canonical JSON value."""
    import hashlib

    return hashlib.sha256(canonical(value)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    """The lowercase SHA-256 of a file's raw bytes."""
    return file_hash(Path(path))
