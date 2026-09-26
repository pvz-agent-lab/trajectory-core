"""Read, validate and summarize one sealed ``lvz.engine-replay.v1`` trajectory.

The public object is a thin, typed facade over the legacy reader closure.  A
successful ``load_trajectory`` means the reader re-derived the manifest
identity, every evidence file digest, the closed native audit stream and the
session trace, and cross-checked every recorded step.  ``summary()`` is a
projection of that verified object; it is never used to skip the verification.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from ._paths import require_within, resolve_under
from .errors import EvidenceError, IncompleteEvidenceError, UnsupportedSchemaError
from .identity import MANIFEST_FILE, TRAJECTORY_SCHEMA, end_boundary
from .jsonio import read_json
from .legacy_lvz import trajectory as _legacy_trajectory
from .legacy_lvz.evidence_tree import TreePlacement

STEP_METHODS = frozenset({"commit", "advance"})
INTERVENTIONS = STEP_METHODS | {"capture_frame", "pause"}

__all__ = [
    "Trajectory",
    "TrajectorySummary",
    "StepSummary",
    "SummaryTermination",
    "load_trajectory",
    "validate_trajectory",
    "build_trajectory",
    "read_manifest",
]


@dataclass(frozen=True)
class StepSummary:
    """One recorded request, projected from the verified manifest."""

    index: int
    method: str
    request_id: str
    before: dict[str, int] | None
    after: dict[str, int] | None
    requested_ticks: int | None
    executed_ticks: int | None
    stop_reason: str | None
    actions: tuple[dict[str, Any], ...]
    action_results: int
    failed_action: dict[str, Any] | None
    observation: dict[str, Any] | None


@dataclass(frozen=True)
class SummaryTermination:
    """The final recorded outcome, without guessing a missing one."""

    final_stop_reason: str | None
    executed_ticks: int
    action_failures: int
    reached_scene_change: bool


@dataclass(frozen=True)
class TrajectorySummary:
    """The public schema of one trajectory: identity, placement, termination."""

    schema: str
    trajectory_id: str
    source: str
    steps: int
    initial_version: dict[str, int]
    end_boundary: dict[str, int]
    branch_id: str | None
    trunk: bool | None
    tree_id: str | None
    parent: dict[str, Any] | None
    root_trajectory_id: str | None
    root_identity: dict[str, Any] | None
    termination: SummaryTermination
    steps_detail: tuple[StepSummary, ...]
    evidence: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["termination"] = asdict(self.termination)
        value["steps_detail"] = [asdict(item) for item in self.steps_detail]
        return value


def _manifest_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate / MANIFEST_FILE if candidate.is_dir() else candidate


def read_manifest(path: str | Path) -> dict[str, Any]:
    """Read a manifest, prove it stays inside its bundle, and reject unknown schemas.

    A manifest is itself a file reference: a symlink that leaves the bundle
    directory is a containment failure, checked before the manifest bytes are
    opened and before any evidence it names is read.
    """
    manifest_path = _manifest_path(path)
    require_within(manifest_path.parent, manifest_path, label="trajectory manifest")
    if not manifest_path.is_file():
        raise IncompleteEvidenceError(f"trajectory manifest is missing: {manifest_path}")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise EvidenceError(f"trajectory manifest must be an object: {manifest_path}")
    schema = manifest.get("schema")
    if schema != TRAJECTORY_SCHEMA:
        raise UnsupportedSchemaError(
            f"unsupported trajectory schema {schema!r}; this package reads {TRAJECTORY_SCHEMA!r}"
        )
    return manifest


def _before(step: dict[str, Any]) -> dict[str, int] | None:
    expect = step["request"].get("expect")
    return dict(expect) if isinstance(expect, dict) else None


def _after(step: dict[str, Any]) -> dict[str, int] | None:
    if step["request"]["method"] == "capture_frame":
        value = step.get("after_version")
    else:
        value = step.get("result", {}).get("observation", {}).get("version")
    return dict(value) if isinstance(value, dict) else None


def _step_summaries(manifest: dict[str, Any]) -> tuple[StepSummary, ...]:
    summaries = []
    for index, step in enumerate(manifest.get("steps") or []):
        request = step.get("request") or {}
        result = step.get("result") or {}
        params = request.get("params") or {}
        raw_actions = params.get("actions")
        actions: list[Any] = raw_actions if isinstance(raw_actions, list) else []
        outcomes = result.get("action_results") or []
        failed = next((item for item in outcomes if isinstance(item, dict) and item.get("ok") is False), None)
        requested = result.get("requested_ticks")
        observation = result.get("observation")
        summaries.append(
            StepSummary(
                index=index,
                method=str(request.get("method")),
                request_id=str(request.get("request_id")),
                before=_before(step),
                after=_after(step),
                requested_ticks=requested if type(requested) is int else None,
                executed_ticks=result.get("executed_ticks") if type(result.get("executed_ticks")) is int else None,
                stop_reason=result.get("stop_reason") if isinstance(result.get("stop_reason"), str) else None,
                actions=tuple(copy.deepcopy(action) for action in actions if isinstance(action, dict)),
                action_results=len(outcomes) if isinstance(outcomes, list) else 0,
                failed_action=copy.deepcopy(failed) if failed is not None else None,
                observation=copy.deepcopy(observation) if isinstance(observation, dict) else None,
            )
        )
    return tuple(summaries)


class Trajectory:
    """A verified, read-only view of one sealed trajectory bundle."""

    def __init__(self, legacy: _legacy_trajectory.Trajectory) -> None:
        self._legacy = legacy

    @property
    def directory(self) -> Path:
        return self._legacy.directory

    @property
    def manifest(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._legacy.manifest)

    @property
    def initial(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._legacy.initial)

    @property
    def steps(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._legacy.steps)

    @property
    def audit(self) -> Any:
        """The verified legacy audit index (advanced callers only)."""
        return self._legacy.audit

    @property
    def trajectory_id(self) -> str:
        return str(self.manifest["trajectory_id"])

    @property
    def placement(self) -> dict[str, Any] | None:
        section = self.manifest.get("tree")
        return section if isinstance(section, dict) else None

    def summary(self) -> TrajectorySummary:
        manifest = self.manifest
        placement = self.placement or {}
        parent = placement.get("parent") if isinstance(placement.get("parent"), dict) else None
        root = placement.get("root") if isinstance(placement.get("root"), dict) else None
        details = _step_summaries(manifest)
        reasons = tuple(step.stop_reason for step in details)
        failures = tuple(step for step in details if step.failed_action is not None)
        termination = SummaryTermination(
            final_stop_reason=reasons[-1] if reasons else None,
            executed_ticks=sum(step.executed_ticks or 0 for step in details),
            action_failures=len(failures),
            reached_scene_change=any(reason == "scene_changed" for reason in reasons),
        )
        evidence = manifest.get("files")
        return TrajectorySummary(
            schema=str(manifest.get("schema")),
            trajectory_id=self.trajectory_id,
            source=str(manifest.get("source")),
            steps=len(details),
            initial_version=dict(manifest["initial"]["observation"]["version"]),
            end_boundary=dict(end_boundary(manifest)),
            branch_id=str(placement.get("branch_id")) if placement else None,
            trunk=bool(placement["trunk"]) if placement else None,
            tree_id=str(placement.get("tree_id")) if placement else None,
            parent=dict(parent) if parent else None,
            root_trajectory_id=str(root["trajectory_id"]) if root else None,
            root_identity=copy.deepcopy(root.get("identity")) if root else None,
            termination=termination,
            steps_detail=details,
            evidence=dict(evidence) if isinstance(evidence, dict) else {},
        )

    def verify(self) -> dict[str, Any]:
        """Project the machine-readable report of the already-verified bundle.

        Every public ``Trajectory`` comes from a full load that already
        re-derived the identity, evidence digests, audit and session trace, so
        this never starts a second expensive read of the same bytes.
        """
        return {
            "schema": "trajectory-core.trajectory-report.v1",
            "path": str(self.directory),
            "status": "valid",
            "trajectory_id": self.trajectory_id,
            "summary": self.summary().to_dict(),
            "scope": "manifest identity, every evidence digest, closed audit stream and session trace",
        }


def _bounded_bundle_references(path: str | Path, manifest: dict[str, Any]) -> None:
    """Prove every referenced bundle path stays inside the bundle before any read.

    The legacy reader hashes declared evidence through resolved containment
    checks, but it reads the audit manifest and codec receipt first.  Bounding
    the whole reference set (including the implicit ``audit`` directory) here
    keeps that first read inside the bundle as well.
    """
    root = _manifest_path(path).parent
    files = manifest.get("files")
    if isinstance(files, dict):
        for name in files:
            if isinstance(name, str):
                resolve_under(root, name)
    resolve_under(root, "audit")


def load_trajectory(path: str | Path) -> Trajectory:
    """Fully verify and load one sealed trajectory directory or manifest file."""
    manifest = read_manifest(path)
    _bounded_bundle_references(path, manifest)
    try:
        legacy = _legacy_trajectory.Trajectory.load(path)
    except UnsupportedSchemaError:
        raise
    except EvidenceError:
        raise
    return Trajectory(legacy)


def validate_trajectory(path: str | Path) -> dict[str, Any]:
    """Validate a trajectory and return the report instead of an object."""
    return load_trajectory(path).verify()


def build_trajectory(
    trace: str | Path | None,
    audit_directory: str | Path,
    output_directory: str | Path,
    *,
    initial: str | Path | None = None,
    tree: TreePlacement | None = None,
) -> Trajectory:
    """Package one already closed recording into a new sealed directory.

    Writers are honest about their guards: the source recording must already be
    closed (the old reader refuses a missing close), the destination must not
    exist, and the packaged result is fully re-verified before it is returned.
    """
    legacy = _legacy_trajectory.build_trajectory(trace, audit_directory, output_directory, initial=initial, tree=tree)
    return Trajectory(legacy)
