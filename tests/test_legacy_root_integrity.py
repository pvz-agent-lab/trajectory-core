"""Legacy root-integrity adapter: references, target types and generation clashes (#35).

Isolated legacy behaviour: these game-state semantics are kept out of the public
schema but remain byte-compatible for existing evidence. See NOTICES.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trajectory_core import EvidenceError, content_identity
from trajectory_core.legacy_lvz.root_integrity import check_root, check_state, field_path, identity, read_root

PLANT = identity(1001, 1)  # plants: generation 1001, slot 1
ZOMBIE = identity(1001, 2)  # zombies: generation 1001, slot 2 (the same numbers, another pool)
PEER = identity(1002, 3)  # zombies: generation 1002, slot 3
ATTACHMENT = identity(2000, 1)
PLANT_BODY = f"plants/{PLANT:08x}/body"
ZOMBIE_BODY = f"zombies/{ZOMBIE:08x}/body"
PEER_BODY = f"zombies/{PEER:08x}/body"


def entity(value, fields=None):
    return {"id_or_free_next": value, "fields": {f"{offset:08x}": field for offset, field in (fields or {}).items()}}


def pool(*, next_key, used, slots, count=None):
    return {
        "used": used,
        "capacity": 1024,
        "count": len(slots) if count is None else count,
        "free_head": 0,
        "next_key": next_key,
        "slots": {str(slot): value for slot, value in slots.items()},
    }


def live(*owners):
    return {"state": {"type": 1}, "owners": list(owners)}


def root_state():
    """One legal root: both references resolve, both links are owned in place."""
    return {
        "schema": "lvz.audit.v1",
        "board": {"00005624": {"status": "live", "node": "board/fwoosh/0"}, "00005628": {"status": "null"}},
        "plants": pool(
            next_key=1002,
            used=2,
            slots={
                0: {"id_or_free_next": 1},
                1: entity(PLANT, {0x12C: ZOMBIE, 0x94: {"status": "live", "node": PLANT_BODY}, 0x141: 0, 0x142: 0}),
            },
        ),
        "zombies": pool(
            next_key=1003,
            used=4,
            slots={
                0: {"id_or_free_next": 2},
                2: entity(
                    ZOMBIE,
                    {
                        0xEC: 0,
                        0xF0: PEER,
                        0xF4: 0,
                        0x100: 0,
                        0x110: ATTACHMENT,
                        0x118: {"status": "live", "node": ZOMBIE_BODY},
                        0x128: PLANT,
                    },
                ),
                3: entity(PEER, {0xEC: 1, 0xF0: 0, 0x118: {"status": "expired"}, 0x150: {"status": "null"}}),
            },
        ),
        "reanimations": {
            "schema": "lvz.reanimation-links.v1",
            "valid": True,
            "issues": [],
            "nodes": {
                "board/fwoosh/0": live("board/fwoosh/0"),
                PLANT_BODY: live(f"plants/{PLANT:08x}/body"),
                ZOMBIE_BODY: live(f"zombies/{ZOMBIE:08x}/body"),
            },
        },
    }


def packaged(directory: Path, state: dict, **manifest):
    """A trajectory bundle whose first complete state is ``state``."""
    (directory / "audit").mkdir(parents=True)
    deltas = directory / "audit" / "state-deltas.jsonl"
    rows = [
        {"schema": "lvz.audit.v1", "seq": 0, "kind": "pre_step", "payload": {}, "initial": state},
        {
            "schema": "lvz.audit.v1",
            "seq": 1,
            "kind": "post_step",
            "payload": {},
            "patch": [{"op": "replace", "path": "/zombies/next_key", "value": 1001}],
        },
    ]
    deltas.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
    record = {
        "schema": "lvz.engine-replay.v1",
        "source": "session_trace",
        "initial": {},
        "steps": [],
        "files": {"audit/state-deltas.jsonl": hashlib.sha256(deltas.read_bytes()).hexdigest()},
        "scope": "synthetic state; no game and no network",
    }
    record.update(manifest)
    record["trajectory_id"] = content_identity(record)
    (directory / "trajectory.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


class RootIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, change=None):
        state = root_state()
        if change is not None:
            change(state)
        return check_state(state)

    def only(self, result, kind, path):
        self.assertFalse(result["consistent"])
        self.assertEqual([entry["kind"] for entry in result["violations"]], [kind])
        self.assertEqual([entry["field_path"] for entry in result["violations"]], [path])
        return result["violations"][0]

    def test_legal_root_is_consistent(self):
        self.assertEqual(check_state(root_state()), {"consistent": True, "violations": []})

    def test_absent_target_is_dangling(self):
        violation = self.only(
            self.check(lambda state: state["plants"]["slots"]["1"]["fields"].update({"0000012c": identity(1000, 5)})),
            "dangling_reference",
            "/plants/slots/1/fields/0000012c",
        )
        self.assertIn("beyond used", violation["detail"])

    def test_free_slot_and_stale_generation_are_dangling(self):
        free = self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"000000f0": identity(1003, 0)})),
            "dangling_reference",
            "/zombies/slots/2/fields/000000f0",
        )
        self.assertIn("slot 0 is free", free["detail"])
        stale = self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"000000f0": identity(1000, 3)})),
            "dangling_reference",
            "/zombies/slots/2/fields/000000f0",
        )
        self.assertIn(f"now carries 0x{PEER:08x}", stale["detail"])

    def test_slot_index_in_an_id_field_is_a_type_mismatch(self):
        violation = self.only(
            self.check(lambda state: state["plants"]["slots"]["1"]["fields"].update({"0000012c": 5})),
            "type_mismatch",
            "/plants/slots/1/fields/0000012c",
        )
        self.assertIn("no generation bits", violation["detail"])

    def test_reference_to_another_pool_is_a_type_mismatch(self):
        violation = self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"00000128": PEER})),
            "type_mismatch",
            "/zombies/slots/2/fields/00000128",
        )
        self.assertIn("live zombies object", violation["detail"])

    def test_non_integer_id_and_raw_animation_handle_are_type_mismatches(self):
        self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"000000f0": "3"})),
            "type_mismatch",
            "/zombies/slots/2/fields/000000f0",
        )
        raw = self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"00000118": ZOMBIE})),
            "type_mismatch",
            "/zombies/slots/2/fields/00000118",
        )
        self.assertIn("not a raw handle", raw["detail"])

    def test_live_link_owned_by_another_owner_is_a_type_mismatch(self):
        def change(state):
            state["reanimations"]["nodes"][PEER_BODY] = live(f"zombies/{PEER:08x}/body")
            state["zombies"]["slots"]["3"]["fields"]["00000118"] = {"status": "live", "node": PEER_BODY}
            state["zombies"]["slots"]["2"]["fields"]["00000118"] = {"status": "live", "node": PEER_BODY}

        violation = self.only(self.check(change), "type_mismatch", "/zombies/slots/2/fields/00000118")
        self.assertIn(f"zombies/{PEER:08x}/body", violation["detail"])

    def test_expired_link_on_a_live_owner_is_dangling(self):
        self.only(
            self.check(
                lambda state: state["zombies"]["slots"]["2"]["fields"].update({"00000118": {"status": "expired"}})
            ),
            "dangling_reference",
            "/zombies/slots/2/fields/00000118",
        )

    def test_missing_animation_node_is_dangling(self):
        violation = self.only(
            self.check(
                lambda state: state["zombies"]["slots"]["2"]["fields"].update(
                    {"00000118": {"status": "live", "node": "zombies/00010001/body"}}
                )
            ),
            "dangling_reference",
            "/zombies/slots/2/fields/00000118",
        )
        self.assertIn("not a live animation node", violation["detail"])

    def test_recorded_animation_issue_is_reported(self):
        def change(state):
            state["reanimations"]["valid"] = False
            state["reanimations"]["issues"] = [
                {"path": "/zombies/slots/2/fields/00000118", "problem": "live_owner_dangling_handle"}
            ]

        violation = self.only(self.check(change), "dangling_reference", "/zombies/slots/2/fields/00000118")
        self.assertIn("live_owner_dangling_handle", violation["detail"])

    def test_recorded_dangling_status_is_a_dangling_reference(self):
        violation = self.only(
            self.check(
                lambda state: state["zombies"]["slots"]["2"]["fields"].update({"00000118": {"status": "dangling"}})
            ),
            "dangling_reference",
            "/zombies/slots/2/fields/00000118",
        )
        self.assertIn("dangling", violation["detail"])

    def test_loaded_dat_generation_signature(self):
        """D3-3: the counter restarts at 1001 while saved objects keep their keys."""

        def change(state):
            state["zombies"]["next_key"] = 1001
            state["plants"]["next_key"] = 1001

        result = self.check(change)
        self.assertFalse(result["consistent"])
        self.assertEqual([entry["kind"] for entry in result["violations"]], ["generation_conflict"] * 2)
        self.assertEqual(
            [entry["field_path"] for entry in result["violations"]], ["/plants/next_key", "/zombies/next_key"]
        )
        self.assertIn("ABA", result["violations"][1]["detail"])
        self.assertIn("generation 1002", result["violations"][1]["detail"])

    def test_two_live_objects_may_not_share_a_generation(self):
        def change(state):
            state["zombies"]["slots"]["1"] = entity(identity(1001, 1), {0xEC: 0})
            state["zombies"]["count"] = 3

        violation = self.only(self.check(change), "generation_conflict", "/zombies/slots/2/id_or_free_next")
        self.assertIn("already live at slot 1", violation["detail"])

    def test_reissued_reference_generation_is_a_conflict(self):
        violation = self.only(
            self.check(lambda state: state["zombies"]["slots"]["2"]["fields"].update({"000000f0": identity(1002, 9)})),
            "generation_conflict",
            "/zombies/slots/2/fields/000000f0",
        )
        self.assertIn("was re-issued", violation["detail"])

    def test_violations_are_sorted_by_field_path(self):
        def change(state):
            state["plants"]["slots"]["1"]["fields"]["0000012c"] = identity(1000, 5)
            state["zombies"]["slots"]["2"]["fields"]["00000118"] = {"status": "expired"}

        result = self.check(change)
        self.assertEqual(
            [entry["field_path"] for entry in result["violations"]],
            ["/plants/slots/1/fields/0000012c", "/zombies/slots/2/fields/00000118"],
        )
        self.assertEqual(
            [entry["kind"] for entry in result["violations"]], ["dangling_reference", "dangling_reference"]
        )

    def test_packaged_trajectory_reads_its_first_complete_state(self):
        bundle = self.root / "bundle"
        packaged(bundle, root_state())
        self.assertEqual(check_root(bundle), {"consistent": True, "violations": []})
        self.assertEqual(read_root(bundle), root_state())
        # The later patch resets next_key and is deliberately not the root.
        self.assertIn('"patch"', (bundle / "audit" / "state-deltas.jsonl").read_text())

    def test_packaged_trajectory_reports_an_inconsistent_root(self):
        state = root_state()
        state["zombies"]["next_key"] = 1001
        bundle = self.root / "loaded"
        packaged(bundle, state)
        result = check_root(bundle)
        self.assertEqual([entry["field_path"] for entry in result["violations"]], ["/zombies/next_key"])
        self.assertEqual(result["violations"][0]["kind"], "generation_conflict")

    def test_packaged_trajectory_refuses_a_tampered_bundle(self):
        bundle = self.root / "tampered"
        record = packaged(bundle, root_state())
        deltas = bundle / "audit" / "state-deltas.jsonl"
        # Sealed state deltas that no longer match the hash in the manifest.
        deltas.write_text(
            deltas.read_text(encoding="utf-8").replace('"next_key": 1003', '"next_key": 1007'),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaises(EvidenceError):
            check_root(bundle)
        # A manifest whose content identity no longer covers its own fields.
        record["files"]["audit/state-deltas.jsonl"] = hashlib.sha256(deltas.read_bytes()).hexdigest()
        record["trajectory_id"] = "0" * 64
        (bundle / "trajectory.json").write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(EvidenceError):
            check_root(bundle)

    def test_state_json_file_is_accepted(self):
        path = self.root / "root-state.json"
        path.write_text(json.dumps(root_state()), encoding="utf-8")
        self.assertEqual(read_root(path), root_state())
        self.assertEqual(check_root(path), {"consistent": True, "violations": []})
        self.assertEqual(check_root(root_state()), {"consistent": True, "violations": []})

    def test_malformed_inputs_are_rejected(self):
        with self.assertRaises(EvidenceError):
            check_state({})
        with self.assertRaises(EvidenceError):
            check_state({"schema": "lvz.audit.v1"})
        broken = copy.deepcopy(root_state())
        broken["zombies"]["slots"]["2"]["id_or_free_next"] = identity(1001, 4)
        with self.assertRaises(EvidenceError):
            check_state(broken)
        empty = copy.deepcopy(root_state())
        empty["reanimations"]["nodes"] = []
        with self.assertRaises(EvidenceError):
            check_state(empty)
        with self.assertRaises(EvidenceError):
            read_root(self.root / "missing.json")
        bare = self.root / "bare"
        bare.mkdir()
        with self.assertRaises(EvidenceError):
            read_root(bare)

    def test_field_paths_are_lowercase_hex_offsets(self):
        self.assertEqual(field_path("zombies", 2, 0x118), "/zombies/slots/2/fields/00000118")

    def test_board_level_references_are_checked(self):
        self.only(self.check(lambda state: state["board"].update({"00005588": 5})), "type_mismatch", "/board/00005588")
        self.only(
            self.check(lambda state: state["board"].update({"00005628": {"status": "live", "node": "board/fwoosh/1"}})),
            "dangling_reference",
            "/board/00005628",
        )
