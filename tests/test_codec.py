"""Offline tests for the gzip audit-evidence container contract (legacy closure)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from trajectory_core.legacy_lvz import evidence_codec as ec
from trajectory_core.legacy_lvz.audit_compare import AuditTail, EventStream, EvidenceError


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sample(directory: Path, rows: int = 3) -> list[str]:
    lines = [
        json.dumps({"kind": "row", "seq": index, "payload": {"value": "x" * 64, "index": index}}, sort_keys=True)
        for index in range(rows)
    ]
    for name in ("events.jsonl", "state-deltas.jsonl"):
        (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def test_containers_reproduce_plain_bytes_and_are_deterministic(tmp_path: Path) -> None:
    directory = tmp_path / "archive"
    directory.mkdir()
    lines = sample(directory)
    plain = {name: (directory / name).read_bytes() for name in ("events.jsonl", "state-deltas.jsonl")}
    receipt = ec.compress_evidence(directory)
    assert receipt["state"] == "complete"
    assert (directory / ec.RECEIPT).is_file()
    for name, raw in plain.items():
        stored = directory / (name + ec.SUFFIX)
        assert stored.is_file()
        assert not (directory / name).exists()
        assert gzip.decompress(stored.read_bytes()) == raw
        entry = receipt["files"][name]
        assert (entry["plain_sha256"], entry["plain_bytes"]) == (hashlib.sha256(raw).hexdigest(), len(raw))
        assert entry["stored_sha256"] == sha(stored)
        assert entry["stored_bytes"] < entry["plain_bytes"]
    store = ec.EvidenceStore(directory)
    assert store.compressed
    assert list(EventStream(store, "events.jsonl")) == [json.loads(line) for line in lines]
    # Deterministic containers: identical input bytes give identical output.
    other = tmp_path / "archive-two"
    other.mkdir()
    sample(other)
    ec.compress_evidence(other)
    assert (directory / ("events.jsonl" + ec.SUFFIX)).read_bytes() == (
        other / ("events.jsonl" + ec.SUFFIX)
    ).read_bytes()


def test_interrupted_compression_rolls_back_to_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "archive"
    directory.mkdir()
    sample(directory)
    original = ec._compress_file
    calls: list[str] = []

    def failing(source: Path, destination: Path) -> None:
        calls.append(source.name)
        if len(calls) > 1:
            raise RuntimeError("simulated disk failure")
        original(source, destination)

    monkeypatch.setattr(ec, "_compress_file", failing)
    with pytest.raises(RuntimeError):
        ec.compress_evidence(directory)
    assert sorted(path.name for path in directory.iterdir()) == ["events.jsonl", "state-deltas.jsonl"]
    store = ec.EvidenceStore(directory)
    assert not store.compressed
    assert store.exists("events.jsonl")


def test_tampered_container_and_undeclared_forms_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "tampered"
    directory.mkdir()
    sample(directory)
    ec.compress_evidence(directory)
    stored = directory / ("events.jsonl" + ec.SUFFIX)
    raw = bytearray(stored.read_bytes())
    raw[-5] ^= 0x40
    stored.write_bytes(bytes(raw))
    store = ec.EvidenceStore(directory)
    with pytest.raises(ValueError, match="stored audit evidence SHA-256 changed"):
        store.verify("events.jsonl")
    with pytest.raises(Exception, match="SHA-256 changed"):
        list(EventStream(store, "events.jsonl"))

    missing_receipt = tmp_path / "no-receipt"
    missing_receipt.mkdir()
    sample(missing_receipt)
    (missing_receipt / ("events.jsonl" + ec.SUFFIX)).write_bytes(gzip.compress(b"{}\n"))
    with pytest.raises(ValueError, match="lacks its codec receipt"):
        ec.EvidenceStore(missing_receipt)

    leftover = tmp_path / "leftover"
    leftover.mkdir()
    sample(leftover)
    ec.compress_evidence(leftover)
    (leftover / "events.jsonl").write_text('{"different": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="leftover plain evidence differs"):
        ec.EvidenceStore(leftover).verify("events.jsonl")

    vanished = tmp_path / "vanished"
    vanished.mkdir()
    sample(vanished)
    ec.compress_evidence(vanished)
    (vanished / ("events.jsonl" + ec.SUFFIX)).unlink()
    with pytest.raises(ValueError, match="missing audit evidence"):
        ec.EvidenceStore(vanished).open("events.jsonl")


def test_legacy_plain_archives_keep_their_contract(tmp_path: Path) -> None:
    directory = tmp_path / "plain"
    directory.mkdir()
    sample(directory)
    store = ec.EvidenceStore(directory)
    assert not store.compressed
    assert store.stored_path("events.jsonl") == directory / "events.jsonl"
    assert len(list(EventStream(store, "events.jsonl"))) == 3


def test_live_tail_refuses_a_compressed_archive(tmp_path: Path) -> None:
    directory = tmp_path / "compressed"
    directory.mkdir()
    sample(directory)
    (directory / "manifest.json").write_text(json.dumps({"schema": "lvz.audit.v1"}), encoding="utf-8")
    ec.compress_evidence(directory)
    with pytest.raises(EvidenceError, match="requires plain evidence"):
        AuditTail(directory)
