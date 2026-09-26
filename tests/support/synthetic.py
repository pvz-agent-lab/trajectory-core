"""A synthetic, game-free recorder for the legacy ``lvz.*`` evidence format.

This is an independent small simulator adapted from the old repository's test
helper ``ModelTransport`` (commit 11917a7, see ``NOTICES.md``).  It writes the
exact same audit artifacts and session trace as the old runtime protocol, but
without a socket, a client or a game: ``SyntheticRuntime.exchange`` takes the
request dictionary directly, and ``RecordingDriver`` emits the session-trace
records the old ``Client`` emitted.  Everything here is test-only and is never
part of the installed package.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from pathlib import Path

from trajectory_core.legacy_lvz.audit_compare import SCHEMA, digests
from trajectory_core.legacy_lvz.trajectory import capture_initial, identity_from_launcher

GAME = {
    "schema": SCHEMA,
    "target": "synthetic-test-only",
    "loaded_signatures_match": True,
    "coverage": {"complete_game_state": False},
    "original_engine_replay_verified": False,
}
BUILD = {"runtime_protocol": 1, "avz_commit": "synthetic-test-only", "pointer_bits": 32}
ARTIFACTS = {
    "input_hashes": {"game": "1" * 64},
    "module_hashes": {"runtime": "2" * 64},
    "profile_hashes": {"profile": "3" * 64},
}


class SessionTrace:
    """The append-only JSONL writer from ``llm_vs_zombies.session`` (test copy)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._closed = False

    def emit(self, kind: str, data: object) -> dict:
        record = {"schema": 1, "seq": self._seq, "recorded_at": "synthetic", "kind": kind, "data": data}
        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        self._file.flush()
        self._seq += 1
        return record

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True

    def __enter__(self) -> SessionTrace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SyntheticRuntime:
    """Writes closed audit evidence for advance/commit requests."""

    def __init__(
        self, directory: str | Path, *, epoch: int = 1, revision: int = 0, terminal_tick: int | None = None
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.game = copy.deepcopy(GAME)
        (self.directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
        self.files = {
            name: (self.directory / name).open("w", encoding="utf-8", newline="\n")
            for name in ("events.jsonl", "checksums.jsonl", "state-deltas.jsonl")
        }
        self.seq = 0
        self.tick = 0
        self.revision = revision
        self.epoch = epoch
        self.sun = 500
        self.previous: dict | None = None
        self.closed = False
        self.terminal_tick = terminal_tick
        self.game_ui = 3

    def version(self) -> dict:
        return {"epoch": self.epoch, "tick": self.tick, "revision": self.revision}

    def observe(self) -> dict:
        return {
            "version": self.version(),
            "game_ui": self.game_ui,
            "scene": 3,
            "sun": self.sun,
            "game_clock": self.tick,
            "wave": 1,
            "plants": [],
            "zombies": [],
            "seeds": [],
        }

    def state(self) -> dict:
        return {
            "schema": SCHEMA,
            "rng": {"target": GAME["target"], "state": 1 + self.tick},
            "board": {"tick": self.tick, "sun": self.sun, "render_effect": 0},
        }

    def write(self, name: str, value: dict) -> None:
        self.files[name].write(json.dumps(value, separators=(",", ":")) + "\n")
        self.files[name].flush()

    def event(self, kind: str, payload: dict) -> None:
        value = {"schema": SCHEMA, "seq": self.seq, "kind": kind, "version": self.version(), "payload": payload}
        self.seq += 1
        if kind in {"pre_step", "post_step"}:
            state = self.state()
            self.write("checksums.jsonl", dict(value, digests=digests(state)))
            delta = (
                {"initial": state}
                if self.previous is None
                else {"patch": [{"op": "replace", "path": "", "value": state}]}
            )
            self.write("state-deltas.jsonl", dict(value, **delta))
            self.previous = state
        else:
            self.write("events.jsonl", value)

    def exchange(self, request: dict) -> dict:
        method, params, rid = request["method"], request["params"], request["request_id"]
        if method == "hello":
            result = {
                "build": copy.deepcopy(BUILD),
                "game": copy.deepcopy(self.game),
                "session": "synthetic",
                "pid": 123,
                "capabilities": dict.fromkeys(("observe", "advance", "commit", "audit_snapshot"), True),
            }
        elif method == "observe":
            result = self.observe()
        elif method == "audit_snapshot":
            result = {"state": self.state(), "version": self.version()}
        elif method == "status":
            result = {
                "state": "paused_at_boundary",
                "fault": None,
                "version": self.version(),
                "pending_request_id": None,
                "executed_ticks": 0,
            }
        elif method == "pause":
            result = {"state": "paused_at_boundary", "observation": self.observe()}
        elif method == "stop_recording":
            self.event("recording_closed", {"request_id": rid})
            result = {"closed": True, "observation": self.observe()}
        elif method in {"commit", "advance"}:
            result = self._step(method, params, rid, request)
        else:
            raise AssertionError(f"synthetic runtime does not implement {method}")
        return {"protocol": 1, "request_id": rid, "ok": True, "result": result}

    def _step(self, method: str, params: dict, rid: str, request: dict) -> dict:
        terminal = False
        self.event("request_started", {"request_id": rid, "request": copy.deepcopy(request)})
        outcomes = []
        count = params["max_ticks" if method == "advance" else "advance_ticks"]
        executed, reason = 0, "budget_exhausted"
        for ordinal, action in enumerate(params.get("actions", [])):
            outcome = {"ok": action["op"] == "plant", "action": action, "ordinal": ordinal}
            if not outcome["ok"]:
                outcome["error"] = "synthetic_failure"
            outcomes.append(outcome)
            self.revision += 1
            self.event("action", {"request_id": rid, "ordinal": ordinal, "action": action, "result": outcome})
            if not outcome["ok"]:
                reason = "action_failed"
                break
        if reason != "action_failed":
            for _ in range(count):
                self.event("pre_step", {"request_id": rid, "requested_ticks": count, "executed_ticks": executed})
                self.tick += 1
                self.revision = 0
                executed += 1
                terminal = self.tick == self.terminal_tick
                if terminal:
                    self.game_ui = 2
                self.event("post_step", {"request_id": rid, "native_tick_delta": 1, "executed_ticks": executed})
                if terminal:
                    self.event(
                        "terminal_transition",
                        {
                            "request_id": rid,
                            "native_tick_delta": 1,
                            "tick_delta_verified": True,
                            "board_identity_preserved": True,
                        },
                    )
                    reason = "scene_changed"
                    break
        result = {
            "action_results": outcomes,
            "requested_ticks": count,
            "executed_ticks": executed,
            "stop_reason": reason,
            "observation": self.observe(),
        }
        self.event("request_completed", {"request_id": rid, "result": result})
        if terminal:
            self.epoch += 1
            self.tick = self.revision = 0
        return result

    def close(self) -> None:
        if not self.closed:
            for stream in self.files.values():
                stream.close()
            self.closed = True


class RecordingDriver:
    """The minimal session-trace-emitting client surface ``capture_initial`` needs."""

    def __init__(self, runtime: SyntheticRuntime, trace: SessionTrace) -> None:
        self.runtime = runtime
        self.trace = trace
        self.version: dict | None = None
        self._ids: set[str] = set()

    def request(
        self, method: str, params: dict | None = None, *, expect: dict | None = None, request_id: str | None = None
    ) -> dict:
        if method in {"commit", "advance"} and expect is None:
            if self.version is None:
                self.observe()
            expect = self.version
        rid = uuid.uuid4().hex if request_id is None else request_id
        request = {"protocol": 1, "request_id": rid, "method": method, "params": dict(params or {})}
        if expect is not None:
            request["expect"] = dict(expect)
        self.trace.emit("request", request)
        self._ids.add(rid)
        response = self.runtime.exchange(request)
        self.trace.emit("response", response)
        result = response["result"]
        if method in {"commit", "advance"}:
            self.version = result["observation"]["version"]
        elif method == "observe":
            self.version = result["version"]
        return result

    def hello(self) -> dict:
        return self.request("hello")

    def observe(self) -> dict:
        return self.request("observe")

    def commit(self, actions: list[dict], *, advance_ticks: int = 0) -> dict:
        return self.request("commit", {"actions": [dict(a) for a in actions], "advance_ticks": advance_ticks})

    def advance(self, max_ticks: int) -> dict:
        return self.request("advance", {"max_ticks": max_ticks})

    def stop_recording(self) -> dict:
        return self.request("stop_recording", expect=self.version)


def record_session(
    root: str | Path,
    *,
    seed_actions: bool = True,
    terminal_tick: int | None = None,
    plan: Callable[[RecordingDriver], None] | None = None,
) -> Path:
    """Write one closed synthetic recording and return its session trace path."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    runtime = SyntheticRuntime(root / "audit", terminal_tick=terminal_tick)
    with SessionTrace(root / "session.jsonl") as trace:
        driver = RecordingDriver(runtime, trace)
        identity = identity_from_launcher(driver.hello(), ARTIFACTS)
        capture_initial(driver, identity=identity, initialization={"synthetic": True, "seed": 42})
        if plan is not None:
            plan(driver)
        elif seed_actions:
            driver.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=3)
            driver.advance(2)
        driver.stop_recording()
    runtime.close()
    return root / "session.jsonl"
