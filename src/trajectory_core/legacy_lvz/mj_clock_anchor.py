"""Fixed-target B(0) write for the App absolute counter; never a spot value.

``initial_mj_clock_anchor_v1`` anchors ``LawnApp+0x838`` (the field AvZ calls
``MjClock`` and the candidate decompilation calls ``mAppCounter``) to a target
that the experiment declares once. Cross-run ``before`` values may differ; the
``after`` value must equal the declared target. No comparator offsets the field
and no other field is normalized.
"""

from __future__ import annotations

import copy

from . import app_update_anchor, b0_normalization, sound_effects

MODE = "initial_mj_clock_anchor_v1"
METHOD = "mj_clock_anchor"
EVENT = "mj_clock_anchored"
FAILED_EVENT = "mj_clock_anchor_failed"
SCHEMA = "lvz.mj-clock-anchor.v1"
MAX_VALUE = 0x7FFFFFFF
# The existing App anchor must remain the actual prewarm boundary, so the new
# fixed write is inserted between it and the warm draw.
PRECEDED_BY = app_update_anchor.EVENT
SPEC = {
    "mode": MODE,
    "available": True,
    "field": "LawnApp+0x838/mAppCounter",
    "phase": "after_seed_before_warm",
    "requires_audio_mode": sound_effects.MODE,
    "once_per_epoch": True,
    "value_range": [0, MAX_VALUE],
    "target_policy": "explicit_fixed_common_target",
    "source_actual_target": False,
    "preceded_by": PRECEDED_BY,
    "original_engine_bitwise_unmodified": False,
    "live_verified": False,
    "receipt_event": EVENT,
}


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("MJ clock anchor: " + message)


def target(value):
    if type(value) is not int or not 0 <= value <= MAX_VALUE:
        fail("target must be an integer in 0..2147483647")
    return value


def mode(game):
    if "mj_clock_anchor" not in game:
        return None
    if not sound_effects.same(game["mj_clock_anchor"], SPEC):
        fail("unsupported or incomplete declared configuration")
    if sound_effects.mode(game) != sound_effects.MODE:
        fail("fixed MJ clock anchoring requires the explicit allocation-none audio mode")
    from .audit_compare import DRAW_SCHEDULE_MODE, draw_mode

    if draw_mode(game) != DRAW_SCHEDULE_MODE:
        fail("fixed MJ clock anchoring requires controlled warm drawing")
    return MODE


def negotiate(hello):
    declared = mode(hello.get("game", {}))
    caps = hello.get("capabilities", {})
    for key in (MODE, METHOD):
        actual = caps.get(key, False)
        if type(actual) is not bool or actual != bool(declared):
            fail("hello capability/configuration mismatch")
    return declared


def target_from_recipe(recipe):
    """Return the declared fixed target; the recipe never records a spot value."""
    if METHOD not in recipe:
        return None
    block = recipe[METHOD]
    if (
        not isinstance(block, dict)
        or set(block) != {"configuration", "mj_clock"}
        or not sound_effects.same(block["configuration"], SPEC)
    ):
        fail("initialization recipe configuration is invalid")
    return target(block["mj_clock"])


def receipt(value, game, *, requested=None, before_version=None, after_version=None, seed=None):
    from .audit_compare import SCHEMA as AUDIT_SCHEMA
    from .audit_compare import _seeded_rng, digests, version

    if not mode(game):
        fail("receipt lacks an explicit initialization contract")
    keys = {
        "schema",
        "mode",
        "before",
        "after",
        "requested",
        "before_state",
        "after_state",
        "before_version",
        "after_version",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != SCHEMA
        or value["mode"] != MODE
        or not sound_effects.uint(value["before"])
    ):
        fail("receipt shape or original counter is invalid")
    target(value["before"])
    wanted = target(value["requested"])
    if type(value["after"]) is not int or value["after"] != wanted:
        fail("actual readback differs from the declared fixed target")
    if requested is not None and wanted != target(requested):
        fail("receipt belongs to a different declared fixed target")
    pre, post = version(value["before_version"]), version(value["after_version"])
    if pre["tick"] != 0 or post != dict(pre, revision=pre["revision"] + 1):
        fail("anchor must be one initialization revision at tick zero")
    if before_version is not None and not sound_effects.same(pre, before_version):
        fail("receipt before version differs from actual request boundary")
    if after_version is not None and not sound_effects.same(post, after_version):
        fail("receipt after version differs from actual reply/event boundary")
    for key, counter in (("before_state", value["before"]), ("after_state", wanted)):
        state = value[key]
        if (
            not isinstance(state, dict)
            or state.get("schema") != AUDIT_SCHEMA
            or state.get("rng", {}).get("target") != game.get("target")
            or state.get("app", {}).get("ui") != 3
            or not sound_effects.same(
                state.get("draw_schedule"),
                {"mode": "deterministic_draw_schedule_v1", "warm_frames": 0, "step_frames": 0},
            )
            or type(state.get("app", {}).get("mj_clock")) is not int
            or state["app"]["mj_clock"] != counter
        ):
            fail("receipt lacks the full ready prewarm state or its actual App counter")
        sound_effects.state(state, game)  # Ordinary integer-bit/schema validation.
        digests(state)
        if seed is not None:
            if not sound_effects.uint(seed) or not sound_effects.same(state["rng"].get("instances"), _seeded_rng(seed)):
                fail("anchor did not preserve the actual freshly seeded RNG")
    expected = copy.deepcopy(value["before_state"])
    expected["app"]["mj_clock"] = wanted
    if not sound_effects.same(expected, value["after_state"]):
        fail("anchor changed captured state outside the declared App counter")
    return value


def initial(marker):
    game = marker["identity"]["game"]
    declared = mode(game)
    wanted = target_from_recipe(marker["initialization"])
    if not declared:
        if wanted is not None:
            fail("recipe cannot upgrade an older execution mode")
        return
    if b0_normalization.unified_owns_fields(game):
        # The declared unified table owns this field, so this reader stays out of
        # the way -- but a run must not carry both shapes for the same field.
        if wanted is not None:
            fail("one run cannot mix the unified B(0) normalization table with a legacy fixed MJ clock anchor")
        return
    if wanted is None:
        fail("new mode requires an explicitly declared fixed initial target")
    if marker["observation"].get("mj_clock_anchored") is not True:
        fail("B0 observation does not confirm the actual one-shot fixed anchor")
    if not isinstance(marker["state"].get("app"), dict) or marker["state"]["app"].get("mj_clock") != wanted:
        fail("B0 actual App counter differs from the fixed recorded target")


def semantics(value, *, map_version=lambda value: value):
    """Compare effects; each raw before state remains independently validated."""
    if value is None:
        return None
    result = copy.deepcopy({k: v for k, v in value.items() if k not in {"before", "before_state"}})
    result["before_version"] = map_version(result["before_version"])
    result["after_version"] = map_version(result["after_version"])
    return result


class Evidence:
    def __init__(self, game):
        self.game = game
        # A declared unified table owns the field, so this legacy reader is then
        # disabled exactly as it is for a runtime that never declared it: the
        # unified reader validates the single table and refuses a legacy event.
        self.enabled = bool(mode(game)) and not b0_normalization.unified_owns_fields(game)
        self.anchor = self.anchor_event = self.seed_event = None
        self.app_event = None
        self.warm_started = self.seen_frame = False

    def event(self, event):
        kind = event["kind"]
        if kind == FAILED_EVENT:
            fail("native anchor failure invalidates strict evidence")
        if kind == app_update_anchor.EVENT:
            # The App anchor stays the actual prewarm boundary; this operation
            # starts from its complete after-state in the same epoch.
            self.app_event = copy.deepcopy(event)
        if kind == EVENT:
            if not self.enabled or self.anchor is not None or self.warm_started or self.seen_frame:
                fail("undeclared, duplicate, or late fixed anchor")
            payload = event["payload"]
            if (
                set(payload) != {"request_id", "anchor"}
                or not isinstance(payload["request_id"], str)
                or not payload["request_id"]
                or self.seed_event is None
            ):
                fail("anchor lacks its actual request or preceding seed event")
            seed = self.seed_event["payload"]["seed"]
            value = receipt(payload["anchor"], self.game, after_version=event["version"], seed=seed)
            seed_version = self.seed_event["version"]
            before = value["before_version"]
            if (
                seed_version["epoch"] != before["epoch"]
                or seed_version["tick"] != 0
                or seed_version["revision"] > before["revision"]
            ):
                fail("seed does not precede the anchor in the same initialization epoch")
            if app_update_anchor.mode(self.game):
                if self.app_event is None:
                    fail("fixed anchor lacks its actual preceding App update anchor")
                if not sound_effects.same(self.app_event["version"], before):
                    fail("fixed anchor is not the next revision after the App update anchor")
                if not sound_effects.same(self.app_event["payload"]["anchor"]["after_state"], value["before_state"]):
                    fail("fixed anchor does not start from the App anchor after-state")
            self.anchor, self.anchor_event = copy.deepcopy(value), copy.deepcopy(event)
        elif self.enabled and kind == "rng_seeded":
            if (
                self.anchor is not None
                or self.warm_started
                or self.seen_frame
                or not sound_effects.uint(event["payload"].get("seed"))
            ):
                fail("seed changed after anchoring or has invalid evidence")
            self.seed_event = copy.deepcopy(event)
        elif self.enabled and kind in {"rng_restored", "clocks_restored"}:
            if self.anchor is not None or self.warm_started or self.seen_frame:
                fail("initialization changed after the one-shot fixed anchor")
            if kind == "rng_restored":
                self.seed_event = None
        elif self.enabled and kind == "render_preparing":
            if self.anchor is None or self.warm_started:
                fail("warm drawing requires exactly one completed fixed anchor")
            expected = dict(self.anchor["after_version"], revision=self.anchor["after_version"]["revision"] + 1)
            if not sound_effects.same(expected, event["version"]):
                fail("fixed anchor is not the actual last initialization revision")
            self.warm_started = True
        elif self.enabled and kind in {"request_started", "action"} and not self.warm_started:
            fail("simulation action precedes the required fixed anchor/warm boundary")

    def frame(self, frame):
        if self.enabled and (self.anchor is None or not self.warm_started):
            fail("simulation frame lacks the required fixed anchor")
        self.seen_frame = True

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        wanted = target_from_recipe(marker["initialization"])
        if self.anchor is None or not self.warm_started or self.anchor["after"] != wanted:
            fail("B0 lacks matching actual fixed-target initialization evidence")
        expected = dict(self.anchor["after_version"], revision=self.anchor["after_version"]["revision"] + 1)
        if not sound_effects.same(expected, marker["observation"]["version"]):
            fail("B0 is not the actual next warm boundary")
        if marker["initialization"].get("seed") != self.seed_event["payload"]["seed"]:
            fail("recipe seed differs from the native seed before the fixed anchor")
