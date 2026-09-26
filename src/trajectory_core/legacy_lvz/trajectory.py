"""The read-and-package closure of the legacy ``lvz.engine-replay.v1`` format.

This module is a reduced adaptation of ``src/llm_vs_zombies/engine_replay.py``
at commit 11917a7 (see ``NOTICES.md``). It keeps exactly the old reader and
packager:

* ``Trajectory.load`` -- fully re-derive a packaged trajectory: manifest
  identity, evidence file hashes, the closed native audit and the session
  trace, then every recorded step against both.
* ``build_trajectory`` -- package one already closed recording; it refuses an
  existing output directory and never edits the source.
* the initial-marker and step validators those two share.

Everything that touches a live runtime (``replay``, ``ReplaySession``,
``demo``, the CLI) was dropped: this closure never launches a game, opens a
socket or imports ``llm_vs_zombies.client``.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import app_update_anchor, evidence_codec, fp_environment, mj_clock_anchor, sound_counter, sound_effects
from . import b0_normalization as b0
from .audit_compare import (
    DRAW_SCHEDULE_MODE,
    AuditLog,
    EvidenceError,
    audit_files,
    canonical,
    digests,
    draw_mode,
    engine_call_mode,
    file_hash,
    first_difference,
    jsonl,
    read_json,
    validate_engine_origin,
    version,
)
from .audit_compare import SCHEMA as AUDIT_SCHEMA
from .client_branch import BranchScopeError, declared_branch_scope
from .evidence_tree import TreePlacement, attach_tree, content_identity, validate_embedded_tree

SCHEMA = "lvz.engine-replay.v1"
READ_ONLY = {"hello", "observe", "status", "audit_snapshot"}
STEP_METHODS = {"commit", "advance"}
INTERVENTIONS = STEP_METHODS | {"capture_frame", "pause"}


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class TraceSink(Protocol):
    """The one method ``capture_initial`` uses on a recording trace."""

    def emit(self, kind: str, data: Any) -> dict: ...


class RecordingSession(Protocol):
    """The duck-typed surface ``capture_initial`` needs from a live recorder.

    The core package never implements this protocol; tests and the future
    rollout package provide it. Keeping it here documents the exact calls the
    old ``Client`` had to answer without importing the socket client.
    """

    trace: TraceSink

    def hello(self) -> dict: ...

    def observe(self) -> dict: ...

    def request(self, method: str) -> dict: ...


def _artifacts(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise EvidenceError("nonempty artifact SHA-256 identity is required")
    for item in value.values():
        if isinstance(item, dict):
            _artifacts(item)
        elif not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None:
            raise EvidenceError("artifact identity must contain lowercase SHA-256 values")


def identity_from_launcher(hello: dict, launcher: dict) -> dict:
    """Bind game/runtime/profile assets as well as runtime-advertised identity."""
    artifacts = {key: launcher[key] for key in ("input_hashes", "module_hashes", "profile_hashes")}
    if "resource_hashes" in launcher:
        artifacts["resource_hashes"] = launcher["resource_hashes"]
    _artifacts(artifacts)
    if not isinstance(hello.get("build"), dict) or not isinstance(hello.get("game"), dict):
        raise EvidenceError("hello lacks build/game identity")
    sound_effects.negotiate(hello)
    b0.negotiate(hello)
    app_update_anchor.negotiate(hello)
    mj_clock_anchor.negotiate(hello)
    sound_counter.negotiate(hello)
    fp_environment.negotiate(hello)
    return {
        "build": copy.deepcopy(hello["build"]),
        "game": copy.deepcopy(hello["game"]),
        "artifacts": copy.deepcopy(artifacts),
    }


def capture_initial(client: RecordingSession, *, identity: dict, initialization: dict) -> dict:
    """Call after initialization, before the first experiment action.

    The marker and state are captured from the live client. The caller supplies
    checked launcher artifact hashes; these must never be copied from a desired
    trajectory when describing a newly launched process.
    """
    hello = client.hello()
    sound_effects.negotiate(hello)
    b0.negotiate(hello)
    app_update_anchor.negotiate(hello)
    mj_clock_anchor.negotiate(hello)
    sound_counter.negotiate(hello)
    fp_environment.negotiate(hello)
    _validate_identity(identity)
    if hello.get("build") != identity["build"] or hello.get("game") != identity["game"]:
        raise EvidenceError("launcher/runtime identity mismatch")
    if hello.get("capabilities", {}).get("audit_snapshot") is not True:
        raise EvidenceError("runtime does not provide audit_snapshot")
    observation = client.observe()
    snapshot = client.request("audit_snapshot")
    if snapshot.get("version") != observation["version"]:
        raise EvidenceError("initial observation and state are from different boundaries")
    marker = {
        "schema": SCHEMA,
        "identity": copy.deepcopy(identity),
        "initialization": copy.deepcopy(initialization),
        "observation": observation,
        "state": snapshot["state"],
    }
    branch = declared_branch_scope(hello)
    if branch is not None:
        # The runtime's own scope, so a later replay can prove whether it
        # continued this branch or started a different one.
        marker["branch"] = copy.deepcopy(branch)
    if engine_call_mode(identity["game"]):
        marker["engine_call_origin"] = copy.deepcopy(snapshot.get("engine_call"))
    fp_evidence = fp_environment.evidence(snapshot, identity["game"])
    if fp_evidence is not None:
        marker["fixed_fp_origin"] = copy.deepcopy(fp_evidence)
    _validate_initial(marker)
    client.trace.emit("replay_initial", marker)
    return marker


def _validate_identity(identity: dict) -> None:
    if not isinstance(identity, dict) or set(identity) != {"build", "game", "artifacts"}:
        raise EvidenceError("replay identity requires build, game, and artifacts")
    _artifacts(identity["artifacts"])
    if not identity["build"] or identity["build"].get("runtime_protocol") != 1:
        raise EvidenceError("unsupported runtime build identity")
    game = identity["game"]
    if game.get("schema") != AUDIT_SCHEMA or game.get("loaded_signatures_match") is not True:
        raise EvidenceError("unsupported game target identity")
    draw_mode(game)
    engine_call_mode(game)
    sound_effects.artifacts(game, identity["artifacts"])
    b0.mode(game)
    app_update_anchor.mode(game)
    mj_clock_anchor.mode(game)
    sound_counter.mode(game)
    fp_environment.mode(game)


def _validate_initial(initial: dict) -> None:
    if initial.get("schema") != SCHEMA:
        raise EvidenceError("initial marker schema mismatch")
    _validate_identity(initial.get("identity"))
    if "branch" in initial:
        try:
            branch = declared_branch_scope({"branch": initial["branch"]})
        except BranchScopeError as error:
            raise EvidenceError(f"initial marker branch scope is invalid: {error}") from error
        if branch is None:
            raise EvidenceError("initial marker branch scope is empty")
    observation = initial.get("observation", {})
    if version(observation.get("version"))["tick"] != 0:
        raise EvidenceError("replay must begin at controller tick zero; this is not snapshot restore")
    if observation.get("game_ui") != 3 or observation.get("scene") is None:
        raise EvidenceError("initial marker must describe a ready fight board")
    if not isinstance(initial.get("initialization"), dict) or not initial["initialization"]:
        raise EvidenceError("initialization recipe is required")
    if not isinstance(initial.get("state"), dict) or initial["state"].get("schema") != AUDIT_SCHEMA:
        raise EvidenceError("initial full audit_snapshot is required")
    if initial["state"].get("rng", {}).get("target") != initial["identity"]["game"].get("target"):
        raise EvidenceError("initial RNG/game target mismatch")
    mode = draw_mode(initial["identity"]["game"])
    if mode:
        expected = {"mode": mode, "warm_frames": 1, "step_frames": 0}
        if (
            initial["initialization"].get("execution_mode") != mode
            or initial["initialization"].get("draw_schedule") != initial["identity"]["game"]["draw_schedule"]
            or observation.get("render_prepared") is not True
            or first_difference(expected, initial["state"].get("draw_schedule"))
        ):
            raise EvidenceError("initial marker must declare the prepared controlled draw mode")
    elif "draw_schedule" in initial["state"] or initial["initialization"].get("execution_mode") == DRAW_SCHEDULE_MODE:
        raise EvidenceError("initial controlled drawing lacks an explicit engine identity")
    if engine_call_mode(initial["identity"]["game"]):
        validate_engine_origin(initial.get("engine_call_origin"))
    elif "engine_call_origin" in initial:
        raise EvidenceError("engine call origin lacks an explicit engine identity")
    sound_effects.initial(initial)
    b0.initial(initial)
    app_update_anchor.initial(initial)
    mj_clock_anchor.initial(initial)
    sound_counter.initial(initial)
    fp_environment.initial(initial)
    digests(initial["state"])


def _validate_capture_mode(response, before, after, mode):
    result = response.get("result", {})
    if not mode:
        if result.get("mode") == DRAW_SCHEDULE_MODE or result.get("method") == "cached_controlled_engine_frame":
            raise EvidenceError("cached controlled capture lacks an explicit engine identity")
        return
    if after != before:
        raise EvidenceError("cached capture changed the simulation boundary")
    if response.get("ok") is not True:
        return  # An explicit RPC failure has no invented frame receipt.
    if result.get("forced_render") is not False:
        raise EvidenceError("controlled capture must read its cache without drawing")
    if result.get("capture_ok"):
        if (
            result.get("mode") != mode
            or result.get("method") != "cached_controlled_engine_frame"
            or result.get("frame_version") != before
            or result.get("version") != before
            or result.get("known_rng_unchanged") is not True
            or type(result.get("game_clock_before")) is not int
            or result.get("game_clock_before") != result.get("game_clock_after")
            or result.get("width") != 800
            or result.get("height") != 600
            or result.get("pixel_format") != "bgr24"
        ):
            raise EvidenceError("cached capture receipt is stale or lacks its read-only guards")


def _capture_response(response: dict) -> tuple[dict, dict | None]:
    """Normalize capture evidence without treating image equality as state equality.

    SessionTrace may retain only a raw-pixel hash/length, while the Client's live
    response still contains base64. Both produce the same metadata contract.
    """
    if response.get("ok") is False:
        error = response.get("error")
        if not isinstance(error, dict) or not all(isinstance(error.get(k), str) for k in ("code", "message")):
            raise EvidenceError("capture error response is malformed")
        return {"ok": False, "error": copy.deepcopy(error)}, None
    result = response.get("result")
    if response.get("ok") is not True or not isinstance(result, dict) or type(result.get("capture_ok")) is not bool:
        raise EvidenceError("capture response must explicitly report capture_ok")
    metadata = copy.deepcopy(result)
    raw = metadata.pop("pixels_base64", None)
    evidence = metadata.pop("pixels_evidence", None)
    metadata.pop("trace_metadata_only", None)
    if raw is not None:
        if not isinstance(raw, str):
            raise EvidenceError("capture pixel payload must be base64 text")
        try:
            pixels = base64.b64decode(raw, validate=True)
        except ValueError as error:
            raise EvidenceError("capture pixel payload is invalid base64") from error
        decoded = {"sha256": hashlib.sha256(pixels).hexdigest(), "byte_length": len(pixels)}
        if evidence is not None and evidence != decoded:
            raise EvidenceError("capture pixel payload and hash evidence disagree")
        evidence = decoded
    if evidence is not None:
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"sha256", "byte_length"}
            or not isinstance(evidence["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None
            or type(evidence["byte_length"]) is not int
            or evidence["byte_length"] < 0
        ):
            raise EvidenceError("invalid capture pixel hash/length evidence")
    if metadata["capture_ok"] and evidence is None:
        raise EvidenceError("successful capture needs pixels or lightweight pixel evidence")
    version(metadata.get("version"))
    for name in ("forced_render", "used_3d", "known_rng_unchanged", "state_checked"):
        if name in metadata and type(metadata[name]) is not bool:
            raise EvidenceError(f"capture {name} must be boolean")
    if metadata.get("forced_render") is True:
        if type(metadata.get("known_rng_unchanged")) is not bool or any(
            type(metadata.get(name)) is not int for name in ("game_clock_before", "game_clock_after")
        ):
            raise EvidenceError("forced render is missing known-RNG/clock guard evidence")
    return {"ok": True, "result": metadata}, copy.deepcopy(evidence)


def _step_end_version(step: dict) -> dict:
    return (
        step["after_version"]
        if step["request"]["method"] == "capture_frame"
        else step["result"]["observation"]["version"]
    )


def _trace_steps(path: Path) -> tuple[dict, list[dict]]:
    initial, pending, steps = None, None, []
    stopped = False
    ids = set()
    capture_errors = set()
    last_capture = None
    last_pause = None
    for seq, event in enumerate(jsonl(path)):
        if (
            type(event.get("schema")) is not int
            or event["schema"] != 1
            or type(event.get("seq")) is not int
            or event["seq"] != seq
        ):
            raise EvidenceError("SessionTrace schema/sequence mismatch")
        kind, data = event.get("kind"), event.get("data")
        if kind == "replay_initial":
            if initial is not None or pending:
                raise EvidenceError("exactly one initial marker at a completed boundary is required")
            _validate_initial(data)
            initial = data
        elif kind == "request":
            if pending is not None:
                raise EvidenceError("overlapping requests are not supported by sequential replay")
            if not isinstance(data, dict) or type(data.get("protocol")) is not int or data["protocol"] != 1:
                raise EvidenceError("invalid request protocol")
            request_id = data.get("request_id")
            if not isinstance(request_id, str) or not request_id or request_id in ids:
                raise EvidenceError("missing/duplicate source request ID")
            ids.add(request_id)
            if initial is not None and data.get("method") not in READ_ONLY | INTERVENTIONS | {"stop_recording"}:
                raise EvidenceError(f"unsupported intervening mutation: {data.get('method')}")
            if initial is not None and stopped and data.get("method") not in READ_ONLY:
                raise EvidenceError("mutation after recording was closed")
            if data.get("method") in INTERVENTIONS | {"stop_recording"}:
                if last_pause is not None and "state_after" not in last_pause:
                    raise EvidenceError("pause requires a post-control audit snapshot before any later mutation")
                last_pause = None
                last_capture = None
            pending = data
        elif kind == "response":
            if pending is None or not isinstance(data, dict) or data.get("request_id") != pending["request_id"]:
                raise EvidenceError("response has no matching request")
            if type(data.get("protocol")) is not int or data["protocol"] != 1:
                raise EvidenceError("response protocol mismatch")
            if initial is not None:
                if pending["method"] == "capture_frame":
                    capture_response, pixels = _capture_response(data)
                    step = {"request": pending, "capture_response": capture_response, "pixels_evidence": pixels}
                    if capture_response["ok"]:
                        step["after_version"] = copy.deepcopy(capture_response["result"]["version"])
                    else:
                        capture_errors.add(pending["request_id"])
                    steps.append(step)
                    last_capture = step
                elif data.get("ok") is not True or not isinstance(data.get("result"), dict):
                    raise EvidenceError(
                        "source request did not complete successfully; action failures must be in action_results"
                    )
                elif pending["method"] in STEP_METHODS:
                    steps.append({"request": pending, "result": data["result"]})
                elif pending["method"] == "pause":
                    if (
                        not steps
                        or steps[-1]["request"]["method"] not in STEP_METHODS
                        or steps[-1]["result"].get("executed_ticks", 0) <= 0
                    ):
                        raise EvidenceError("pause replay requires an already completed advancement")
                    last_pause = {"request": pending, "result": data["result"]}
                    steps.append(last_pause)
                elif pending["method"] == "stop_recording":
                    if data["result"].get("closed") is not True:
                        raise EvidenceError("source recording did not close")
                    stopped = True
                elif last_capture is not None and pending["method"] in {"observe", "audit_snapshot"}:
                    result = data["result"]
                    actual_version = version(result.get("version"))
                    if "after_version" in last_capture and last_capture["after_version"] != actual_version:
                        raise EvidenceError("capture response and subsequent boundary disagree")
                    last_capture["after_version"] = copy.deepcopy(actual_version)
                    if pending["method"] == "observe":
                        last_capture.setdefault("observation_after", result)
                    else:
                        if not isinstance(result.get("state"), dict) or result["state"].get("schema") != AUDIT_SCHEMA:
                            raise EvidenceError("capture post-state snapshot is malformed")
                        last_capture.setdefault("state_after", result["state"])
                elif last_pause is not None and pending["method"] == "audit_snapshot":
                    result = data["result"]
                    if (
                        result.get("version") != last_pause["result"].get("observation", {}).get("version")
                        or not isinstance(result.get("state"), dict)
                        or result["state"].get("schema") != AUDIT_SCHEMA
                    ):
                        raise EvidenceError("pause post-state snapshot is missing its unchanged boundary")
                    if "state_after" in last_pause and last_pause["state_after"] != result["state"]:
                        raise EvidenceError("game state changed while paused")
                    last_pause["state_after"] = result["state"]
            pending = None
        elif initial is not None and kind in {"invalid_response", "exception"}:
            if (
                kind == "exception"
                and isinstance(data, dict)
                and data.get("request_id") in capture_errors
                and data.get("type") == "RemoteError"
                and data.get("outcome_unknown") is False
            ):
                capture_errors.remove(data["request_id"])
                continue
            raise EvidenceError("source trace contains an uncertain or rejected outcome")
    if initial is None or pending is not None or not stopped:
        raise EvidenceError("source needs initial marker, completed requests, and stop_recording")
    return initial, steps


def _native_steps(audit: AuditLog) -> list[dict]:
    steps, pending = [], None
    for event in audit.control_events:
        payload = event["payload"]
        if event["kind"] == "request_started":
            if pending is not None:
                raise EvidenceError("overlapping native requests")
            pending = payload["request"]
            if pending.get("request_id") != payload.get("request_id"):
                raise EvidenceError("native request identity mismatch")
        elif event["kind"] == "request_completed":
            if pending is None or pending["request_id"] != payload.get("request_id"):
                raise EvidenceError("unmatched native request completion")
            steps.append({"request": pending, "result": payload["result"]})
            pending = None
    if pending:
        raise EvidenceError("unfinished native request")
    return steps


def _terminal_event(audit: AuditLog, request_id: str, *, required: bool) -> dict | None:
    request_events = audit.request_events(request_id)
    events = [event for event in request_events if event["kind"] == "terminal_transition"]
    if not required:
        if events:
            raise EvidenceError("terminal transition without scene_changed result")
        return None
    if len(events) != 1:
        raise EvidenceError("scene_changed requires exactly one terminal_transition")
    event = events[0]
    payload = event["payload"]
    zero = (
        engine_call_mode(audit.manifest)
        and payload.get("transition_kind") == "terminal_zero_clock_update"
        and payload.get("terminal_call_verified") is True
        and payload.get("clock_delta_measured") is True
    )
    if (
        payload.get("tick_delta_verified") is not (False if zero else True)
        or payload.get("board_identity_preserved") is not True
        or type(payload.get("native_tick_delta")) is not int
        or payload["native_tick_delta"] != (0 if zero else 1)
    ):
        raise EvidenceError("terminal transition cannot prove one actual step on the original Board")
    frames = audit.request_headers(request_id)
    completions = [item for item in request_events if item["kind"] == "request_completed"]
    if (
        not frames
        or len(completions) != 1
        or frames[-1].kind != "post_step"
        or not frames[-1].seq < event["seq"] < completions[0]["seq"]
        or event["version"] != frames[-1].version
    ):
        raise EvidenceError("terminal transition is missing its final post_step/completion boundary")
    return event


def _validate_native_details(audit: AuditLog, request: dict, result: dict) -> None:
    rid, before = request["request_id"], request["expect"]
    selected = audit.request_events(rid)
    starts = [event for event in selected if event["kind"] == "request_started"]
    ends = [event for event in selected if event["kind"] == "request_completed"]
    recorded = starts[0]["payload"].get("request") if len(starts) == 1 else None
    if (
        len(starts) != 1
        or len(ends) != 1
        or starts[0]["version"] != before
        or not isinstance(recorded, dict)
        or _request_content(recorded) != _request_content(request)
        or ends[0]["payload"].get("result") != result
        or ends[0]["version"] != result["observation"]["version"]
    ):
        raise EvidenceError("native request/completion identity, result, or version mismatch")
    actions = [event for event in selected if event["kind"] == "action"]
    if len(actions) != len(result["action_results"]):
        raise EvidenceError("native action evidence count mismatch")
    for ordinal, (event, outcome) in enumerate(zip(actions, result["action_results"])):
        wanted = {
            "request_id": rid,
            "ordinal": ordinal,
            "action": request["params"]["actions"][ordinal],
            "result": outcome,
        }
        expected_version = dict(before, revision=before["revision"] + ordinal + 1)
        if (
            event["payload"] != wanted
            or event["version"] != expected_version
            or not starts[0]["seq"] < event["seq"] < ends[0]["seq"]
        ):
            raise EvidenceError("native action attempts, outcomes, or frame order mismatch")


def _request_content(request: dict) -> dict:
    """The branch scope is a namespace claim, never part of request content."""
    return {key: value for key, value in request.items() if key != "branch"}


RECORDER_SIDE_GAME_KEYS = ("lifecycle_recording", "lifecycle_probes")


def _game_identity(manifest: dict) -> dict:
    """The game identity excludes recorder-side capability declarations.

    ``lifecycle_recording``/``lifecycle_probes`` are written by the recorder
    adapter into its own audit manifest; the launcher hello cannot declare
    them and they are validated by the lifecycle contract instead.
    """
    if not isinstance(manifest, dict):
        return manifest
    return {key: value for key, value in manifest.items() if key not in RECORDER_SIDE_GAME_KEYS}


def _validate_steps(initial: dict, steps: list[dict], audit: AuditLog) -> None:
    if not steps:
        raise EvidenceError("trajectory has no executed requests")
    if _game_identity(audit.manifest) != initial["identity"]["game"]:
        raise EvidenceError("native audit target/coverage identity differs from hello")
    audit.validate_draw_initial(initial)
    audit.validate_audio_initial(initial)
    audit.validate_app_anchor_initial(initial)
    audit.validate_sound_counter_initial(initial)
    audit.validate_fp_initial(initial)
    mode = draw_mode(audit.manifest)
    call_mode = engine_call_mode(audit.manifest)
    if _native_steps(audit) != [step for step in steps if step["request"]["method"] in STEP_METHODS]:
        raise EvidenceError("native authoritative requests/results differ from source trajectory")
    current = initial["observation"]["version"]
    request_ids = set()
    pause_state_checks = {}
    for step_index, step in enumerate(steps):
        req, result = step["request"], step.get("result")
        request_id = req.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256 or request_id in request_ids:
            raise EvidenceError("native request ID is missing, oversized or duplicated")
        request_ids.add(request_id)
        if type(req.get("protocol")) is not int or req["protocol"] != 1 or req.get("method") not in INTERVENTIONS:
            raise EvidenceError("trajectory skipped a mutation or has stale request version")
        if req["method"] != "pause" or "expect" in req:
            version(req.get("expect"))
        params = req.get("params")
        if not isinstance(params, dict):
            raise EvidenceError("request params must be an object")
        method = req["method"]
        if method == "pause":
            before = steps[step_index - 1] if step_index else None
            if (
                params != {}
                or ("expect" in req and req["expect"] != current)
                or before is None
                or before["request"]["method"] not in STEP_METHODS
                or before["result"]["executed_ticks"] <= 0
                or not isinstance(result, dict)
                or result.get("state") != "paused_at_boundary"
                or result.get("observation") != before["result"]["observation"]
                or result["observation"].get("version") != current
                or not isinstance(step.get("state_after"), dict)
            ):
                raise EvidenceError("pause is not a proven no-op at a completed paused boundary")
            if audit.request_headers(request_id) or audit.request_events(request_id):
                raise EvidenceError("no-op pause unexpectedly created native actions or steps")
            previous_frames = audit.request_headers(before["request"]["request_id"])
            if not previous_frames or previous_frames[-1].kind != "post_step":
                raise EvidenceError("pause is missing the previous completed audit boundary")
            pause_state_checks[previous_frames[-1].seq] = step["state_after"]
            continue
        if method == "capture_frame":
            response = step["capture_response"]
            after = version(step.get("after_version"))
            _validate_capture_mode(response, current, after, mode)
            if (
                after["epoch"] != current["epoch"]
                or after["tick"] != current["tick"]
                or after["revision"] not in {current["revision"], current["revision"] + 1}
            ):
                raise EvidenceError("capture is missing an actual bounded post-response boundary")
            if response["ok"]:
                if req["expect"] != current or response["result"]["version"] != after:
                    raise EvidenceError("completed capture used a stale or inconsistent version")
                if response["result"]["capture_ok"] and after != current:
                    raise EvidenceError("successful capture silently changed the boundary")
            if "observation_after" in step and step["observation_after"]["version"] != after:
                raise EvidenceError("capture post-observation disagrees with actual boundary")
            if "state_after" in step:
                digests(step["state_after"])
            if audit.request_headers(request_id):
                raise EvidenceError("capture cannot be represented as simulated game steps")
            current = after
            continue
        if req["expect"] != current:
            raise EvidenceError("trajectory skipped a mutation or has stale request version")
        count = params.get("max_ticks" if method == "advance" else "advance_ticks")
        if (
            type(count) is not int
            or not 0 <= count <= 100000
            or type(result.get("requested_ticks")) is not int
            or result["requested_ticks"] != count
        ):
            raise EvidenceError("requested tick count mismatch")
        executed = result.get("executed_ticks")
        if type(executed) is not int or not 0 <= executed <= count:
            raise EvidenceError("invalid executed tick count")
        outcomes, actions = result.get("action_results"), params.get("actions", [])
        if not isinstance(outcomes, list) or not isinstance(actions, list) or len(outcomes) > len(actions):
            raise EvidenceError("invalid action outcome count")
        if method == "advance" and (actions or outcomes):
            raise EvidenceError("advance cannot contain actions")
        failed = False
        for ordinal, outcome in enumerate(outcomes):
            if (
                not isinstance(outcome, dict)
                or type(outcome.get("ok")) is not bool
                or outcome.get("ordinal") != ordinal
                or outcome.get("action") != actions[ordinal]
                or failed
            ):
                raise EvidenceError("action order/identity/result mismatch")
            failed = outcome["ok"] is False
        if failed:
            if executed or result.get("stop_reason") != "action_failed":
                raise EvidenceError("failed action must stop before advancement")
        elif len(outcomes) != len(actions):
            raise EvidenceError("successful trajectory is missing action outcomes")
        if not failed and result.get("stop_reason") not in {"budget_exhausted", "wave_changed", "scene_changed"}:
            raise EvidenceError("source terminated for an unsupported external condition")
        terminal = result.get("stop_reason") == "scene_changed"
        _terminal_event(audit, req["request_id"], required=terminal)
        terminal_zero = call_mode and result.get("terminal_zero_clock_calls") == 1
        if terminal and (
            step_index != len(steps) - 1
            or (executed == 0 and not terminal_zero)
            or result.get("observation", {}).get("game_ui") == 3
        ):
            raise EvidenceError("verified scene transition must end the final request after an actual step")
        if result.get("stop_reason") == "budget_exhausted" and executed != count:
            raise EvidenceError("budget was not actually exhausted")
        after = version(result.get("observation", {}).get("version"))
        wanted = {
            "epoch": current["epoch"],
            "tick": current["tick"] + executed,
            "revision": 0 if executed else current["revision"] + len(outcomes),
        }
        if after != wanted:
            raise EvidenceError("actual source version does not match execution")
        _validate_native_details(audit, req, result)
        frames = audit.request_headers(req["request_id"])
        call_count = result.get("executed_engine_calls") if call_mode else executed
        if (
            type(call_count) is not int
            or call_count != executed + int(bool(terminal_zero))
            or len(frames) != call_count * 2
        ):
            raise EvidenceError("source lacks pre/post evidence for every executed tick")
        progressed = 0
        for index, frame in enumerate(frames):
            if frame.kind == "post_step":
                progressed += frame.payload["native_tick_delta"]
            expected_version = {
                "epoch": current["epoch"],
                "tick": current["tick"] + progressed,
                "revision": current["revision"] + len(outcomes) if progressed == 0 else 0,
            }
            if frame.version != expected_version or frame.payload.get("executed_ticks") != progressed:
                raise EvidenceError("source audit ticks do not follow request")
            if frame.kind == "pre_step" and frame.payload.get("requested_ticks") != count:
                raise EvidenceError("source audit requested budget differs from request")
        native_actions = [
            event["payload"] for event in audit.request_events(req["request_id"]) if event["kind"] == "action"
        ]
        expected_actions = [
            {"request_id": req["request_id"], "ordinal": i, "action": actions[i], "result": outcome}
            for i, outcome in enumerate(outcomes)
        ]
        if native_actions != expected_actions:
            raise EvidenceError("native action attempts/results were lost or reordered")
        current = after
    if pause_state_checks:
        # Pauses are uncommon control probes. One sequential pass proves their
        # post-snapshots equal the actual prior post-step without random seeks
        # or equating a non-cryptographic digest with the full reconstructed state.
        for frame in audit._frames(reuse_state=True):
            if frame.seq in pause_state_checks and frame.canonical_state != canonical(pause_state_checks[frame.seq]):
                raise EvidenceError("pause changed the captured game state")


@dataclass
class Trajectory:
    directory: Path
    manifest: dict
    audit: AuditLog

    @property
    def initial(self):
        return self.manifest["initial"]

    @property
    def steps(self):
        return self.manifest["steps"]

    @classmethod
    def load(cls, path: str | Path) -> Trajectory:
        path = Path(path)
        if path.is_dir():
            path = path / "trajectory.json"
        manifest = read_json(path)
        if manifest.get("schema") != SCHEMA or manifest.get("trajectory_id") != content_identity(manifest):
            raise EvidenceError("trajectory schema/content identity mismatch")
        validate_embedded_tree(manifest)
        audit_directory = path.parent / "audit"
        store = evidence_codec.EvidenceStore(audit_directory, error=EvidenceError)
        expected_files = {
            "audit/" + store.stored_path(name).name
            for name in audit_files(audit_directory, read_json(audit_directory / "manifest.json"))
        }
        if store.compressed:
            expected_files.add(f"audit/{evidence_codec.RECEIPT}")
        if manifest.get("source") == "session_trace":
            expected_files.add("session.jsonl")
        elif manifest.get("source") == "native":
            expected_files.add("initial.json")
        else:
            raise EvidenceError("unknown trajectory source")
        if set(manifest.get("files", {})) != expected_files:
            raise EvidenceError("trajectory evidence file set is incomplete")
        for name, checksum in manifest["files"].items():
            candidate = (path.parent / name).resolve()
            if (
                not candidate.is_relative_to(path.parent.resolve())
                or not candidate.is_file()
                or file_hash(candidate) != checksum
            ):
                raise EvidenceError(f"evidence SHA-256 mismatch: {name}")
        audit = AuditLog(path.parent / "audit", require_closed=True)
        if manifest["source"] == "session_trace":
            initial, steps = _trace_steps(path.parent / "session.jsonl")
        else:
            initial, steps = read_json(path.parent / "initial.json"), _native_steps(audit)
            _validate_initial(initial)
        if manifest.get("initial") != initial or manifest.get("steps") != steps:
            raise EvidenceError("trajectory contents do not match authoritative evidence")
        _validate_steps(initial, steps, audit)
        return cls(path.parent, manifest, audit)


def build_trajectory(
    trace: str | Path | None,
    audit_directory: str | Path,
    output_directory: str | Path,
    *,
    initial: str | Path | None = None,
    tree: TreePlacement | None = None,
) -> Trajectory:
    """Package a closed recording. Exactly one source is required.

    Native-only input still requires a live-captured initial.json identity/state.
    It cannot recover an omitted initial state from later observations.
    An optional tree placement adds one manifest section; it never changes the
    content identity of the recording and is verified separately.
    """
    if (trace is None) == (initial is None):
        raise ValueError("provide exactly one of SessionTrace or initial metadata")
    audit = AuditLog(audit_directory, require_closed=True)
    if trace is not None:
        marker, steps = _trace_steps(Path(trace))
    else:
        marker, steps = read_json(Path(initial)), _native_steps(audit)
        _validate_initial(marker)
    _validate_steps(marker, steps, audit)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    (output / "audit").mkdir()
    filenames = []
    for name in audit.evidence_files:
        # Copy the stored form (plain or a verified gzip container) and keep the
        # codec receipt with it so the packaged trajectory stays self-describing.
        stored = audit.store.stored_path(name)
        dest = f"audit/{stored.name}"
        shutil.copyfile(stored, output / dest)
        filenames.append(dest)
    if audit.store.compressed:
        shutil.copyfile(Path(audit_directory) / evidence_codec.RECEIPT, output / "audit" / evidence_codec.RECEIPT)
        filenames.append(f"audit/{evidence_codec.RECEIPT}")
    source_name = "session.jsonl" if trace is not None else "initial.json"
    shutil.copyfile(Path(trace if trace is not None else initial), output / source_name)
    filenames.append(source_name)
    manifest = {
        "schema": SCHEMA,
        "source": "session_trace" if trace is not None else "native",
        "initial": marker,
        "steps": steps,
        "files": {name: file_hash(output / name) for name in filenames},
        "scope": "captured state only; no completeness or original-engine determinism certification",
    }
    manifest["trajectory_id"] = content_identity(manifest)
    if tree is not None:
        manifest["tree"] = attach_tree(manifest, tree)
    _write(output / "trajectory.json", manifest)
    return Trajectory.load(output)
