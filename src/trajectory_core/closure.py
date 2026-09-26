"""Formal producer closure: a producer-protocol seal over a controlled family.

A *formal closure* is the versioned, offline-verifiable record of a controlled
experiment family: one packaged tree, one outcome document per node, one rerun
report per baseline/rerun pair, a producer receipt and a complete input
inventory.  The core is the contract authority, not a process supervisor: it
can prove that the record closes exactly over the bytes on disk, and it can
show what the producer attested, but it can never observe a game process or
prove by itself that no writer was active.  That epistemic split is explicit in
:meth:`FormalClosure.report`:

* ``content_integrity`` -- what this package re-derived from the bytes;
* ``producer_attested_closure`` -- what the receipt claims, and whether the
  receipt is synthetic;
* ``unverifiable_runtime_claims`` -- claims core cannot independently prove.

A receipt is always a *separate file supplied by the caller*.  There is no
boolean switch that fabricates one, and a synthetic receipt must say so: a
closure bound to a synthetic receipt loads (it is a demo fixture), but its
report never presents it as a producer's runtime proof.  Because a receipt that
only names tree identities cannot attest bytes that changed afterwards, a v2
receipt also binds the complete producer-owned artifact inventory (tree/index/
node evidence, outcome files and rerun reports) by normalized path and digest,
excluding the receipt and the closure file themselves.  Missing, extra or
reused scope entries fail closed.  Old, narrower receipts are rejected as
unknown schemas; they are never silently upgraded.

The v2 closure is the *controlled-family v1 profile*
(:data:`CONTROLLED_FAMILY_PROFILE`): every tree node carries exactly one
outcome and belongs to exactly one baseline/rerun pair in exactly one role.
That is a deliberate versioned restriction of this narrow experiment family,
not a limit of the generic tree/trajectory readers: larger or unpaired trees
remain fully usable through ``load_tree`` / ``validate_tree`` and the other
tree APIs.  Expanding the profile (single-arm closures, multi-pair designs) is
a new profile decision, not a change to the generic core.

A loaded :class:`FormalClosure` keeps an isolated snapshot of the validated
record and receipt.  ``report()`` never re-reads the package: mutating files on
disk or the dictionary returned by ``record`` after a successful load cannot
turn a synthetic or invalid claim into a trusted one, and the report stays the
snapshot that was actually verified (or the load fails).

* ``lvz.issue99-shovel-fork-seal.v1`` records stay readable through
  :func:`trajectory_core.verify_seal` only; loading one as a formal closure is
  an ``UnsupportedSchemaError`` and formal status is never inferred from it.

The writer and the loader validate with the same function; the writer
additionally refuses an existing destination, detects input changes before and
during the write, and only removes an output this call itself created (proved
by the exclusive acquisition and the file identity it captured, never by byte
equality with a competing writer's file).
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ._paths import normalize_reference, require_within, resolve_under
from .errors import ClosureError, IncompleteEvidenceError, PathContractError, UnsupportedSchemaError
from .jsonio import canonical, digest_sha256, file_sha256, read_json
from .outcome import Outcome, read_outcome
from .tree import load_tree

FORMAL_CLOSURE_SCHEMA = "trajectory-core.formal-closure.v2"
PRODUCER_RECEIPT_SCHEMA = "trajectory-core.producer-closure-receipt.v2"
RERUN_REPORT_SCHEMA = "trajectory-core.rerun-report.v1"
CLOSURE_REPORT_SCHEMA = "trajectory-core.closure-report.v1"
CONTROLLED_FAMILY_PROFILE = "trajectory-core.controlled-family.v1"
CLOSURE_FILE = "closure.json"
SUPPORTED_ATTESTATIONS = frozenset({"no_active_writers"})
SUPPORTED_VERDICTS = frozenset({"equal", "different", "unverified"})

__all__ = [
    "FORMAL_CLOSURE_SCHEMA",
    "PRODUCER_RECEIPT_SCHEMA",
    "RERUN_REPORT_SCHEMA",
    "CLOSURE_REPORT_SCHEMA",
    "CONTROLLED_FAMILY_PROFILE",
    "SUPPORTED_ATTESTATIONS",
    "SUPPORTED_VERDICTS",
    "FormalClosure",
    "validate_closure_record",
    "read_producer_receipt",
    "read_rerun_report",
    "seal_formal_closure",
    "load_formal_closure",
    "validate_formal_closure",
]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_CLOSURE_KEYS = {"schema", "profile", "closure_id", "tree", "outcomes", "pairs", "producer_receipt", "inputs"}
_TREE_KEYS = {"path", "tree_id", "nodes"}
_NODE_BINDING_KEYS = {"key", "branch_id", "trunk", "parent_key", "trajectory_id", "manifest_sha256"}
_OUTCOME_KEYS = {"key", "trajectory_id", "path", "sha256", "outcome_id"}
_PAIR_KEYS = {"id", "baseline", "rerun", "report"}
_REFERENCE_KEYS = {"path", "sha256"}
_INPUT_KEYS = {"path", "sha256"}
_RECEIPT_KEYS = {"schema", "producer", "synthetic", "attestation", "statement", "method", "scope"}
_PRODUCER_KEYS = {"name", "version"}
_RECEIPT_SCOPE_KEYS = {"tree_id", "nodes", "artifacts"}
_SCOPE_NODE_KEYS = {"key", "trajectory_id", "manifest_sha256"}
_SCOPE_ARTIFACT_KEYS = {"path", "sha256"}
_RERUN_REQUIRED_KEYS = {"schema", "baseline", "rerun", "verdict", "synthetic"}
_RERUN_OPTIONAL_KEYS = {"detail"}
_RERUN_IDENTITY_KEYS = {"key", "trajectory_id"}


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ClosureError(f"{label} must be a lowercase SHA-256 value")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClosureError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ClosureError(f"{label} must be a list")
    return value


def _exact(value: dict[str, Any], label: str, required: set[str], *, optional: set[str] | None = None) -> None:
    optional = optional or set()
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise ClosureError(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise ClosureError(f"{label} is missing required fields: {', '.join(missing)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClosureError(f"{label} must be a nonempty string")
    return value


def _resolve_file(root: Path, reference: Any, label: str) -> Path:
    text = _text(reference, f"{label} path")
    resolved = resolve_under(root, text)
    if not resolved.is_file():
        raise IncompleteEvidenceError(f"{label} is missing: {text}")
    return resolved


def _inventory(root: Path, *, exclude: Path | None = None) -> list[dict[str, str]]:
    """The complete, symlink-free file inventory of one closure package.

    Every file under ``root`` appears exactly once; a symlink anywhere (file or
    directory) is a ``PathContractError``, and so is the same resolved path
    reached twice.  This is the read-only inventory a formal closure promises:
    missing, extra and aliased files all fail closed.
    """
    base = Path(root).resolve()
    excluded = exclude.resolve() if exclude is not None else None
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(directory)
        for name in sorted(dirnames):
            candidate = here / name
            if candidate.is_symlink():
                raise PathContractError(f"a formal closure package must not contain symlinks: {candidate}")
        for name in sorted(filenames):
            candidate = here / name
            if candidate.is_symlink():
                raise PathContractError(f"a formal closure package must not contain symlinks: {candidate}")
            resolved = candidate.resolve()
            if excluded is not None and resolved == excluded:
                continue
            if resolved != base and not resolved.is_relative_to(base):
                raise PathContractError(f"an inventoried file escapes its package root: {candidate}")
            relative = resolved.relative_to(base).as_posix()
            if relative in seen:
                raise PathContractError(f"the same resolved file appears twice in the package inventory: {relative}")
            seen.add(relative)
            entries.append({"path": relative, "sha256": file_sha256(resolved)})
    entries.sort(key=lambda entry: entry["path"])
    return entries


def read_producer_receipt(path: str | Path) -> dict[str, Any]:
    """Read and strictly validate one producer closure receipt.

    The receipt is the producer's declaration that its writers stopped, which
    identities that covers and which producer-owned artifacts it closes.  A v2
    receipt must bind every producer-owned artifact (the complete package
    inventory except the receipt itself) by normalized path and digest; the
    closure validator proves that equality against the package.  ``synthetic``
    is required and must be an explicit boolean: there is no default, so a
    fixture can never quietly pass as a live producer's proof.
    """
    receipt_path = Path(path)
    if not receipt_path.is_file():
        raise IncompleteEvidenceError(f"producer receipt is missing: {receipt_path}")
    record = read_json(receipt_path)
    if not isinstance(record, dict):
        raise ClosureError("a producer receipt must be an object")
    if record.get("schema") != PRODUCER_RECEIPT_SCHEMA:
        raise UnsupportedSchemaError(
            f"unsupported producer receipt schema {record.get('schema')!r}; defined: {PRODUCER_RECEIPT_SCHEMA!r}"
        )
    _exact(record, "producer receipt", _RECEIPT_KEYS)
    producer = _object(record["producer"], "producer receipt producer")
    _exact(producer, "producer receipt producer", _PRODUCER_KEYS)
    _text(producer["name"], "producer name")
    _text(producer["version"], "producer version")
    if type(record["synthetic"]) is not bool:
        raise ClosureError("producer receipt synthetic must be an explicit boolean")
    if record["attestation"] not in SUPPORTED_ATTESTATIONS:
        raise ClosureError(
            f"unsupported producer attestation {record['attestation']!r}; supported: {sorted(SUPPORTED_ATTESTATIONS)}"
        )
    _text(record["statement"], "producer receipt statement")
    _text(record["method"], "producer receipt method")
    scope = _object(record["scope"], "producer receipt scope")
    _exact(scope, "producer receipt scope", _RECEIPT_SCOPE_KEYS)
    _sha(scope["tree_id"], "producer receipt scope tree_id")
    seen_nodes: set[str] = set()
    for entry in _list(scope["nodes"], "producer receipt scope nodes"):
        binding = _object(entry, "producer receipt scope node")
        _exact(binding, "producer receipt scope node", _SCOPE_NODE_KEYS)
        key = _text(binding["key"], "producer receipt scope node key")
        if key in seen_nodes:
            raise ClosureError(f"producer receipt scope binds node {key!r} more than once")
        seen_nodes.add(key)
        _sha(binding["trajectory_id"], f"producer receipt scope node {key!r} trajectory_id")
        _sha(binding["manifest_sha256"], f"producer receipt scope node {key!r} manifest_sha256")
    artifacts: list[dict[str, str]] = []
    seen_artifacts: set[str] = set()
    for entry in _list(scope["artifacts"], "producer receipt scope artifacts"):
        binding = _object(entry, "producer receipt scope artifact")
        _exact(binding, "producer receipt scope artifact", _SCOPE_ARTIFACT_KEYS)
        reference = normalize_reference(_text(binding["path"], "producer receipt scope artifact path"))
        if reference in seen_artifacts:
            raise ClosureError(f"producer receipt scope binds artifact {reference!r} more than once")
        seen_artifacts.add(reference)
        artifacts.append(
            {
                "path": reference,
                "sha256": _sha(binding["sha256"], f"producer receipt scope artifact {reference!r} sha256"),
            }
        )
    return {
        "schema": PRODUCER_RECEIPT_SCHEMA,
        "producer": {"name": producer["name"], "version": producer["version"]},
        "synthetic": record["synthetic"],
        "attestation": record["attestation"],
        "statement": record["statement"],
        "method": record["method"],
        "scope": {"tree_id": scope["tree_id"], "nodes": list(scope["nodes"]), "artifacts": artifacts},
    }


def read_rerun_report(path: str | Path) -> dict[str, Any]:
    """Read and strictly validate one rerun report.

    The report carries the two identities it compares.  The closure loader
    cross-checks them against the bound tree nodes; a report that names a
    different trajectory or key is a conflicting identity and fails closed.
    """
    report_path = Path(path)
    if not report_path.is_file():
        raise IncompleteEvidenceError(f"rerun report is missing: {report_path}")
    record = read_json(report_path)
    if not isinstance(record, dict):
        raise ClosureError("a rerun report must be an object")
    if record.get("schema") != RERUN_REPORT_SCHEMA:
        raise UnsupportedSchemaError(
            f"unsupported rerun report schema {record.get('schema')!r}; defined: {RERUN_REPORT_SCHEMA!r}"
        )
    _exact(record, "rerun report", _RERUN_REQUIRED_KEYS, optional=_RERUN_OPTIONAL_KEYS)
    for role in ("baseline", "rerun"):
        identity = _object(record[role], f"rerun report {role}")
        _exact(identity, f"rerun report {role}", _RERUN_IDENTITY_KEYS)
        _text(identity["key"], f"rerun report {role} key")
        _sha(identity["trajectory_id"], f"rerun report {role} trajectory_id")
    if record["verdict"] not in SUPPORTED_VERDICTS:
        raise ClosureError(f"unsupported rerun verdict {record['verdict']!r}; supported: {sorted(SUPPORTED_VERDICTS)}")
    if type(record["synthetic"]) is not bool:
        raise ClosureError("rerun report synthetic must be an explicit boolean")
    detail = record.get("detail", {})
    if not isinstance(detail, dict):
        raise ClosureError("rerun report detail must be an object")
    return {
        "schema": RERUN_REPORT_SCHEMA,
        "baseline": dict(record["baseline"]),
        "rerun": dict(record["rerun"]),
        "verdict": record["verdict"],
        "synthetic": record["synthetic"],
        "detail": detail,
    }


def _actual_nodes(tree: Any) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for node in tree.nodes:
        key = str(node["key"])
        if key in nodes:
            raise ClosureError(f"the sealed tree indexes node {key!r} more than once")
        nodes[key] = {
            "key": key,
            "branch_id": node["branch_id"],
            "trunk": node["trunk"],
            "parent_key": node["parent_key"],
            "trajectory_id": node["trajectory_id"],
            "manifest_sha256": file_sha256(tree.node_manifest(node)),
        }
    return nodes


def _check_receipt_scope(receipt: Mapping[str, Any], tree_id: str, nodes: Mapping[str, Mapping[str, Any]]) -> None:
    scope = receipt["scope"]
    if scope["tree_id"] != tree_id:
        raise ClosureError(
            f"producer receipt scope tree_id {scope['tree_id']!r} conflicts with the bound tree {tree_id!r}"
        )
    declared = {str(entry["key"]): entry for entry in scope["nodes"]}
    extra = sorted(set(declared) - set(nodes))
    missing = sorted(set(nodes) - set(declared))
    if extra or missing:
        raise ClosureError(
            "producer receipt scope does not match the bound tree nodes "
            f"(missing: {missing or 'none'}, extra: {extra or 'none'})"
        )
    for key, node in nodes.items():
        entry = declared[key]
        for field in ("trajectory_id", "manifest_sha256"):
            if entry[field] != node[field]:
                raise ClosureError(f"producer receipt scope node {key!r} {field} conflicts with the bound node")


def _check_receipt_artifacts(
    receipt: Mapping[str, Any], receipt_relative: str, actual_inputs: list[dict[str, str]]
) -> None:
    """Prove the receipt's artifact scope is exactly the producer-owned inventory.

    The producer-owned inventory is every inventoried package file except the
    receipt itself (and the closure file, which the inventory already
    excludes), so the receipt and closure can never be part of their own
    scope.  Both path and digest must match; a missing, extra, reused or
    changed entry fails.
    """
    producer_artifacts = sorted(
        (item for item in actual_inputs if item["path"] != receipt_relative), key=lambda item: item["path"]
    )
    declared_artifacts = sorted(receipt["scope"]["artifacts"], key=lambda item: item["path"])
    if declared_artifacts == producer_artifacts:
        return
    declared = {item["path"]: item["sha256"] for item in declared_artifacts}
    present = {item["path"]: item["sha256"] for item in producer_artifacts}
    missing = sorted(set(present) - set(declared))
    extra = sorted(set(declared) - set(present))
    changed = sorted(path for path in set(declared) & set(present) if declared[path] != present[path])
    raise ClosureError(
        "producer receipt scope artifacts do not match the producer-owned package contents "
        f"(uncovered: {missing or 'none'}, out of scope: {extra or 'none'}, changed: {changed or 'none'})"
    )


def _validate_closure_record(
    record: Any, root: str | Path, *, closure_file: str | Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one closure record and return it with its validated receipt.

    The public :func:`validate_closure_record` returns only the record; the
    loader additionally keeps the isolated, already validated receipt snapshot
    so :meth:`FormalClosure.report` never has to re-read package bytes.
    """
    package_root = Path(root)
    if not package_root.is_dir():
        raise ClosureError(f"closure package root is not a directory: {package_root}")
    package_root = package_root.resolve()
    if not isinstance(record, dict):
        raise ClosureError("a formal closure record must be an object")
    if record.get("schema") != FORMAL_CLOSURE_SCHEMA:
        raise UnsupportedSchemaError(
            f"unsupported closure schema {record.get('schema')!r}; defined: {FORMAL_CLOSURE_SCHEMA!r}"
        )
    _exact(record, "closure record", _CLOSURE_KEYS)
    if record["profile"] != CONTROLLED_FAMILY_PROFILE:
        raise UnsupportedSchemaError(
            f"unsupported closure profile {record['profile']!r}; defined: {CONTROLLED_FAMILY_PROFILE!r}"
        )
    declared_id = _sha(record["closure_id"], "closure_id")
    if digest_sha256({key: value for key, value in record.items() if key != "closure_id"}) != declared_id:
        raise ClosureError("closure_id does not match the canonical record content")

    tree_reference = _object(record["tree"], "closure tree")
    _exact(tree_reference, "closure tree", _TREE_KEYS)
    tree_directory = resolve_under(package_root, _text(tree_reference["path"], "closure tree path"))
    if not tree_directory.is_dir():
        raise IncompleteEvidenceError(f"sealed tree directory is missing: {tree_reference['path']!r}")
    tree = load_tree(tree_directory)
    if tree_reference["tree_id"] != tree.tree_id:
        raise ClosureError(
            f"closure tree.tree_id {tree_reference['tree_id']!r} conflicts with the loaded tree {tree.tree_id!r}"
        )
    actual_nodes = _actual_nodes(tree)
    declared_nodes: dict[str, dict[str, Any]] = {}
    for entry in _list(tree_reference["nodes"], "closure tree nodes"):
        binding = _object(entry, "closure tree node")
        _exact(binding, "closure tree node", _NODE_BINDING_KEYS)
        key = _text(binding["key"], "closure tree node key")
        if key in declared_nodes:
            raise ClosureError(f"closure tree binds node {key!r} more than once")
        declared_nodes[key] = binding
    missing = sorted(set(actual_nodes) - set(declared_nodes))
    extra = sorted(set(declared_nodes) - set(actual_nodes))
    if missing or extra:
        raise ClosureError(
            "closure tree bindings do not match the sealed tree "
            f"(missing: {missing or 'none'}, extra: {extra or 'none'})"
        )
    for key, actual in actual_nodes.items():
        binding = declared_nodes[key]
        conflicts = [name for name in _NODE_BINDING_KEYS if binding[name] != actual[name]]
        if conflicts:
            raise ClosureError(
                f"closure tree node {key!r} conflicts with the sealed tree on: {', '.join(sorted(conflicts))}"
            )

    receipt_reference = _object(record["producer_receipt"], "closure producer_receipt")
    _exact(receipt_reference, "closure producer_receipt", _REFERENCE_KEYS)
    receipt_path = _resolve_file(package_root, receipt_reference["path"], "producer receipt")
    if file_sha256(receipt_path) != _sha(receipt_reference["sha256"], "producer receipt sha256"):
        raise ClosureError("producer receipt digest does not match the sealed bytes")
    receipt = read_producer_receipt(receipt_path)
    receipt_relative = receipt_path.relative_to(package_root).as_posix()
    _check_receipt_scope(receipt, tree.tree_id, actual_nodes)

    outcome_entries: list[dict[str, Any]] = []
    covered_outcomes: set[str] = set()
    for entry in _list(record["outcomes"], "closure outcomes"):
        binding = _object(entry, "closure outcome")
        _exact(binding, "closure outcome", _OUTCOME_KEYS)
        key = _text(binding["key"], "closure outcome key")
        if key not in actual_nodes:
            raise ClosureError(f"closure outcome binds a node the tree does not have: {key!r}")
        if key in covered_outcomes:
            raise ClosureError(f"closure outcome binds node {key!r} more than once")
        if binding["trajectory_id"] != actual_nodes[key]["trajectory_id"]:
            raise ClosureError(f"closure outcome {key!r} trajectory_id conflicts with the bound tree node")
        outcome_path = _resolve_file(package_root, binding["path"], f"outcome document for {key}")
        if file_sha256(outcome_path) != _sha(binding["sha256"], f"outcome document {key!r} sha256"):
            raise ClosureError(f"outcome document {key!r} digest does not match the sealed bytes")
        outcome: Outcome = read_outcome(outcome_path)
        if outcome.outcome_id != _sha(binding["outcome_id"], f"outcome document {key!r} outcome_id"):
            raise ClosureError(f"outcome document {key!r} outcome_id conflicts with its sealed content")
        covered_outcomes.add(key)
        outcome_entries.append(binding)
    missing = sorted(set(actual_nodes) - covered_outcomes)
    if missing:
        raise ClosureError(f"closure outcomes do not cover every tree node; missing: {missing}")

    pair_entries: list[dict[str, Any]] = []
    covered_nodes: dict[str, str] = {}
    pair_ids: set[str] = set()
    for entry in _list(record["pairs"], "closure pairs"):
        pair = _object(entry, "closure pair")
        _exact(pair, "closure pair", _PAIR_KEYS)
        pair_id = _text(pair["id"], "closure pair id")
        if pair_id in pair_ids:
            raise ClosureError(f"closure pair id {pair_id!r} is declared more than once")
        pair_ids.add(pair_id)
        baseline = _text(pair["baseline"], f"closure pair {pair_id} baseline")
        rerun = _text(pair["rerun"], f"closure pair {pair_id} rerun")
        for role, key in (("baseline", baseline), ("rerun", rerun)):
            if key not in actual_nodes:
                raise ClosureError(f"closure pair {pair_id!r} binds a node the tree does not have: {role}={key!r}")
            if key in covered_nodes:
                raise ClosureError(f"closure node {key!r} is bound by more than one pair or role")
            covered_nodes[key] = pair_id
        report_reference = _object(pair["report"], f"closure pair {pair_id} report")
        _exact(report_reference, f"closure pair {pair_id} report", _REFERENCE_KEYS)
        report_path = _resolve_file(package_root, report_reference["path"], f"rerun report for {pair_id}")
        if file_sha256(report_path) != _sha(report_reference["sha256"], f"rerun report {pair_id} sha256"):
            raise ClosureError(f"rerun report {pair_id!r} digest does not match the sealed bytes")
        report = read_rerun_report(report_path)
        for role, bound_key in (("baseline", baseline), ("rerun", rerun)):
            identity = report[role]
            node = actual_nodes[bound_key]
            if identity["key"] != bound_key or identity["trajectory_id"] != node["trajectory_id"]:
                raise ClosureError(
                    f"rerun report {pair_id!r} {role} identity conflicts with the bound node {bound_key!r}"
                )
        pair_entries.append(pair)
    missing = sorted(set(actual_nodes) - set(covered_nodes))
    if missing:
        raise ClosureError(f"closure pairs do not cover every tree node; missing: {missing}")

    inputs: list[dict[str, str]] = []
    seen_inputs: set[str] = set()
    for entry in _list(record["inputs"], "closure inputs"):
        binding = _object(entry, "closure input")
        _exact(binding, "closure input", _INPUT_KEYS)
        input_path = _resolve_file(package_root, binding["path"], "inventoried input")
        relative = input_path.relative_to(package_root).as_posix()
        if relative in seen_inputs:
            raise ClosureError(f"closure inventory binds {relative!r} more than once")
        seen_inputs.add(relative)
        if file_sha256(input_path) != _sha(binding["sha256"], f"inventoried input {relative!r} sha256"):
            raise ClosureError(f"inventoried input {relative!r} digest does not match the sealed bytes")
        inputs.append({"path": relative, "sha256": binding["sha256"]})
    inputs.sort(key=lambda item: item["path"])
    excluded: Path | None = None
    if closure_file is not None:
        candidate = Path(closure_file)
        excluded = (candidate if candidate.is_absolute() else package_root / candidate).resolve()
    actual_inputs = _inventory(package_root, exclude=excluded)
    if inputs != actual_inputs:
        recorded = {item["path"] for item in inputs}
        present = {item["path"] for item in actual_inputs}
        missing = sorted(present - recorded)
        extra = sorted(recorded - present)
        raise ClosureError(
            "closure inventory does not match the package contents "
            f"(unrecorded files: {missing or 'none'}, unreadable records: {extra or 'none'}, or a digest changed)"
        )
    _check_receipt_artifacts(receipt, receipt_relative, actual_inputs)

    return record, receipt


def validate_closure_record(record: Any, root: str | Path, *, closure_file: str | Path | None = None) -> dict[str, Any]:
    """The single validation contract used by both the loader and the writer.

    Re-derives the tree, every node digest, every outcome identity, every rerun
    report identity, the producer receipt tree/node scope and complete
    producer-owned artifact inventory, and the complete input inventory.
    Missing, duplicated, omitted or extra references, bad digests, unknown
    schemas and conflicting identities all raise ``ClosureError`` (or a more
    specific subclass).
    """
    validated, _receipt = _validate_closure_record(record, root, closure_file=closure_file)
    return validated


class FormalClosure:
    """A validated formal closure record bound to its package directory.

    Construction is internal to loading/sealing: the instance keeps isolated
    copies of the validated record and receipt, so neither a later file change
    nor mutation of the public ``record`` snapshot can alter a reported
    decision.
    """

    def __init__(
        self,
        directory: Path,
        path: Path,
        record: dict[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        self._directory = directory
        self._path = path
        self._record = copy.deepcopy(record)
        self._receipt: dict[str, Any] | None = copy.deepcopy(dict(receipt)) if receipt is not None else None

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record(self) -> dict[str, Any]:
        """An isolated copy of the validated record; mutating it is harmless."""
        return copy.deepcopy(self._record)

    @property
    def closure_id(self) -> str:
        return str(self._record["closure_id"])

    @property
    def tree_id(self) -> str:
        return str(self._record["tree"]["tree_id"])

    @property
    def profile(self) -> str:
        return str(self._record["profile"])

    @property
    def counts(self) -> dict[str, int]:
        counts = {
            "nodes": len(self._record["tree"]["nodes"]),
            "outcomes": len(self._record["outcomes"]),
            "pairs": len(self._record["pairs"]),
            "inputs": len(self._record["inputs"]),
        }
        if self._receipt is not None:
            counts["producer_artifacts"] = len(self._receipt["scope"]["artifacts"])
        return counts

    def report(self) -> dict[str, Any]:
        """The validated snapshot report, with the producer claim kept separate.

        Nothing here re-reads the package: the receipt and the record were
        validated when this instance was loaded, and both are held as isolated
        copies.  A file changed after loading therefore never changes this
        report, and a synthetic receipt is never promoted to a trusted claim.
        """
        if self._receipt is None:
            raise ClosureError(
                "this formal closure was not built from a validated load; call load_formal_closure or "
                "seal_formal_closure instead of constructing FormalClosure directly"
            )
        receipt = self._receipt
        counts = self.counts
        accepted = receipt["synthetic"] is False
        return {
            "schema": CLOSURE_REPORT_SCHEMA,
            "path": str(self._path),
            "profile": self.profile,
            "closure_id": self.closure_id,
            "status": "valid",
            "tree_id": self.tree_id,
            "content_integrity": {
                "status": "verified",
                "scope": (
                    "re-derived tree identity and chains, every node manifest digest, every outcome identity, "
                    "every rerun report binding, the producer receipt tree/node scope and complete "
                    "producer-owned artifact path/digest scope, and the complete input inventory"
                ),
                "runtime_facts_verified_by_core": False,
            },
            "producer_attested_closure": {
                "attested": accepted,
                "synthetic": receipt["synthetic"],
                "attestation": receipt["attestation"],
                "producer": dict(receipt["producer"]),
                "statement": receipt["statement"],
                "method": receipt["method"],
                "scope": {
                    "tree_id": self.tree_id,
                    "nodes": counts["nodes"],
                    "producer_artifacts": len(receipt["scope"]["artifacts"]),
                },
                "core_checks": (
                    "receipt schema; tree/node scope equality with the bound tree nodes; artifact path/digest "
                    "equality with the complete producer-owned package inventory; the attesting runtime fact "
                    "itself is not verifiable offline"
                ),
            },
            "unverifiable_runtime_claims": [
                receipt["statement"],
                "trajectory-core cannot observe a game, a writer process or the absence of an active writer",
            ],
            "counts": counts,
            "problems": [],
        }


def _closure_file(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate / CLOSURE_FILE if candidate.is_dir() else candidate


def load_formal_closure(path: str | Path, *, root: str | Path | None = None) -> FormalClosure:
    """Read and fully validate one formal closure record (directory or file).

    The closure file itself is bounded before ``read_json``: an explicit root
    must contain it (after symlink resolution), and a symlinked closure record
    is rejected even when the link stays inside the package, so no byte of a
    record outside the declared package is ever opened.
    """
    closure_path = _closure_file(path)
    package_root = Path(root) if root is not None else closure_path.parent
    if not package_root.is_dir():
        raise ClosureError(f"closure package root is not a directory: {package_root}")
    package_root = package_root.resolve()
    require_within(package_root, closure_path, label="formal closure record")
    if closure_path.is_symlink():
        raise PathContractError(f"a formal closure record must not be a symlink: {closure_path}")
    if not closure_path.is_file():
        raise IncompleteEvidenceError(f"formal closure record is missing: {closure_path}")
    record = read_json(closure_path)
    validated, receipt = _validate_closure_record(record, package_root, closure_file=closure_path)
    return FormalClosure(package_root, closure_path, validated, receipt=receipt)


def validate_formal_closure(path: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Validate a formal closure and return the machine-readable report."""
    return load_formal_closure(path, root=root).report()


def _write_record(destination: Path, payload: bytes) -> tuple[int, int]:
    """Acquire the destination exclusively and write ``payload``.

    Returns the device/inode identity of the file this call created.  A
    ``FileExistsError`` means a competing writer owns the destination and
    nothing is removed.  A failure after acquisition removes only the file
    whose identity matches the one this call captured.
    """
    try:
        stream = destination.open("xb")
    except FileExistsError:
        raise ClosureError(f"formal closure destination already exists: {destination}") from None
    identity: tuple[int, int] | None = None
    try:
        with stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            stream.write(payload)
            stream.flush()
    except BaseException:
        _remove_owned_output(destination, identity)
        raise
    return identity


def _remove_owned_output(destination: Path, identity: tuple[int, int] | None) -> None:
    """Remove a failed output only while it is still the file this call created.

    ``identity`` is captured from this call's exclusive acquisition; a path
    that no longer resolves to that file belongs to a competing writer and is
    left untouched.  Byte equality is never used as ownership proof.
    """
    if identity is None:
        return
    try:
        current = os.stat(destination)
        if (current.st_dev, current.st_ino) != identity:
            return
        destination.unlink()
    except OSError:
        pass


def _build_outcome_entry(root: Path, node: Mapping[str, Any], reference: str) -> dict[str, Any]:
    outcome_path = _resolve_file(root, reference, f"outcome document for {node['key']}")
    outcome = read_outcome(outcome_path)
    return {
        "key": node["key"],
        "trajectory_id": node["trajectory_id"],
        "path": reference,
        "sha256": file_sha256(outcome_path),
        "outcome_id": outcome.outcome_id,
    }


def _build_pair_entry(root: Path, pair: Mapping[str, Any]) -> dict[str, Any]:
    pair_id = _text(pair.get("id"), "pair id")
    baseline = _text(pair.get("baseline"), f"pair {pair_id!r} baseline")
    rerun = _text(pair.get("rerun"), f"pair {pair_id!r} rerun")
    report_reference = _text(pair.get("report"), f"pair {pair_id!r} report")
    report_path = _resolve_file(root, report_reference, f"rerun report for {pair_id}")
    read_rerun_report(report_path)
    return {
        "id": pair_id,
        "baseline": baseline,
        "rerun": rerun,
        "report": {"path": report_reference, "sha256": file_sha256(report_path)},
    }


def seal_formal_closure(
    package_root: str | Path,
    destination: str | Path,
    *,
    tree: str,
    outcomes: Mapping[str, str],
    pairs: Iterable[Mapping[str, Any]],
    receipt: str,
) -> FormalClosure:
    """Write a formal closure over an existing, unchanged package.

    Every referenced artifact must already exist and the producer receipt must
    already bind the complete producer-owned artifact inventory; the record is
    built from the bytes on disk, validated with the same contract the loader
    uses, and then written with an exclusive create.  An existing destination is
    never overwritten, a competing writer's file is never removed (byte
    equality is not ownership), and input changes before or during the write
    abort the seal.
    """
    root = Path(package_root)
    if not root.is_dir():
        raise ClosureError(f"closure package root is not a directory: {root}")
    root = root.resolve()
    destination_path = Path(destination).resolve()
    require_within(root, destination_path, label="formal closure destination")
    if destination_path.exists():
        raise ClosureError(f"formal closure destination already exists: {destination_path}")

    tree_reference = normalize_reference(str(tree))
    receipt_reference = normalize_reference(str(receipt))
    outcome_references = {str(key): normalize_reference(str(value)) for key, value in outcomes.items()}

    tree_directory = resolve_under(root, tree_reference)
    if not tree_directory.is_dir():
        raise IncompleteEvidenceError(f"sealed tree directory is missing: {tree_reference!r}")
    tree_view = load_tree(tree_directory)
    nodes = _actual_nodes(tree_view)
    unknown = sorted(set(outcome_references) - set(nodes))
    if unknown:
        raise ClosureError(f"outcome bindings name nodes the tree does not have: {unknown}")
    missing = sorted(set(nodes) - set(outcome_references))
    if missing:
        raise ClosureError(f"outcome bindings do not cover every tree node; missing: {missing}")

    inputs = _inventory(root)
    record: dict[str, Any] = {
        "schema": FORMAL_CLOSURE_SCHEMA,
        "profile": CONTROLLED_FAMILY_PROFILE,
        "tree": {
            "path": tree_reference,
            "tree_id": tree_view.tree_id,
            "nodes": [dict(nodes[key]) for key in sorted(nodes)],
        },
        "outcomes": [_build_outcome_entry(root, nodes[key], outcome_references[key]) for key in sorted(nodes)],
        "pairs": [_build_pair_entry(root, pair) for pair in pairs],
        "producer_receipt": {
            "path": receipt_reference,
            "sha256": file_sha256(_resolve_file(root, receipt_reference, "producer receipt")),
        },
        "inputs": inputs,
    }
    record["closure_id"] = digest_sha256(record)
    validated, receipt_snapshot = _validate_closure_record(record, root, closure_file=destination_path)

    payload = canonical(validated) + b"\n"
    identity: tuple[int, int] | None = None
    try:
        identity = _write_record(destination_path, payload)
        if _inventory(root, exclude=destination_path) != inputs:
            raise ClosureError("package inputs changed while the formal closure was being written")
    except BaseException:
        _remove_owned_output(destination_path, identity)
        raise
    return FormalClosure(root, destination_path, validated, receipt=receipt_snapshot)
