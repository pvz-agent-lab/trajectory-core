"""Behavior contract of the formal producer closure (issue #3).

Positive cases use the synthetic two-branch + two-rerun fixture; negative cases
mutate a copy and re-close the record exactly the way an adversarial writer
would (new file digest, then a recomputed ``closure_id``), so every rejection
is about the contract and not only about a stale content id.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

import trajectory_core as tc
from conftest import read_json, write_json
from trajectory_core.jsonio import digest_sha256


@pytest.fixture()
def stage(formal_lab: dict, tmp_path: Path) -> Path:
    target = tmp_path / "package"
    shutil.copytree(formal_lab["stage"], target)
    return target


@pytest.fixture()
def stage_factory(formal_lab: dict, tmp_path: Path) -> Any:
    """A factory that copies the pristine demo package on demand."""
    from itertools import count

    counter = count()

    def make() -> Path:
        target = tmp_path / f"package-{next(counter)}"
        shutil.copytree(formal_lab["stage"], target)
        return target

    return make


def rewrite_closure(stage: Path, change: Any) -> Path:
    """Edit the closure record and recompute its content id."""
    path = stage / "closure.json"
    record = read_json(path)
    change(record)
    record["closure_id"] = digest_sha256({key: value for key, value in record.items() if key != "closure_id"})
    write_json(path, record)
    return path


def update_receipt(stage: Path, change: Any) -> None:
    """Edit the receipt, then re-bind its digest, its inventory entry and the closure id."""
    receipt = stage / "receipt.json"
    record = read_json(receipt)
    change(record)
    write_json(receipt, record)
    digest = tc.file_hash(receipt)

    def rebind(value: dict) -> None:
        value["producer_receipt"]["sha256"] = digest
        for item in value["inputs"]:
            if item["path"] == "receipt.json":
                item["sha256"] = digest

    rewrite_closure(stage, rebind)


def update_report(stage: Path, pair_id: str, change: Any) -> None:
    """Edit a rerun report, then re-bind its digest, inventory entry and the closure id."""
    report = stage / "reports" / f"{pair_id}.json"
    record = read_json(report)
    change(record)
    write_json(report, record)
    digest = tc.file_hash(report)

    def rebind(value: dict) -> None:
        _rebind_report(value, pair_id, digest)
        for item in value["inputs"]:
            if item["path"] == f"reports/{pair_id}.json":
                item["sha256"] = digest

    rewrite_closure(stage, rebind)


def _rebind_report(record: dict, pair_id: str, digest: str) -> None:
    for pair in record["pairs"]:
        if pair["id"] == pair_id:
            pair["report"]["sha256"] = digest


def test_formal_closure_round_trips(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    report = closure.report()
    assert report["schema"] == tc.CLOSURE_REPORT_SCHEMA
    assert report["status"] == "valid"
    assert report["content_integrity"]["status"] == "verified"
    assert report["content_integrity"]["runtime_facts_verified_by_core"] is False
    assert report["producer_attested_closure"]["attested"] is False
    assert report["producer_attested_closure"]["synthetic"] is True
    assert report["unverifiable_runtime_claims"]
    assert report["counts"] == {"nodes": 4, "outcomes": 4, "pairs": 2, "inputs": 32}
    assert tc.validate_formal_closure(stage / "closure.json")["status"] == "valid"


def test_real_producer_receipt_is_reported_as_attested(stage: Path) -> None:
    update_receipt(stage, lambda record: record.update(synthetic=False))
    report = tc.validate_formal_closure(stage / "closure.json")
    assert report["producer_attested_closure"]["attested"] is True
    assert report["producer_attested_closure"]["synthetic"] is False


def test_reading_a_formal_closure_is_read_only(stage: Path) -> None:
    def inventory() -> dict[str, str]:
        return {str(path.relative_to(stage)): tc.file_hash(path) for path in sorted(stage.rglob("*")) if path.is_file()}

    before = inventory()
    tc.load_formal_closure(stage / "closure.json")
    tc.validate_formal_closure(stage / "closure.json")
    assert inventory() == before


def test_old_seal_schema_is_never_upgraded_to_a_formal_closure(stage: Path) -> None:
    write_json(stage / "closure.json", {"schema": "lvz.issue99-shovel-fork-seal.v1", "tree": "tree"})
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.load_formal_closure(stage / "closure.json")


def test_formal_closure_schema_is_not_a_legacy_seal(stage: Path) -> None:
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.verify_seal(stage / "closure.json", source_root=stage)


def test_unknown_closure_field_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record.update(extra=True))
    with pytest.raises(tc.ClosureError, match="unknown fields"):
        tc.load_formal_closure(stage / "closure.json")


def test_tampered_outcome_is_rejected(stage: Path) -> None:
    outcome = stage / "outcomes" / "root.json"
    outcome.write_bytes(outcome.read_bytes() + b"\n")
    with pytest.raises(tc.ClosureError, match="digest does not match"):
        tc.load_formal_closure(stage / "closure.json")


def test_removed_outcome_is_rejected(stage: Path) -> None:
    (stage / "outcomes" / "root.json").unlink()
    with pytest.raises(tc.EvidenceError):
        tc.load_formal_closure(stage / "closure.json")


def test_extra_outcome_reference_is_rejected(stage: Path) -> None:
    def change(record: dict) -> None:
        record["outcomes"].append(
            {
                "key": "ghost",
                "trajectory_id": "f" * 64,
                "path": "outcomes/root.json",
                "sha256": "e" * 64,
                "outcome_id": "d" * 64,
            }
        )

    rewrite_closure(stage, change)
    with pytest.raises(tc.ClosureError, match="does not have"):
        tc.load_formal_closure(stage / "closure.json")


def test_duplicate_outcome_reference_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["outcomes"].append(dict(record["outcomes"][0])))
    with pytest.raises(tc.ClosureError, match="more than once"):
        tc.load_formal_closure(stage / "closure.json")


def test_omitted_outcome_reference_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["outcomes"].pop())
    with pytest.raises(tc.ClosureError, match="do not cover every tree node"):
        tc.load_formal_closure(stage / "closure.json")


def test_conflicting_outcome_identity_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["outcomes"][0].update(trajectory_id="a" * 64))
    with pytest.raises(tc.ClosureError, match="conflicts with the bound tree node"):
        tc.load_formal_closure(stage / "closure.json")


def test_tree_node_binding_conflict_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["tree"]["nodes"][0].update(manifest_sha256="b" * 64))
    with pytest.raises(tc.ClosureError, match="conflicts with the sealed tree"):
        tc.load_formal_closure(stage / "closure.json")


def test_missing_or_extra_tree_node_binding_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["tree"]["nodes"].pop())
    with pytest.raises(tc.ClosureError, match="missing"):
        tc.load_formal_closure(stage / "closure.json")
    rewrite_closure(stage, lambda record: record["tree"]["nodes"].append(dict(record["tree"]["nodes"][0])))
    with pytest.raises(tc.ClosureError, match="more than once"):
        tc.load_formal_closure(stage / "closure.json")


def test_rerun_report_identity_conflict_is_rejected(stage: Path) -> None:
    update_report(stage, "control-rerun", lambda record: record["rerun"].update(trajectory_id="c" * 64))
    with pytest.raises(tc.ClosureError, match="identity conflicts"):
        tc.load_formal_closure(stage / "closure.json")


def test_rerun_report_schema_is_checked(stage: Path) -> None:
    update_report(stage, "control-rerun", lambda record: record.update(schema="trajectory-core.rerun-report.v2"))
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.load_formal_closure(stage / "closure.json")


def test_pair_coverage_is_closed(stage_factory: Any) -> None:
    def duplicate_node(record: dict) -> None:
        record["pairs"][1]["baseline"] = record["pairs"][0]["baseline"]

    package = stage_factory()
    rewrite_closure(package, duplicate_node)
    with pytest.raises(tc.ClosureError, match="more than one pair or role"):
        tc.load_formal_closure(package / "closure.json")

    def foreign_node(record: dict) -> None:
        record["pairs"][1]["rerun"] = "ghost"

    package = stage_factory()
    rewrite_closure(package, foreign_node)
    with pytest.raises(tc.ClosureError, match="does not have"):
        tc.load_formal_closure(package / "closure.json")

    package = stage_factory()
    rewrite_closure(package, lambda record: record["pairs"].pop())
    with pytest.raises(tc.ClosureError, match="do not cover every tree node"):
        tc.load_formal_closure(package / "closure.json")


def test_receipt_scope_must_match_the_bound_nodes(stage: Path) -> None:
    update_receipt(
        stage,
        lambda record: record["scope"]["nodes"][0].update(trajectory_id="e" * 64),
    )
    with pytest.raises(tc.ClosureError, match="conflicts with the bound node|does not match"):
        tc.load_formal_closure(stage / "closure.json")
    update_receipt(stage, lambda record: record["scope"]["nodes"].pop())
    with pytest.raises(tc.ClosureError, match="does not match"):
        tc.load_formal_closure(stage / "closure.json")


def test_receipt_requires_an_explicit_synthetic_flag(stage: Path) -> None:
    receipt = stage / "receipt.json"
    record = read_json(receipt)
    record.pop("synthetic")
    write_json(receipt, record)
    with pytest.raises(tc.ClosureError, match="missing required fields"):
        tc.read_producer_receipt(receipt)
    record["synthetic"] = "yes"
    write_json(receipt, record)
    with pytest.raises(tc.ClosureError, match="explicit boolean"):
        tc.read_producer_receipt(receipt)


def test_receipt_schema_and_attestation_are_closed(stage: Path) -> None:
    receipt = stage / "receipt.json"
    record = read_json(receipt)
    record["schema"] = "trajectory-core.producer-closure-receipt.v2"
    write_json(receipt, record)
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.read_producer_receipt(receipt)
    record["schema"] = tc.PRODUCER_RECEIPT_SCHEMA
    record["attestation"] = "trust_me"
    write_json(receipt, record)
    with pytest.raises(tc.ClosureError, match="attestation"):
        tc.read_producer_receipt(receipt)


def test_tampered_receipt_bytes_are_rejected(stage: Path) -> None:
    receipt = stage / "receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    with pytest.raises(tc.ClosureError, match="digest does not match"):
        tc.load_formal_closure(stage / "closure.json")


def test_inventory_rejects_an_unrecorded_file(stage: Path) -> None:
    (stage / "stray.txt").write_text("unrecorded\n", encoding="utf-8")
    with pytest.raises(tc.ClosureError, match="inventory"):
        tc.load_formal_closure(stage / "closure.json")


def test_inventory_rejects_a_changed_file(stage: Path) -> None:
    (stage / "reports" / "control-rerun.json").write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(tc.EvidenceError):
        tc.load_formal_closure(stage / "closure.json")


def test_path_traversal_is_rejected(stage: Path) -> None:
    rewrite_closure(stage, lambda record: record["tree"].update(path="../outside"))
    with pytest.raises(tc.PathContractError):
        tc.load_formal_closure(stage / "closure.json")
    rewrite_closure(stage, lambda record: record["tree"].update(path="tree"))
    rewrite_closure(stage, lambda record: record["outcomes"][0].update(path="C:/absolute/outcome.json"))
    with pytest.raises(tc.PathContractError):
        tc.load_formal_closure(stage / "closure.json")


def test_symlinked_outcome_escape_is_rejected(stage: Path) -> None:
    outcome = stage / "outcomes" / "root.json"
    outside = stage.parent / "outside-outcome.json"
    shutil.copyfile(outcome, outside)
    outcome.unlink()
    try:
        outcome.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    with pytest.raises(tc.PathContractError):
        tc.load_formal_closure(stage / "closure.json")


def test_sealing_refuses_an_existing_destination(stage: Path) -> None:
    destination = stage / "closure.json"
    destination.write_bytes(b"competing-writer-bytes")
    with pytest.raises(tc.EvidenceError, match="already exists"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes={
                key: f"outcomes/{key}.json"
                for key in ("root", "issue3-control-b", "issue3-intervention-a", "issue3-intervention-b")
            },
            pairs=[],
            receipt="receipt.json",
        )
    assert destination.read_bytes() == b"competing-writer-bytes"


def test_sealing_requires_complete_outcome_coverage(stage: Path) -> None:
    destination = stage / "closed.json"
    with pytest.raises(tc.ClosureError, match="missing"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes={"root": "outcomes/root.json"},
            pairs=[],
            receipt="receipt.json",
        )
    assert not destination.exists()


def test_sealing_refuses_a_destination_outside_the_package(stage: Path) -> None:
    with pytest.raises(tc.PathContractError):
        tc.seal_formal_closure(
            stage,
            stage.parent / "closure.json",
            tree="tree",
            outcomes={
                key: f"outcomes/{key}.json"
                for key in ("root", "issue3-control-b", "issue3-intervention-a", "issue3-intervention-b")
            },
            pairs=[],
            receipt="receipt.json",
        )


def _full_outcomes() -> dict[str, str]:
    return {
        key: f"outcomes/{key}.json"
        for key in ("root", "issue3-control-b", "issue3-intervention-a", "issue3-intervention-b")
    }


def _full_pairs() -> list[dict[str, str]]:
    return [
        {
            "id": "control-rerun",
            "baseline": "root",
            "rerun": "issue3-control-b",
            "report": "reports/control-rerun.json",
        },
        {
            "id": "intervention-rerun",
            "baseline": "issue3-intervention-a",
            "rerun": "issue3-intervention-b",
            "report": "reports/intervention-rerun.json",
        },
    ]


def test_sealing_detects_a_change_during_the_write(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_validate = tc.closure.validate_closure_record

    def mutate_after_validation(record: dict, root: Path, *, closure_file: Path | None = None) -> dict:
        result = real_validate(record, root, closure_file=closure_file)
        (stage / "outcomes" / "root.json").write_bytes((stage / "outcomes" / "root.json").read_bytes() + b"\n")
        return result

    monkeypatch.setattr(tc.closure, "validate_closure_record", mutate_after_validation)
    with pytest.raises(tc.ClosureError, match="changed"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes=_full_outcomes(),
            pairs=_full_pairs(),
            receipt="receipt.json",
        )
    assert not destination.exists()


def test_sealing_removes_the_output_it_created_when_a_check_fails(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_inventory = tc.closure._inventory
    calls = {"count": 0}

    def flaky_inventory(root: Path, *, exclude: Path | None = None) -> list[dict[str, str]]:
        result = real_inventory(root, exclude=exclude)
        calls["count"] += 1
        if calls["count"] >= 3:
            return [*result, {"path": "ghost.json", "sha256": "0" * 64}]
        return result

    monkeypatch.setattr(tc.closure, "_inventory", flaky_inventory)
    with pytest.raises(tc.ClosureError, match="changed"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes=_full_outcomes(),
            pairs=_full_pairs(),
            receipt="receipt.json",
        )
    assert not destination.exists()


def test_sealing_never_deletes_a_competing_writers_file(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_write = tc.closure._write_record

    def competing(target: Path, payload: bytes) -> None:
        Path(target).write_bytes(b"competing-writer-bytes")
        real_write(target, payload)

    monkeypatch.setattr(tc.closure, "_write_record", competing)
    with pytest.raises(tc.EvidenceError, match="already exists"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes=_full_outcomes(),
            pairs=_full_pairs(),
            receipt="receipt.json",
        )
    assert destination.read_bytes() == b"competing-writer-bytes"


def test_sealing_removes_a_partial_file_it_created(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_open = Path.open

    class FailingStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def write(self, data: bytes) -> int:
            raise OSError("disk full")

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *exc: object) -> bool:
            self._stream.close()
            return False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        stream = real_open(self, *args, **kwargs)
        return FailingStream(stream) if self.name == destination.name else stream

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="disk full"):
        tc.seal_formal_closure(
            stage,
            destination,
            tree="tree",
            outcomes=_full_outcomes(),
            pairs=_full_pairs(),
            receipt="receipt.json",
        )
    assert not destination.exists()


def test_sealing_writes_a_reloadable_record(stage: Path) -> None:
    destination = stage / "sealed.json"
    closure = tc.seal_formal_closure(
        stage,
        destination,
        tree="tree",
        outcomes=_full_outcomes(),
        pairs=_full_pairs(),
        receipt="receipt.json",
    )
    assert closure.path == destination
    assert destination.read_bytes() == tc.canonical(closure.record) + b"\n"
    reloaded = tc.load_formal_closure(destination)
    assert reloaded.closure_id == closure.closure_id
    report = reloaded.report()
    assert report["counts"]["nodes"] == 4
    assert report["counts"]["pairs"] == 2
