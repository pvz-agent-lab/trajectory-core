"""Behavior contract of the versioned public outcome schema (issue #3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trajectory_core as tc


def legacy_plan(**overrides: object) -> dict:
    return {"schema": "lvz.evaluation-plan.v2", **overrides}


def legacy_case(**overrides: object) -> dict:
    return {
        "status": "completed",
        "outcome": "full_cycle_completed",
        "full_cycle": True,
        "rounds_completed": 1,
        "maximum_wave": 20,
        "final_observation": {"scene": 3},
        **overrides,
    }


def adapt(plan: dict | None = None, case: dict | None = None, **kwargs: object) -> tc.Outcome:
    if case is None:
        case = legacy_case()
    options: dict = {"source": "unit-test", "expected_scene": 3}
    options.update(kwargs)
    return tc.outcome_from_legacy(plan, case, **options)


def test_flags_to_complete_is_read_as_rounds_without_rescaling() -> None:
    outcome = adapt(legacy_plan(flags_to_complete=2))
    assert outcome.plan_unit == "round"
    assert outcome.plan_rounds == 2
    assert outcome.provenance["kind"] == "legacy-adapted"
    note = " ".join(outcome.provenance["adaptations"])
    assert "pre-#97" in note
    assert "never divided or rescaled" in note
    assert outcome.verification_status == "unverified"


def test_rounds_to_complete_is_already_in_rounds() -> None:
    outcome = adapt(legacy_plan(rounds_to_complete=3))
    assert outcome.plan_rounds == 3
    assert any("already declared in rounds" in item for item in outcome.provenance["adaptations"])


def test_absent_target_keeps_the_historical_default_of_one_round() -> None:
    outcome = adapt(legacy_plan())
    assert outcome.plan_rounds == 1
    assert any("historical default of one round" in item for item in outcome.provenance["adaptations"])


def test_full_cycle_is_not_goal_completion() -> None:
    """The #97 acceptance case: one cycle done, declared two-round target missed."""
    outcome = adapt(legacy_plan(flags_to_complete=2), legacy_case(outcome="terminal_before_complete"))
    assert outcome.rounds_completed == 1
    assert outcome.cycle_completed is True
    assert outcome.goal_reached is False
    assert outcome.termination_reason == "terminal_before_complete"
    assert outcome.truncation_reason is None
    assert "never used as goal achievement" in " ".join(outcome.provenance["adaptations"])


def test_declared_target_reached_requires_the_declared_round_count() -> None:
    outcome = adapt(legacy_plan(flags_to_complete=1))
    assert outcome.cycle_completed is True
    assert outcome.goal_reached is True


def test_missing_facts_stay_unknown() -> None:
    outcome = adapt(None, {})
    assert outcome.plan_unit is None
    assert outcome.plan_rounds is None
    assert outcome.rounds_completed is None
    assert outcome.cycle_completed is None
    assert outcome.goal_reached is None
    assert outcome.run_status == "unknown"
    assert outcome.execution_extent == "unknown"
    assert outcome.termination_reason is None
    assert outcome.truncation_reason is None
    assert outcome.verification_status == "unverified"


def test_declared_true_cycle_cannot_be_upgraded_from_missing_progress() -> None:
    with pytest.raises(tc.OutcomeContractError, match="known progress facts"):
        adapt(legacy_plan(), {"full_cycle": True})


@pytest.mark.parametrize(
    "document",
    [
        legacy_plan(flags_to_complete=True),
        legacy_plan(rounds_to_complete=True),
    ],
)
def test_boolean_target_is_not_a_count(document: dict) -> None:
    with pytest.raises(tc.OutcomeContractError):
        adapt(document)


def test_boolean_progress_is_not_a_count() -> None:
    with pytest.raises(tc.OutcomeContractError, match="booleans are not counts"):
        adapt(legacy_plan(), legacy_case(rounds_completed=True))
    with pytest.raises(tc.OutcomeContractError, match="booleans are not counts"):
        adapt(legacy_plan(), legacy_case(maximum_wave=True))


def test_mixing_the_legacy_alias_and_the_new_field_is_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="cannot mix"):
        adapt(legacy_plan(flags_to_complete=1, rounds_to_complete=1))


def test_case_target_conflicting_with_the_plan_is_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="conflicts with the plan target"):
        adapt(legacy_plan(flags_to_complete=2), legacy_case(rounds_to_complete=1))


def test_contradictory_cycle_is_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="contradicts"):
        adapt(legacy_plan(), legacy_case(rounds_completed=0))


def test_contradictory_goal_is_rejected_by_the_builder() -> None:
    with pytest.raises(tc.OutcomeContractError, match="contradicts"):
        tc.outcome_document(
            plan_unit="round",
            plan_rounds=2,
            rounds_completed=1,
            maximum_wave=20,
            final_scene=3,
            expected_scene=3,
            goal_reached=True,
            provenance_kind="native",
            provenance_source="unit-test",
        )


def test_goal_cannot_be_true_without_a_declared_target() -> None:
    with pytest.raises(tc.OutcomeContractError, match="declared round target"):
        tc.outcome_document(
            rounds_completed=5,
            maximum_wave=20,
            final_scene=3,
            expected_scene=3,
            goal_reached=True,
            provenance_kind="native",
            provenance_source="unit-test",
        )


def test_no_inference_from_full_cycle_alone() -> None:
    outcome = adapt(None, legacy_case())
    assert outcome.cycle_completed is True
    assert outcome.goal_reached is None


def test_unknown_plan_schema_fails_closed() -> None:
    with pytest.raises(tc.UnsupportedSchemaError):
        adapt({"schema": "lvz.evaluation-plan.v99"})


def test_unknown_case_status_is_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="unknown legacy case status"):
        adapt(legacy_plan(), legacy_case(status="teleported"))


def test_legacy_statuses_map_to_the_public_run_status() -> None:
    completed = adapt(legacy_plan(), legacy_case(status="completed"))
    assert completed.run_status == "completed"
    assert completed.execution_extent == "complete"
    failed = adapt(legacy_plan(), legacy_case(status="failed"))
    assert failed.run_status == "failed"
    assert failed.execution_extent == "partial"
    startup_failed = adapt(
        legacy_plan(),
        legacy_case(status="startup_failed", rounds_completed=None, maximum_wave=None, full_cycle=None),
    )
    assert startup_failed.run_status == "failed"
    assert startup_failed.execution_extent == "not_executed"
    incomplete = adapt(legacy_plan(), legacy_case(status="incomplete"))
    assert incomplete.run_status == "aborted"
    assert incomplete.execution_extent == "partial"


def test_a_startup_failure_with_recorded_progress_is_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="not_executed"):
        adapt(legacy_plan(), legacy_case(status="startup_failed"))


def test_failure_before_execution_is_distinct_from_failure_after_partial_execution() -> None:
    before = adapt(
        legacy_plan(),
        legacy_case(status="startup_failed", rounds_completed=None, maximum_wave=None, full_cycle=None),
    )
    assert before.run_status == "failed"
    assert before.execution_extent == "not_executed"
    after = adapt(legacy_plan(), legacy_case(status="failed", rounds_completed=1))
    assert after.run_status == "failed"
    assert after.execution_extent == "partial"
    # Zero completed rounds cannot prove that nothing ran: the first round may
    # have been partial and unrecorded, so the extent must stay unknown.
    ambiguous = adapt(
        legacy_plan(),
        legacy_case(status="failed", rounds_completed=0, maximum_wave=0, full_cycle=None),
    )
    assert ambiguous.run_status == "failed"
    assert ambiguous.execution_extent == "unknown"


def test_a_truncated_run_is_partial_not_complete() -> None:
    truncated = adapt(legacy_plan(), legacy_case(status="incomplete", outcome="tick_budget_exhausted"))
    assert truncated.run_status == "aborted"
    assert truncated.execution_extent == "partial"
    assert truncated.truncation_reason == "tick_budget_exhausted"


def test_completed_invocation_with_an_unmet_goal_is_not_success() -> None:
    outcome = adapt(legacy_plan(flags_to_complete=2), legacy_case(outcome="terminal_before_complete"))
    assert outcome.run_status == "completed"
    assert outcome.execution_extent == "complete"
    assert outcome.cycle_completed is True
    assert outcome.goal_reached is False


def test_explicit_execution_extent_must_satisfy_the_contract() -> None:
    declared = adapt(
        legacy_plan(),
        legacy_case(status="failed", rounds_completed=0, full_cycle=None),
        execution_extent="partial",
    )
    assert declared.execution_extent == "partial"
    assert any("caller declared execution extent" in note for note in declared.provenance["adaptations"])
    with pytest.raises(tc.OutcomeContractError, match="execution_extent"):
        adapt(legacy_plan(), legacy_case(), execution_extent="teleported")


def test_execution_extent_contradictions_are_rejected() -> None:
    with pytest.raises(tc.OutcomeContractError, match="not_executed"):
        tc.outcome_document(
            rounds_completed=1,
            maximum_wave=20,
            run_status="failed",
            execution_extent="not_executed",
            provenance_kind="native",
            provenance_source="unit-test",
        )
    with pytest.raises(tc.OutcomeContractError, match="truncation"):
        tc.outcome_document(
            rounds_completed=1,
            maximum_wave=20,
            run_status="aborted",
            execution_extent="complete",
            truncation_reason="tick_budget_exhausted",
            provenance_kind="native",
            provenance_source="unit-test",
        )


def test_truncation_reasons_stay_separate_from_termination() -> None:
    outcome = adapt(legacy_plan(), legacy_case(outcome="tick_budget_exhausted"))
    assert outcome.truncation_reason == "tick_budget_exhausted"
    assert outcome.termination_reason is None


def test_synthetic_demo_cannot_claim_verification() -> None:
    with pytest.raises(tc.OutcomeContractError, match="synthetic-demo"):
        tc.outcome_document(
            plan_unit="round",
            plan_rounds=1,
            rounds_completed=1,
            maximum_wave=20,
            final_scene=3,
            expected_scene=3,
            verification_status="verified",
            verification_scope="fixture",
            provenance_kind="synthetic-demo",
            provenance_source="unit-test",
        )


def test_legacy_adapted_cannot_claim_verification() -> None:
    with pytest.raises(tc.OutcomeContractError, match="legacy-adapted"):
        tc.outcome_document(
            plan_unit="round",
            plan_rounds=1,
            verification_status="verified",
            verification_scope="legacy read",
            provenance_kind="legacy-adapted",
            provenance_source="unit-test",
        )


def test_outcome_document_round_trips_through_a_file(tmp_path: Path) -> None:
    document = tc.outcome_document(
        plan_unit="round",
        plan_rounds=1,
        rounds_completed=1,
        maximum_wave=20,
        final_scene=3,
        expected_scene=3,
        run_status="completed",
        termination_reason="full_cycle_completed",
        verification_status="unverified",
        verification_scope="unit test",
        provenance_kind="native",
        provenance_source="unit-test",
    )
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    outcome = tc.read_outcome(path)
    assert outcome.to_dict() == document
    assert outcome.outcome_id == tc.outcome_id(document)
    assert outcome.cycle_completed is True
    assert outcome.goal_reached is True


def test_tampered_outcome_id_is_rejected(tmp_path: Path) -> None:
    document = tc.outcome_document(provenance_kind="native", provenance_source="unit-test")
    document["outcome_id"] = "0" * 64
    with pytest.raises(tc.OutcomeContractError, match="outcome_id"):
        tc.validate_outcome(document)
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    with pytest.raises(tc.OutcomeContractError, match="outcome_id"):
        tc.read_outcome(path)


def test_unknown_fields_and_schemas_are_rejected() -> None:
    document = tc.outcome_document(provenance_kind="native", provenance_source="unit-test")
    document["verdict"] = "pass"
    with pytest.raises(tc.OutcomeContractError, match="unknown fields"):
        tc.validate_outcome(document)
    document = tc.outcome_document(provenance_kind="native", provenance_source="unit-test")
    document["schema"] = "trajectory-core.outcome.v99"
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.validate_outcome(document)


def test_unknown_unit_fails_closed() -> None:
    with pytest.raises(tc.UnsupportedSchemaError, match="unit"):
        tc.outcome_document(plan_unit="flag", plan_rounds=2, provenance_kind="native", provenance_source="unit-test")


def test_unit_is_required_for_a_declared_round_count() -> None:
    with pytest.raises(tc.OutcomeContractError, match="plan.unit"):
        tc.outcome_document(plan_rounds=1, provenance_kind="native", provenance_source="unit-test")
