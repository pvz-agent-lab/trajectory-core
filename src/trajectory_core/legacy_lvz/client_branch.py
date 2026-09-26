"""Pure branch-scope grammar shared with the legacy tree ``branch_id``.

Extracted from ``src/llm_vs_zombies/client.py`` at commit 11917a7 (see
``NOTICES.md``): the old checker raised ``ProtocolError`` while validating a
hello record, and that class lives in the socket client. This module keeps the
same grammar and the same accepted fields without importing the runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..errors import EvidenceError

BRANCH_SCHEMA = "lvz.branch-scope.v1"
_BRANCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


class BranchScopeError(EvidenceError):
    """A branch scope declaration does not satisfy the legacy grammar."""


def branch_scope_id(value: Any) -> str:
    """The branch-scope grammar, shared with the evidence tree's branch_id."""
    if not isinstance(value, str) or _BRANCH_ID.fullmatch(value) is None:
        raise ValueError("branch_id must be 1-64 characters of [A-Za-z0-9._:-] starting alphanumeric")
    return value


def declared_branch_scope(hello: Mapping[str, Any]) -> dict[str, Any] | None:
    """The runtime's own branch scope, or ``None`` for a pre-branch runtime.

    A runtime instance owns exactly one scope. Its dedup namespace is
    ``(branch_id, epoch, request_id)``; requests carry the scope id so a cloned
    or rebound process can never answer for a branch it did not execute.
    """
    value = hello.get("branch")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema") != BRANCH_SCHEMA:
        raise BranchScopeError("hello branch scope is missing or malformed")
    mode = value.get("mode")
    if mode not in ("branch", "unscoped"):
        raise BranchScopeError("hello branch scope mode is invalid")
    declared = value.get("branch_id")
    if mode == "branch":
        if not isinstance(declared, str):
            raise BranchScopeError("a branch-scoped runtime must declare branch_id")
        try:
            declared = branch_scope_id(declared)
        except ValueError as error:
            raise BranchScopeError(f"hello branch_id is invalid: {error}") from error
    elif declared is not None:
        raise BranchScopeError("an unscoped runtime must not declare branch_id")
    parent = value.get("parent_branch_id")
    if parent is not None:
        try:
            parent = branch_scope_id(parent)
        except ValueError as error:
            raise BranchScopeError(f"hello parent branch is invalid: {error}") from error
    origin = value.get("origin")
    if origin is not None and not isinstance(origin, str):
        raise BranchScopeError("hello branch origin is invalid")
    dedup_key = value.get("dedup_key")
    if dedup_key is not None and not isinstance(dedup_key, str):
        raise BranchScopeError("hello dedup key is invalid")
    return {
        "schema": BRANCH_SCHEMA,
        "mode": mode,
        "branch_id": declared,
        "parent_branch_id": parent,
        "origin": origin,
        "dedup_key": dedup_key,
    }
