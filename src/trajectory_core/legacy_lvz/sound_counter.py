"""Native experimental allocation counter and its absolute evidence chain."""

from __future__ import annotations

import copy

from . import app_update_anchor, b0_normalization, sound_effects

MODE = "sound_effects_counter_origin_v1"
METHOD = "sound_counter_origin"
EVENT = "sound_counter_origin_bound"
RAW_FILE = "sound-counter-raw.jsonl"
SPEC = {
    "mode": MODE,
    "available": True,
    "field": "/sound_effects/calls",
    "phase": "after_seed_before_app_anchor_and_warm",
    "once_per_recording": True,
    "counter_bits": 32,
    "wrap_allowed": False,
    "raw_counter_reset": False,
    "snapshot_representation": "native_calls_since_origin",
    "receipt_event": EVENT,
    "raw_boundary_evidence": RAW_FILE,
    "live_verified": False,
}


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("sound counter: " + message)


def mode(game):
    if "sound_counter" not in game:
        return None
    legacy = app_update_anchor.mode(game) == app_update_anchor.MODE
    unified = b0_normalization.mode(game) == b0_normalization.MODE
    if not sound_effects.same(game["sound_counter"], SPEC) or not (legacy or unified):
        fail("unsupported declaration or missing explicit App anchor capability")
    if unified and not legacy and b0_normalization.APP_UPDATE_FIELD not in b0_normalization.declared_fields(game):
        # The origin is the real boundary the App update count is written from.
        fail("unified declaration lacks the App update count field the origin binds")
    return MODE


def negotiate(hello):
    declared = mode(hello.get("game", {}))
    for key in (MODE, METHOD):
        value = hello.get("capabilities", {}).get(key, False)
        if type(value) is not bool or value != bool(declared):
            fail("hello capability/declaration mismatch")
    return declared


def receipt(value, game, *, before_version=None, after_version=None, seed=None):
    from .audit_compare import SCHEMA, _seeded_rng, digests, version

    if not mode(game):
        fail("origin receipt lacks its declared mode")
    keys = {
        "schema",
        "mode",
        "origin_raw_calls",
        "raw_before",
        "raw_after",
        "before_state",
        "after_state",
        "before_version",
        "after_version",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != "lvz.sound-counter-origin.v1"
        or value["mode"] != MODE
        or not sound_effects.uint(value["origin_raw_calls"])
    ):
        fail("invalid origin receipt shape/counter")
    pre, post = version(value["before_version"]), version(value["after_version"])
    if pre["tick"] != 0 or post != dict(pre, revision=pre["revision"] + 1):
        fail("origin must be one actual tick-zero initialization revision")
    if before_version is not None and not sound_effects.same(pre, before_version):
        fail("origin request version mismatch")
    if after_version is not None and not sound_effects.same(post, after_version):
        fail("origin response/event version mismatch")
    for name in ("raw_before", "raw_after"):
        sound_effects.receipt(value[name], game["sound_effects"], pre_resume=False)
        if value[name]["calls"] != value["origin_raw_calls"]:
            fail("origin differs from actual absolute counter")
    if not sound_effects.same(value["raw_before"], value["raw_after"]):
        fail("binding changed raw counter, ownership, or health")
    for key, calls, scope in (
        ("before_state", value["origin_raw_calls"], "bootstrap_lifetime"),
        ("after_state", 0, "experiment"),
    ):
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
            fail("origin requires captured ready prewarm state")
        sound = sound_effects.state(state, game)
        if sound["counter_scope"] != scope or sound["calls"] != calls:
            fail("native scope/counter readback differs from origin receipt")
        digests(state)
        if seed is not None and (
            not sound_effects.uint(seed) or not sound_effects.same(state["rng"].get("instances"), _seeded_rng(seed))
        ):
            fail("origin changed or lacks the actual freshly seeded RNG")
    expected = copy.deepcopy(value["before_state"])
    expected["sound_effects"].update(calls=0, counter_scope="experiment")
    if not sound_effects.same(expected, value["after_state"]):
        fail("origin changed captured state beyond the two declared presentation fields")
    return value


def initial(marker):
    game, recipe = marker["identity"]["game"], marker["initialization"]
    if not mode(game):
        if "sound_counter" in recipe:
            fail("recipe cannot upgrade an older counter scope")
        return
    if not sound_effects.same(recipe.get("sound_counter"), {"configuration": game["sound_counter"]}):
        fail("initial counter recipe is missing or mismatched")
    if marker["observation"].get("counter_origin_bound") is not True:
        fail("B0 does not confirm the actual bound origin")
    if sound_effects.state(marker["state"], game)["counter_scope"] != "experiment":
        fail("B0 is not a native experimental counter state")


def semantics(value, *, map_version=lambda value: value):
    if value is None:
        return None
    # The App anchor follows this operation. Its actual after_state performs
    # the full cross-process state comparison; never erase App fields here.
    return {
        "schema": value["schema"],
        "mode": value["mode"],
        "before_version": map_version(value["before_version"]),
        "after_version": map_version(value["after_version"]),
        "bound_calls": value["after_state"]["sound_effects"]["calls"],
        "counter_scope": value["after_state"]["sound_effects"]["counter_scope"],
    }


def health_semantics(health, game):
    if not mode(game):
        return copy.deepcopy(health)
    if not isinstance(health, dict) or health.get("counter_scope") != "experiment":
        fail("closed counter lacks experiment scope")
    if (
        any(not sound_effects.uint(health.get(k)) for k in ("calls", "origin_raw_calls", "raw_calls"))
        or health["raw_calls"] < health["origin_raw_calls"]
        or health["raw_calls"] - health["origin_raw_calls"] != health["calls"]
    ):
        fail("closed raw/origin/experiment arithmetic mismatch")
    return copy.deepcopy({k: v for k, v in health.items() if k not in {"origin_raw_calls", "raw_calls"}})


class Evidence:
    def __init__(self, game, activation=None):
        self.game, self.enabled = game, bool(mode(game))
        self.activation = activation
        if self.enabled:
            sound_effects.activation(activation, game)
        self.origin = self.seed_event = self.anchor = None
        self.first_calls = None
        self.last_raw = activation["recorder_attach"]["calls"] if self.enabled else None
        self.last_calls = 0
        self.health = None
        self.frames = 0

    def event(self, event):
        kind, payload = event["kind"], event["payload"]
        if kind == "sound_counter_origin_failed":
            fail("native origin failure invalidates strict evidence")
        if kind == EVENT:
            if not self.enabled or self.origin is not None or self.anchor is not None or self.frames:
                fail("undeclared, duplicate, or late origin event")
            if (
                set(payload) != {"request_id", "counter_origin"}
                or not isinstance(payload["request_id"], str)
                or not payload["request_id"]
                or self.seed_event is None
            ):
                fail("origin lacks request identity or actual preceding seed")
            value = receipt(
                payload["counter_origin"],
                self.game,
                after_version=event["version"],
                seed=self.seed_event["payload"]["seed"],
            )
            pre, seeded = value["before_version"], self.seed_event["version"]
            if seeded["epoch"] != pre["epoch"] or seeded["tick"] != 0 or seeded["revision"] > pre["revision"]:
                fail("origin seed is from a different initialization boundary")
            raw = value["raw_before"]
            attached = self.activation["recorder_attach"]
            if raw["calls"] < self.last_raw or not sound_effects.same(
                {k: v for k, v in raw.items() if k != "calls"}, {k: v for k, v in attached.items() if k != "calls"}
            ):
                fail("origin counter/ownership differs from attached bootstrap")
            self.origin, self.last_raw = copy.deepcopy(value), raw["calls"]
        elif self.enabled and kind == "rng_seeded":
            if self.origin is not None or not sound_effects.uint(payload.get("seed")):
                fail("invalid seed or initialization mutation after counter origin")
            self.seed_event = copy.deepcopy(event)
        elif self.enabled and kind in {"rng_restored", "clocks_restored"}:
            if self.origin is not None:
                fail("initialization mutation after counter origin")
            if kind == "rng_restored":
                self.seed_event = None
        elif self.enabled and kind in {app_update_anchor.EVENT, b0_normalization.EVENT}:
            if self.origin is None or self.anchor is not None:
                fail("App boundary lacks unique preceding counter origin")
            # The legacy per-field anchor and the unified table carry the same
            # before_version/before_state contract for this boundary.
            value = payload.get("anchor") or payload.get("normalization", {})
            if not sound_effects.same(
                value.get("before_version"), self.origin["after_version"]
            ) or not sound_effects.same(value.get("before_state"), self.origin["after_state"]):
                fail("App boundary does not start from the full actual origin after-state")
            self.anchor = copy.deepcopy(value)
        elif self.enabled and kind in {"render_preparing", "request_started", "action"}:
            if self.origin is None or self.anchor is None:
                fail("warm/action precedes counter origin and App anchor")
        elif self.enabled and kind == "sound_effects_closed":
            if self.origin is None or self.health is not None:
                fail("close lacks a unique completed origin")
            health_semantics(payload, self.game)
            if (
                payload["origin_raw_calls"] != self.origin["origin_raw_calls"]
                or payload["raw_calls"] < self.last_raw
                or payload["calls"] < self.last_calls
            ):
                fail("close raw counter regressed or changed its origin")
            self.health = copy.deepcopy(payload)

    def frame(self, frame, raw):
        """Standalone entry: always validate the complete current sound state."""
        if not self.enabled:
            return
        self.__accept_frame(frame, raw, sound_effects.state(frame.state, self.game))

    def frame_with_audio(self, audio, frame, raw):
        """Validate audio and raw counter together, without a caller token/cache.

        Only the actual audio checker for this same manifest participates. The
        checked subtree never leaves this synchronous operation before raw
        evidence is checked; the next frame always validates its state again.
        """
        if type(audio) is not sound_effects.Evidence or audio.game is not self.game:
            fail("composed frame requires the audio checker for this manifest")
        sound = sound_effects.Evidence.frame(audio, frame)
        self.__accept_frame(frame, raw, sound)

    def __accept_frame(self, frame, raw, sound):
        # Private shared implementation. Both entry points above obtain sound
        # from a full validation in this call; neither accepts caller authority.
        if not self.enabled:
            return
        expected_keys = {
            "schema",
            "seq",
            "kind",
            "version",
            "engine_call_id",
            "raw_calls",
            "origin_raw_calls",
            "experiment_calls",
        }
        call = frame.payload.get("engine_call", {}).get("engine_call_id")
        if (
            self.origin is None
            or self.anchor is None
            or self.health is not None
            or not isinstance(raw, dict)
            or set(raw) != expected_keys
            or raw["schema"] != "lvz.sound-counter-raw.v1"
            or type(call) is not int
            or call <= 0
            or any(
                not sound_effects.same(raw[k], v)
                for k, v in {
                    "seq": frame.seq,
                    "kind": frame.kind,
                    "version": frame.version,
                    "engine_call_id": call,
                }.items()
            )
            or any(not sound_effects.uint(raw[k]) for k in ("raw_calls", "origin_raw_calls", "experiment_calls"))
        ):
            fail("raw counter evidence lacks exact frame/call binding")
        origin = self.origin["origin_raw_calls"]
        if (
            raw["origin_raw_calls"] != origin
            or raw["raw_calls"] < self.last_raw
            or raw["raw_calls"] < origin
            or raw["raw_calls"] - origin != raw["experiment_calls"]
            or raw["experiment_calls"] != sound["calls"]
            or sound["counter_scope"] != "experiment"
        ):
            fail("raw counter arithmetic/scope differs from native semantic state")
        self.last_raw, self.last_calls = raw["raw_calls"], raw["experiment_calls"]
        if self.first_calls is None:
            self.first_calls = self.last_calls
        self.frames += 1

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        if self.origin is None or self.anchor is None:
            fail("B0 lacks actual origin/App-anchor chain")
        calls = marker["state"]["sound_effects"]["calls"]
        total = self.origin["origin_raw_calls"] + calls
        if (
            total > 0xFFFFFFFF
            or (self.first_calls is not None and calls > self.first_calls)
            or (self.health is not None and calls > self.health["calls"])
        ):
            fail("B0 native counter exceeds first frame/close or wraps")
        if self.first_calls is None:
            self.last_raw, self.last_calls = total, calls
