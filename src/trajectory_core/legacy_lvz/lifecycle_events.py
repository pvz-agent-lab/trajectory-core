"""Offline contract and validator for the #111 lifecycle recording stream.

This module is the file-contract side of the native measurement layer. It
never imports a game, a Win32 API or the resident runtime; the native recorder
writes ``audit/lifecycle-events.jsonl`` plus an atomic
``audit/lifecycle-close-receipt.jsonl``, and this reader proves what is on
disk.

Deliberate separation of meanings:

* A trajectory without the ``lifecycle_recording`` capability is
  ``unavailable``. That is not zero events and must never be read as success.
* A trajectory whose capability is explicitly disabled is ``disabled``; a
  lifecycle file that exists without an enabled capability is refused.
* An enabled trajectory with a receipt is only ``valid`` when the receipt
  exists, the events digest and byte count match the receipt, every record
  carries the receipt's run/session/branch identity, the initialization
  contract and formal schema hold, invocation nesting is LIFO-consistent, the
  capture sequences are strictly increasing with no duplicates, and the audit
  events stream records no probe fault. Gaps are legitimate (nested
  initialization exists child-first) and are reported, never required to be
  contiguous.
* With ``require_close=False`` (a live reader) a missing receipt yields
  ``open`` instead of a failure, but the records that are present must still
  satisfy the same contract.
* The receipt is written only after the events stream was flushed, closed and
  checked, so its absence is the fail-closed answer for a crash, a failed
  write or a failed close. An in-memory ``closed`` flag is not evidence.

Only the initialization capture point exists in this delivery. The validator
reports that limit instead of inventing death/removal/recycle facts, and it
never claims the full-window first-kill gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from pathlib import Path
from typing import Any

from . import evidence_codec

MODE = "lvz.lifecycle-recording.v1"
ENVELOPE_SCHEMA = "lvz.lifecycle-record.v1"
EVENT_SCHEMA = "lvz.lifecycle-event.v1"
RECEIPT_SCHEMA = "lvz.lifecycle-close-receipt.v1"
VALIDATION_SCHEMA = "lvz.lifecycle-validation.v1"
EVENTS_FILE = "lifecycle-events.jsonl"
RECEIPT_FILE = "lifecycle-close-receipt.jsonl"
SEQUENCE_DOMAIN = "lvz.measurement.capture-sequence"
PROBE_NAME = "zombie-initialize-exit"
PROBE_SCHEMA = "lvz.spawn.v1"
KIND_INITIALIZATION = "zombie_initialized"
# Issue #111 lifecycle probes (same file, versioned records). The v2 records
# carry exact-store capture facts with raw before/after values; the v1 stream
# stays byte-compatible and older trajectories remain readable.
PROBE_MODE = "lvz.lifecycle-probes.v1"
PROBE_RECEIPT_SCHEMA = "lvz.lifecycle-close-receipt.v2"
PROBE_SET = ("zombie-phase-store", "zombie-removal-marked", "zombie-slot-recycle")
# Site id -> (kind, expected raw phase value or None for non-phase stores).
PROBE_SITE_KINDS = {
    "phase-playdeathanim": ("zombie_phase_transition", 1),
    "phase-applyburn": ("zombie_phase_transition", 2),
    "phase-mowdown": ("zombie_phase_transition", 3),
    "phase-catapult": ("zombie_phase_transition", 1),
    "phase-zamboni": ("zombie_phase_transition", 1),
    "removal-mdead": ("zombie_removal_marked", None),
    "recycle-guard": ("zombie_slot_recycle_candidate", None),
    "recycle-commit": ("zombie_slot_recycle_commit", None),
}
_PROBE_SITE_KEYS = {"id", "va", "window_bytes", "bytes", "continuation_va"}
_PROBES_CAPABILITY_KEYS = {
    "mode",
    "enabled",
    "record_schema",
    "event_schemas",
    "probe_set",
    "session_id",
    "build",
    "sites",
    "patch_windows_evidence",
    "probe_counters",
    "healthy",
    "pending_candidate",
    "active",
    "installed",
    "live_validated",
}
_PROBE_COUNTER_BASE_KEYS = {
    "captured",
    "queued",
    "delivered",
    "overflow",
    "wrong_thread",
    "inactive_suppressed",
    "classify_refused",
    "read_failed",
    "live_skips",
    "unmatched_commits",
    "pair_mismatch",
    "overwritten_pending",
    "faults",
}
_PROBE_COUNTER_KEYS = _PROBE_COUNTER_BASE_KEYS | {"persisted"}
_PROBE_HEALTH_KEYS = {
    "installed",
    "active",
    "healthy",
    "pending_candidate",
    "patched_sites",
    "counters",
    "sites",
    "reader_protected",
    "pending_callbacks",
}
_RECEIPT_V2_KEYS = {
    "schema",
    "run_id",
    "branch_id",
    "session_id",
    "sequence_domain",
    "envelope_schema",
    "event_schemas",
    "probe",
    "build",
    "manifest_sha256",
    "records",
    "first_capture_sequence",
    "last_capture_sequence",
    "bytes",
    "sha256",
    "counters",
    "probe_health",
    "completed",
    "persistence",
}
_RECEIPT_V2_COUNTER_KEYS = {"initialization", "probes", "total"}
_TOTAL_COUNTER_KEYS = {"persisted", "records", "bytes"}
PROBE_EVENT_SCHEMA = "lvz.lifecycle-event.v2"
PROBE_KINDS = (
    "zombie_phase_transition",
    "zombie_removal_marked",
    "zombie_slot_recycle_candidate",
    "zombie_slot_recycle_commit",
)
_PROBE_EVENT_KEYS = {
    "schema",
    "kind",
    "capture_sequence",
    "version",
    "version_phase",
    "engine_call_id",
    "entity",
    "object",
    "probe",
    "complete",
}
_PROBE_OBJECT_KEYS = {"class", "on_board", "wave", "board"}
_PROBE_PHASE_KEYS = {"site", "before", "after"}
_PROBE_REMOVAL_KEYS = {"source", "before", "after", "frame"}
_PROBE_REMOVAL_FRAME_KEYS = {"status", "return_into_diewithloot", "return_into_applyburn", "callsite_bytes_match"}
_PROBE_FRAME_STATUSES = {"missing", "out_of_stack", "foreign_return", "reversed", "validated"}
_PROBE_RECYCLE_CANDIDATE_KEYS = {"state", "slot", "free_head_before", "count_before"}
_PROBE_RECYCLE_COMMIT_KEYS = {"state", "slot", "candidate_capture_sequence", "free_head_after", "count_after"}
_PROBE_PROBE_KEYS = {"name", "schema", "sequence_domain"}
CAPABILITY_KEY = "lifecycle_recording"
_MAX_RECORD_BYTES = 32 << 20
# ZombieInitialize's native entry stack is bounded by kDepthLimit in
# determinism/spawn_hook.cpp. Never allocate a stack from an unchecked id.
_MAX_INVOCATION_DEPTH = 32
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ENVELOPE_KEYS = {"schema", "file_seq", "run_id", "branch_id", "session_id", "sequence_domain", "event"}
_EVENT_KEYS = {
    "schema",
    "kind",
    "capture_sequence",
    "version",
    "version_phase",
    "engine_call_id",
    "invocation",
    "entity",
    "before_after",
    "classification",
    "probe",
    "complete",
}
_VERSION_KEYS = {"epoch", "tick", "revision"}
_INVOCATION_KEYS = {"invocation_id", "depth", "parent_invocation_id"}
_ENTITY_KEYS = {"id", "slot", "generation"}
_BEFORE_AFTER_KEYS = {"before", "after"}
_AFTER_KEYS = {"id", "slot", "generation", "row0", "type", "game_clock"}
_CLASSIFICATION_KEYS = {"class", "cause"}
_EVENT_PROBE_KEYS = {"name", "schema", "sequence_domain"}
_CAPABILITY_KEYS = {
    "mode",
    "enabled",
    "event_schema",
    "envelope_schema",
    "receipt_schema",
    "sequence_domain",
    "session_id",
    "probe",
    "build",
    "files",
    "live_validated",
}
_CAPABILITY_PROBE_KEYS = {"name", "schema", "event_kind"}
_BUILD_KEYS = {"module", "sha256"}
_FILES_KEYS = {"events", "close_receipt"}
_RECEIPT_KEYS = {
    "schema",
    "run_id",
    "branch_id",
    "session_id",
    "sequence_domain",
    "envelope_schema",
    "event_schema",
    "probe",
    "build",
    "manifest_sha256",
    "records",
    "first_capture_sequence",
    "last_capture_sequence",
    "bytes",
    "sha256",
    "counters",
    "probe_health",
    "completed",
    "persistence",
}
_RECEIPT_PROBE_KEYS = {"name", "schema"}
_COUNTER_KEYS = {
    "captured",
    "delivered",
    "persisted",
    "overflow",
    "wrong_thread",
    "nesting_mismatch",
    "incomplete_events",
}
_HEALTH_KEYS = {"captured", "queued", "wrong_thread_calls", "faults", "overflow", "active_initializers", "healthy"}
_PERSISTENCE_KEYS = {"method", "receipt_written_after_close"}

# Probe-fault kinds that disqualify a lifecycle run even when its own file set
# is structurally complete: the audit stream is the authoritative statement
# that the capture probe was healthy.
_PROBE_FAULT_KIND = "spawn_hook_fault"
_PROBE_CLOSE_KIND = "spawn_hook_closed"

# The three capture points that would be needed to conclude anything about
# deaths, removals or recycling are still review_required in the phase-A
# capture table. They are listed here so every report states the limit.
_UNIMPLEMENTED_FACT_CLASSES = ("confirmed_death_stage", "removal_unclassified", "slot_recycle")


class LifecycleError(ValueError):
    """The lifecycle evidence cannot be read or does not satisfy its contract."""


def mode(manifest: dict) -> str:
    """Classify one audit manifest: enabled, disabled or unavailable.

    ``unavailable`` means the trajectory predates the capability and says
    nothing about event counts. A declared but malformed declaration (including
    an explicit null) is an error, not unavailable.
    """
    if not isinstance(manifest, dict):
        raise LifecycleError("audit manifest must be an object")
    if CAPABILITY_KEY not in manifest:
        return "unavailable"
    block = manifest[CAPABILITY_KEY]
    if not isinstance(block, dict):
        raise LifecycleError("lifecycle_recording capability must be an object when declared")
    if block.get("mode") != MODE:
        raise LifecycleError("unsupported lifecycle recording mode")
    enabled = block.get("enabled")
    if type(enabled) is not bool:
        raise LifecycleError("lifecycle_recording.enabled must be a boolean")
    if not enabled:
        if set(block) != {"mode", "enabled"}:
            raise LifecycleError("a disabled lifecycle declaration must contain only mode and enabled")
        return "disabled"
    problems: list[str] = []
    _exact_keys(block, _CAPABILITY_KEYS, "capability", problems)
    _require(
        block.get("event_schema") == EVENT_SCHEMA and block.get("envelope_schema") == ENVELOPE_SCHEMA,
        "lifecycle schema declaration mismatch",
        problems,
    )
    _require(
        block.get("receipt_schema") == RECEIPT_SCHEMA, "lifecycle close-receipt schema declaration mismatch", problems
    )
    _require(block.get("sequence_domain") == SEQUENCE_DOMAIN, "unsupported lifecycle sequence domain", problems)
    session = block.get("session_id")
    _require(type(session) is int and session > 0, "lifecycle session identity must be a positive integer", problems)
    probe = block.get("probe")
    _require(
        isinstance(probe, dict)
        and _exact_keys(probe, _CAPABILITY_PROBE_KEYS, "capability.probe", problems)
        and probe.get("name") == PROBE_NAME
        and probe.get("schema") == PROBE_SCHEMA
        and probe.get("event_kind") == KIND_INITIALIZATION,
        "lifecycle probe declaration mismatch",
        problems,
    )
    files = block.get("files")
    _require(
        isinstance(files, dict)
        and _exact_keys(files, _FILES_KEYS, "capability.files", problems)
        and files.get("events") == EVENTS_FILE
        and files.get("close_receipt") == RECEIPT_FILE,
        "lifecycle evidence file declaration mismatch",
        problems,
    )
    _require(_valid_build(block.get("build")), "lifecycle build identity declaration mismatch", problems)
    _require(type(block.get("live_validated")) is bool, "lifecycle live_validated must be a boolean", problems)
    if problems:
        raise LifecycleError("; ".join(problems))
    return "enabled"


def _valid_build(build) -> bool:
    return (
        isinstance(build, dict)
        and set(build) == _BUILD_KEYS
        and isinstance(build.get("module"), str)
        and bool(build["module"])
        and _valid_sha256(build.get("sha256"))
    )


def _valid_sha256(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _require(condition: bool, message: str, problems: list[str]) -> bool:
    if not condition:
        problems.append(message)
    return condition


def _exact_keys(value, expected: set[str], label: str, problems: list[str]) -> bool:
    if not isinstance(value, dict):
        problems.append(f"{label} must be an object")
        return False
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        problems.append(f"{label} keys differ: " + "; ".join(detail))
        return False
    return True


def _is_int(value) -> bool:
    return type(value) is int


def _present(store: evidence_codec.EvidenceStore, name: str) -> bool:
    if store.compressed:
        return store.entry(name) is not None
    return (store.directory / name).is_file()


def _read_plain(store: evidence_codec.EvidenceStore, name: str) -> bytes:
    with store.open(name) as stream:
        return stream.read()


def _record_event(record) -> dict | None:
    if not isinstance(record, dict):
        return None
    event = record.get("event")
    return event if isinstance(event, dict) else None


def _scan_probe_events(store: evidence_codec.EvidenceStore, *, allow_partial_tail: bool) -> dict:
    """Scan the audit events stream for explicit probe faults.

    The lifecycle receipt remains the success barrier; this scan proves the
    capture probe itself was healthy. A present but unreadable stream is
    reported as a problem instead of being silently ignored. Storage/container
    failures propagate to the public boundary as a contract error; only a
    live reader may tolerate a final partially flushed line.
    """
    report = {
        "present": False,
        "scanned": False,
        "fault_events": 0,
        "closed_unhealthy": False,
        "probes_closed_present": False,
        "probes_closed_payload": None,
        "probe_fault_events": 0,
        "problems": [],
    }
    if not _present(store, "events.jsonl"):
        return report
    report["present"] = True
    with store.open("events.jsonl") as stream:
        while line := stream.readline(_MAX_RECORD_BYTES + 1):
            if len(line) > _MAX_RECORD_BYTES:
                report["problems"].append("audit events.jsonl record exceeds the reader bound")
                break
            if not line.endswith(b"\n"):
                if allow_partial_tail:
                    break
                report["problems"].append("audit events.jsonl is not a complete JSONL stream")
                break
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                report["problems"].append("audit events.jsonl has an unreadable record")
                break
            if not isinstance(value, dict):
                report["problems"].append("audit events.jsonl contains a non-object record")
                break
            kind = value.get("kind")
            if kind == _PROBE_FAULT_KIND:
                report["fault_events"] += 1
            elif kind == _PROBE_CLOSE_KIND:
                payload = value.get("payload")
                if not isinstance(payload, dict) or payload.get("healthy") is not True:
                    report["closed_unhealthy"] = True
            elif kind == "lifecycle_probes_closed":
                report["probes_closed_present"] = True
                report["probes_closed_payload"] = value.get("payload")
            elif kind == "lifecycle_probe_fault":
                report["probe_fault_events"] += 1
    report["scanned"] = True
    return report


def _decode_records(data: bytes, *, allow_partial_tail: bool) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    if data and not data.endswith(b"\n"):
        if allow_partial_tail:
            boundary = data.rfind(b"\n")
            data = data[: boundary + 1] if boundary >= 0 else b""
        else:
            problems.append("lifecycle events file is truncated: the last record has no trailing newline")
    records: list[dict] = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            problems.append(f"line {number}: empty lifecycle record")
            continue
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"line {number}: lifecycle record is not valid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number}: lifecycle record must be an object")
            continue
        records.append(value)
    return records, problems


def _check_envelope(record: dict, index: int, capability: dict) -> list[str]:
    problems: list[str] = []
    label = f"line {index + 1}"
    _exact_keys(record, _ENVELOPE_KEYS, label, problems)
    _require(record.get("schema") == ENVELOPE_SCHEMA, f"{label}: envelope schema is not {ENVELOPE_SCHEMA}", problems)
    _require(
        _is_int(record.get("file_seq")) and record["file_seq"] == index,
        f"{label}: file_seq must be the written record index {index}",
        problems,
    )
    for key in ("run_id", "branch_id"):
        value = record.get(key)
        _require(isinstance(value, str) and bool(value), f"{label}: {key} must be a non-empty string", problems)
    _require(
        _is_int(record.get("session_id")) and record["session_id"] == capability.get("session_id"),
        f"{label}: session_id does not match the declared session identity",
        problems,
    )
    _require(
        record.get("sequence_domain") == SEQUENCE_DOMAIN,
        f"{label}: sequence_domain does not match the declared session identity",
        problems,
    )
    event = record.get("event")
    if not isinstance(event, dict):
        problems.append(f"{label}: event must be an object")
        return problems
    _check_event(event, label, problems)
    return problems


def _check_event(event: dict, label: str, problems: list[str]) -> None:
    if isinstance(event, dict) and event.get("schema") == PROBE_EVENT_SCHEMA:
        _check_probe_event(event, label, problems)
        return
    if not _exact_keys(event, _EVENT_KEYS, f"{label} event", problems):
        return
    _require(event.get("schema") == EVENT_SCHEMA, f"{label}: event schema is not {EVENT_SCHEMA}", problems)
    _require(
        event.get("kind") == KIND_INITIALIZATION, f"{label}: this delivery only records {KIND_INITIALIZATION}", problems
    )
    _require(
        _is_int(event.get("capture_sequence")) and event["capture_sequence"] > 0,
        f"{label}: capture_sequence must be a positive integer",
        problems,
    )
    _require(event.get("complete") is True, f"{label}: event is not marked complete", problems)
    probe = event.get("probe")
    _require(
        isinstance(probe, dict)
        and _exact_keys(probe, _EVENT_PROBE_KEYS, f"{label} event.probe", problems)
        and probe.get("name") == PROBE_NAME
        and probe.get("schema") == PROBE_SCHEMA
        and probe.get("sequence_domain") == SEQUENCE_DOMAIN,
        f"{label}: event probe declaration does not match the capability",
        problems,
    )
    _check_classification(event.get("classification"), label, problems)
    _check_version(event, label, problems)
    _check_invocation(event.get("invocation"), label, problems)
    _check_entity(event, event.get("entity"), label, problems)
    _check_before_after(event, label, problems)


def _check_probe_event(event: dict, label: str, problems: list[str]) -> None:
    """Strict contract for one v2 exact-store lifecycle fact."""
    kind = event.get("kind")
    expected = set(_PROBE_EVENT_KEYS)
    if kind == "zombie_phase_transition":
        expected.add("phase")
    elif kind == "zombie_removal_marked":
        expected.add("removal")
    else:
        expected.add("recycle")
    if not _exact_keys(event, expected, f"{label} probe event", problems):
        return
    _require(kind in PROBE_KINDS, f"{label}: unsupported probe kind {kind!r}", problems)
    _require(
        _is_int(event.get("capture_sequence")) and event["capture_sequence"] > 0,
        f"{label}: capture_sequence must be a positive integer",
        problems,
    )
    _require(event.get("complete") is True, f"{label}: event is not marked complete", problems)
    _check_version(event, label, problems)
    _require(
        event.get("version_phase") in ("controlled_boundary", "uncontrolled_update"),
        f"{label}: probe version_phase must be controlled_boundary or uncontrolled_update",
        problems,
    )
    entity = event.get("entity")
    if not _exact_keys(entity, _ENTITY_KEYS, f"{label} probe event.entity", problems):
        return
    identifier = entity.get("id")
    _require(_is_int(identifier) and identifier > 0, f"{label}: entity id must be a positive integer", problems)
    if _is_int(identifier):
        _require(entity.get("slot") == (identifier & 0xFFFF), f"{label}: entity slot must be the low 16 bits", problems)
        _require(
            entity.get("generation") == (identifier >> 16),
            f"{label}: entity generation must be the high 16 bits",
            problems,
        )
        # The engine's own delete loop treats generation 0 as a free slot, so a
        # live captured object can never carry it.
        _require(entity.get("generation") > 0, f"{label}: entity generation 0 is a free slot, not a fact", problems)
    obj = event.get("object")
    if _exact_keys(obj, _PROBE_OBJECT_KEYS, f"{label} probe event.object", problems):
        _require(obj.get("class") == "zombie", f"{label}: object class must be zombie", problems)
        _require(type(obj.get("on_board")) is bool, f"{label}: object on_board must be a boolean", problems)
        _require(_is_int(obj.get("wave")), f"{label}: object wave must be an integer", problems)
        _require(
            _is_int(obj.get("board")) and obj["board"] > 0,
            f"{label}: object board must be a live pointer value",
            problems,
        )
    probe = event.get("probe")
    _require(
        isinstance(probe, dict)
        and _exact_keys(probe, _PROBE_PROBE_KEYS, f"{label} probe event.probe", problems)
        and probe.get("name") == "zombie-lifecycle-store"
        and probe.get("schema") == PROBE_EVENT_SCHEMA
        and probe.get("sequence_domain") == SEQUENCE_DOMAIN,
        f"{label}: probe declaration does not match {PROBE_EVENT_SCHEMA}",
        problems,
    )
    if kind == "zombie_phase_transition":
        phase = event.get("phase")
        if _exact_keys(phase, _PROBE_PHASE_KEYS, f"{label} probe event.phase", problems):
            site = phase.get("site")
            binding = PROBE_SITE_KINDS.get(site)
            _require(
                binding is not None and binding[0] == kind, f"{label}: phase site is not a frozen phase store", problems
            )
            _require(
                _is_int(phase.get("before")) and _is_int(phase.get("after")),
                f"{label}: phase before/after must be raw integers",
                problems,
            )
            if binding is not None and _is_int(phase.get("after")):
                _require(
                    phase["after"] == binding[1],
                    f"{label}: phase after value does not match the locked store at {site}",
                    problems,
                )
        if _is_int(identifier):
            _require(
                not event.get("phase") or event["phase"].get("after") is not None, f"{label}: phase payload", problems
            )
    elif kind == "zombie_removal_marked":
        removal = event.get("removal")
        if _exact_keys(removal, _PROBE_REMOVAL_KEYS, f"{label} probe event.removal", problems):
            _require(
                removal.get("source") == "dienoloot_mdead_store",
                f"{label}: removal source must be the locked mDead store",
                problems,
            )
            _require(
                _is_int(removal.get("before")) and _is_int(removal.get("after")),
                f"{label}: removal before/after must be raw integers",
                problems,
            )
            _require(removal.get("after") == 1, f"{label}: mDead store writes exactly 1", problems)
            frame = removal.get("frame")
            if _exact_keys(frame, _PROBE_REMOVAL_FRAME_KEYS, f"{label} removal.frame", problems):
                status = frame.get("status")
                _require(status in _PROBE_FRAME_STATUSES, f"{label}: frame status is not a known state", problems)
                inner = frame.get("return_into_diewithloot")
                outer = frame.get("return_into_applyburn")
                for key, value in (("return_into_diewithloot", inner), ("return_into_applyburn", outer)):
                    _require(
                        value is None or (_is_int(value) and value > 0),
                        f"{label}: frame {key} must be null or a positive address",
                        problems,
                    )
                _require(
                    type(frame.get("callsite_bytes_match")) is bool,
                    f"{label}: frame callsite_bytes_match must be a boolean",
                    problems,
                )
                if status == "validated":
                    _require(
                        _is_int(inner) and _is_int(outer),
                        f"{label}: a validated frame must carry both returns",
                        problems,
                    )
                elif status == "foreign_return":
                    _require(
                        _is_int(inner) and outer is None,
                        f"{label}: a foreign return carries only the bounded first return",
                        problems,
                    )
                elif status == "reversed":
                    _require(
                        inner is None and outer is None, f"{label}: a reversed frame must not publish returns", problems
                    )
                elif status in ("missing", "out_of_stack"):
                    _require(
                        inner is None and outer is None, f"{label}: an invalid frame must not publish returns", problems
                    )
    else:
        recycle = event.get("recycle")
        expected_keys = (
            _PROBE_RECYCLE_CANDIDATE_KEYS if kind == "zombie_slot_recycle_candidate" else _PROBE_RECYCLE_COMMIT_KEYS
        )
        if _exact_keys(recycle, expected_keys, f"{label} probe event.recycle", problems):
            if kind == "zombie_slot_recycle_candidate":
                _require(recycle.get("state") == "candidate", f"{label}: candidate state mismatch", problems)
                for key in ("slot", "free_head_before", "count_before"):
                    _require(_is_int(recycle.get(key)), f"{label}: recycle {key} must be an integer", problems)
                if _is_int(recycle.get("count_before")):
                    _require(
                        recycle["count_before"] >= 1, f"{label}: candidate count_before must be positive", problems
                    )
            else:
                _require(recycle.get("state") == "committed", f"{label}: commit state mismatch", problems)
                _require(
                    _is_int(recycle.get("candidate_capture_sequence")) and recycle["candidate_capture_sequence"] > 0,
                    f"{label}: commit must reference a candidate capture sequence",
                    problems,
                )
                for key in ("slot", "free_head_after", "count_after"):
                    _require(_is_int(recycle.get(key)), f"{label}: recycle {key} must be an integer", problems)


def _check_classification(classification, label: str, problems: list[str]) -> None:
    if not _exact_keys(classification, _CLASSIFICATION_KEYS, f"{label} event.classification", problems):
        return
    _require(
        classification.get("class") == "initialization",
        f"{label}: classification class must be initialization",
        problems,
    )
    _require(
        classification.get("cause") == "unknown",
        f"{label}: classification cause must be unknown for an initialization observation",
        problems,
    )


def _check_version(event: dict, label: str, problems: list[str]) -> None:
    phase = event.get("version_phase")
    version = event.get("version")
    call_id = event.get("engine_call_id")
    if phase == "initialization":
        _require(version is None, f"{label}: initialization event must carry an explicit null version", problems)
        _require(call_id is None, f"{label}: initialization event must carry an explicit null engine_call_id", problems)
    elif phase == "controlled_boundary":
        if _exact_keys(version, _VERSION_KEYS, f"{label} event.version", problems):
            for key in ("epoch", "tick", "revision"):
                _require(
                    _is_int(version.get(key)) and version[key] >= 0,
                    f"{label}: event.version.{key} must be a non-negative integer",
                    problems,
                )
        _require(
            call_id is None or (_is_int(call_id) and call_id > 0),
            f"{label}: engine_call_id must be null or a positive integer",
            problems,
        )
    elif phase == "uncontrolled_update":
        # Gameplay stores outside a controlled call have no honest call id.
        _require(version is None, f"{label}: uncontrolled probe event must carry a null version", problems)
        _require(call_id is None, f"{label}: uncontrolled probe event must carry a null engine_call_id", problems)
    else:
        problems.append(f"{label}: version_phase must be initialization, controlled_boundary or uncontrolled_update")


def _check_invocation(invocation, label: str, problems: list[str]) -> None:
    if not _exact_keys(invocation, _INVOCATION_KEYS, f"{label} event.invocation", problems):
        return
    invocation_id = invocation.get("invocation_id")
    depth = invocation.get("depth")
    parent = invocation.get("parent_invocation_id")
    _require(
        _is_int(invocation_id) and invocation_id > 0, f"{label}: invocation_id must be a positive integer", problems
    )
    if not _require(
        _is_int(depth) and depth >= 0, f"{label}: invocation depth must be a non-negative integer", problems
    ):
        return
    if depth == 0:
        _require(parent is None, f"{label}: a top-level invocation must have a null parent_invocation_id", problems)
    else:
        _require(_is_int(parent) and parent > 0, f"{label}: a nested invocation must reference its parent", problems)
        if _is_int(parent) and _is_int(invocation_id):
            _require(
                parent < invocation_id, f"{label}: parent_invocation_id must be allocated before its child", problems
            )


def _check_entity(event: dict, entity, label: str, problems: list[str]) -> None:
    if not _exact_keys(entity, _ENTITY_KEYS, f"{label} event.entity", problems):
        return
    identifier = entity.get("id")
    if not _require(_is_int(identifier) and identifier > 0, f"{label}: entity id must be a positive integer", problems):
        return
    _require(
        _is_int(entity.get("slot")) and entity.get("slot") == (identifier & 0xFFFF),
        f"{label}: entity slot must be the low 16 bits of its id",
        problems,
    )
    _require(
        _is_int(entity.get("generation")) and entity.get("generation") == (identifier >> 16),
        f"{label}: entity generation must be the high 16 bits of its id",
        problems,
    )


def _check_before_after(event: dict, label: str, problems: list[str]) -> None:
    before_after = event.get("before_after")
    if not _exact_keys(before_after, _BEFORE_AFTER_KEYS, f"{label} event.before_after", problems):
        return
    _require(
        before_after.get("before") is None,
        f"{label}: initialization event must carry an explicit null before snapshot",
        problems,
    )
    after = before_after.get("after")
    if not _exact_keys(after, _AFTER_KEYS, f"{label} event.before_after.after", problems):
        return
    identifier = after.get("id")
    if _require(_is_int(identifier) and identifier > 0, f"{label}: after.id must be a positive integer", problems):
        _require(
            _is_int(after.get("slot")) and after.get("slot") == (identifier & 0xFFFF),
            f"{label}: after.slot must be the low 16 bits of its id",
            problems,
        )
        _require(
            _is_int(after.get("generation")) and after.get("generation") == (identifier >> 16),
            f"{label}: after.generation must be the high 16 bits of its id",
            problems,
        )
        entity = event.get("entity") if isinstance(event.get("entity"), dict) else {}
        _require(
            entity.get("id") == identifier
            and entity.get("slot") == after.get("slot")
            and entity.get("generation") == after.get("generation"),
            f"{label}: after snapshot identity must match the event entity",
            problems,
        )
    _require(_is_int(after.get("row0")), f"{label}: after.row0 must be an integer", problems)
    _require(
        _is_int(after.get("type")) and after["type"] >= 0,
        f"{label}: after.type must be a non-negative integer",
        problems,
    )
    _require(
        _is_int(after.get("game_clock")) and after["game_clock"] >= 0,
        f"{label}: after.game_clock must be a non-negative integer",
        problems,
    )


def _sequence_problems(records: list[dict]) -> tuple[list[str], dict]:
    problems: list[str] = []
    previous: int | None = None
    previous_index: int | None = None
    first = last = None
    sequences: list[int] = []
    for index, record in enumerate(records):
        event = _record_event(record)
        sequence = event.get("capture_sequence") if event else None
        if not _is_int(sequence):
            continue
        sequences.append(sequence)
        if first is None:
            first = sequence
        last = sequence
        if previous is not None and sequence <= previous:
            reason = "duplicated" if sequence == previous else "out of order"
            problems.append(
                f"line {index + 1}: capture_sequence {sequence} is {reason} "
                f"(after {previous} on line {previous_index + 1})"
            )
        previous, previous_index = sequence, index
    gaps = None if first is None else (last - first + 1) - len(sequences)
    return problems, {
        "first_capture_sequence": first,
        "last_capture_sequence": last,
        "unique_sequences": len(set(sequences)),
        "gaps": gaps,
        "rule": "strictly increasing within one run/session/sequence_domain; gaps allowed, "
        "duplicates and decreases are refused",
    }


def _probe_contract(manifest: dict, records: list[dict], *, live_prefix: bool = False) -> list[str]:
    """Validate the v2 probe capability, site binding and candidate/commit pair."""
    problems: list[str] = []
    events = [record.get("event") for record in records]
    probe_events = [event for event in events if isinstance(event, dict) and event.get("schema") == PROBE_EVENT_SCHEMA]
    block = manifest.get("lifecycle_probes")
    if block is None:
        if probe_events:
            problems.append("lifecycle probe records exist without an enabled lifecycle_probes capability")
        return problems
    if not isinstance(block, dict) or block.get("mode") != PROBE_MODE:
        problems.append("unsupported lifecycle_probes capability mode")
        return problems
    if block.get("enabled") is not True:
        if probe_events:
            problems.append("lifecycle probe records exist while the capability is disabled")
        return problems
    required_keys = {key for key in _PROBES_CAPABILITY_KEYS if key != "event_schemas"}
    missing_keys = sorted(required_keys - set(block))
    unexpected_keys = sorted(set(block) - _PROBES_CAPABILITY_KEYS)
    if missing_keys:
        problems.append(f"lifecycle_probes is missing required keys: {missing_keys}")
    if unexpected_keys:
        problems.append(f"lifecycle_probes has unexpected keys: {unexpected_keys}")
    _require(
        block.get("record_schema") == PROBE_EVENT_SCHEMA,
        "lifecycle_probes record_schema must be lvz.lifecycle-event.v2",
        problems,
    )
    # The merged native writer declares v2 through record_schema; the formal
    # event_schemas list was only emitted by the fixture. Accept the recorded
    # shape and validate event_schemas strictly when it is present.
    if "event_schemas" in block:
        schemas = block.get("event_schemas")
        _require(
            isinstance(schemas, list) and PROBE_EVENT_SCHEMA in schemas,
            "lifecycle_probes event_schemas must declare the v2 event schema",
            problems,
        )
    _require(
        block.get("probe_set") == list(PROBE_SET),
        "lifecycle_probes probe_set must match the frozen probe set",
        problems,
    )
    if not isinstance(block.get("patch_windows_evidence"), str) or not block["patch_windows_evidence"]:
        problems.append("lifecycle_probes must bind the patch-window evidence")
    if type(block.get("live_validated")) is not bool:
        problems.append("lifecycle_probes live_validated must be a boolean")
    session = block.get("session_id")
    _require(_is_int(session) and session > 0, "lifecycle_probes session_id must be a positive integer", problems)
    if not _valid_build(block.get("build")):
        problems.append("lifecycle_probes build identity is incomplete")
    lifecycle = manifest.get(CAPABILITY_KEY) if isinstance(manifest.get(CAPABILITY_KEY), dict) else {}
    if lifecycle:
        if session != lifecycle.get("session_id"):
            problems.append("lifecycle_probes session identity does not match lifecycle_recording")
        if block.get("build") != lifecycle.get("build"):
            problems.append("lifecycle_probes build identity does not match lifecycle_recording")
    sites = block.get("sites")
    if not isinstance(sites, list) or len(sites) != len(PROBE_SITE_KINDS):
        problems.append("lifecycle_probes sites must list the eight frozen windows")
    else:
        seen: set = set()
        for site in sites:
            if not isinstance(site, dict) or not _exact_keys(site, _PROBE_SITE_KEYS, "probe site", problems):
                continue
            site_id = site.get("id")
            if site.get("id") not in PROBE_SITE_KINDS:
                problems.append(f"lifecycle_probes declares an unknown site: {site_id!r}")
                continue
            if site_id in seen:
                problems.append(f"lifecycle_probes declares a duplicate site: {site_id}")
            seen.add(site_id)
            _require(_is_int(site.get("va")) and site["va"] > 0, f"probe site {site_id}: va must be positive", problems)
            size = site.get("window_bytes")
            _require(_is_int(size) and 1 <= size <= 7, f"probe site {site_id}: window_bytes out of range", problems)
            _require(
                isinstance(site.get("continuation_va"), int), f"probe site {site_id}: continuation required", problems
            )
            _require(
                isinstance(site.get("bytes"), list)
                and len(site["bytes"]) == size
                and all(_is_int(byte) and 0 <= byte <= 255 for byte in site["bytes"]),
                f"probe site {site_id}: bytes must match window_bytes",
                problems,
            )
        missing = set(PROBE_SITE_KINDS) - seen
        if missing:
            problems.append(f"lifecycle_probes is missing frozen sites: {sorted(missing)}")
    counters = block.get("probe_counters")
    if not isinstance(counters, dict) or not _exact_keys(
        counters, _PROBE_COUNTER_BASE_KEYS, "lifecycle_probes.probe_counters", problems
    ):
        problems.append("lifecycle_probes.probe_counters must be the final installed counter set")
    for key in ("healthy", "pending_candidate", "active", "installed"):
        if type(block.get(key)) is not bool:
            problems.append(f"lifecycle_probes.{key} must be a boolean")
    if not live_prefix:
        if block.get("healthy") is not True:
            problems.append("lifecycle_probes final health is not healthy")
        if block.get("pending_candidate") is not False:
            problems.append("lifecycle_probes final state still has a pending candidate")
        if block.get("active") is not False:
            problems.append("lifecycle_probes final state is still active")
    if not probe_events:
        return problems
    candidates: dict[int, dict] = {}
    committed: set = set()
    for event in probe_events:
        kind = event.get("kind")
        sequence = event.get("capture_sequence")
        if not _is_int(sequence):
            continue
        event_site = None
        if kind == "zombie_phase_transition" and isinstance(event.get("phase"), dict):
            event_site = event["phase"].get("site")
        elif kind == "zombie_removal_marked":
            event_site = "removal-mdead"
        elif kind == "zombie_slot_recycle_candidate":
            event_site = "recycle-guard"
        elif kind == "zombie_slot_recycle_commit":
            event_site = "recycle-commit"
        binding = PROBE_SITE_KINDS.get(event_site)
        _require(
            binding is not None and binding[0] == kind,
            f"capture_sequence {sequence}: event kind does not match a frozen site",
            problems,
        )
        if kind == "zombie_slot_recycle_candidate":
            counters = event.get("recycle") if isinstance(event.get("recycle"), dict) else {}
            entity = event.get("entity") if isinstance(event.get("entity"), dict) else {}
            candidates[sequence] = {
                "entity": entity.get("id"),
                "slot": counters.get("slot"),
                "board": (event.get("object") or {}).get("board"),
                "free_head_before": counters.get("free_head_before"),
                "count_before": counters.get("count_before"),
            }
        elif kind == "zombie_slot_recycle_commit":
            counters = event.get("recycle") if isinstance(event.get("recycle"), dict) else {}
            entity = event.get("entity") if isinstance(event.get("entity"), dict) else {}
            reference = counters.get("candidate_capture_sequence")
            if not _is_int(reference):
                problems.append(f"recycle commit {sequence} must reference an integer candidate sequence")
                continue
            if reference in committed:
                problems.append(f"recycle commit {sequence} reuses candidate {reference}")
            committed.add(reference)
            candidate = candidates.get(reference)
            if candidate is None:
                problems.append(f"recycle commit {sequence} references unknown candidate {reference}")
                continue
            if entity.get("id") != candidate["entity"]:
                problems.append(f"recycle commit {sequence} entity does not match its candidate")
            if counters.get("slot") != candidate["slot"]:
                problems.append(f"recycle commit {sequence} slot does not match its candidate")
            if (event.get("object") or {}).get("board") != candidate["board"]:
                problems.append(f"recycle commit {sequence} board does not match its candidate")
            if counters.get("free_head_after") != candidate["slot"]:
                problems.append(f"recycle commit {sequence} head is not the freed slot")
            if not (
                _is_int(counters.get("count_after"))
                and _is_int(candidate["count_before"])
                and counters["count_after"] + 1 == candidate["count_before"]
            ):
                problems.append(f"recycle commit {sequence} count transition does not match its candidate")
    for sequence, candidate in candidates.items():
        if sequence not in committed and not live_prefix:
            problems.append(f"recycle candidate {sequence} has no commit in this stream")
    return problems


def _nesting_problems(records: list[dict], *, strict: bool) -> list[str]:
    """Prove the invocation forest is LIFO-consistent.

    Invocation ids are allocated at call entry and every record is written at
    its exit, so a valid session's exits form a stack-pop sequence over the
    contiguous entry counter 1..N. Reconstructing that stack strictly checks
    duplicates, depth, the declared parent, parent-before-child order and
    crossed/impossible nesting at once.

    ``strict=False`` validates a live prefix: unfinished ancestors and
    not-yet-written ids are expected, so only contradictions (duplicates,
    wrong depth/parent, out-of-order pop) are rejected. A completed stream
    (receipt present, or a closed read) is always strict.
    """
    problems: list[str] = []
    invocations: list[tuple[int, int, object, object]] = []
    seen: dict[int, int] = {}
    for index, record in enumerate(records):
        event = _record_event(record)
        invocation = event.get("invocation") if event else None
        if not isinstance(invocation, dict):
            continue
        invocation_id = invocation.get("invocation_id")
        if not _is_int(invocation_id) or invocation_id <= 0:
            continue
        if invocation_id in seen:
            problems.append(
                f"line {index + 1}: duplicate invocation_id {invocation_id} (first at line {seen[invocation_id] + 1})"
            )
            continue
        seen[invocation_id] = index
        invocations.append((index, invocation_id, invocation.get("depth"), invocation.get("parent_invocation_id")))
    if not invocations:
        return problems
    if strict:
        expected_ids = list(range(1, len(invocations) + 1))
        if sorted(seen) != expected_ids:
            problems.append(
                f"invocation ids must be the contiguous session counter starting at 1; found {sorted(seen)}"
            )
            return problems
    stack: list[int] = []
    next_id = 1
    for index, invocation_id, depth, parent in invocations:
        if invocation_id >= next_id:
            if len(stack) + invocation_id - next_id + 1 > _MAX_INVOCATION_DEPTH:
                problems.append(f"line {index + 1}: invocation entry gap exceeds the native nesting bound")
                return problems
            stack.extend(range(next_id, invocation_id + 1))
            next_id = invocation_id + 1
        if not stack or stack[-1] != invocation_id:
            problems.append(f"line {index + 1}: invocation {invocation_id} exits outside its nesting order")
            return problems
        ancestors = stack[:-1]
        expected_parent = ancestors[-1] if ancestors else None
        if depth is not None and depth != len(ancestors):
            problems.append(
                f"line {index + 1}: invocation {invocation_id} depth {depth!r} does not match "
                f"its nesting depth {len(ancestors)}"
            )
        if parent != expected_parent:
            problems.append(
                f"line {index + 1}: invocation {invocation_id} parent {parent!r} does not match "
                f"the LIFO parent {expected_parent!r}"
            )
        stack.pop()
    if strict and stack:
        problems.append(f"invocation nesting is still open at end of stream: {stack}")
    return problems


def _cross_check_receipt(
    receipt,
    capability: dict,
    records: list[dict],
    events_bytes: bytes,
    manifest_bytes: bytes | None,
    problems: list[str],
    probes_block: dict | None = None,
) -> None:
    if isinstance(receipt, dict) and receipt.get("schema") == PROBE_RECEIPT_SCHEMA:
        _cross_check_receipt_v2(receipt, capability, records, events_bytes, manifest_bytes, problems, probes_block)
        return
    if not _exact_keys(receipt, _RECEIPT_KEYS, "close receipt", problems):
        return

    def require(condition: bool, message: str) -> None:
        _require(condition, message, problems)

    require(receipt.get("schema") == RECEIPT_SCHEMA, f"close receipt schema is not {RECEIPT_SCHEMA}")
    require(receipt.get("completed") is True, "close receipt is not marked completed")
    require(receipt.get("envelope_schema") == ENVELOPE_SCHEMA, "close receipt envelope schema mismatch")
    require(receipt.get("event_schema") == EVENT_SCHEMA, "close receipt event schema mismatch")
    require(receipt.get("sequence_domain") == SEQUENCE_DOMAIN, "close receipt sequence domain mismatch")
    for key in ("run_id", "branch_id"):
        value = receipt.get(key)
        require(isinstance(value, str) and bool(value), f"close receipt {key} must be a non-empty string")
    require(
        _is_int(receipt.get("session_id")) and receipt.get("session_id") == capability.get("session_id"),
        "close receipt session identity does not match the capability",
    )
    build = receipt.get("build")
    require(
        _valid_build(build) and build == capability.get("build"),
        "close receipt build identity does not match the capability",
    )
    receipt_probe = receipt.get("probe")
    require(
        isinstance(receipt_probe, dict)
        and _exact_keys(receipt_probe, _RECEIPT_PROBE_KEYS, "close receipt.probe", problems)
        and receipt_probe.get("name") == PROBE_NAME
        and receipt_probe.get("schema") == PROBE_SCHEMA,
        "close receipt probe declaration mismatch",
    )
    require(
        _is_int(receipt.get("records")) and receipt.get("records") >= 0 and receipt.get("records") == len(records),
        f"close receipt records={receipt.get('records')!r} does not match {len(records)} parsed records",
    )
    require(
        _is_int(receipt.get("bytes")) and receipt.get("bytes") == len(events_bytes),
        "close receipt byte count does not match the events file",
    )
    digest = hashlib.sha256(events_bytes).hexdigest()
    require(
        _valid_sha256(receipt.get("sha256")) and receipt.get("sha256") == digest,
        "close receipt SHA-256 does not match the events file bytes",
    )
    if manifest_bytes is not None:
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        require(
            receipt.get("manifest_sha256") == manifest_digest,
            "close receipt manifest_sha256 does not match audit/manifest.json",
        )
    else:
        require(_valid_sha256(receipt.get("manifest_sha256")), "close receipt must bind the audit manifest digest")

    sequences: list[int] = []
    for record in records:
        event = _record_event(record)
        if isinstance(event, dict):
            value = event.get("capture_sequence")
            if _is_int(value):
                sequences.append(value)
    first = sequences[0] if sequences else None
    last = sequences[-1] if sequences else None
    if receipt.get("records") == 0:
        require(
            receipt.get("first_capture_sequence") is None and receipt.get("last_capture_sequence") is None,
            "a zero-record receipt must carry null capture-sequence bounds",
        )
    else:
        require(
            _is_int(receipt.get("first_capture_sequence")) and receipt.get("first_capture_sequence") == first,
            "close receipt first capture_sequence mismatch",
        )
        require(
            _is_int(receipt.get("last_capture_sequence")) and receipt.get("last_capture_sequence") == last,
            "close receipt last capture_sequence mismatch",
        )

    counters = receipt.get("counters")
    if _exact_keys(counters, _COUNTER_KEYS, "close receipt.counters", problems):
        for key in ("captured", "delivered", "persisted"):
            require(
                _is_int(counters.get(key)) and counters.get(key) == len(records),
                f"close receipt counter {key} does not match the record count",
            )
        for key in ("overflow", "wrong_thread", "nesting_mismatch", "incomplete_events"):
            require(
                _is_int(counters.get(key)) and counters.get(key) == 0,
                f"close receipt counter {key} must be zero for a completed session",
            )
    health = receipt.get("probe_health")
    if _exact_keys(health, _HEALTH_KEYS, "close receipt.probe_health", problems):
        for key in ("faults", "overflow", "wrong_thread_calls"):
            require(_is_int(health.get(key)) and health.get(key) == 0, f"close receipt probe health {key} must be zero")
        require(
            _is_int(health.get("active_initializers")) and health.get("active_initializers") == 0,
            "close receipt probe still has an active initializer",
        )
        require(
            _is_int(health.get("queued")) and health.get("queued") == 0, "close receipt probe still has queued records"
        )
        require(health.get("healthy") is True, "close receipt probe health is not healthy")
        require(
            _is_int(health.get("captured")) and health.get("captured") == len(records),
            "close receipt probe captured count does not match the record count",
        )

    persistence = receipt.get("persistence")
    if _exact_keys(persistence, _PERSISTENCE_KEYS, "close receipt.persistence", problems):
        require(
            persistence.get("method") == "flush_close_then_atomic_receipt", "close receipt persistence method mismatch"
        )
        require(
            persistence.get("receipt_written_after_close") is True,
            "close receipt must assert it was written after the events close",
        )


def _cross_check_receipt_v2(
    receipt,
    capability: dict,
    records: list[dict],
    events_bytes: bytes,
    manifest_bytes: bytes | None,
    problems: list[str],
    probes_block: dict | None = None,
) -> None:
    """Complete close receipt for a mixed v1/v2 probe run."""
    if not _exact_keys(receipt, _RECEIPT_V2_KEYS, "close receipt v2", problems):
        return

    def require(condition: bool, message: str) -> None:
        _require(condition, message, problems)

    require(receipt.get("completed") is True, "close receipt v2 is not marked completed")
    require(receipt.get("envelope_schema") == ENVELOPE_SCHEMA, "close receipt v2 envelope schema mismatch")
    require(
        receipt.get("event_schemas") == [EVENT_SCHEMA, PROBE_EVENT_SCHEMA],
        "close receipt v2 event_schemas must list the v1 and v2 event schemas",
    )
    require(receipt.get("sequence_domain") == SEQUENCE_DOMAIN, "close receipt v2 sequence domain mismatch")
    for key in ("run_id", "branch_id"):
        value = receipt.get(key)
        require(isinstance(value, str) and bool(value), f"close receipt v2 {key} must be a non-empty string")
    require(
        _is_int(receipt.get("session_id")) and receipt.get("session_id") == capability.get("session_id"),
        "close receipt v2 session identity does not match the capability",
    )
    require(
        _valid_build(receipt.get("build")) and receipt.get("build") == capability.get("build"),
        "close receipt v2 build identity does not match the capability",
    )
    receipt_probe = receipt.get("probe")
    require(
        isinstance(receipt_probe, dict)
        and _exact_keys(receipt_probe, _RECEIPT_PROBE_KEYS, "close receipt v2.probe", problems)
        and receipt_probe.get("name") == PROBE_NAME
        and receipt_probe.get("schema") == PROBE_SCHEMA,
        "close receipt v2 probe declaration mismatch",
    )
    require(
        _is_int(receipt.get("records")) and receipt.get("records") == len(records),
        "close receipt v2 records does not match the parsed records",
    )
    require(
        _is_int(receipt.get("bytes")) and receipt.get("bytes") == len(events_bytes),
        "close receipt v2 byte count does not match the events file",
    )
    digest = hashlib.sha256(events_bytes).hexdigest()
    require(
        _valid_sha256(receipt.get("sha256")) and receipt.get("sha256") == digest,
        "close receipt v2 SHA-256 does not match the events file bytes",
    )
    if manifest_bytes is not None:
        require(
            receipt.get("manifest_sha256") == hashlib.sha256(manifest_bytes).hexdigest(),
            "close receipt v2 manifest_sha256 does not match audit/manifest.json",
        )
    sequences: list[int] = []
    kinds: dict[str, int] = {}
    for record in records:
        event = _record_event(record)
        if isinstance(event, dict):
            value = event.get("capture_sequence")
            if _is_int(value):
                sequences.append(value)
            kind = event.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1
    first = sequences[0] if sequences else None
    last = sequences[-1] if sequences else None
    if receipt.get("records") == 0:
        require(
            receipt.get("first_capture_sequence") is None and receipt.get("last_capture_sequence") is None,
            "a zero-record receipt must carry null capture-sequence bounds",
        )
    else:
        require(receipt.get("first_capture_sequence") == first, "close receipt v2 first capture_sequence mismatch")
        require(receipt.get("last_capture_sequence") == last, "close receipt v2 last capture_sequence mismatch")
    counters = receipt.get("counters")
    if not isinstance(counters, dict) or not _exact_keys(
        counters, _RECEIPT_V2_COUNTER_KEYS, "close receipt v2.counters", problems
    ):
        return
    initialization = counters.get("initialization")
    probes = counters.get("probes")
    total = counters.get("total")
    v1_count = kinds.get(KIND_INITIALIZATION, 0)
    v2_count = len(records) - v1_count
    if (
        isinstance(initialization, dict)
        and set(initialization) == {"persisted"}
        and _is_int(initialization.get("persisted"))
    ):
        require(
            initialization["persisted"] == v1_count,
            "close receipt v2 initialization persisted does not match the v1 records",
        )
    elif isinstance(initialization, dict) and set(initialization) == _COUNTER_KEYS:
        for key in ("captured", "delivered", "persisted"):
            require(_is_int(initialization.get(key)), f"close receipt v2 initialization {key} must be an integer")
        require(
            _is_int(initialization.get("captured")) and initialization.get("captured") >= v1_count,
            "close receipt v2 initialization captured is smaller than the persisted records",
        )
        require(
            _is_int(initialization.get("delivered")) and initialization.get("delivered") >= v1_count,
            "close receipt v2 initialization delivered is smaller than the persisted records",
        )
        require(
            initialization.get("persisted") == v1_count,
            "close receipt v2 initialization persisted does not match the v1 records",
        )
        for key in ("overflow", "wrong_thread", "nesting_mismatch", "incomplete_events"):
            require(initialization.get(key) == 0, f"close receipt v2 initialization counter {key} must be zero")
    else:
        problems.append("close receipt v2 initialization counters are not the recorded compact or full shape")
    if isinstance(probes, dict) and set(probes) == {"persisted"} and _is_int(probes.get("persisted")):
        require(probes["persisted"] == v2_count, "close receipt v2 probe persisted does not match the v2 records")
    elif isinstance(probes, dict) and set(probes) == _PROBE_COUNTER_KEYS:
        for key in ("captured", "delivered", "persisted"):
            require(_is_int(probes.get(key)), f"close receipt v2 probe {key} must be an integer")
        require(probes.get("persisted") == v2_count, "close receipt v2 probe persisted does not match the v2 records")
        require(probes.get("captured") == probes.get("persisted"), "close receipt v2 probe captured/persisted mismatch")
        require(
            probes.get("delivered") == probes.get("persisted"), "close receipt v2 probe delivered/persisted mismatch"
        )
        require(probes.get("queued") == 0, "close receipt v2 still has queued probe records")
        for key in (
            "overflow",
            "wrong_thread",
            "unmatched_commits",
            "pair_mismatch",
            "overwritten_pending",
            "faults",
            "read_failed",
            "classify_refused",
            "inactive_suppressed",
        ):
            require(probes.get(key) == 0, f"close receipt v2 probe counter {key} must be zero")
    else:
        problems.append("close receipt v2 probe counters are not the recorded compact or full shape")
    if isinstance(total, dict) and _exact_keys(total, _TOTAL_COUNTER_KEYS, "close receipt v2.counters.total", problems):
        require(
            total.get("records") == len(records) and total.get("persisted") == len(records),
            "close receipt v2 total record count mismatch",
        )
        require(total.get("bytes") == len(events_bytes), "close receipt v2 total byte count mismatch")
    else:
        problems.append("close receipt v2 total counters are incomplete")
    health = receipt.get("probe_health")
    if isinstance(health, dict) and _exact_keys(health, _PROBE_HEALTH_KEYS, "close receipt v2.probe_health", problems):
        require(health.get("healthy") is True, "close receipt v2 probe health is not healthy")
        require(health.get("pending_candidate") is False, "close receipt v2 still has a pending candidate")
        require(health.get("active") is False, "close receipt v2 probe capture is still active")
        health_counters = health.get("counters") if isinstance(health.get("counters"), dict) else {}
        require(
            health_counters.get("captured") == health_counters.get("delivered")
            and health_counters.get("captured") == health_counters.get("persisted")
            and health_counters.get("captured") == v2_count,
            "close receipt v2 probe health counters do not match the v2 records",
        )
        require(health.get("reader_protected") is False, "close receipt v2 reader protection must be released")
        require(health.get("pending_callbacks") == 0, "close receipt v2 must not have callbacks in flight")
        final = probes_block if isinstance(probes_block, dict) else {}
        if final.get("enabled") is True and isinstance(health.get("counters"), dict):
            health_counters = {key: value for key, value in health["counters"].items() if key != "persisted"}
            require(
                health_counters == final.get("probe_counters"),
                "close receipt v2 probe health differs from the final manifest counters",
            )
            require(
                health.get("sites") == final.get("sites"),
                "close receipt v2 probe health sites differ from the manifest",
            )
            require(
                health.get("installed") is False,
                "close receipt v2 probe health must be recorded after a successful unload",
            )
    else:
        problems.append("close receipt v2 probe health is incomplete")
    persistence = receipt.get("persistence")
    if _exact_keys(persistence, _PERSISTENCE_KEYS, "close receipt v2.persistence", problems):
        require(
            persistence.get("method") == "flush_close_then_atomic_receipt",
            "close receipt v2 persistence method mismatch",
        )
        require(
            persistence.get("receipt_written_after_close") is True,
            "close receipt v2 must be written after the events close",
        )


def _probe_health_core(health) -> object:
    """The audit close event is written before the recorder adds its record
    count to the receipt health, so that single derived field is ignored."""
    if not isinstance(health, dict):
        return health
    core = dict(health)
    counters = core.get("counters")
    if isinstance(counters, dict):
        core["counters"] = {key: value for key, value in counters.items() if key != "persisted"}
    return core


def _identity_problems(
    records: list[dict], capability: dict, receipt: dict | None, *, check_capability: bool = True
) -> list[str]:
    problems: list[str] = []
    first: dict | None = None
    for index, record in enumerate(records):
        identity = {key: record.get(key) for key in ("run_id", "branch_id", "session_id", "sequence_domain")}
        if first is None:
            first = identity
        elif identity != first:
            problems.append(f"line {index + 1}: lifecycle record mixes run/session/branch identities")
            break
    if first is not None:
        if check_capability and first["session_id"] != capability.get("session_id"):
            problems.append("lifecycle records carry a session id that does not match the capability")
        if receipt is not None:
            if first["run_id"] != receipt.get("run_id"):
                problems.append("lifecycle records carry a run id that does not match the close receipt")
            if first["branch_id"] != receipt.get("branch_id"):
                problems.append("lifecycle records carry a branch id that does not match the close receipt")
            if first["sequence_domain"] != receipt.get("sequence_domain"):
                problems.append("lifecycle records carry a sequence domain that does not match the close receipt")
    return problems


def _display(value) -> str:
    if isinstance(value, str):
        return value
    if _is_int(value):
        return str(value)
    return repr(value)


def validate(
    directory: str | Path,
    *,
    manifest: dict | None = None,
    recorder: str | Path | None = None,
    require_close: bool = True,
) -> dict:
    """Public boundary for lifecycle validation.

    Supported storage/decode failures (missing/truncated/corrupt gzip, an
    unreadable codec receipt, invalid UTF-8, unreadable JSON identity files)
    are converted to :class:`LifecycleError` so the CLI reports a contract
    error and strict readers surface ``EvidenceError``. Programming errors are
    deliberately not swallowed.
    """
    directory = Path(directory)
    try:
        return _validate(directory, manifest=manifest, recorder=recorder, require_close=require_close)
    except LifecycleError:
        raise
    except (OSError, EOFError, zlib.error, UnicodeError, json.JSONDecodeError, evidence_codec.CodecError) as exc:
        raise LifecycleError(f"lifecycle evidence is unreadable: {exc}") from exc


def load_records(directory: str | Path) -> list[dict]:
    """Decode the envelope records of an audit directory for offline analysis."""
    directory = Path(directory)
    store = evidence_codec.EvidenceStore(directory, error=LifecycleError)
    if not _present(store, EVENTS_FILE):
        return []
    data = _read_plain(store, EVENTS_FILE)
    records, _ = _decode_records(data, allow_partial_tail=False)
    return records


def _validate(directory: Path, *, manifest: dict | None, recorder: str | Path | None, require_close: bool) -> dict:
    if manifest is None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise LifecycleError(f"audit manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LifecycleError(f"audit manifest is unreadable: {exc}") from exc
    classification = mode(manifest)
    store = evidence_codec.EvidenceStore(directory, error=LifecycleError)
    block = manifest.get(CAPABILITY_KEY) or {}
    report: dict[str, Any] = {
        "schema": VALIDATION_SCHEMA,
        "status": classification,
        "directory": str(directory),
        "require_close": require_close,
        "capability": {
            "declared": CAPABILITY_KEY in manifest,
            "mode": block.get("mode"),
            "enabled": block.get("enabled"),
            "session_id": block.get("session_id"),
            "sequence_domain": block.get("sequence_domain"),
            "probe": block.get("probe"),
            "build": block.get("build"),
            "files": block.get("files"),
        },
        "records": {
            "count": None,
            "kinds": {},
            "identities": [],
            "first_capture_sequence": None,
            "last_capture_sequence": None,
            "unique_sequences": None,
            "gaps": None,
            "rule": None,
        },
        "close_receipt": {"present": False, "records": None, "sha256": None},
        "probe_events": {
            "present": False,
            "scanned": False,
            "fault_events": 0,
            "closed_unhealthy": False,
            "problems": [],
        },
        "problems": [],
        "claims": {
            "capture_table": "only zombie_initialize_exit is installed",
            "initialization_facts_only": True,
            "unimplemented_fact_classes": list(_UNIMPLEMENTED_FACT_CLASSES),
            "first_kill_gate": "unverified",
            "first_kill_proven": False,
            "note": "No death, removal or slot-recycle capture point exists yet, and call-internal "
            "ordering beyond initialization is not measured. This validator reports "
            "initialization persistence facts and never claims the full-window first kill.",
        },
    }
    if classification == "unavailable":
        stray = [name for name in (EVENTS_FILE, RECEIPT_FILE) if _present(store, name)]
        if stray:
            report["status"] = "failed"
            report["problems"].append(
                "lifecycle evidence exists but the manifest does not declare the capability: " + ", ".join(stray)
            )
            return report
        report["claims"]["note"] = (
            "This trajectory predates (or does not declare) the lifecycle "
            "recording capability. Its lifecycle state is unavailable: not zero "
            "events and not an implicit success. "
        ) + report["claims"]["note"]
        return report
    if classification == "disabled":
        stray = [name for name in (EVENTS_FILE, RECEIPT_FILE) if _present(store, name)]
        if stray:
            report["status"] = "failed"
            report["problems"].append(
                "lifecycle recording is disabled but lifecycle evidence exists: " + ", ".join(stray)
            )
        return report

    events_present = _present(store, EVENTS_FILE)
    receipt_present = _present(store, RECEIPT_FILE)
    if not events_present:
        report["status"] = "failed"
        report["problems"].append("declared lifecycle recording has no lifecycle-events.jsonl")
        return report
    events_bytes = _read_plain(store, EVENTS_FILE)
    # A live reader may see any prefix of the writer's record stream and a
    # partially flushed tail; a present receipt (or a closed read) makes the
    # file strict: complete nesting, contiguous invocation ids and no partial
    # record may be tolerated.
    live_prefix = not require_close and not receipt_present
    records, problems = _decode_records(events_bytes, allow_partial_tail=live_prefix)
    problems += [problem for index, record in enumerate(records) for problem in _check_envelope(record, index, block)]
    problems += _identity_problems(records, block, None)
    sequence_problems, sequence_summary = _sequence_problems(records)
    problems += sequence_problems
    problems += _nesting_problems(records, strict=not live_prefix)
    problems += _probe_contract(manifest, records, live_prefix=live_prefix)

    report["records"] = {
        "count": len(records),
        "kinds": _kind_counts(records),
        "identities": sorted(
            {tuple(_display(record.get(key)) for key in ("run_id", "branch_id", "session_id")) for record in records}
        ),
        **sequence_summary,
    }
    probe = _scan_probe_events(store, allow_partial_tail=live_prefix)
    report["probe_events"] = {
        key: probe[key]
        for key in (
            "present",
            "scanned",
            "fault_events",
            "closed_unhealthy",
            "probes_closed_present",
            "probe_fault_events",
            "problems",
        )
    }
    if probe["fault_events"]:
        problems.append(
            f"audit events record {probe['fault_events']} explicit probe fault(s): the lifecycle capture was unhealthy"
        )
    if probe["probe_fault_events"]:
        problems.append(
            f"audit events record {probe['probe_fault_events']} lifecycle probe fault(s): "
            "the exact-store capture was unhealthy"
        )
    if probe["closed_unhealthy"]:
        problems.append("audit spawn_hook_closed reports an unhealthy probe close")
    problems += probe["problems"]

    manifest_bytes = None
    manifest_path = store.directory / "manifest.json"
    if manifest_path.is_file():
        manifest_bytes = manifest_path.read_bytes()

    if not receipt_present:
        if require_close:
            report["status"] = "failed"
            report["problems"] = problems + [
                "lifecycle close receipt is missing: the events stream was not proven flushed and closed "
                "(crash, failed write, failed close or an aborted session)"
            ]
            return report
        report["status"] = "failed" if problems else "open"
        report["problems"] = problems
        return report
    receipt_bytes = _read_plain(store, RECEIPT_FILE)
    receipt_records, receipt_decode_problems = _decode_records(receipt_bytes, allow_partial_tail=False)
    if receipt_decode_problems:
        problems += [f"close receipt: {problem}" for problem in receipt_decode_problems]
    if len(receipt_records) != 1:
        problems.append(f"close receipt must contain exactly one record, found {len(receipt_records)}")
        receipt = receipt_records[0] if receipt_records else None
    else:
        receipt = receipt_records[0]
    report["close_receipt"] = {
        "present": True,
        "records": receipt.get("records") if receipt else None,
        "sha256": receipt.get("sha256") if receipt else None,
    }
    if receipt is not None:
        _cross_check_receipt(
            receipt, block, records, events_bytes, manifest_bytes, problems, manifest.get("lifecycle_probes")
        )
        probes_block = manifest.get("lifecycle_probes")
        if isinstance(probes_block, dict) and probes_block.get("enabled") is True:
            if not probe["probes_closed_present"]:
                problems.append("probe run has no lifecycle_probes_closed audit event")
            elif _probe_health_core(probe["probes_closed_payload"]) != _probe_health_core(receipt.get("probe_health")):
                problems.append("lifecycle_probes_closed payload does not match the close receipt probe_health")
        problems += _identity_problems(records, block, receipt, check_capability=False)
        problems += _run_identity_bindings(directory, manifest, block, receipt, recorder)
    report["problems"] = problems
    report["status"] = "valid" if not problems else "failed"
    return report


def _kind_counts(records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        event = _record_event(record)
        kind = event.get("kind") if event else None
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _run_identity_bindings(
    directory: Path, manifest: dict, capability: dict, receipt: dict, recorder: str | Path | None
) -> list[str]:
    """Bind the lifecycle identity to the run and build evidence beside it.

    The run manifest and launcher receipt already exist for experiments; when
    present they must agree with the lifecycle identity. A bare audit
    directory (offline fixture) is still validated on its own bytes.
    """
    problems: list[str] = []
    run_manifest_path = directory.parent / "manifest.json"
    if run_manifest_path.is_file():
        try:
            run_manifest = json.loads(run_manifest_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"run manifest is present but unreadable: {exc}")
            run_manifest = None
        if not isinstance(run_manifest, dict):
            problems.append("run manifest is present but is not an object")
        elif isinstance(run_manifest, dict):
            run_id = run_manifest.get("run_id")
            if isinstance(run_id, str) and run_id and run_id != receipt.get("run_id"):
                problems.append("run manifest run_id does not match the lifecycle close receipt")
            implementation = run_manifest.get("implementation")
            if isinstance(implementation, dict) and isinstance(implementation.get("recorder_sha256"), str):
                declared_build = capability.get("build") if isinstance(capability, dict) else None
                if not isinstance(declared_build, dict) or implementation["recorder_sha256"] != declared_build.get(
                    "sha256"
                ):
                    problems.append(
                        "run manifest implementation.recorder_sha256 does not match the "
                        "declared lifecycle build identity"
                    )
    launcher_path = directory.parent / "launcher.json"
    if launcher_path.is_file():
        try:
            launcher = json.loads(launcher_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"launcher evidence is present but unreadable: {exc}")
            launcher = None
        if not isinstance(launcher, dict):
            problems.append("launcher evidence is present but is not an object")
        elif isinstance(launcher, dict) and isinstance(launcher.get("branch_id"), str):
            if launcher["branch_id"] != receipt.get("branch_id"):
                problems.append("launcher branch identity does not match the lifecycle close receipt")
    if recorder is not None:
        path = Path(recorder)
        if not path.is_file():
            problems.append(f"recorder binary is missing: {path}")
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            declared_build = capability.get("build") if isinstance(capability, dict) else None
            if not isinstance(declared_build, dict) or digest != declared_build.get("sha256"):
                problems.append("recorder binary does not match the declared lifecycle build identity")
    return problems
