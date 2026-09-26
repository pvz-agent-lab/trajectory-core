"""Fail-closed contract for the explicit allocation-none SFX experiment.

No helper here substitutes audio results or changes game state. Original mode
remains absence of this contract; old recordings cannot acquire it implicitly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

MODE = "sound_effects_allocation_none_v1"
EVIDENCE = "audio-activation.json"
ENGINE_SHA256 = "f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322"
ORIGINAL = [0x55, 0x8B, 0xEC, 0x83, 0xE4, 0xC0]
SCOPE = "110 Foley histories and slots; active parameters; 32 empty manager channels; actual App update count; allocation counter"


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("sound effects: " + message)


def uint(value):
    return type(value) is int and 0 <= value <= 0xFFFFFFFF


def same(left, right):
    """JSON equality that never treats booleans as integer evidence."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def configured(value="original"):
    if value not in ("original", MODE):
        fail("unsupported configured mode")
    return value


def mode(game):
    spec = game.get("sound_effects")
    if spec is None:
        if "sound_effects" in game:
            fail("null mode is not a declaration")
        return None
    expected = {
        "mode": MODE,
        "installed": True,
        "activation": "before_primary_thread_resume",
        "entry_rva": 0x1C7650,
        "original_bytes": ORIGINAL,
        "owner_pinned": True,
        "engine_sha256": ENGINE_SHA256,
        "original_pitch_variation_code": True,
        "sound_allocation": "always_null",
        "music_virtualized": False,
        "original_engine_bitwise_unmodified": False,
        "live_verified": False,
        "required_evidence": EVIDENCE,
        "closed_event": "sound_effects_closed",
        "state_scope": SCOPE,
    }
    if (
        not isinstance(spec, dict)
        or set(spec) != set(expected) | {"bootstrap_sha256"}
        or any(type(spec.get(k)) is not type(v) or spec[k] != v for k, v in expected.items())
        or not isinstance(spec.get("bootstrap_sha256"), str)
        or not re.fullmatch("[0-9a-f]{64}", spec["bootstrap_sha256"])
    ):
        fail("unsupported or incomplete native mode")
    return MODE


def negotiate(hello, expected=None):
    actual = mode(hello.get("game", {}))
    capability = hello.get("capabilities", {}).get(MODE, False)
    if type(capability) is not bool or capability != bool(actual):
        fail("hello capability/mode mismatch")
    if expected is not None and (actual or "original") != configured(expected):
        fail("launcher and resident runtime modes differ")
    return actual


def receipt(value, spec, *, pre_resume):
    required = {
        "mode",
        "installed",
        "before_primary_thread_resume",
        "phase",
        "primary_thread",
        "owner_module",
        "owner_pinned",
        "entry",
        "replacement",
        "patch_owned",
        "calls",
        "errors",
        "preexisting_app",
        "original_bytes",
        "patch_bytes",
        "engine_sha256",
        "bootstrap_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        fail("activation receipt shape mismatch")
    exact = {
        "mode": MODE,
        "installed": True,
        "before_primary_thread_resume": True,
        "phase": "sealed_before_resume",
        "owner_pinned": True,
        "entry": 0x5C7650,
        "patch_owned": True,
        "errors": 0,
        "preexisting_app": 0,
        "original_bytes": ORIGINAL,
        "engine_sha256": ENGINE_SHA256,
        "bootstrap_sha256": spec["bootstrap_sha256"],
    }
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in exact.items()):
        fail("activation receipt identity/phase mismatch")
    if any(not uint(value[k]) or not value[k] for k in ("primary_thread", "owner_module", "replacement")):
        fail("invalid actual activation addresses/thread")
    if not uint(value["calls"]) or (pre_resume and value["calls"] != 0):
        fail("audio calls occurred before primary resume")
    patch = value["patch_bytes"]
    if (
        not isinstance(patch, list)
        or len(patch) != 6
        or any(type(b) is not int or not 0 <= b <= 255 for b in patch)
        or patch[0] != 0xE9
        or patch[5] != 0x90
        or (value["entry"] + 5 + int.from_bytes(bytes(patch[1:5]), "little")) & 0xFFFFFFFF != value["replacement"]
    ):
        fail("entry jump does not bind the actual replacement")


def activation(value, game):
    if not mode(game):
        fail("activation evidence lacks declared mode")
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "configuration", "pre_resume", "recorder_attach"}
        or value["schema"] != "lvz.audio-activation.v1"
        or not same(value["configuration"], game["sound_effects"])
    ):
        fail("activation file/configuration mismatch")
    before, after = value["pre_resume"], value["recorder_attach"]
    receipt(before, game["sound_effects"], pre_resume=True)
    receipt(after, game["sound_effects"], pre_resume=False)
    if {k: v for k, v in before.items() if k != "calls"} != {k: v for k, v in after.items() if k != "calls"}:
        fail("activation ownership changed between resume and recorder attach")


def state(value, game):
    from . import sound_counter

    declared = mode(game)
    sound = value.get("sound_effects")
    if not declared:
        if "sound_effects" in value:
            fail("state lacks an explicit engine mode")
        return None
    required = {
        "mode",
        "calls",
        "errors",
        "app_update_count",
        "active_types",
        "histories",
        "parameters",
        "channels",
        "slots_empty",
        "patch_owned",
    }
    if sound_counter.mode(game):
        required.add("counter_scope")
        if not isinstance(sound, dict) or sound.get("counter_scope") not in {"bootstrap_lifetime", "experiment"}:
            fail("invalid declared native counter scope")
    if (
        not isinstance(sound, dict)
        or set(sound) != required
        or sound["mode"] != MODE
        or sound["slots_empty"] is not True
        or sound["patch_owned"] is not True
        or type(sound["errors"]) is not int
        or sound["errors"] != 0
        or not uint(sound["calls"])
        or not uint(sound["app_update_count"])
    ):
        fail("invalid native audio state/health")
    count = sound["active_types"]
    if not uint(count) or not 1 <= count <= 110:
        fail("invalid active Foley type count")
    histories = sound["histories"]
    if not isinstance(histories, list) or len(histories) != 110:
        fail("all 110 Foley histories are required")
    for history in histories:
        if (
            not isinstance(history, dict)
            or set(history) != {"last_variation", "slots"}
            or not uint(history["last_variation"])
        ):
            fail("invalid Foley history")
        slots = history["slots"]
        if not isinstance(slots, list) or len(slots) != 8:
            fail("all eight Foley slots are required")
        for slot in slots:
            if (
                not isinstance(slot, list)
                or len(slot) != 5
                or any(not uint(n) for n in slot)
                or slot[:2] != [0, 0]
                or slot[2] not in (0, 1)
            ):
                fail("Foley instance/refcount not empty or invalid fields")
    parameters = sound["parameters"]
    if not isinstance(parameters, list) or len(parameters) != count:
        fail("active Foley parameters missing")
    for i, param in enumerate(parameters):
        if (
            not isinstance(param, dict)
            or set(param) != {"type", "pitch_bits", "flags", "sound_id_rvas", "sound_ids"}
            or type(param["type"]) is not int
            or param["type"] != i
            or not uint(param["pitch_bits"])
            or not uint(param["flags"])
        ):
            fail("invalid Foley parameters")
        rvas, ids = param["sound_id_rvas"], param["sound_ids"]
        if not isinstance(rvas, list) or not isinstance(ids, list) or len(rvas) != 10 or len(ids) != 10:
            fail("sound resource parameter evidence missing")
        if any(
            (r is None) != (s is None)
            or (r is not None and (not uint(r) or not 0x299000 <= r <= 0x35DC18 or not uint(s)))
            for r, s in zip(rvas, ids)
        ):
            fail("invalid sound resource identity")
    if sound["channels"] != [0] * 32 or any(type(n) is not int for n in sound["channels"]):
        fail("manager channels are not actually empty")
    return sound


def state_sha256(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def artifacts(game, value):
    if not mode(game):
        return
    if value.get("module_hashes", {}).get("lvz-bootstrap.dll") != game["sound_effects"]["bootstrap_sha256"]:
        fail("bootstrap artifact differs from activated owner")
    if (
        value.get("input_hashes", {}).get("game/local-engine/PlantsVsZombies.exe")
        != game["sound_effects"]["engine_sha256"]
    ):
        fail("engine artifact differs from the locked activation target")


def initial(marker):
    game = marker["identity"]["game"]
    sound = state(marker["state"], game)
    recipe = marker["initialization"]
    if sound is not None:
        expected = {"configuration": game["sound_effects"], "b0_state_sha256": state_sha256(sound)}
        if not same(recipe.get("sound_effects"), expected):
            fail("initial audio state/recipe mismatch")
        artifacts(game, marker["identity"]["artifacts"])
    elif "sound_effects" in recipe:
        fail("audio recipe lacks mode")


class Evidence:
    def __init__(self, game, receipt_file=None):
        from . import sound_counter

        self.game = game
        self.experimental_counter = bool(sound_counter.mode(game))
        self.enabled = bool(mode(game))
        if self.enabled:
            activation(receipt_file, game)
        elif receipt_file is not None:
            fail("activation evidence lacks an explicit engine mode")
        self.activation = copy.deepcopy(receipt_file)
        self.attach_calls = receipt_file["recorder_attach"]["calls"] if self.enabled else None
        self.calls = 0 if self.experimental_counter else self.attach_calls
        self.first_calls = None
        self.health = None
        self.version = None

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        calls = marker["state"]["sound_effects"]["calls"]
        if (not self.experimental_counter and calls < self.attach_calls) or (
            self.first_calls is not None and calls > self.first_calls
        ):
            fail("B0 allocation counter is outside attach/first-frame boundaries")
        if self.health is not None and calls > self.health["calls"]:
            fail("B0 allocation counter exceeds closed counter")
        if self.first_calls is None:
            self.calls = calls

    def frame(self, frame):
        """Validate this frame completely and return its checked sound subtree.

        The return is for immediate synchronous composition, not a reusable
        validation token: patch decoding may mutate the same object next frame.
        """
        sound = state(frame.state, self.game)
        if sound is None:
            return
        if self.experimental_counter and sound["counter_scope"] != "experiment":
            fail("audited frame has an unbound lifetime counter")
        if self.health is not None or (self.calls is not None and sound["calls"] < self.calls):
            fail("audio counters regressed or frame followed close")
        if self.first_calls is None:
            self.first_calls = sound["calls"]
        self.calls = sound["calls"]
        self.version = frame.version
        return sound

    def event(self, event):
        if event["kind"] != "sound_effects_closed":
            return
        if not self.enabled or self.health is not None:
            fail("undeclared or duplicate audio close")
        h = event["payload"]
        keys = {"mode", "healthy", "calls", "errors", "patch_owned", "owner_pinned", "slots_empty", "scope"}
        if self.experimental_counter:
            from .sound_counter import health_semantics

            keys.update({"counter_scope", "origin_raw_calls", "raw_calls"})
            health_semantics(h, self.game)
        if (
            not isinstance(h, dict)
            or set(h) != keys
            or h["mode"] != MODE
            or h["healthy"] is not True
            or h["errors"] != 0
            or type(h["errors"]) is not int
            or h["patch_owned"] is not True
            or h["owner_pinned"] is not True
            or h["slots_empty"] is not True
            or h["scope"] != "allocation-none SFX experiment; music/exhaustive determinism unverified"
            or not uint(h["calls"])
            or (self.calls is not None and h["calls"] < self.calls)
        ):
            fail("unhealthy or incomplete audio close")
        self.health = copy.deepcopy(h)
