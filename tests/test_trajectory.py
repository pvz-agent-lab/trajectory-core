"""Behavior contract of the public trajectory reader."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import trajectory_core as tc
from conftest import build_bundle, read_json, write_json
from support.synthetic import RecordingDriver, SessionTrace, SyntheticRuntime


def test_manifest_identity_and_summary_are_rederived(lab: dict) -> None:
    trajectory = tc.load_trajectory(lab["plain"])
    manifest = trajectory.manifest
    summary = trajectory.summary()
    assert trajectory.trajectory_id == tc.content_identity(manifest)
    assert summary.schema == tc.TRAJECTORY_SCHEMA
    assert summary.source == "session_trace"
    assert summary.steps == len(manifest["steps"]) == 2
    assert summary.initial_version == {"epoch": 1, "tick": 0, "revision": 0}
    assert summary.end_boundary == {"epoch": 1, "tick": 5, "revision": 0}
    assert summary.branch_id is None
    assert summary.tree_id is None
    assert summary.termination.final_stop_reason == "budget_exhausted"
    assert summary.termination.executed_ticks == 5
    assert summary.termination.action_failures == 0
    assert set(manifest["files"]) == set(summary.evidence)
    assert all(len(value) == 64 for value in summary.evidence.values())
    assert trajectory.verify()["status"] == "valid"


def test_directory_and_manifest_file_are_equivalent(lab: dict) -> None:
    by_directory = tc.load_trajectory(lab["plain"])
    by_file = tc.load_trajectory(Path(lab["plain"]) / "trajectory.json")
    assert by_directory.trajectory_id == by_file.trajectory_id


def test_actions_and_stop_reasons_are_projected_without_interpretation(lab: dict) -> None:
    trajectory = tc.load_trajectory(lab["plain"])
    first, second = trajectory.summary().steps_detail
    assert first.method == "commit"
    assert first.requested_ticks == 3
    assert first.executed_ticks == 3
    assert first.stop_reason == "budget_exhausted"
    assert first.action_results == 1
    assert first.actions[0]["op"] == "plant"
    assert first.failed_action is None
    assert first.observation is not None
    assert second.method == "advance"
    assert second.executed_ticks == 2


def test_failed_action_is_visible_in_summary(tmp_path: Path) -> None:
    from support.synthetic import record_session

    trace = record_session(
        tmp_path / "failed-source",
        seed_actions=False,
        plan=lambda driver: driver.commit([{"op": "invalid"}], advance_ticks=4),
    )
    bundle = tmp_path / "failed-bundle"
    tc.build_trajectory(trace, tmp_path / "failed-source" / "audit", bundle)
    summary = tc.load_trajectory(bundle).summary()
    assert summary.termination.action_failures == 1
    assert summary.termination.final_stop_reason == "action_failed"
    assert summary.steps_detail[0].failed_action is not None


def test_unknown_schema_fails_closed(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "unknown-schema")
    manifest = read_json(bundle / "trajectory.json")
    manifest["schema"] = "lvz.engine-replay.v99"
    write_json(bundle / "trajectory.json", manifest)
    with pytest.raises(tc.UnsupportedSchemaError):
        tc.load_trajectory(bundle)


def test_tampered_evidence_byte_is_rejected(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "tampered")
    session = bundle / "session.jsonl"
    raw = bytearray(session.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    session.write_bytes(bytes(raw))
    with pytest.raises(tc.EvidenceError, match="SHA-256 mismatch"):
        tc.load_trajectory(bundle)


def test_missing_evidence_file_is_rejected(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "missing")
    (bundle / "audit" / "state-deltas.jsonl").unlink()
    with pytest.raises(tc.EvidenceError):
        tc.load_trajectory(bundle)


def test_truncated_session_tail_is_rejected(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "truncated")
    session = bundle / "session.jsonl"
    session.write_bytes(session.read_bytes() + b'{"schema": 1, "seq":')
    with pytest.raises(tc.EvidenceError):
        tc.load_trajectory(bundle)


def test_missing_manifest_is_incomplete_evidence(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "no-manifest")
    (bundle / "trajectory.json").unlink()
    with pytest.raises(tc.IncompleteEvidenceError):
        tc.load_trajectory(bundle)


def test_symlinked_manifest_escape_is_rejected(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path, "manifest-link")
    manifest = bundle / "trajectory.json"
    outside = tmp_path / "outside-manifest.json"
    shutil.copyfile(manifest, outside)
    manifest.unlink()
    try:
        manifest.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    with pytest.raises(tc.PathContractError, match="escapes its package root"):
        tc.load_trajectory(bundle)
    with pytest.raises(tc.PathContractError, match="escapes its package root"):
        tc.validate_trajectory(bundle)


def test_symlinked_evidence_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = build_bundle(tmp_path, "evidence-link")
    evidence = bundle / "session.jsonl"
    outside = tmp_path / "outside-session.jsonl"
    shutil.copyfile(evidence, outside)
    evidence.unlink()
    try:
        evidence.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    hashed: list[Path] = []
    real_hash = tc.trajectory._legacy_trajectory.file_hash

    def spy(path):
        hashed.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(tc.trajectory._legacy_trajectory, "file_hash", spy)
    with pytest.raises(tc.PathContractError, match="escapes its package root"):
        tc.load_trajectory(bundle)
    assert hashed == []


def test_symlinked_audit_directory_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = build_bundle(tmp_path, "audit-link")
    outside = tmp_path / "outside-audit"
    shutil.copytree(bundle / "audit", outside)
    shutil.rmtree(bundle / "audit")
    try:
        (bundle / "audit").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    reads: list[Path] = []
    real_read_json = tc.trajectory._legacy_trajectory.read_json

    def spy(path):
        reads.append(Path(path))
        return real_read_json(path)

    monkeypatch.setattr(tc.trajectory._legacy_trajectory, "read_json", spy)
    with pytest.raises(tc.PathContractError, match="escapes its package root"):
        tc.load_trajectory(bundle)
    assert reads == []


def test_sealing_refuses_a_recording_without_close_evidence(tmp_path: Path) -> None:
    source = tmp_path / "open-source"
    source.mkdir()
    runtime = SyntheticRuntime(source / "audit")
    with SessionTrace(source / "session.jsonl") as trace:
        driver = RecordingDriver(runtime, trace)
        driver.hello()
        driver.observe()
        driver.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=1)
        # The recording is deliberately never closed.
    runtime.close()
    output = tmp_path / "sealed"
    with pytest.raises(tc.EvidenceError):
        tc.build_trajectory(source / "session.jsonl", source / "audit", output)
    assert not output.exists()


def test_sealing_refuses_to_overwrite_an_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    trace_path = _record(source)
    output = tmp_path / "already-there"
    output.mkdir()
    (output / "keep.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        tc.build_trajectory(trace_path, source / "audit", output)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_validate_trajectory_reads_the_bundle_once(lab: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    real_load = tc.trajectory._legacy_trajectory.Trajectory.load

    def spy(path):
        calls.append(Path(path))
        return real_load(path)

    monkeypatch.setattr(tc.trajectory._legacy_trajectory.Trajectory, "load", spy)
    assert tc.validate_trajectory(lab["plain"])["status"] == "valid"
    assert len(calls) == 1
    calls.clear()
    trajectory = tc.load_trajectory(lab["plain"])
    assert trajectory.verify()["status"] == "valid"
    trajectory.verify()
    assert len(calls) == 1


def test_reading_is_read_only(lab: dict) -> None:
    def inventory(root: Path) -> dict:
        return {str(path.relative_to(root)): tc.file_hash(path) for path in sorted(root.rglob("*")) if path.is_file()}

    before = inventory(Path(lab["tree"]))
    tc.validate_tree(lab["tree"])
    tc.load_trajectory(lab["trunk"])
    assert inventory(Path(lab["tree"])) == before


def test_reads_do_not_depend_on_the_current_directory(lab: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    absolute = tc.load_trajectory(lab["plain"]).trajectory_id
    monkeypatch.chdir(Path(lab["root"]))
    relative = Path(lab["plain"]).name
    assert tc.load_trajectory(relative).trajectory_id == absolute


def test_tree_placement_does_not_change_content_identity(lab: dict) -> None:
    plain = tc.load_trajectory(lab["plain"])
    trunk = tc.load_trajectory(lab["trunk"])
    assert plain.trajectory_id == trunk.trajectory_id
    assert trunk.placement is not None
    assert trunk.placement["branch_id"] == "main"
    assert trunk.summary().tree_id == tc.load_tree(lab["tree"]).tree_id
    assert trunk.summary().root_identity is not None
    assert trunk.summary().root_trajectory_id == trunk.trajectory_id


def _record(root: Path) -> Path:
    from support.synthetic import record_session

    return record_session(root)
