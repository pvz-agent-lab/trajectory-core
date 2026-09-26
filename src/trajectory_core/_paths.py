"""Path contract helpers: every reference resolves under an explicit root.

The old readers already refuse manifest file references that escape their
bundle, but a caller-visible contract is easier to test and reuse.  Nothing in
this module touches ``os.getcwd``; a relative reference is always joined to the
explicit root the caller supplied.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import PathContractError


def _reject_absolute(reference: str) -> str:
    """Return ``reference`` with host-neutral separators or raise.

    Backslashes are read as separators on every host, so a drive/UNC reference
    written on Windows cannot be mistaken for one relative filename when the
    reader runs on POSIX. Absolute, drive and UNC prefixes are rejected
    uniformly instead of relying on the host filesystem's separator rules.
    """
    cleaned = reference.replace("\\", "/")
    if PurePosixPath(cleaned).is_absolute() or cleaned.startswith("//"):
        raise PathContractError(f"reference must be relative to the package root: {reference!r}")
    if PureWindowsPath(reference).drive:
        raise PathContractError(f"reference must not carry a drive or UNC prefix: {reference!r}")
    if not cleaned or cleaned in {".", "./"}:
        raise PathContractError("reference must not be empty")
    return cleaned


def normalize_reference(reference: str, *, legacy_windows: bool = False) -> str:
    """Return a POSIX-style relative reference or raise ``PathContractError``.

    The grammar is host-neutral. ``legacy_windows`` marks a caller that is
    reading an old Windows-written record (for example a seal); backslash
    separators are normalized on every host either way, and the parameter no
    longer changes the accepted grammar.
    """
    if not isinstance(reference, str) or not reference.strip():
        raise PathContractError(f"reference must be a nonempty path string: {reference!r}")
    cleaned = _reject_absolute(reference)
    parts: list[str] = []
    for part in cleaned.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise PathContractError(f"reference must not traverse outside its package root: {reference!r}")
        parts.append(part)
    if not parts:
        raise PathContractError(f"reference resolves to the package root itself: {reference!r}")
    return "/".join(parts)


def resolve_under(root: Path, reference: str, *, legacy_windows: bool = False) -> Path:
    """Resolve ``reference`` under ``root`` and prove containment after symlinks.

    Rejects absolute paths, drive/UNC prefixes, ``..`` traversal and symlink
    escapes; a path that does not exist is *not* rejected here so callers can
    report the missing artifact themselves.
    """
    root = Path(root)
    relative = normalize_reference(reference, legacy_windows=legacy_windows)
    base = root.resolve()
    candidate = (base / relative).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise PathContractError(f"reference escapes its package root: {reference!r}")
    return candidate


def require_within(root: Path, path: Path, *, label: str = "path") -> Path:
    """Prove an already-resolved path stays under ``root``."""
    base = Path(root).resolve()
    candidate = Path(path).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise PathContractError(f"{label} escapes its package root: {path}")
    return candidate


def iter_files(root: Path) -> Iterable[Path]:
    """Every file under ``root`` in deterministic order (read-only inventory)."""
    base = Path(root)
    if not base.is_dir():
        raise PathContractError(f"not a directory: {base}")
    return sorted(path for path in base.rglob("*") if path.is_file())
