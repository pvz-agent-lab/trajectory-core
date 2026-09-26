"""Root integrity for reference ids, target types and generation clashes (N7).

P2 of the branch-data contract (``docs/分支数据合同与实施计划.md`` §1.1) admits a
root when it is a legal engine state. This module reads exactly one root state --
from a packaged trajectory (``trajectory.json`` plus the first complete record of
``audit/state-deltas.jsonl``) or from a normalized state JSON handed in directly --
and walks every reference-typed field of ``docs/机制读取点表.json``.

``DataArrayTryToGet`` (``Sexy.TodLib_DataArray.h:152-156``) returns ``nullptr``
unless the slot named by the low 16 bits still carries that complete 32-bit id,
so a reference is only as strong as its generation. Three violation kinds exist:

``dangling_reference``
    The target does not resolve: the slot is free, beyond ``used``, or now
    carries another generation. This is the fail-safe direction of D3 -- the
    target "disappears" -- and it is also how a live owner holding an expired
    animation link is reported.
``type_mismatch``
    The value does not name an object of the declared type: a slot index or any
    other non-id value sits in an id field (D1-1 vs D1-2), the same 32-bit id is
    live in another pool, or a normalized animation reference names a node that a
    different owner/role allocated. The recorder's own
    ``definition_type_pointer_mismatch`` issue is reported here as well.
``generation_conflict``
    The aliasing direction of D3. ``mNextKey`` is not above every live
    generation, so later allocations re-issue a generation that a live object
    already carries and an old reference can start naming an unrelated object
    (ABA). The ``.dat`` signature is D3-3: a load does not restore ``mNextKey``
    together with the objects it restores (the documented reading is a restart at
    1001), so a loaded root keeps live generations the counter has not reached
    yet. Two live objects that already share a generation, and a reference whose
    generation is live in another slot, are reported here too.

Malformed input is rejected with ``EvidenceError`` instead of being reported as
an inconsistency: the native recorder writes every slot from 0 to ``used - 1``
and already refuses a pool whose captured id does not match its slot, so a state
that breaks those invariants was never a captured root. Free-list order (L01),
attachment/particle pools (kept outside board state) and "the root was sampled
from a real match" are deliberately out of scope.

A reference that does not resolve exactly is diagnosed once, in this order:
a value that resolves to a live object of another pool is a type mismatch, a
value whose generation is live in another slot of its own pool is a generation
conflict, and only then is the missing target reported as dangling. Every
violation is reported once per field path, sorted by path, so two runs over the
same state always produce the same report.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import evidence_codec
from .audit_compare import EvidenceError, file_hash, read_json
from .evidence_tree import content_identity

STATE_SCHEMA = "lvz.audit.v1"
TRAJECTORY_SCHEMA = "lvz.engine-replay.v1"
MANIFEST_FILE = "trajectory.json"
STATE_DELTAS = "state-deltas.jsonl"
ANIMATION_SCHEMA = "lvz.reanimation-links.v1"
MAX_RECORD_BYTES = 32 << 20
INDEX_MASK = 0xFFFF
MAX_ID = 0xFFFFFFFF

# The six entity DataArrays of Board::CaptureState, in pool-table order.
POOLS = ("zombies", "plants", "projectiles", "coins", "mowers", "grid_items")
ANIMATION_OWNERS = ("zombies", "plants", "mowers", "grid_items")

# Raw id references: a field holds ``(generation << 16) | slot`` for the target
# pool, so only pools the state carries can resolve it. ``attachments`` and
# ``particles`` are DataArrays outside board state; there the value form is still
# checked, but "the target exists" cannot be decided from this evidence.
ID_REFERENCES = (
    ("plants", 0x12C, "mTargetZombieID", "zombies"),
    ("zombies", 0xF0, "mRelatedZombieID", "zombies"),
    ("zombies", 0xF4, "mFollowerZombieID[0]", "zombies"),
    ("zombies", 0xF8, "mFollowerZombieID[1]", "zombies"),
    ("zombies", 0xFC, "mFollowerZombieID[2]", "zombies"),
    ("zombies", 0x100, "mFollowerZombieID[3]", "zombies"),
    ("zombies", 0x110, "mAttachmentID", "attachments"),
    ("zombies", 0x128, "mTargetPlantID", "plants"),
    ("projectiles", 0x7C, "mAttachmentID", "attachments"),
    ("projectiles", 0x88, "mTargetZombieID", "zombies"),
    ("coins", 0x60, "mAttachmentID", "attachments"),
    ("plants", 0x8C, "mParticleID", "particles"),
    ("grid_items", 0x38, "mGridItemParticleID", "particles"),
)

# Board-level raw id references captured by the same state dump.
BOARD_ID_REFERENCES = (
    (0x5588, "mTutorialParticleID", "particles"),
    (0x5620, "mPoolSparklyParticleID", "particles"),
    *((0x63C + 4 * index, f"mIceParticleID[{index}]", "particles") for index in range(6)),
)

# Animation references: the comparable state replaces the raw handle with a
# reference object whose node must be live and owned by this very field.
ANIMATION_REFERENCES = (
    ("zombies", 0x118, "mBodyReanimID", "body"),
    ("zombies", 0x140, "mBossFireBallReanimID", "boss_fireball"),
    ("zombies", 0x144, "mSpecialHeadReanimID", "special_head"),
    ("zombies", 0x150, "mMoweredReanimID", "mowered"),
    ("plants", 0x94, "mBodyReanimID", "body"),
    ("plants", 0x98, "mHeadReanimID", "head"),
    ("plants", 0x9C, "mHeadReanimID2", "head2"),
    ("plants", 0xA0, "mHeadReanimID3", "head3"),
    ("plants", 0xA4, "mBlinkReanimID", "blink"),
    ("plants", 0xA8, "mLightReanimID", "light"),
    ("plants", 0xAC, "mSleepingReanimID", "sleeping"),
    ("mowers", 0x1C, "mReanimID", "body"),
    ("grid_items", 0x34, "mGridItemReanimID", "body"),
)

BOARD_FWOOSH = (0x5624, 0x5744)  # ReanimationID[MAX_GRID_SIZE_Y][12]
_DEAD_OFFSETS = {"zombies": 0xEC, "plants": 0x141, "mowers": 0x30, "grid_items": 0x20}
_PLANT_SQUISHED = 0x142  # Plant::Squish retires all effects while mDead is still false
_ZOMBIE_TYPE = 0x24
_ZOMBIE_HAS_OBJECT = 0xBC
_ZOMBIE_FLAG = 1
_ANIMATION_PROBLEMS = {
    "live_owner_dangling_handle": "dangling_reference",
    "definition_type_pointer_mismatch": "type_mismatch",
}

_ID_BY_POOL = {name: tuple(entry for entry in ID_REFERENCES if entry[0] == name) for name in POOLS}
_ANIMATION_BY_POOL = {
    name: tuple(entry for entry in ANIMATION_REFERENCES if entry[0] == name) for name in ANIMATION_OWNERS
}


def identity(generation: int, slot: int) -> int:
    """A DataArray id: the generation counter in the high half, the slot below."""
    return (generation << 16) | slot


def field_path(pool: str, slot, offset: int) -> str:
    return f"/{pool}/slots/{slot}/fields/{offset:08x}"


def _uint32(value, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ID:
        raise EvidenceError(f"{label} must be a uint32 integer")
    return value


def _counter(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceError(f"{label} must be a nonnegative integer")
    return value


class _Pool:
    """One captured DataArray: its generation counter and its live ids."""

    __slots__ = ("name", "next_key", "used", "slots", "live", "fields")

    def __init__(self, name: str, value):
        if not isinstance(value, dict) or not isinstance(value.get("slots"), dict):
            raise EvidenceError(f"{name} pool is malformed")
        self.name = name
        self.next_key = _counter(value.get("next_key"), f"{name} next_key")
        self.used = _counter(value["used"], f"{name} used") if "used" in value else None
        self.slots = value["slots"]
        self.live: dict[int, int] = {}
        self.fields: dict[int, dict] = {}
        for key, entry in self.slots.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                raise EvidenceError(f"{name} pool slot key must be a decimal index")
            if not isinstance(entry, dict):
                raise EvidenceError(f"{name} pool slot {key} is malformed")
            available = _uint32(entry.get("id_or_free_next"), f"{name} slot {key} id")
            if "fields" not in entry:
                continue  # a free slot holds the next free index, never a generation
            slot = int(key)
            if available >> 16 == 0 or available & INDEX_MASK != slot:
                raise EvidenceError(f"{name} slot {key} id does not match its slot")
            if not isinstance(entry["fields"], dict):
                raise EvidenceError(f"{name} slot {key} fields must be an object")
            self.live[slot] = available
            self.fields[slot] = entry["fields"]


def _generation_violations(pools) -> list[tuple[str, str, str]]:
    found = []
    for name, pool in pools.items():
        at_risk = sorted((value >> 16, slot) for slot, value in pool.live.items() if value >> 16 >= pool.next_key)
        if at_risk:
            generation, slot = at_risk[-1]
            found.append(
                (
                    f"/{name}/next_key",
                    "generation_conflict",
                    f"next_key {pool.next_key} is not above the live generation {generation} at slot "
                    f"{slot} ({len(at_risk)} live id(s) already reach it): the next allocations re-issue "
                    f"a generation that is still in use, so a reference written before a save/load pair "
                    f"can resolve to an unrelated object (D3-3 / ABA)",
                )
            )
        seen: dict[int, int] = {}
        for slot, value in sorted(pool.live.items()):
            generation = value >> 16
            if generation in seen:
                found.append(
                    (
                        f"/{name}/slots/{slot}/id_or_free_next",
                        "generation_conflict",
                        f"generation {generation} is already live at slot {seen[generation]}; two live "
                        f"objects of one pool may not share it (D3-2 wrap or an aliased re-issue)",
                    )
                )
            else:
                seen[generation] = slot
    return found


def _id_violation(path: str, field: str, value, target: str, pools, live_ids):
    """Resolve one raw id the way ``DataArrayTryToGet`` would, then diagnose it."""
    if type(value) is not int or not 0 <= value <= MAX_ID:
        return (path, "type_mismatch", f"{field} holds {value!r}; a DataArray reference is a uint32 id")
    if value == 0:
        return None  # an explicit null reference names nothing
    generation, target_slot = value >> 16, value & INDEX_MASK
    if generation == 0:
        return (
            path,
            "type_mismatch",
            f"{field} holds 0x{value:08x}, which carries no generation bits: the field names a slot "
            f"index or a free-list value, not an id (D1-1 vs D1-2)",
        )
    owner = pools.get(target)
    if owner is None:
        # The state carries no pool of the declared type: the value form was
        # checked, the target cannot be resolved from this evidence.
        return None
    if owner.live.get(target_slot) == value:
        return None  # DataArrayTryToGet returns this very object
    others = sorted(name for name in live_ids.get(value, ()) if name != target)
    if others:
        return (
            path,
            "type_mismatch",
            f"{field} 0x{value:08x} does not resolve in {target} but names a live {'/'.join(others)} "
            f"object; the field requires {target}",
        )
    reissued = [
        (other, other_id)
        for other, other_id in sorted(owner.live.items())
        if other_id >> 16 == generation and other_id != value
    ]
    if reissued:
        other, other_id = reissued[0]
        return (
            path,
            "generation_conflict",
            f"{field} 0x{value:08x} points at slot {target_slot}, but its generation {generation} is "
            f"live at slot {other} (0x{other_id:08x}): the generation was re-issued after a free, so a "
            f"reference can resolve to an unrelated object that reused it (D3-3 / ABA)",
        )
    if owner.used is not None and target_slot >= owner.used:
        return (
            path,
            "dangling_reference",
            f"{field} 0x{value:08x} does not resolve in {target}: slot {target_slot} is beyond used ({owner.used})",
        )
    if str(target_slot) not in owner.slots:
        return (
            path,
            "dangling_reference",
            f"{field} 0x{value:08x} does not resolve in {target}: the state carries no slot entry for {target_slot}",
        )
    if target_slot not in owner.live:
        return (
            path,
            "dangling_reference",
            f"{field} 0x{value:08x} does not resolve in {target}: slot {target_slot} is free",
        )
    return (
        path,
        "dangling_reference",
        f"{field} 0x{value:08x} does not resolve in {target}: slot {target_slot} now carries "
        f"0x{owner.live[target_slot]:08x}, so the referenced generation is gone (fail-safe of D3)",
    )


def _id_violations(state, pools) -> list[tuple[str, str, str]]:
    live_ids: dict[int, set[str]] = {}
    for name, pool in pools.items():
        for value in pool.live.values():
            live_ids.setdefault(value, set()).add(name)
    found = []
    for name, pool in pools.items():
        for slot, _owner in sorted(pool.live.items()):
            fields = pool.fields[slot]
            for _, offset, field, target in _ID_BY_POOL[name]:
                value = fields.get(f"{offset:08x}")
                if value is None:
                    continue
                violation = _id_violation(field_path(name, slot, offset), field, value, target, pools, live_ids)
                if violation is not None:
                    found.append(violation)
    board = state.get("board")
    if isinstance(board, dict):
        for offset, field, target in BOARD_ID_REFERENCES:
            value = board.get(f"{offset:08x}")
            if value is None:
                continue
            violation = _id_violation(f"/board/{offset:08x}", field, value, target, pools, live_ids)
            if violation is not None:
                found.append(violation)
    return found


def _retired(pool: str, fields: dict, role: str) -> bool:
    """Owner lifecycle: a retired owner may legitimately hold an expired link."""
    if fields.get(f"{_DEAD_OFFSETS[pool]:08x}", 0):
        return True
    if pool == "plants" and fields.get(f"{_PLANT_SQUISHED:08x}", 0):
        return True
    if pool == "zombies" and role == "special_head":
        # DropFlag retires +144 and only clears mHasObject (determinism/
        # reanimation_audit.cpp flag path), so a live flag zombie may keep it.
        return fields.get(f"{_ZOMBIE_TYPE:08x}") == _ZOMBIE_FLAG and fields.get(f"{_ZOMBIE_HAS_OBJECT:08x}") == 0
    return False


def _animation_reference(path: str, field: str, value, anchor: str, nodes, retired: bool):
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return (
            path,
            "type_mismatch",
            f"{field} holds {value!r}; a normalized state carries a reference object, not a raw handle",
        )
    status = value["status"]
    if status == "null":
        return None
    if status in ("live", "retiring"):
        node = value.get("node")
        if not isinstance(node, str):
            return (path, "type_mismatch", f"{field} is {status} without a node")
        if nodes is None:
            return None  # no comparable animation section: the node cannot be resolved here
        if node not in nodes:
            return (
                path,
                "dangling_reference",
                f"{field} names {node!r}, which is not a live animation node of this state",
            )
        owners = nodes[node].get("owners") if isinstance(nodes[node], dict) else None
        if not isinstance(owners, list):
            raise EvidenceError("comparable animation node has no owner list")
        if anchor not in owners:
            return (
                path,
                "type_mismatch",
                f"{field} requires the animation owned by {anchor}; {node!r} is owned by {owners}",
            )
        return None
    if status == "dangling":
        # The recorder writes this when a live owner's handle stops resolving.
        return (
            path,
            "dangling_reference",
            f"{field} does not resolve to a live animation node (recorded status dangling)",
        )
    if status == "expired":
        if retired:
            return None
        return (path, "dangling_reference", f"{field} is expired while its owner ({anchor}) is still alive")
    return (path, "type_mismatch", f"{field} carries unknown reference status {status!r}")


def _animation_violations(state, pools) -> list[tuple[str, str, str]]:
    section, found, nodes = state.get("reanimations"), [], None
    if section is not None:
        if not isinstance(section, dict) or section.get("schema") != ANIMATION_SCHEMA:
            raise EvidenceError("comparable animation state schema mismatch")
        nodes = section.get("nodes")
        if not isinstance(nodes, dict):
            raise EvidenceError("comparable animation state has no nodes")
        issues = section.get("issues")
        if not isinstance(issues, list):
            raise EvidenceError("comparable animation state has no issues list")
        for issue in issues:
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get("path"), str)
                or not isinstance(issue.get("problem"), str)
            ):
                raise EvidenceError("comparable animation issue is malformed")
            # Unknown problems default to the fail-safe direction: the recorder
            # rejected a link, so the root is not a legal state.
            found.append(
                (
                    issue["path"],
                    _ANIMATION_PROBLEMS.get(issue["problem"], "dangling_reference"),
                    f"recorded animation issue: {issue['problem']}",
                )
            )
    for name in ANIMATION_OWNERS:
        pool = pools.get(name)
        if pool is None:
            continue
        for slot, value in sorted(pool.live.items()):
            fields = pool.fields[slot]
            for _, offset, field, role in _ANIMATION_BY_POOL[name]:
                reference = fields.get(f"{offset:08x}")
                if reference is None:
                    continue
                violation = _animation_reference(
                    field_path(name, slot, offset),
                    field,
                    reference,
                    f"{name}/{value:08x}/{role}",
                    nodes,
                    _retired(name, fields, role),
                )
                if violation is not None:
                    found.append(violation)
    board = state.get("board")
    if isinstance(board, dict):
        for offset in range(*BOARD_FWOOSH, 4):
            reference = board.get(f"{offset:08x}")
            if reference is None:
                continue
            violation = _animation_reference(
                f"/board/{offset:08x}",
                "fwoosh reanimation",
                reference,
                f"board/fwoosh/{(offset - BOARD_FWOOSH[0]) // 4}",
                nodes,
                False,
            )
            if violation is not None:
                found.append(violation)
    return found


def check_state(state) -> dict:
    """Report every reference violation of one normalized root state."""
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise EvidenceError("root state must be a lvz.audit.v1 object")
    pools = {name: _Pool(name, state[name]) for name in POOLS if name in state}
    if not pools:
        raise EvidenceError("root state carries no captured entity pool")
    found = [*_generation_violations(pools), *_id_violations(state, pools), *_animation_violations(state, pools)]
    violations = sorted(set(found))
    return {
        "consistent": not violations,
        "violations": [{"field_path": path, "kind": kind, "detail": detail} for path, kind, detail in violations],
    }


def _first_state(store) -> dict:
    """The first complete state of a state-delta stream; later records are patches."""
    with store.open(STATE_DELTAS) as stream:
        for line in stream:
            if len(line) > MAX_RECORD_BYTES:
                raise EvidenceError("state delta record is oversized")
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                raise EvidenceError("state delta record is not JSON") from error
            if not isinstance(record, dict) or record.get("schema") != STATE_SCHEMA:
                raise EvidenceError("state delta schema mismatch")
            if isinstance(record.get("initial"), dict):
                return record["initial"]
            if not isinstance(record.get("patch"), list):
                raise EvidenceError("state delta record carries neither a complete state nor a patch")
    raise EvidenceError("state deltas carry no complete state")


def _packaged_root(directory: Path) -> dict:
    manifest = read_json(directory / MANIFEST_FILE)
    if not isinstance(manifest, dict) or manifest.get("schema") != TRAJECTORY_SCHEMA:
        raise EvidenceError("packaged trajectory schema mismatch")
    if manifest.get("trajectory_id") != content_identity(manifest):
        raise EvidenceError("packaged trajectory content identity mismatch")
    store = evidence_codec.EvidenceStore(directory / "audit", error=EvidenceError)
    stored = store.stored_path(STATE_DELTAS)
    files = manifest.get("files")
    recorded = files.get(f"audit/{stored.name}") if isinstance(files, dict) else None
    if not stored.is_file() or not isinstance(recorded, str) or file_hash(stored) != recorded:
        raise EvidenceError("packaged trajectory state deltas are missing or do not match their recorded hash")
    return _first_state(store)


def read_root(source) -> dict:
    """The root state of a packaged trajectory directory or a state JSON file/object."""
    if isinstance(source, dict):
        return source
    path = Path(source)
    if path.is_dir():
        if not (path / MANIFEST_FILE).is_file():
            raise EvidenceError(f"packaged trajectory is missing {MANIFEST_FILE}: {path}")
        return _packaged_root(path)
    if not path.is_file():
        raise EvidenceError(f"root source is neither a directory nor a file: {path}")
    return read_json(path)


def check_root(source) -> dict:
    """Check the root of a packaged trajectory or of a normalized state JSON."""
    return check_state(read_root(source))
