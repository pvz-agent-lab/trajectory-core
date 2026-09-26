"""The versioned public outcome contract (``trajectory-core.outcome.v2``).

The outcome contract separates facts that the old evaluation reports merged:

* the declared plan target, in its own unit (the game counts one ``round`` as
  two flags and twenty waves);
* the measured progress (completed rounds, maximum wave, final scene);
* whether one full cycle completed (the historical ``full_cycle`` gate);
* whether the *declared target* was reached;
* the run lifecycle status, the execution extent (not executed / partial /
  complete / unknown), the termination reason and the truncation reason;
* how well the document itself is verified.

Unknown facts stay ``null``; a missing count is never read as ``false`` and a
boolean is never accepted where a count belongs.  A complete cycle is **not**
evidence that the declared target was reached: the two are separate fields and
the validator rejects a document that contradicts itself.  The lifecycle
status is likewise never read as goal success: a completed invocation may
still report ``goal.reached = false``.  ``run.execution_extent`` is an
independent axis that distinguishes a failure before any execution from a
failure after partial execution and from a completed invocation; zero
completed rounds is a recorded count and does not prove that nothing ran, so
an unproven extent stays ``"unknown"``.  For legacy evidence the extent is
proven by the source stage only (the source play loop's
``full_cycle_completed`` outcome, the historical ``startup_failed`` marker,
or a completed-round delta together with a documented explicit
premature-stop/truncation outcome); the case/suite lifecycle status, an
absolute wave and a progress delta alone are not execution evidence.  The v2
contract is the first published shape; v1 drafts were never released and are
rejected as unknown schemas.

The legacy adapter (:func:`outcome_from_legacy`) interprets a pre-migration
``lvz.evaluation-plan.v1/v2`` document plus a case report without changing the
historical unit.  ``flags_to_complete`` is the pre-#97 field *name*; it always
counted rounds (two flags, twenty waves) and is never silently divided or
rescaled.  The adapter records its source, the input schema and the evidence
scope so an adapted document cannot be mistaken for a live result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import IncompleteEvidenceError, OutcomeContractError, UnsupportedSchemaError
from .jsonio import digest_sha256, read_json

OUTCOME_SCHEMA = "trajectory-core.outcome.v2"
OUTCOME_UNIT = "round"
SUPPORTED_UNITS = frozenset({OUTCOME_UNIT})
LEGACY_PLAN_SCHEMAS = frozenset({"lvz.evaluation-plan.v1", "lvz.evaluation-plan.v2"})
RUN_STATUSES = frozenset({"completed", "failed", "aborted", "unknown"})
# How much of the invocation actually executed; independent of goal success.
# ``not_executed`` and ``partial`` must stay distinguishable from a completed
# invocation, and an unproven extent must stay ``unknown``.
EXECUTION_EXTENTS = frozenset({"not_executed", "partial", "complete", "unknown"})
VERIFICATION_STATUSES = frozenset({"verified", "unverified", "failed", "unknown"})
PROVENANCE_KINDS = frozenset({"native", "legacy-adapted", "synthetic-demo"})
# The historical gate: one round is two flags and twenty waves.
CYCLE_WAVE_MINIMUM = 20
# Truncation reasons the legacy runner can stop a source run with.
TRUNCATION_REASONS = frozenset({"tick_budget_exhausted", "wall_budget_exhausted", "disk_reserve_stop"})
# Documented source play-loop outcomes that explicitly name a stop before the
# configured full-cycle end.  A positive completed-round delta proves only
# that execution started, so it is ``partial`` only together with one of
# these; any other outcome (including an unrecognized string) stays unknown.
INCOMPLETE_SOURCE_OUTCOMES = frozenset({"terminal_before_complete"}) | TRUNCATION_REASONS
LEGACY_RUN_STATUSES = {
    "completed": "completed",
    "failed": "failed",
    "startup_failed": "failed",
    "incomplete": "aborted",
}

_TOP_KEYS = {"schema", "outcome_id", "plan", "progress", "cycle", "goal", "run", "verification", "provenance"}
_PLAN_KEYS = {"unit", "rounds"}
_PROGRESS_KEYS = {"rounds_completed", "maximum_wave", "final_scene", "expected_scene"}
_CYCLE_KEYS = {"completed", "basis"}
_GOAL_KEYS = {"reached", "basis"}
_RUN_KEYS = {"status", "execution_extent", "termination_reason", "truncation_reason"}
_VERIFICATION_KEYS = {"status", "scope"}
_PROVENANCE_KEYS = {"kind", "source", "input_schema", "evidence_scope", "adaptations"}

_SHA256_LENGTH = 64
_LEGACY_SCOPE = "legacy plan/case fields only; no live or offline re-verification was performed"


def _require_int(value: Any, label: str, *, minimum: int) -> int | None:
    """A count is a real integer or unknown; ``True`` is never a count."""
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise OutcomeContractError(f"{label} must be an integer >= {minimum} or null (booleans are not counts)")
    return value


def _require_text(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OutcomeContractError(f"{label} must be a nonempty string")
    return value


def _require_bool_or_none(value: Any, label: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise OutcomeContractError(f"{label} must be a boolean or null")


def _require_object(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OutcomeContractError(f"{label} must be an object")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise OutcomeContractError(f"{label} has unknown fields: {', '.join(unknown)}")
    return value


def _require_exact_keys(value: dict[str, Any], label: str, keys: set[str]) -> None:
    missing = sorted(keys - set(value))
    if missing:
        raise OutcomeContractError(f"{label} is missing required fields: {', '.join(missing)}")


def _derived_cycle(progress: Mapping[str, Any]) -> bool | None:
    """The historical full-cycle gate, or ``None`` while a required fact is unknown.

    A known failing condition makes the gate ``False``; ``True`` requires every
    condition to be known.  A missing fact never becomes ``False``.
    """
    rounds = progress["rounds_completed"]
    wave = progress["maximum_wave"]
    scene = progress["final_scene"]
    expected = progress["expected_scene"]
    if rounds == 0:
        return False
    if wave is not None and wave < CYCLE_WAVE_MINIMUM:
        return False
    if scene is not None and expected is not None and scene != expected:
        return False
    if rounds is None or wave is None or scene is None or expected is None:
        return None
    return bool(rounds >= 1 and wave >= CYCLE_WAVE_MINIMUM and scene == expected)


def _derived_goal(plan: Mapping[str, Any], progress: Mapping[str, Any]) -> bool | None:
    """Whether the declared target was reached, or ``None`` while unknown."""
    target = plan["rounds"]
    if target is None:
        return None
    rounds = progress["rounds_completed"]
    if rounds == 0:
        return False
    wave = progress["maximum_wave"]
    scene = progress["final_scene"]
    expected = progress["expected_scene"]
    if wave is not None and wave < CYCLE_WAVE_MINIMUM:
        return False
    if scene is not None and expected is not None and scene != expected:
        return False
    if rounds is None or wave is None or scene is None or expected is None:
        return None
    return bool(rounds >= target and wave >= CYCLE_WAVE_MINIMUM and scene == expected)


def outcome_id(document: Mapping[str, Any]) -> str:
    """The content identity of an outcome document (its own id excluded)."""
    return digest_sha256({key: value for key, value in document.items() if key != "outcome_id"})


def validate_outcome(document: Any) -> dict[str, Any]:
    """Strictly validate one outcome document and return it unchanged.

    Rejects unknown schemas, unknown fields, boolean counts, missing required
    fields, self-contradictory cycle/goal declarations and verification claims
    that the provenance cannot support.
    """
    if not isinstance(document, dict):
        raise OutcomeContractError("an outcome document must be an object")
    if document.get("schema") != OUTCOME_SCHEMA:
        raise UnsupportedSchemaError(
            f"unsupported outcome schema {document.get('schema')!r}; this package defines {OUTCOME_SCHEMA!r}"
        )
    _require_exact_keys(document, "outcome document", _TOP_KEYS)
    unknown = sorted(set(document) - _TOP_KEYS)
    if unknown:
        raise OutcomeContractError(f"outcome document has unknown fields: {', '.join(unknown)}")

    plan = _require_object(document["plan"], "plan", _PLAN_KEYS)
    _require_exact_keys(plan, "plan", _PLAN_KEYS)
    unit = plan["unit"]
    if unit is not None and unit not in SUPPORTED_UNITS:
        raise UnsupportedSchemaError(f"unsupported plan unit {unit!r}; supported: {sorted(SUPPORTED_UNITS)}")
    rounds = _require_int(plan["rounds"], "plan.rounds", minimum=1)
    if rounds is not None and unit != OUTCOME_UNIT:
        raise OutcomeContractError("plan.rounds requires plan.unit to be 'round'")

    progress = _require_object(document["progress"], "progress", _PROGRESS_KEYS)
    _require_exact_keys(progress, "progress", _PROGRESS_KEYS)
    for name in sorted(_PROGRESS_KEYS):
        progress[name] = _require_int(progress[name], f"progress.{name}", minimum=0)

    cycle = _require_object(document["cycle"], "cycle", _CYCLE_KEYS)
    _require_exact_keys(cycle, "cycle", _CYCLE_KEYS)
    cycle_completed = _require_bool_or_none(cycle["completed"], "cycle.completed")
    cycle_basis = _require_text(cycle["basis"], "cycle.basis")

    goal = _require_object(document["goal"], "goal", _GOAL_KEYS)
    _require_exact_keys(goal, "goal", _GOAL_KEYS)
    goal_reached = _require_bool_or_none(goal["reached"], "goal.reached")
    goal_basis = _require_text(goal["basis"], "goal.basis")

    run = _require_object(document["run"], "run", _RUN_KEYS)
    _require_exact_keys(run, "run", _RUN_KEYS)
    if run["status"] not in RUN_STATUSES:
        raise OutcomeContractError(f"run.status must be one of {sorted(RUN_STATUSES)}")
    execution_extent = run["execution_extent"]
    if execution_extent not in EXECUTION_EXTENTS:
        raise OutcomeContractError(f"run.execution_extent must be one of {sorted(EXECUTION_EXTENTS)}")
    termination = _require_text(run["termination_reason"], "run.termination_reason", allow_none=True)
    truncation = _require_text(run["truncation_reason"], "run.truncation_reason", allow_none=True)
    if truncation is not None and truncation not in TRUNCATION_REASONS:
        raise OutcomeContractError(f"run.truncation_reason must be one of {sorted(TRUNCATION_REASONS)} or null")
    # Only an actual advancement delta contradicts "nothing ran": a positive
    # absolute wave or scene can be only the loaded initial state, so it is not
    # execution evidence.
    if execution_extent == "not_executed" and progress["rounds_completed"]:
        raise OutcomeContractError(
            "run.execution_extent='not_executed' contradicts recorded progress (progress.rounds_completed)"
        )
    if execution_extent == "complete" and truncation is not None:
        raise OutcomeContractError("run.execution_extent='complete' contradicts a recorded truncation_reason")

    verification = _require_object(document["verification"], "verification", _VERIFICATION_KEYS)
    _require_exact_keys(verification, "verification", _VERIFICATION_KEYS)
    if verification["status"] not in VERIFICATION_STATUSES:
        raise OutcomeContractError(f"verification.status must be one of {sorted(VERIFICATION_STATUSES)}")
    verification_status = str(verification["status"])
    verification_scope = _require_text(verification["scope"], "verification.scope")

    provenance = _require_object(document["provenance"], "provenance", _PROVENANCE_KEYS)
    _require_exact_keys(provenance, "provenance", _PROVENANCE_KEYS)
    kind = provenance["kind"]
    if kind not in PROVENANCE_KINDS:
        raise OutcomeContractError(f"provenance.kind must be one of {sorted(PROVENANCE_KINDS)}")
    source = _require_text(provenance["source"], "provenance.source")
    input_schema = _require_text(provenance["input_schema"], "provenance.input_schema", allow_none=True)
    evidence_scope = _require_text(provenance["evidence_scope"], "provenance.evidence_scope")
    adaptations = provenance["adaptations"]
    if not isinstance(adaptations, list) or any(not isinstance(item, str) or not item.strip() for item in adaptations):
        raise OutcomeContractError("provenance.adaptations must be a list of nonempty strings")

    # Missing facts stay unknown: a claim may not be upgraded beyond what the
    # known conditions support, and a known contradiction is rejected.
    derived_cycle = _derived_cycle(progress)
    if derived_cycle is not None:
        if cycle_completed is not None and cycle_completed is not derived_cycle:
            raise OutcomeContractError(
                f"cycle.completed={cycle_completed!r} contradicts the recorded progress (derived {derived_cycle!r})"
            )
    elif cycle_completed is True:
        raise OutcomeContractError("cycle.completed=true requires known rounds, wave and scene facts")
    derived_goal = _derived_goal(plan, progress)
    if derived_goal is not None:
        if goal_reached is not None and goal_reached is not derived_goal:
            raise OutcomeContractError(
                f"goal.reached={goal_reached!r} contradicts the declared target and recorded progress "
                f"(derived {derived_goal!r})"
            )
    elif goal_reached is True:
        raise OutcomeContractError("goal.reached=true requires a declared round target and known progress facts")

    if kind == "synthetic-demo" and verification_status == "verified":
        raise OutcomeContractError("a synthetic-demo outcome cannot claim verification.status='verified'")
    if kind == "legacy-adapted" and verification_status == "verified":
        raise OutcomeContractError("legacy-adapted evidence is never auto-upgraded to verification.status='verified'")
    if verification_status == "verified" and not (verification_scope or "").strip():
        raise OutcomeContractError("verification.status='verified' requires a declared verification scope")

    outcome_digest = document["outcome_id"]
    if not isinstance(outcome_digest, str) or len(outcome_digest) != _SHA256_LENGTH:
        raise OutcomeContractError("outcome_id must be a lowercase SHA-256 value")
    if outcome_id(document) != outcome_digest:
        raise OutcomeContractError("outcome_id does not match the canonical document content")

    # Rebind the normalized values so accidental later mutation cannot change
    # what was validated.
    document["plan"] = {"unit": unit, "rounds": rounds}
    document["progress"] = dict(progress)
    document["cycle"] = {"completed": cycle_completed, "basis": cycle_basis}
    document["goal"] = {"reached": goal_reached, "basis": goal_basis}
    document["run"] = {
        "status": run["status"],
        "execution_extent": execution_extent,
        "termination_reason": termination,
        "truncation_reason": truncation,
    }
    document["verification"] = {"status": verification_status, "scope": verification_scope}
    document["provenance"] = {
        "kind": kind,
        "source": source,
        "input_schema": input_schema,
        "evidence_scope": evidence_scope,
        "adaptations": list(adaptations),
    }
    return document


def outcome_document(
    *,
    plan_unit: str | None = None,
    plan_rounds: int | None = None,
    rounds_completed: int | None = None,
    maximum_wave: int | None = None,
    final_scene: int | None = None,
    expected_scene: int | None = None,
    cycle_completed: bool | None = None,
    goal_reached: bool | None = None,
    run_status: str = "unknown",
    execution_extent: str = "unknown",
    termination_reason: str | None = None,
    truncation_reason: str | None = None,
    verification_status: str = "unverified",
    verification_scope: str = "unspecified",
    provenance_kind: str = "native",
    provenance_source: str = "unspecified",
    input_schema: str | None = None,
    evidence_scope: str = "unspecified",
    adaptations: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a strictly valid outcome document, deriving what the facts allow.

    ``cycle_completed`` and ``goal_reached`` default to the value the recorded
    facts imply and stay ``null`` while a required fact is unknown; an explicit
    value must agree with the facts or validation fails.
    """
    plan = {"unit": plan_unit, "rounds": plan_rounds}
    progress = {
        "rounds_completed": rounds_completed,
        "maximum_wave": maximum_wave,
        "final_scene": final_scene,
        "expected_scene": expected_scene,
    }
    derived_cycle = _derived_cycle(progress)
    derived_goal = _derived_goal(plan, progress)
    if cycle_completed is None:
        cycle_completed = derived_cycle
    if goal_reached is None:
        goal_reached = derived_goal
    if cycle_completed is None:
        cycle_basis = "unknown: at least one progress fact is missing"
    elif cycle_completed:
        cycle_basis = "at least one round completed, wave >= 20 and the declared final scene reached"
    else:
        cycle_basis = "a required cycle condition is known to be unsatisfied or missing"
    if goal_reached is None:
        goal_basis = "unknown: no declared round target or a required progress fact is missing"
    elif goal_reached:
        goal_basis = "completed rounds >= declared target with the cycle conditions reached"
    else:
        goal_basis = "declared round target not reached or a required cycle condition failed"
    document: dict[str, Any] = {
        "schema": OUTCOME_SCHEMA,
        "plan": plan,
        "progress": progress,
        "cycle": {"completed": cycle_completed, "basis": cycle_basis},
        "goal": {"reached": goal_reached, "basis": goal_basis},
        "run": {
            "status": run_status,
            "execution_extent": execution_extent,
            "termination_reason": termination_reason,
            "truncation_reason": truncation_reason,
        },
        "verification": {"status": verification_status, "scope": verification_scope},
        "provenance": {
            "kind": provenance_kind,
            "source": provenance_source,
            "input_schema": input_schema,
            "evidence_scope": evidence_scope,
            "adaptations": list(adaptations),
        },
    }
    document["outcome_id"] = outcome_id(document)
    return validate_outcome(document)


@dataclass(frozen=True)
class Outcome:
    """A validated outcome document with typed access to its decisions."""

    document: dict[str, Any]

    @property
    def outcome_id(self) -> str:
        return str(self.document["outcome_id"])

    @property
    def plan_unit(self) -> str | None:
        value = self.document["plan"]["unit"]
        return value if isinstance(value, str) else None

    @property
    def plan_rounds(self) -> int | None:
        value = self.document["plan"]["rounds"]
        return value if type(value) is int else None

    @property
    def rounds_completed(self) -> int | None:
        value = self.document["progress"]["rounds_completed"]
        return value if type(value) is int else None

    @property
    def cycle_completed(self) -> bool | None:
        value = self.document["cycle"]["completed"]
        return value if type(value) is bool else None

    @property
    def goal_reached(self) -> bool | None:
        value = self.document["goal"]["reached"]
        return value if type(value) is bool else None

    @property
    def run_status(self) -> str:
        return str(self.document["run"]["status"])

    @property
    def execution_extent(self) -> str:
        return str(self.document["run"]["execution_extent"])

    @property
    def termination_reason(self) -> str | None:
        value = self.document["run"]["termination_reason"]
        return value if isinstance(value, str) else None

    @property
    def truncation_reason(self) -> str | None:
        value = self.document["run"]["truncation_reason"]
        return value if isinstance(value, str) else None

    @property
    def verification_status(self) -> str:
        return str(self.document["verification"]["status"])

    @property
    def provenance(self) -> dict[str, Any]:
        value = self.document["provenance"]
        return dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OUTCOME_SCHEMA,
            "outcome_id": self.outcome_id,
            "plan": dict(self.document["plan"]),
            "progress": dict(self.document["progress"]),
            "cycle": dict(self.document["cycle"]),
            "goal": dict(self.document["goal"]),
            "run": dict(self.document["run"]),
            "verification": dict(self.document["verification"]),
            "provenance": dict(self.document["provenance"]),
        }


def read_outcome(path: str | Path) -> Outcome:
    """Read and fully validate one outcome document file."""
    outcome_path = Path(path)
    if not outcome_path.is_file():
        raise IncompleteEvidenceError(f"outcome document is missing: {outcome_path}")
    return Outcome(validate_outcome(read_json(outcome_path)))


def _legacy_count(value: Any, label: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise OutcomeContractError(f"legacy {label} must be an integer >= {minimum} or null (booleans are not counts)")
    return value


def _legacy_plan(document: Mapping[str, Any] | None) -> tuple[str | None, int | None, str | None, list[str]]:
    """Return ``(unit, rounds, input_schema, adaptations)`` for one legacy plan."""
    if document is None:
        return None, None, None, ["no legacy plan was supplied: the declared target stays unknown"]
    if not isinstance(document, Mapping):
        raise OutcomeContractError("legacy plan must be an object")
    schema = document.get("schema")
    if schema not in LEGACY_PLAN_SCHEMAS:
        raise UnsupportedSchemaError(
            f"unsupported legacy plan schema {schema!r}; supported: {sorted(LEGACY_PLAN_SCHEMAS)}"
        )
    adaptations: list[str] = []
    if "flags_to_complete" in document and "rounds_to_complete" in document:
        raise OutcomeContractError(
            "a legacy plan cannot mix rounds_to_complete with its pre-#97 alias flags_to_complete"
        )
    if "rounds_to_complete" in document:
        rounds = _legacy_count(document["rounds_to_complete"], "rounds_to_complete", minimum=1)
        field = "rounds_to_complete"
        adaptations.append("rounds_to_complete is already declared in rounds; the unit is unchanged")
    elif "flags_to_complete" in document:
        rounds = _legacy_count(document["flags_to_complete"], "flags_to_complete", minimum=1)
        field = "flags_to_complete"
        adaptations.append(
            "flags_to_complete is the pre-#97 field name and always counted rounds "
            "(1 round = 2 flags = 20 waves); it is read as rounds and never divided or rescaled"
        )
    else:
        rounds = 1
        field = "default"
        adaptations.append("the absent legacy target field kept the historical default of one round")
    if rounds is None:
        raise OutcomeContractError(f"legacy {field} must declare a positive round count")
    return OUTCOME_UNIT, rounds, str(schema), adaptations


def _legacy_execution_extent(status: str | None, progress: Mapping[str, Any], source_outcome: str | None) -> str:
    """Derive only the source execution extent the legacy facts actually prove.

    The extent describes the **source play loop**, not the suite/case
    lifecycle; ``status`` is consulted only for the historical
    ``startup_failed`` marker (the source run never existed, so nothing ran).
    ``source_outcome`` is the play loop's own verdict: ``full_cycle_completed``
    is its configured end and therefore proves ``complete``.  A positive
    ``maximum_wave`` is *not* proof of execution: the historical runner
    initializes it from the loaded initial observation, so a saved state can
    already be on wave 1 or later.  A positive ``rounds_completed`` delta
    (final minus initial completed rounds) proves execution started, but not
    that it stopped early: a complete source with a missing reason must stay
    ``unknown``.  Only the delta together with a documented explicit
    premature-stop/truncation source outcome proves ``partial``.  A
    truncation reason alone proves nothing (a budget can stop before the
    first action), and an unrecognized outcome string is not proof.
    Everything else stays ``unknown``; callers that know more must declare
    the extent explicitly.
    """
    if status == "startup_failed":
        return "not_executed"
    if source_outcome == "full_cycle_completed":
        return "complete"
    rounds = progress["rounds_completed"]
    if rounds is not None and rounds >= 1 and source_outcome in INCOMPLETE_SOURCE_OUTCOMES:
        return "partial"
    return "unknown"


def outcome_from_legacy(
    plan: Mapping[str, Any] | None,
    case: Mapping[str, Any],
    *,
    source: str,
    expected_scene: int | None = None,
    evidence_scope: str | None = None,
    input_schema: str | None = None,
    execution_extent: str | None = None,
) -> Outcome:
    """Adapt legacy plan/case documents to ``trajectory-core.outcome.v2``.

    The adapter never re-verifies the legacy evidence: the adapted document is
    ``legacy-adapted`` and ``unverified``, and it records the exact input schema
    and every semantic adaptation.  ``full_cycle`` is reported as
    ``cycle.completed`` only; the declared target is decided separately.
    ``execution_extent`` is derived from the source-stage facts when the caller
    does not declare it (``startup_failed`` -> ``not_executed``; the source
    play loop's ``full_cycle_completed`` outcome -> ``complete``; a positive
    completed-round delta together with a documented explicit
    premature-stop/truncation outcome -> ``partial``; otherwise
    ``unknown``).  The case/suite lifecycle status and an absolute wave are
    not execution evidence, and a progress delta without a documented
    premature stop is not proof that execution stopped early; an explicit
    value must satisfy the outcome contract.
    """
    if not isinstance(case, Mapping):
        raise OutcomeContractError("legacy case report must be an object")
    unit, rounds, plan_schema, adaptations = _legacy_plan(plan)

    case_rounds = _legacy_count(case.get("rounds_to_complete"), "case rounds_to_complete", minimum=1)
    if case_rounds is not None:
        if rounds is not None and case_rounds != rounds:
            raise OutcomeContractError(
                f"legacy case rounds_to_complete={case_rounds} conflicts with the plan target {rounds}"
            )
        if rounds is None:
            unit, rounds = OUTCOME_UNIT, case_rounds
            adaptations.append("the case report's rounds_to_complete supplied the declared target")

    status = case.get("status")
    if status is None:
        run_status = "unknown"
    else:
        if status not in LEGACY_RUN_STATUSES:
            raise OutcomeContractError(f"unknown legacy case status {status!r}")
        run_status = LEGACY_RUN_STATUSES[status]
        adaptations.append(f"legacy case status {status!r} mapped to run.status {run_status!r}")

    full_cycle = case.get("full_cycle")
    if full_cycle is not None and type(full_cycle) is not bool:
        raise OutcomeContractError("legacy full_cycle must be a boolean or null")
    if full_cycle is not None:
        adaptations.append(
            "the historical full_cycle gate is reported as cycle.completed; it is never used as goal achievement"
        )

    rounds_completed = _legacy_count(case.get("rounds_completed"), "case rounds_completed", minimum=0)
    maximum_wave = _legacy_count(case.get("maximum_wave"), "case maximum_wave", minimum=0)
    final_observation = case.get("final_observation")
    final_scene: int | None = None
    if final_observation is not None:
        if not isinstance(final_observation, Mapping):
            raise OutcomeContractError("legacy final_observation must be an object")
        final_scene = _legacy_count(final_observation.get("scene"), "case final_observation.scene", minimum=0)

    termination: str | None = None
    truncation: str | None = None
    outcome_reason = case.get("outcome")
    if outcome_reason is not None:
        if not isinstance(outcome_reason, str) or not outcome_reason.strip():
            raise OutcomeContractError("legacy case outcome must be a nonempty string or null")
        if outcome_reason in TRUNCATION_REASONS:
            truncation = outcome_reason
            adaptations.append(f"legacy case outcome {outcome_reason!r} reported as run.truncation_reason")
        else:
            termination = outcome_reason
            adaptations.append(f"legacy case outcome {outcome_reason!r} reported as run.termination_reason")

    if full_cycle is True:
        required = {"rounds_completed": rounds_completed, "maximum_wave": maximum_wave, "final scene": final_scene}
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise OutcomeContractError(
                "legacy full_cycle=true cannot be bound to known progress facts: "
                + ", ".join(missing)
                + " missing from the case report"
            )
        if expected_scene is None:
            raise OutcomeContractError(
                "legacy full_cycle=true requires the caller's expected_scene; the adapter will not guess a scene"
            )

    progress = {
        "rounds_completed": rounds_completed,
        "maximum_wave": maximum_wave,
        "final_scene": final_scene,
        "expected_scene": expected_scene,
    }
    if execution_extent is None:
        execution_extent = _legacy_execution_extent(status, progress, outcome_reason)
        adaptations.append(f"legacy source facts imply execution extent {execution_extent!r}")
        if execution_extent == "unknown":
            adaptations.append(
                "the legacy case facts do not prove that source execution started or reached its configured end; "
                "extent stays unknown"
            )
    else:
        adaptations.append(f"the caller declared execution extent {execution_extent!r}")

    document = outcome_document(
        plan_unit=unit,
        plan_rounds=rounds,
        rounds_completed=rounds_completed,
        maximum_wave=maximum_wave,
        final_scene=final_scene,
        expected_scene=expected_scene,
        cycle_completed=full_cycle,
        run_status=run_status,
        execution_extent=execution_extent,
        termination_reason=termination,
        truncation_reason=truncation,
        verification_status="unverified",
        verification_scope=evidence_scope or _LEGACY_SCOPE,
        provenance_kind="legacy-adapted",
        provenance_source=source,
        input_schema=input_schema or plan_schema or "lvz.evaluation.v1 case report",
        evidence_scope=evidence_scope or _LEGACY_SCOPE,
        adaptations=adaptations,
    )
    return Outcome(document)
