"""Explicit prewarm App update-count initialization; never normalizes state.

``initial_app_update_anchor_v1`` anchors ``LawnApp+0x484`` (``mUpdateCount``,
the counter behind ``/sound_effects/app_update_count``) to a target the
experiment declares. The run's own observed counter is never accepted as that
target: anchoring each world to its own value keeps every world internally
consistent and still leaves the cross-world counter different. Cross-run
``before`` values may differ; the ``after`` readback must equal the declared
common target.
"""

from __future__ import annotations

import copy

from . import b0_normalization, sound_effects

MODE = "initial_app_update_anchor_v1"
METHOD = "app_update_anchor"
EVENT = "app_update_anchored"
# The declared fixed target lives in the plan/recipe (``Plan.app_update_count``
# and ``app_update_anchor: {configuration, app_update_count}``). The capability
# manifest cannot gain a policy key without invalidating already sealed
# archives, so the declaration contract is stated here and enforced by the
# reader plus ``initialization.apply_recipe``, which refuses to fall back to the
# run's own observed counter.
TARGET_POLICY = "explicit_fixed_common_target"
# The optional fixed MJ clock anchor is inserted between this App anchor and
# the warm draw. The literal avoids importing the newer module here.
MJ_CLOCK_EVENT = "mj_clock_anchored"
MAX_VALUE = 0x7FFFFFFF
SPEC = {
    "mode": MODE,
    "available": True,
    "field": "LawnApp+0x484/mUpdateCount",
    "phase": "after_seed_before_warm",
    "requires_audio_mode": sound_effects.MODE,
    "once_per_epoch": True,
    "value_range": [0, MAX_VALUE],
    "demo_policy": "reject_original_demo_recording_or_playback",
    "original_engine_bitwise_unmodified": False,
    "live_verified": False,
    "receipt_event": EVENT,
}


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("App update anchor: " + message)


def target(value):
    if type(value) is not int or not 0 <= value <= MAX_VALUE:
        fail("target must be an integer in 0..2147483647")
    return value


def mode(game):
    if "app_update_anchor" not in game:
        return None
    if not sound_effects.same(game["app_update_anchor"], SPEC):
        fail("unsupported or incomplete declared configuration")
    if sound_effects.mode(game) != sound_effects.MODE:
        fail("initial anchoring requires the explicit allocation-none audio mode")
    from .audit_compare import DRAW_SCHEDULE_MODE, draw_mode

    if draw_mode(game) != DRAW_SCHEDULE_MODE:
        fail("initial anchoring requires controlled warm drawing")
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
        or set(block) != {"configuration", "app_update_count"}
        or not sound_effects.same(block["configuration"], SPEC)
    ):
        fail("initialization recipe configuration is invalid")
    return target(block["app_update_count"])


def receipt(value, game, *, requested=None, before_version=None, after_version=None, seed=None):
    from .audit_compare import SCHEMA, _seeded_rng, digests, version

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
        "demo_before",
        "demo_after",
        "before_version",
        "after_version",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != "lvz.app-update-anchor.v1"
        or value["mode"] != MODE
        or not sound_effects.uint(value["before"])
    ):
        fail("receipt shape or original counter is invalid")
    target(value["before"])
    demo = value["demo_before"]
    if (
        not isinstance(demo, dict)
        or set(demo) != {"00000510", "00000511", "00000578", "0000049c", "000004a0"}
        or any(not sound_effects.uint(n) for n in demo.values())
        or demo["00000510"] != 0
        or demo["00000511"] != 0
        or demo["000004a0"] > 255
        or not sound_effects.same(demo, value["demo_after"])
    ):
        fail("original demo is active or its raw guard fields changed")
    wanted = target(value["requested"])
    if type(value["after"]) is not int or value["after"] != wanted:
        fail("actual readback differs from requested target")
    if requested is not None and wanted != target(requested):
        fail("receipt belongs to a different requested target")
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
            or state.get("schema") != SCHEMA
            or state.get("rng", {}).get("target") != game.get("target")
            or state.get("app", {}).get("ui") != 3
            or not sound_effects.same(
                state.get("draw_schedule"),
                {"mode": "deterministic_draw_schedule_v1", "warm_frames": 0, "step_frames": 0},
            )
        ):
            fail("receipt lacks full ready prewarm state")
        sound = sound_effects.state(state, game)
        from . import sound_counter

        if sound_counter.mode(game) and sound["counter_scope"] != "experiment":
            fail("App anchor must follow the actual sound counter origin")
        if sound["app_update_count"] != counter:
            fail("receipt counter differs from captured state")
        digests(state)  # Retain ordinary integer-bit/schema validation.
        if seed is not None:
            if not sound_effects.uint(seed) or not sound_effects.same(state["rng"].get("instances"), _seeded_rng(seed)):
                fail("anchor did not preserve the actual freshly seeded RNG")
    expected = copy.deepcopy(value["before_state"])
    expected["sound_effects"]["app_update_count"] = wanted
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
            fail("one run cannot mix the unified B(0) normalization table with a legacy App update anchor")
        return
    if wanted is None:
        fail("new mode requires an explicitly declared fixed initial target")
    if marker["observation"].get("app_update_anchored") is not True:
        fail("B0 observation does not confirm the actual one-shot anchor")
    sound = sound_effects.state(marker["state"], marker["identity"]["game"])
    if sound["app_update_count"] != wanted:
        fail("B0 actual App counter differs from the recorded target")


def semantics(value, *, map_version=lambda value: value, pending_mj_clock=False):
    """Compare effects; each raw before state remains independently validated.

    With the fixed MJ clock anchor declared, this App anchor is the immediate
    predecessor of a real write to ``/app/mj_clock``, so that single field is
    still unanchored in this intermediate after-state. It is masked here only
    for the cross-process comparison; the following MJ receipt and the B0
    state compare it strictly, and this receipt never rewrites it.
    """
    if value is None:
        return None
    result = copy.deepcopy({k: v for k, v in value.items() if k not in {"before", "before_state"}})
    if pending_mj_clock and isinstance(result.get("after_state"), dict):
        result["after_state"]["app"]["mj_clock"] = None
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
        self.warm_started = self.seen_frame = False
        # At most one declared fixed MJ clock anchor may occupy the single
        # revision between this App anchor and the warm draw.
        self.mj_clock_anchors = 0

    def event(self, event):
        kind = event["kind"]
        if kind in {"app_update_anchor_failed", "mj_clock_anchor_failed"}:
            fail("native anchor failure invalidates strict evidence")
        if kind == EVENT:
            if not self.enabled or self.anchor is not None or self.warm_started or self.seen_frame:
                fail("undeclared, duplicate, or late initial anchor")
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
            self.anchor, self.anchor_event = copy.deepcopy(value), copy.deepcopy(event)
        elif self.enabled and kind == MJ_CLOCK_EVENT:
            if self.anchor is None or self.warm_started or self.seen_frame or self.mj_clock_anchors:
                fail("fixed MJ clock anchor appeared outside its unique prewarm position")
            self.mj_clock_anchors += 1
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
                fail("initialization changed after the one-shot anchor")
            if kind == "rng_restored":
                self.seed_event = None
        elif self.enabled and kind == "render_preparing":
            if self.anchor is None or self.warm_started:
                fail("warm drawing requires exactly one completed anchor")
            expected = dict(
                self.anchor["after_version"],
                revision=self.anchor["after_version"]["revision"] + 1 + self.mj_clock_anchors,
            )
            if not sound_effects.same(expected, event["version"]):
                fail("warm preparation is not the next initialization revision")
            self.warm_started = True
        elif self.enabled and kind in {"request_started", "action"} and not self.warm_started:
            fail("simulation action precedes the required initial anchor/warm boundary")

    def frame(self, frame):
        if self.enabled and (self.anchor is None or not self.warm_started):
            fail("simulation frame lacks the required initial anchor")
        self.seen_frame = True

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        wanted = target_from_recipe(marker["initialization"])
        if self.anchor is None or not self.warm_started or self.anchor["after"] != wanted:
            fail("B0 lacks matching actual anchored initialization evidence")
        expected = dict(
            self.anchor["after_version"], revision=self.anchor["after_version"]["revision"] + 1 + self.mj_clock_anchors
        )
        if not sound_effects.same(expected, marker["observation"]["version"]):
            fail("B0 is not the actual next warm boundary")
        if marker["initialization"].get("seed") != self.seed_event["payload"]["seed"]:
            fail("recipe seed differs from the native seed before anchor")
