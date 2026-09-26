"""One declared B(0) normalization table: one application, one receipt.

``initial_b0_normalization_v1`` replaces the per-field anchors
(``initial_app_update_anchor_v1`` and ``initial_mj_clock_anchor_v1``) with a
single ordered table of ``{field, target, reason}`` entries. Every ``field`` is
a JSON Pointer into the captured B(0) state, the closed field set is declared
by the runtime, and the table is written once in the declared order, consuming
exactly one revision and producing exactly one receipt. The run's own observed
values are never accepted as targets: a declaration that is missing, empty,
``null``, out of range or outside the declared field set is rejected before
any initialization request is sent (fail closed).

``reason`` is review text. It is bound into the sealed run (the recipe file's
SHA-256 is in the manifest) but never into the cross-world root identity, which
projects the table to ordered ``(field, target)`` pairs.
"""

from __future__ import annotations

import copy

from . import sound_effects

MODE = "initial_b0_normalization_v1"
METHOD = "b0_normalization"
EVENT = "b0_normalized"
FAILED_EVENT = "b0_normalization_failed"
SCHEMA = "lvz.b0-normalization.v1"
MAX_VALUE = 0x7FFFFFFF
ORDERING = "declared_order"
PHASE = "after_seed_before_warm"
MAX_REASON = 200

APP_UPDATE_FIELD = "/sound_effects/app_update_count"
MJ_CLOCK_FIELD = "/app/mj_clock"

# The closed field set, mirroring the runtime manifest. ``value_range`` and the
# requirement lists are part of the declaration, not of a caller's choice.
FIELDS = [
    {
        "field": APP_UPDATE_FIELD,
        "native": "LawnApp+0x484",
        "value_range": [0, MAX_VALUE],
        "requires_events": ["rng_seeded", "sound_counter_origin_bound"],
    },
    {
        "field": MJ_CLOCK_FIELD,
        "native": "LawnApp+0x838",
        "value_range": [0, MAX_VALUE],
        "requires_events": ["rng_seeded"],
        "requires_entries": [APP_UPDATE_FIELD],
    },
]
FIELD_NAMES = tuple(spec["field"] for spec in FIELDS)
SPEC = {
    "mode": MODE,
    "available": True,
    "phase": PHASE,
    "requires_audio_mode": sound_effects.MODE,
    "once_per_epoch": True,
    "ordering": ORDERING,
    "fields": copy.deepcopy(FIELDS),
    "original_engine_bitwise_unmodified": False,
    "live_verified": False,
    "receipt_event": EVENT,
}

# Transitional aliases. They build ordinary table entries whose ``reason``
# records where the target came from, so a reviewer can tell a legacy flag from
# an explicit table entry without the target losing its provenance.
LEGACY_REASONS = {
    ("plan", "app_update_count"): "legacy Plan.app_update_count",
    ("plan", "mj_clock"): "legacy Plan.mj_clock",
    ("flag", "app_update_count"): "legacy flag --app-update-count",
    ("flag", "mj_clock"): "legacy flag --mj-clock",
    ("api", "app_update_count"): "legacy app_update_count argument",
    ("api", "mj_clock"): "legacy mj_clock argument",
}


def fail(message):
    from .audit_compare import EvidenceError

    raise EvidenceError("B0 normalization: " + message)


def target(value, field=None):
    if type(value) is not int or not 0 <= value <= MAX_VALUE:
        fail(f"target for {field or 'entry'} must be an integer in 0..2147483647")
    return value


def reason(value, field=None):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REASON
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        fail(f"reason for {field or 'entry'} must be 1..{MAX_REASON} printable characters without newlines")
    return value


def field_name(value):
    if not isinstance(value, str) or value not in FIELD_NAMES:
        fail(f"field must be one of the declared B(0) integer leaves {list(FIELD_NAMES)}")
    return value


def entry(value):
    """Validate one table entry; the key set is exact, unknown keys are rejected."""
    if not isinstance(value, dict) or set(value) != {"field", "target", "reason"}:
        fail("each table entry requires exactly the keys field, target and reason")
    return {
        "field": field_name(value["field"]),
        "target": target(value["target"], value["field"]),
        "reason": reason(value["reason"], value["field"]),
    }


def entries(value, *, field_set=None):
    """Validate the whole ordered table, including the declared-order dependencies."""
    if not isinstance(value, list) or not value:
        fail("the table must be a nonempty ordered array of entries; an empty table is 'not declared'")
    result = [entry(item) for item in value]
    seen, present = [], {item["field"] for item in result}
    for item in result:
        if item["field"] in seen:
            fail(f"field {item['field']} appears more than once in the table")
        for required in next(spec for spec in FIELDS if spec["field"] == item["field"]).get("requires_entries", ()):
            # A declared dependency only has to hold when it is part of this table.
            if required in present and required not in seen:
                fail(f"entry {item['field']} must follow {required} in the declared order")
        seen.append(item["field"])
    if field_set is not None:
        declared = set(field_set)
        for item in result:
            if item["field"] not in declared:
                fail(f"field {item['field']} is outside the runtime-declared field set {sorted(declared)}")
    return result


def alias_entries(*, app_update_count=None, mj_clock=None, source):
    """Build the transitional single/double-entry table used by legacy call sites."""
    if source not in ("plan", "flag", "api"):
        fail("legacy alias source must be plan, flag or api")
    result = []
    if app_update_count is not None:
        result.append(
            {
                "field": APP_UPDATE_FIELD,
                "target": target(app_update_count, "app_update_count"),
                "reason": LEGACY_REASONS[(source, "app_update_count")],
            }
        )
    if mj_clock is not None:
        result.append(
            {
                "field": MJ_CLOCK_FIELD,
                "target": target(mj_clock, "mj_clock"),
                "reason": LEGACY_REASONS[(source, "mj_clock")],
            }
        )
    return entries(result) if result else []


def mode(game):
    """Return the declared unified mode; a partial or mismatched declaration fails."""
    spec = game.get("b0_normalization")
    if spec is None:
        if "b0_normalization" in game:
            fail("null mode is not a declaration")
        return None
    if not sound_effects.same(spec, SPEC):
        fail("unsupported or incomplete declared configuration")
    if sound_effects.mode(game) != sound_effects.MODE:
        fail("B(0) normalization requires the explicit allocation-none audio mode")
    from .audit_compare import DRAW_SCHEDULE_MODE, draw_mode

    if draw_mode(game) != DRAW_SCHEDULE_MODE:
        fail("B(0) normalization requires controlled warm drawing")
    return MODE


def negotiate(hello):
    declared = mode(hello.get("game", {}))
    caps = hello.get("capabilities", {})
    for key in (MODE, METHOD):
        actual = caps.get(key, False)
        if type(actual) is not bool or actual != bool(declared):
            fail("hello capability/configuration mismatch")
    return declared


def unified_owns_fields(game) -> bool:
    """True when the declared unified table, not a legacy anchor, owns the fields.

    One DLL advertises the unified table and the two legacy per-field anchors at
    the same time so that older plans keep working, so a legacy key in an
    archive manifest is not by itself a claim that the legacy anchor was used.
    The declared unified mode alone decides ownership: whenever it is present,
    the table is the authority for ``/sound_effects/app_update_count`` and
    ``/app/mj_clock`` and the legacy readers must neither read nor validate
    them. Whether one run really carried both shapes is a different question,
    answered by :func:`mixed_shapes` from the recipe.
    """
    return mode(game) == MODE


def declared_fields(game):
    return tuple(spec["field"] for spec in game["b0_normalization"]["fields"]) if mode(game) else ()


def recipe_block(game, table):
    """The recipe shape: the declared configuration plus the ordered entries."""
    return {"configuration": copy.deepcopy(game["b0_normalization"]), "entries": entries(table)}


def requested_from_recipe(recipe):
    """Read the declared table; ``None`` means 'no unified declaration'."""
    if METHOD not in recipe:
        return None
    block = recipe[METHOD]
    if not isinstance(block, dict) or set(block) != {"configuration", "entries"}:
        fail("initialization recipe block shape is invalid")
    if not sound_effects.same(block["configuration"], SPEC):
        fail("initialization recipe configuration is invalid")
    return entries(block["entries"])


def legacy_recipe_entries(recipe):
    """Project the two historical recipe blocks for review and coverage only.

    This never upgrades a run: the legacy readers keep their own validation and
    a run whose recipe names either legacy block is never re-read as unified.
    """
    from . import app_update_anchor, mj_clock_anchor

    result = []
    if app_update_anchor.METHOD in recipe:
        result.append(
            {
                "field": APP_UPDATE_FIELD,
                "target": app_update_anchor.target_from_recipe(recipe),
                "reason": "legacy app_update_anchor recipe block",
            }
        )
    if mj_clock_anchor.METHOD in recipe:
        result.append(
            {
                "field": MJ_CLOCK_FIELD,
                "target": mj_clock_anchor.target_from_recipe(recipe),
                "reason": "legacy mj_clock_anchor recipe block",
            }
        )
    return result


def recipe_entries(recipe):
    """The declared normalization targets in either shape, for reporting and coverage."""
    unified = requested_from_recipe(recipe)
    return unified if unified is not None else legacy_recipe_entries(recipe)


def mixed_shapes(game, recipe=None):
    """True when this one run really carries both shapes for the same fields.

    A capability list is never evidence of a mix: one runtime advertises the
    unified table and the legacy anchors together, so its archives declare both
    while only the table was applied. A mix is a property of the run's own
    declaration -- a recipe that carries a legacy anchor block while the unified
    table is declared (or next to the unified block) -- and of the event stream,
    where the readers refuse a legacy anchor event beside ``b0_normalized``.
    """
    declaration = recipe if isinstance(recipe, dict) else {}
    legacy = ("app_update_anchor" in declaration) or ("mj_clock_anchor" in declaration)
    unified = mode(game) == MODE or METHOD in declaration
    return legacy and unified


def value_at(state, pointer):
    """Resolve a declared JSON Pointer against the captured state."""
    node = state
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            fail(f"captured state has no {pointer}")
        node = node[token]
    return node


def _counters(value, requested, label):
    if not isinstance(value, list) or len(value) != len(requested):
        fail(f"{label} must list exactly one value per requested entry")
    result = []
    for item, wanted in zip(value, requested):
        if not isinstance(item, dict) or set(item) != {"field", "value"} or item["field"] != wanted["field"]:
            fail(f"{label} entries must stay in the declared order and name the same fields")
        result.append({"field": item["field"], "value": target(item["value"], item["field"])})
    return result


def receipt(value, game, *, requested=None, before_version=None, after_version=None, seed=None):
    """Validate one unified receipt: the whole table, one revision, one true state delta."""
    from .audit_compare import SCHEMA as AUDIT_SCHEMA
    from .audit_compare import _seeded_rng, digests
    from .audit_compare import version as version_of

    if not mode(game):
        fail("receipt lacks an explicit initialization contract")
    keys = {
        "schema",
        "mode",
        "requested",
        "before",
        "after",
        "before_state",
        "after_state",
        "before_version",
        "after_version",
    }
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != SCHEMA or value["mode"] != MODE:
        fail("receipt shape or schema is invalid")
    wanted = entries(value["requested"], field_set=declared_fields(game))
    if requested is not None:
        if not sound_effects.same(wanted, entries(requested)):
            fail("receipt belongs to a different declared table (field, target or reason differs)")
    before = _counters(value["before"], wanted, "before")
    after = _counters(value["after"], wanted, "after")
    for index, item in enumerate(wanted):
        if after[index]["value"] != item["target"]:
            fail(f"actual readback for {item['field']} differs from the declared target")
    pre, post = version_of(value["before_version"]), version_of(value["after_version"])
    if pre["tick"] != 0 or post != dict(pre, revision=pre["revision"] + 1):
        fail("normalization must be exactly one initialization revision at tick zero")
    if before_version is not None and not sound_effects.same(pre, before_version):
        fail("receipt before version differs from the actual request boundary")
    if after_version is not None and not sound_effects.same(post, after_version):
        fail("receipt after version differs from the actual reply/event boundary")
    expected = None
    for key, counters in (("before_state", before), ("after_state", after)):
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
        ):
            fail("receipt lacks the full ready prewarm state")
        sound = sound_effects.state(state, game)
        from . import sound_counter

        if sound_counter.mode(game) and sound["counter_scope"] != "experiment":
            fail("normalization must follow the actual sound counter origin")
        digests(state)  # Ordinary integer-bit/schema validation; never a substitute for equality.
        for item in counters:
            if value_at(state, item["field"]) != item["value"]:
                fail(f"{key} does not carry the real value of {item['field']}")
        if seed is not None:
            if not sound_effects.uint(seed) or not sound_effects.same(state["rng"].get("instances"), _seeded_rng(seed)):
                fail("normalization did not preserve the actual freshly seeded RNG")
        if expected is None:
            expected = copy.deepcopy(state)
            for item in wanted:
                node = expected
                tokens = item["field"][1:].split("/")
                for token in tokens[:-1]:
                    node = node[token.replace("~1", "/").replace("~0", "~")]
                node[tokens[-1].replace("~1", "/").replace("~0", "~")] = item["target"]
        else:
            # Leaf-wise truth: the captured state may differ only in the table.
            if not sound_effects.same(expected, state):
                fail("normalization changed captured state outside the declared table")
    return value


def initial(marker):
    """B(0) marker check: the one-shot table really landed on its declared targets."""
    game = marker["identity"]["game"]
    declared = mode(game)
    wanted = requested_from_recipe(marker["initialization"])
    if not declared:
        if wanted is not None:
            fail("recipe cannot upgrade an older execution mode")
        return
    if wanted is None:
        fail("new mode requires an explicitly declared nonempty B(0) normalization table")
    if marker["observation"].get("b0_normalized") is not True:
        fail("B0 observation does not confirm the actual one-shot normalization")
    for item in wanted:
        if value_at(marker["state"], item["field"]) != item["target"]:
            fail(f"B0 actual value of {item['field']} differs from the declared target")


def semantics(value, *, map_version=lambda value: value):
    """Compare effects; each raw before state remains independently validated."""
    if value is None:
        return None
    # The pre-values are real but world-specific by construction; the declared
    # targets and the post-state are what must agree across worlds.
    result = copy.deepcopy({key: item for key, item in value.items() if key not in {"before", "before_state"}})
    result["before_version"] = map_version(result["before_version"])
    result["after_version"] = map_version(result["after_version"])
    return result


class Evidence:
    """Stream reader: exactly one table, one revision, immediately before warm."""

    def __init__(self, game):
        self.game, self.enabled = game, bool(mode(game))
        self.anchor = self.seed_event = None
        self.counter_bound = False
        self.warm_started = self.seen_frame = False

    def event(self, event):
        kind = event["kind"]
        if kind == FAILED_EVENT:
            fail("native normalization failure invalidates strict evidence")
        if self.enabled and kind in {"app_update_anchored", "mj_clock_anchored"}:
            fail("one run cannot mix the unified table with a legacy per-field anchor")
        if kind == EVENT:
            if not self.enabled or self.anchor is not None or self.warm_started or self.seen_frame:
                fail("undeclared, duplicate, or late B(0) normalization")
            payload = event["payload"]
            if (
                set(payload) != {"request_id", "normalization"}
                or not isinstance(payload["request_id"], str)
                or not payload["request_id"]
                or self.seed_event is None
            ):
                fail("normalization lacks its actual request or preceding seed event")
            seed = self.seed_event["payload"]["seed"]
            value = receipt(payload["normalization"], self.game, after_version=event["version"], seed=seed)
            seed_version = self.seed_event["version"]
            before = value["before_version"]
            if (
                seed_version["epoch"] != before["epoch"]
                or seed_version["tick"] != 0
                or seed_version["revision"] > before["revision"]
            ):
                fail("seed does not precede the normalization in the same initialization epoch")
            from . import sound_counter

            if sound_counter.mode(self.game) and not self.counter_bound:
                fail("normalization must follow the actual sound counter origin")
            self.anchor = copy.deepcopy(value)
        elif self.enabled and kind == "sound_counter_origin_bound":
            if self.counter_bound:
                fail("duplicate sound counter origin before the normalization table")
            self.counter_bound = True
        elif self.enabled and kind == "rng_seeded":
            if (
                self.anchor is not None
                or self.warm_started
                or self.seen_frame
                or not sound_effects.uint(event["payload"].get("seed"))
            ):
                fail("seed changed after normalizing or has invalid evidence")
            self.seed_event = copy.deepcopy(event)
        elif self.enabled and kind in {"rng_restored", "clocks_restored"}:
            if self.anchor is not None or self.warm_started or self.seen_frame:
                fail("initialization changed after the one-shot normalization")
            if kind == "rng_restored":
                self.seed_event = None
        elif self.enabled and kind == "render_preparing":
            if self.anchor is None or self.warm_started:
                fail("warm drawing requires exactly one completed normalization table")
            expected = dict(self.anchor["after_version"], revision=self.anchor["after_version"]["revision"] + 1)
            if not sound_effects.same(expected, event["version"]):
                fail("warm preparation is not the immediately next initialization revision")
            self.warm_started = True
        elif self.enabled and kind in {"request_started", "action"} and not self.warm_started:
            fail("simulation action precedes the required B(0) normalization/warm boundary")

    def frame(self, frame):
        if self.enabled and (self.anchor is None or not self.warm_started):
            fail("simulation frame lacks the required B(0) normalization")
        self.seen_frame = True

    def initial(self, marker):
        initial(marker)
        if not self.enabled:
            return
        wanted = requested_from_recipe(marker["initialization"])
        if (
            self.anchor is None
            or not self.warm_started
            or not sound_effects.same(
                self.anchor["after"], [{"field": item["field"], "value": item["target"]} for item in wanted]
            )
        ):
            fail("B0 lacks matching actual normalized initialization evidence")
        expected = dict(self.anchor["after_version"], revision=self.anchor["after_version"]["revision"] + 1)
        if not sound_effects.same(expected, marker["observation"]["version"]):
            fail("B0 is not the actual next warm boundary")
        if marker["initialization"].get("seed") != self.seed_event["payload"]["seed"]:
            fail("recipe seed differs from the native seed before the normalization")


def identity_recipe(recipe):
    """Project the table for the root identity: ordered (field, target) pairs only.

    ``reason`` is review text. Binding it into the root identity would split one
    logical root into two, and reject the merge of two worlds that declared the
    same targets with different wording.
    """
    if METHOD not in recipe:
        return copy.deepcopy(recipe)
    projected = copy.deepcopy(recipe)
    block = projected[METHOD]
    if not isinstance(block, dict) or set(block) != {"configuration", "entries"}:
        fail("initialization recipe block shape is invalid")
    raw = block["entries"]
    pairs = raw if _is_projection(raw) else projection(entries(raw))
    block.pop("entries")
    block["entries"] = pairs
    return projected


def projection(table):
    """Ordered ``(field, target)`` pairs: how the table enters the root identity."""
    return [[item["field"], item["target"]] for item in entries(table)]


def _is_projection(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and type(item[1]) is int
            for item in value
        )
    )
