"""Behavior contract of the formal producer closure (issue #3).

Positive cases use the synthetic two-branch + two-rerun fixture; negative cases
mutate a copy and re-close the record exactly the way an adversarial writer
would (new file digest, then a recomputed ``closure_id``), so every rejection
is about the contract and not only about a stale content id.
"""

from __future__ import annotations

import os
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


def _seal(package: Path, destination: Path | None = None) -> tc.FormalClosure:
    return tc.seal_formal_closure(
        package,
        destination if destination is not None else package / "closure.json",
        tree="tree",
        outcomes=_full_outcomes(),
        pairs=_full_pairs(),
        receipt="receipt.json",
    )


def _reseal(package: Path, destination: Path | None = None) -> tc.FormalClosure:
    """Drop the generated closure first, the way a producer re-seals a package."""
    (package / "closure.json").unlink(missing_ok=True)
    return _seal(package, destination)


def test_formal_closure_round_trips(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    report = closure.report()
    assert report["schema"] == tc.CLOSURE_REPORT_SCHEMA
    assert report["status"] == "valid"
    assert report["profile"] == tc.CONTROLLED_FAMILY_PROFILE
    assert report["content_integrity"]["status"] == "verified"
    assert report["content_integrity"]["runtime_facts_verified_by_core"] is False
    assert report["producer_attested_closure"]["attested"] is False
    assert report["producer_attested_closure"]["synthetic"] is True
    assert report["unverifiable_runtime_claims"]
    assert report["counts"] == {"nodes": 4, "outcomes": 4, "pairs": 2, "inputs": 32, "producer_artifacts": 31}
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


def test_the_record_names_the_controlled_family_profile(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    assert closure.record["profile"] == tc.CONTROLLED_FAMILY_PROFILE
    assert closure.report()["profile"] == tc.CONTROLLED_FAMILY_PROFILE
    rewrite_closure(stage, lambda record: record.update(profile="trajectory-core.controlled-family.v2"))
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.load_formal_closure(stage / "closure.json")


def test_generic_tree_apis_do_not_require_the_controlled_family_profile(lab: dict) -> None:
    """The pairing profile is a closure restriction, not a generic tree limit."""
    tree = tc.load_tree(lab["tree"])
    summary = tree.summary()
    assert summary.verification == "full"
    assert len(summary.nodes) == 3
    assert tc.validate_tree(lab["tree"])["status"] == "valid"


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
    record["schema"] = "trajectory-core.producer-closure-receipt.v3"
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


def test_receipt_scope_must_cover_every_producer_artifact(stage: Path) -> None:
    update_receipt(stage, lambda record: record["scope"]["artifacts"].pop())
    with pytest.raises(tc.ClosureError, match="producer receipt scope artifacts"):
        tc.load_formal_closure(stage / "closure.json")


def test_receipt_scope_rejects_an_extra_artifact(stage: Path) -> None:
    update_receipt(
        stage,
        lambda record: record["scope"]["artifacts"].append({"path": "ghost.json", "sha256": "0" * 64}),
    )
    with pytest.raises(tc.ClosureError, match="out of scope"):
        tc.load_formal_closure(stage / "closure.json")


def test_receipt_scope_rejects_the_receipt_and_the_closure_itself(stage: Path) -> None:
    def add_self_references(record: dict) -> None:
        record["scope"]["artifacts"].append({"path": "receipt.json", "sha256": tc.file_hash(stage / "receipt.json")})
        record["scope"]["artifacts"].append({"path": "closure.json", "sha256": "0" * 64})

    update_receipt(stage, add_self_references)
    with pytest.raises(tc.ClosureError, match="out of scope"):
        tc.load_formal_closure(stage / "closure.json")


def test_receipt_artifact_digest_must_match_the_package_bytes(stage: Path) -> None:
    update_receipt(stage, lambda record: record["scope"]["artifacts"][0].update(sha256="0" * 64))
    with pytest.raises(tc.ClosureError, match="changed"):
        tc.load_formal_closure(stage / "closure.json")


def test_receipt_artifact_paths_are_normalized_and_unique(stage: Path) -> None:
    def add_alias(record: dict) -> None:
        first = record["scope"]["artifacts"][0]
        record["scope"]["artifacts"].append({"path": "./" + first["path"], "sha256": first["sha256"]})

    update_receipt(stage, add_alias)
    with pytest.raises(tc.ClosureError, match="more than once"):
        tc.load_formal_closure(stage / "closure.json")

    def traverse(record: dict) -> None:
        record["scope"]["artifacts"][0]["path"] = "../outside.json"

    update_receipt(stage, traverse)
    with pytest.raises(tc.PathContractError):
        tc.read_producer_receipt(stage / "receipt.json")


def test_resealing_with_a_stale_receipt_after_an_outcome_edit_is_rejected(stage_factory: Any) -> None:
    package = stage_factory()
    outcome_path = package / "outcomes" / "root.json"
    record = read_json(outcome_path)
    record["provenance"]["source"] = "edited after the receipt was written"
    record["outcome_id"] = tc.outcome_id(record)
    write_json(outcome_path, record)
    (package / "closure.json").unlink()
    with pytest.raises(tc.ClosureError, match="producer receipt scope artifacts"):
        _seal(package)
    assert not (package / "closure.json").exists()


def test_resealing_with_a_stale_receipt_after_a_report_edit_is_rejected(stage_factory: Any) -> None:
    package = stage_factory()
    report_path = package / "reports" / "control-rerun.json"
    record = read_json(report_path)
    record["detail"] = {"edited_after_receipt": True}
    write_json(report_path, record)
    (package / "closure.json").unlink()
    with pytest.raises(tc.ClosureError, match="producer receipt scope artifacts"):
        _seal(package)
    assert not (package / "closure.json").exists()


def test_report_remains_the_validated_snapshot_when_receipt_bytes_change(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    receipt = stage / "receipt.json"
    record = read_json(receipt)
    record["synthetic"] = False
    write_json(receipt, record)
    report = closure.report()
    assert report["content_integrity"]["status"] == "verified"
    assert report["producer_attested_closure"]["synthetic"] is True
    assert report["producer_attested_closure"]["attested"] is False


def test_mutating_the_exposed_record_does_not_change_the_report(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    before = closure.report()
    exposed = closure.record
    exposed["producer_receipt"]["path"] = "somewhere-else.json"
    exposed["closure_id"] = "0" * 64
    exposed["tree"]["tree_id"] = "f" * 64
    exposed["profile"] = "trajectory-core.controlled-family.v99"
    assert closure.record["closure_id"] == before["closure_id"]
    assert closure.record["tree"]["tree_id"] == before["tree_id"]
    assert closure.record["profile"] == tc.CONTROLLED_FAMILY_PROFILE
    assert closure.report() == before


def test_report_is_stable_after_an_artifact_changes_on_disk(stage: Path) -> None:
    closure = tc.load_formal_closure(stage / "closure.json")
    before = closure.report()
    outcome = stage / "outcomes" / "root.json"
    outcome.write_bytes(outcome.read_bytes() + b"\n")
    assert closure.report() == before


def test_a_symlinked_closure_record_is_never_opened(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = stage.parent / "outside-closure.json"
    shutil.copyfile(stage / "closure.json", outside)
    (stage / "closure.json").unlink()
    try:
        (stage / "closure.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    reads: list[str] = []
    real_read_json = tc.closure.read_json

    def recording(path: Any) -> Any:
        reads.append(str(path))
        return real_read_json(path)

    monkeypatch.setattr(tc.closure, "read_json", recording)
    with pytest.raises(tc.PathContractError):
        tc.load_formal_closure(stage / "closure.json")
    assert reads == []


def test_a_closure_outside_an_explicit_root_is_never_opened(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = stage.parent / "outside-closure.json"
    shutil.copyfile(stage / "closure.json", outside)
    reads: list[str] = []
    real_read_json = tc.closure.read_json

    def recording(path: Any) -> Any:
        reads.append(str(path))
        return real_read_json(path)

    monkeypatch.setattr(tc.closure, "read_json", recording)
    with pytest.raises(tc.PathContractError):
        tc.load_formal_closure(outside, root=stage)
    assert reads == []


def test_a_symlinked_closure_inside_the_package_is_rejected(stage: Path) -> None:
    (stage / "closure.json").unlink()
    try:
        (stage / "closure.json").symlink_to(stage / "receipt.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    with pytest.raises(tc.PathContractError, match="symlink"):
        tc.load_formal_closure(stage / "closure.json")


def test_sealing_refuses_an_existing_destination(stage: Path) -> None:
    destination = stage / "closure.json"
    destination.write_bytes(b"competing-writer-bytes")
    with pytest.raises(tc.EvidenceError, match="already exists"):
        _seal(stage, destination)
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
        _seal(stage, stage.parent / "closure.json")


def test_sealing_detects_a_change_during_the_write(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_validate = tc.closure._validate_closure_record

    def mutate_after_validation(record: dict, root: Path, *, closure_file: Path | None = None) -> tuple:
        result = real_validate(record, root, closure_file=closure_file)
        (stage / "outcomes" / "root.json").write_bytes((stage / "outcomes" / "root.json").read_bytes() + b"\n")
        return result

    monkeypatch.setattr(tc.closure, "_validate_closure_record", mutate_after_validation)
    with pytest.raises(tc.ClosureError, match="changed"):
        _reseal(stage, destination)
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
        _reseal(stage, destination)
    assert not destination.exists()


def test_remove_owned_output_removes_the_file_it_created(stage: Path) -> None:
    destination = stage / "sealed.json"
    destination.write_bytes(b"this call's own output")
    own = os.stat(destination)
    tc.closure._remove_owned_output(destination, (own.st_dev, own.st_ino))
    assert not destination.exists()


def test_sealing_never_deletes_a_competing_writers_file(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_write = tc.closure._write_record

    def competing(target: Path, payload: bytes) -> tuple[int, int]:
        Path(target).write_bytes(b"competing-writer-bytes")
        return real_write(target, payload)

    monkeypatch.setattr(tc.closure, "_write_record", competing)
    with pytest.raises(tc.EvidenceError, match="already exists"):
        _reseal(stage, destination)
    assert destination.read_bytes() == b"competing-writer-bytes"


def test_sealing_never_deletes_a_same_payload_competing_file(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = stage / "sealed.json"
    real_write = tc.closure._write_record
    payloads: list[bytes] = []

    def competing(target: Path, payload: bytes) -> tuple[int, int]:
        payloads.append(payload)
        Path(target).write_bytes(payload)
        return real_write(target, payload)

    monkeypatch.setattr(tc.closure, "_write_record", competing)
    with pytest.raises(tc.EvidenceError, match="already exists"):
        _reseal(stage, destination)
    assert payloads
    assert destination.read_bytes() == payloads[0]


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
        _reseal(stage, destination)
    assert not destination.exists()


def test_sealing_writes_a_reloadable_record(stage: Path) -> None:
    destination = stage / "sealed.json"
    closure = _reseal(stage, destination)
    assert closure.path == destination
    assert destination.read_bytes() == tc.canonical(closure.record) + b"\n"
    reloaded = tc.load_formal_closure(destination)
    assert reloaded.closure_id == closure.closure_id
    report = reloaded.report()
    assert report["counts"]["nodes"] == 4
    assert report["counts"]["pairs"] == 2
    assert report["counts"]["producer_artifacts"] == 31
