"""Strict evidence for the explicitly controlled game-owner FP environment.

Raw status flags and thread identities remain local evidence. Captured control
words and every captured game scalar retain their exact comparison semantics.
"""

from __future__ import annotations

import copy

MODE = "fixed_owner_fp_v1"
EVENT = "fp_environment_activated"
CLOSED = "fp_environment_closed"
RAW_FILE = "fp-environment-raw.jsonl"
SPEC = {
    "mode": MODE,
    "x87_control": 0x027F,
    "mxcsr_control": 0x1F80,
    "mxcsr_control_mask": 0xFFFFFFC0,
    "activation": "initialize_after_finish_title_before_seed_and_enter_game",
    "scope": "game_owner_thread_boundary_controls",
    "activation_count": 1,
    "sticky_status_preserved": True,
    "drift_policy": "latch_fault_without_repair",
    "raw_evidence": RAW_FILE,
    "original_engine_bitwise_unmodified": False,
    "whole_process_fp_controlled": False,
}
PHASES = (
    "loop",
    "initialization",
    "ready",
    "before_original_update",
    "after_original_update",
    "before_warm_draw",
    "after_warm_draw",
    "before_step_draw",
    "after_step_draw",
    "pre_step",
    "post_step",
    "snapshot",
    "close",
)


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("fixed FP: " + message)


def same(a, b):
    from .audit_compare import first_difference

    return first_difference(a, b) is None


def uint(value, bits=32):
    return type(value) is int and 0 <= value < 1 << bits


def mode(game):
    if not isinstance(game, dict):
        fail("game identity is not an object")
    if "fixed_fp" not in game:
        return None
    if not same(game["fixed_fp"], SPEC):
        fail("unsupported fixed_fp declaration")
    if (
        game.get("engine_call_boundary", {}).get("mode") != "controlled_engine_call_v1"
        or game.get("draw_schedule", {}).get("mode") != "deterministic_draw_schedule_v1"
    ):
        fail("fixed mode requires declared engine-call and draw boundaries")
    return MODE


def negotiate(hello):
    declared = mode(hello.get("game", {}))
    capability = hello.get("capabilities", {}).get(MODE, False)
    if type(capability) is not bool or capability != bool(declared):
        fail("capability and declaration differ")
    return declared


def raw(value, *, target=False):
    if (
        not isinstance(value, dict)
        or set(value) != {"x87_control", "x87_status", "mxcsr", "mxcsr_control"}
        or any(not uint(value[k], 16) for k in ("x87_control", "x87_status"))
        or any(not uint(value[k]) for k in ("mxcsr", "mxcsr_control"))
        or value["mxcsr_control"] != value["mxcsr"] & SPEC["mxcsr_control_mask"]
    ):
        fail("malformed raw FP sample")
    if target and (value["x87_control"] != SPEC["x87_control"] or value["mxcsr_control"] != SPEC["mxcsr_control"]):
        fail("persistent FP control drift")
    return value


def activation(value, game):
    from .audit_compare import version

    if not mode(game):
        fail("activation lacks its explicit mode")
    keys = {
        "schema",
        "mode",
        "request_id",
        "version",
        "phase",
        "owner_thread_id",
        "actual_thread_id",
        "game_ui",
        "board_address",
        "before",
        "after",
        "write_attempted",
        "activation_count",
        "ok",
        "first_fault",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != "lvz.fp-activation.v1"
        or value["mode"] != MODE
        or not isinstance(value["request_id"], str)
        or not value["request_id"]
        or value["phase"] != "initialize_before_seed_and_enter_game"
        or not uint(value["owner_thread_id"])
        or value["owner_thread_id"] == 0
        or type(value["actual_thread_id"]) is not int
        or value["actual_thread_id"] != value["owner_thread_id"]
        or type(value["game_ui"]) is not int
        or value["game_ui"] != 1
        or type(value["board_address"]) is not int
        or value["board_address"] != 0
        or type(value["activation_count"]) is not int
        or value["activation_count"] != 1
        or value["write_attempted"] is not True
        or value["ok"] is not True
        or value["first_fault"] is not None
    ):
        fail("invalid owner-thread activation receipt")
    if version(value["version"])["tick"] != 0:
        fail("activation occurred after controlled simulation")
    before, after = raw(value["before"]), raw(value["after"], target=True)
    if before["x87_status"] != after["x87_status"] or (before["mxcsr"] & 63) != (after["mxcsr"] & 63):
        fail("activation did not preserve sticky status")
    return value


def health(value, game, *, closed, owner=None):
    if not mode(game):
        fail("health lacks its explicit mode")
    keys = {
        "schema",
        "mode",
        "activated",
        "activation_count",
        "owner_thread_id",
        "closed",
        "healthy",
        "wrong_thread_checks",
        "raw_frames",
        "checks",
        "first_fault",
        "last_raw",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != "lvz.fp-health.v1"
        or value["mode"] != MODE
        or value["activated"] is not True
        or value["healthy"] is not True
        or value["closed"] is not closed
        or value["first_fault"] is not None
        or type(value["activation_count"]) is not int
        or value["activation_count"] != 1
        or type(value["wrong_thread_checks"]) is not int
        or value["wrong_thread_checks"] != 0
        or not uint(value["owner_thread_id"])
        or value["owner_thread_id"] == 0
        or (owner is not None and value["owner_thread_id"] != owner)
        or not uint(value["raw_frames"], 64)
        or not isinstance(value["checks"], dict)
        or set(value["checks"]) != set(PHASES)
        or any(not uint(v, 64) for v in value["checks"].values())
    ):
        fail("unhealthy or malformed FP monitor")
    checks = value["checks"]
    if checks["close"] != int(closed) or value["raw_frames"] != checks["pre_step"] + checks["post_step"]:
        fail("FP monitor raw-boundary/close count mismatch")
    raw(value["last_raw"], target=True)
    return value


def state(value, game):
    if not mode(game):
        return value.get("fp_environment")
    expected = {"x87_control": SPEC["x87_control"], "mxcsr_control": SPEC["mxcsr_control"]}
    if not same(value.get("fp_environment"), expected):
        fail("captured control words differ from explicit target")
    return value["fp_environment"]


def evidence(snapshot, game):
    value = snapshot.get("fixed_fp")
    if not mode(game):
        if "fixed_fp" in snapshot:
            fail("snapshot cannot upgrade old FP semantics")
        return None
    if not isinstance(value, dict) or set(value) != {"activation", "health"}:
        fail("snapshot lacks complete local FP evidence")
    receipt = activation(value["activation"], game)
    health(value["health"], game, closed=False, owner=receipt["owner_thread_id"])
    state(snapshot["state"], game)
    return value


def initial(marker):
    game, recipe = marker["identity"]["game"], marker["initialization"]
    if not mode(game):
        if "fixed_fp" in recipe or "fixed_fp_origin" in marker:
            fail("initial marker cannot upgrade old FP semantics")
        return
    if not same(recipe.get("fixed_fp"), game["fixed_fp"]):
        fail("initial recipe lacks the actual fixed FP configuration")
    value = evidence({"state": marker["state"], "fixed_fp": marker.get("fixed_fp_origin")}, game)
    from .audit_compare import version

    start, current = version(value["activation"]["version"]), version(marker["observation"]["version"])
    if start["epoch"] > current["epoch"] or (
        start["epoch"] == current["epoch"] and start["revision"] > current["revision"]
    ):
        fail("activation is later than the actual B0")
    h = value["health"]
    if h["raw_frames"] or h["checks"]["pre_step"] or h["checks"]["post_step"]:
        fail("initial FP origin already contains controlled boundaries")
    if (
        h["checks"]["ready"] < 1
        or h["checks"]["snapshot"] < 1
        or h["checks"]["before_warm_draw"] != 1
        or h["checks"]["after_warm_draw"] != 1
        or h["checks"]["before_step_draw"]
        or h["checks"]["after_step_draw"]
    ):
        fail("B0 FP monitor does not cover exactly one warm draw")


def semantics(value):
    # Activation happens before scene creation and therefore before the replay
    # epoch mapping. Local request IDs, thread IDs and raw status stay archived.
    return {
        "schema": value["schema"],
        "mode": value["mode"],
        "phase": value["phase"],
        "game_ui": value["game_ui"],
        "board_address": value["board_address"],
        "activation_count": value["activation_count"],
        "write_attempted": value["write_attempted"],
        "ok": value["ok"],
        "target_controls": {k: value["after"][k] for k in ("x87_control", "mxcsr_control")},
    }


class Evidence:
    def __init__(self, game):
        self.game, self.enabled = game, bool(mode(game))
        self.activation = self.health = self.origin = None
        self.pre = self.post = 0

    def event(self, event):
        kind, payload = event["kind"], event["payload"]
        if kind == "fp_environment_fault":
            fail("native FP fault invalidates strict evidence")
        if kind == EVENT:
            if not self.enabled or self.activation is not None or self.pre or self.post or self.health is not None:
                fail("undeclared, duplicate or late FP activation")
            value = activation(payload, self.game)
            if not same(event["version"], value["version"]):
                fail("activation event and receipt version differ")
            self.activation = copy.deepcopy(value)
        elif kind == CLOSED:
            if not self.enabled or self.activation is None or self.health is not None:
                fail("undeclared, duplicate or unactivated FP close")
            value = health(payload, self.game, closed=True, owner=self.activation["owner_thread_id"])
            if (
                value["raw_frames"] != self.pre + self.post
                or value["checks"]["pre_step"] != self.pre
                or value["checks"]["post_step"] != self.post
                or self.pre != self.post
            ):
                fail("closed FP monitor disagrees with all raw boundaries")
            self.health = copy.deepcopy(value)
            self._origin_counts()
        elif self.enabled and kind in {
            "rng_seeded",
            "render_preparing",
            "render_prepared",
            "request_started",
            "action",
        }:
            if self.activation is None or self.health is not None:
                fail("initialization/update precedes activation or follows close")

    def frame(self, frame, value):
        if not self.enabled:
            return
        if self.activation is None or self.health is not None:
            fail("frame lacks an active FP monitor")
        keys = {"schema", "seq", "kind", "version", "engine_call_id", "payload"}
        expected = {
            "seq": frame.seq,
            "kind": frame.kind,
            "version": frame.version,
            "engine_call_id": frame.payload.get("engine_call", {}).get("engine_call_id"),
        }
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or value["schema"] != "lvz.fp-environment-raw.v1"
            or not uint(expected["engine_call_id"], 64)
            or expected["engine_call_id"] == 0
            or any(not same(value[k], v) for k, v in expected.items())
        ):
            fail("raw FP sample lacks exact boundary/call binding")
        payload = value["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"x87_control", "x87_status", "mxcsr", "mxcsr_control", "owner_thread_id", "actual_thread_id"}
            or any(
                type(payload[k]) is not int or payload[k] != self.activation["owner_thread_id"]
                for k in ("owner_thread_id", "actual_thread_id")
            )
        ):
            fail("raw FP sample owner differs from activation")
        sample = raw({k: v for k, v in payload.items() if not k.endswith("thread_id")}, target=True)
        captured = state(frame.state, self.game)
        if not same(captured, {k: sample[k] for k in captured}):
            fail("raw and semantic FP samples differ")
        if frame.kind == "pre_step":
            self.pre += 1
        elif frame.kind == "post_step":
            self.post += 1
        else:
            fail("unexpected raw FP frame kind")

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        value = marker["fixed_fp_origin"]
        if self.activation is None or not same(self.activation, value["activation"]):
            fail("initial FP evidence is not the native activation event")
        self.origin = copy.deepcopy(value["health"])
        self._origin_counts()

    def _origin_counts(self):
        if self.origin is not None and self.health is not None:
            if any(self.health["checks"][k] < self.origin["checks"][k] for k in PHASES):
                fail("closed monitor counts precede B0 origin")

    def close(self, draw):
        if not self.enabled:
            return
        if self.health is None or draw is None or draw.health is None:
            fail("FP close lacks healthy monitor/draw evidence")
        checks = self.health["checks"]
        if (
            checks["before_original_update"] < checks["after_original_update"]
            or checks["after_original_update"] < self.post
            or checks["ready"] < 1
            or checks["snapshot"] < self.pre + self.post
            or checks["before_warm_draw"] != 1
            or checks["after_warm_draw"] != 1
            or checks["before_step_draw"] != draw.health["step_frames"]
            or checks["after_step_draw"] != draw.health["step_frames"]
        ):
            fail("FP guarded update/draw counts disagree with actual lifecycle")
