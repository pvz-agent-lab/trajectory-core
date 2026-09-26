"""Strict readers and first-field diagnostics for native lvz.audit.v1 evidence.

Matching this deliberately incomplete state schema is not a determinism proof.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import struct
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import EvidenceError
from . import (
    app_update_anchor,
    b0_normalization,
    evidence_codec,
    fp_environment,
    lifecycle_events,
    mj_clock_anchor,
    sound_counter,
    sound_effects,
)

SCHEMA = "lvz.audit.v1"
RAW_ANIMATIONS = "reanimation-handles.jsonl"
PARTICLE_SHAKE_RAW = "particle-shake-seeds.jsonl"
PARTICLE_SHAKE_MODE = "deterministic_particle_shake_v1"
DRAW_SCHEDULE_MODE = "deterministic_draw_schedule_v1"
ENGINE_CALL_MODE = "controlled_engine_call_v1"
ENGINE_CALL_RAW = "engine-call-raw.jsonl"
HOSTED_FIRE_MODE = "hosted_fire_audit_v1"
HOSTED_FIRE_KIND = "hosted_fire"
# The shot's integer fields, in the native digest's canonical order. `tick` is
# also the envelope version's tick; `target_col_text` is display only and is
# re-derived from `target_col_bits` below.
_HOSTED_FIRE_FIELDS = ("plant_index", "plant_id", "plant_row", "plant_col", "target_row", "target_col_bits", "tick")
_HOSTED_FIRE_PAYLOAD_KEYS = {"source", "op", "target_col_text", *_HOSTED_FIRE_FIELDS}
AUDIT_FILES = ("manifest.json", "events.jsonl", "checksums.jsonl", "state-deltas.jsonl")
_POINTER_CACHE_SIZE = 16384
_POINTER_CACHE_MAX_CHARS = 512
_OWNER_RETIREMENT_RULES = ["owner_dead", "plant_squished_remove_effects"]
_FLAG_RETIREMENT_RULES = _OWNER_RETIREMENT_RULES + ["zombie_flag_dropped"]
_PARTICLE_BOUNDARY_LIMIT = 8192
_SPAWN_BOUNDARY_LIMIT = 1024
_JSONL_MAX_RECORD_BYTES = 32 << 20
_FNV_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#if defined(_WIN32)
__declspec(dllexport)
#endif
uint64_t lvz_fnv1a(const unsigned char* bytes, size_t length) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < length; ++i) {
        hash ^= bytes[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
#if defined(_WIN32)
__declspec(dllexport)
#endif
uint64_t lvz_fnv1a_continue(uint64_t hash, const unsigned char* bytes, size_t length) {
    for (size_t i = 0; i < length; ++i) {
        hash ^= bytes[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
"""
_hash_lock = threading.Lock()
_hash_initialized = False
_hash_native = None
_hash_continue = None


def _python_fnv(encoded: bytes, result: int = 14695981039346656037) -> int:
    for byte in encoded:
        result = ((result ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return result


def _native_hash():
    """Optional tiny local C accelerator; never loads into the game process.

    Stdlib-only installations retain the exact Python implementation. The
    compiled helper is a pure optimization: every entry point is compared
    against ``_python_fnv`` before it is trusted, so a wrong or missing toolchain
    can only make the reader slower, never different. Nothing is written into
    the package or the current directory; the cache lives under the system
    temporary directory. ``TRAJECTORY_CORE_FNV=python`` and the legacy
    ``LVZ_PYTHON_FNV=1`` force the pure Python implementation.
    """
    global _hash_initialized, _hash_native, _hash_continue
    with _hash_lock:
        if _hash_initialized:
            return _hash_native
        _hash_initialized = True
        if os.environ.get("LVZ_PYTHON_FNV") == "1" or os.environ.get("TRAJECTORY_CORE_FNV", "").strip().lower() in {
            "python",
            "pure",
            "off",
        }:
            return None
        # Toolchain and process imports live here, not at module import: a
        # plain installation only ever needs the Python implementation.
        import ctypes
        import platform
        import shutil
        import subprocess
        import tempfile

        compiler = os.environ.get("TRAJECTORY_CORE_FNV_COMPILER")
        compiler = compiler or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not compiler:
            return None
        identity = hashlib.sha256(
            (_FNV_SOURCE + platform.machine() + str(ctypes.sizeof(ctypes.c_void_p))).encode()
        ).hexdigest()[:20]
        directory = Path(tempfile.gettempdir()) / "trajectory-core-fnv" / identity
        try:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                directory = Path(tempfile.mkdtemp(prefix="lvz-audit-hash-"))
            library = directory / ("fnv.dll" if os.name == "nt" else "fnv.so")
            if not library.exists():
                source = directory / "fnv.c"
                source.write_text(_FNV_SOURCE, encoding="ascii")
                output = directory / (uuid.uuid4().hex + library.suffix)
                command = [str(compiler), "-O3", "-std=c11", "-shared", str(source), "-o", str(output)]
                command += ["-static"] if os.name == "nt" else ["-fPIC"]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=45,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode:
                    return None
                if not library.exists():
                    os.replace(output, library)
                elif output.exists():
                    output.unlink()
            module = ctypes.CDLL(str(library.resolve()))
            function = module.lvz_fnv1a
            function.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            function.restype = ctypes.c_uint64
            continued = module.lvz_fnv1a_continue
            continued.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
            continued.restype = ctypes.c_uint64
            for fixture in (b"", b"hello", bytes(range(256)), b"\x00\xff\x00"):
                if function(fixture, len(fixture)) != _python_fnv(fixture):
                    return None
                for seed in (0, 1, 0xFFFFFFFFFFFFFFFF, 14695981039346656037):
                    if continued(seed, fixture, len(fixture)) != _python_fnv(fixture, seed):
                        return None
            _hash_native = function  # ctypes retains its owning loaded library.
            _hash_continue = continued
        except (OSError, AttributeError, subprocess.SubprocessError):
            return None
        return _hash_native


def hash_backend() -> str:
    return "native_c_fnv1a" if _native_hash() is not None else "python_fnv1a"


def _fnv(encoded: bytes) -> str:
    function = _hash_native if _hash_initialized else _native_hash()
    value = function(encoded, len(encoded)) if function is not None else _python_fnv(encoded)
    return f"{value:016x}"


def _fnv_continue(seed: int, encoded: bytes) -> int:
    if not _hash_initialized:
        _native_hash()
    return _hash_continue(seed, encoded, len(encoded)) if _hash_continue is not None else _python_fnv(encoded, seed)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data: str | bytes) -> Any:
    try:
        return json.loads(
            data, object_pairs_hook=_unique, parse_constant=lambda value: (_ for _ in ()).throw(EvidenceError(value))
        )
    except (ValueError, UnicodeError) as error:
        raise EvidenceError(f"invalid JSON: {error}") from error


def read_json(path: Path) -> Any:
    return decode(path.read_bytes())


def jsonl(path: Path) -> Iterator[dict]:
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                raise EvidenceError(f"{path.name}:{number}: incomplete JSONL tail")
            value = decode(line)
            if not isinstance(value, dict):
                raise EvidenceError(f"{path.name}:{number}: object required")
            yield value


class EventStream:
    """Repeatable, hash-bound JSONL view; no event payloads remain resident.

    Each read checks inode/size/timestamps and all bytes against the original
    SHA-256. Closing an early iterator also verifies the unread suffix.
    A stored gzip container is verified through its plain bytes, so the bound
    digest is the same logical content the plain recorder produced.
    """

    def __init__(self, store, name: str | None = None):
        if name is None:
            path = Path(store)
            store = evidence_codec.EvidenceStore(path.parent, error=EvidenceError)
            name = path.name
        self.store, self.name = store, name
        path = store.stored_path(name)
        self.path = path
        self.signature = self._signature()
        self.sha256 = store.plain_sha256(name)
        self._check_signature()

    def _signature(self):
        stat = self.path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _check_signature(self):
        if self._signature() != self.signature:
            raise EvidenceError(f"closed audit file was changed/replaced/truncated: {self.path.name}")

    def verify(self):
        self._check_signature()
        self.store.verify(self.name)
        if self.store.plain_sha256(self.name) != self.sha256:
            raise EvidenceError(f"closed audit file SHA-256 changed: {self.path.name}")
        self._check_signature()

    def __iter__(self):
        self._check_signature()
        digest, completed = hashlib.sha256(), False
        try:
            with self.store.open(self.name) as stream:
                number = 0
                while line := stream.readline(_JSONL_MAX_RECORD_BYTES + 1):
                    number += 1
                    if len(line) > _JSONL_MAX_RECORD_BYTES:
                        raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                    digest.update(line)
                    if not line.endswith(b"\n"):
                        raise EvidenceError(f"{self.path.name}:{number}: incomplete JSONL tail")
                    value = decode(line)
                    if not isinstance(value, dict):
                        raise EvidenceError(f"{self.path.name}:{number}: object required")
                    yield value
            completed = True
            if digest.hexdigest() != self.sha256:
                raise EvidenceError(f"closed audit file SHA-256 changed: {self.path.name}")
        finally:
            self._check_signature()
            if not completed:
                # An early close still proves the stored bytes (and, for a
                # container, the plain bytes it reproduces) are unchanged.
                self.store.verify(self.name)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def draw_mode(manifest):
    spec = manifest.get("draw_schedule")
    if spec is None:
        return None
    required = {
        "mode": DRAW_SCHEDULE_MODE,
        "installed": True,
        "original_engine_bitwise_unmodified": False,
        "target_entry_rva": 0x138EB0,
        "autonomous_fight_draws": False,
        "capture": "cached_bgr24_only",
        "rng_restore_after_draw": False,
        "schedule": "one warm draw before B0; one original draw after each verified update before post_step",
    }
    if (
        not isinstance(spec, dict)
        or any(type(spec.get(key)) is not type(value) or spec[key] != value for key, value in required.items())
        or type(spec.get("live_verified")) is not bool
    ):
        raise EvidenceError("unsupported or undeclared controlled draw semantics")
    return DRAW_SCHEDULE_MODE


def engine_call_mode(manifest):
    spec = manifest.get("engine_call_boundary")
    if spec is None:
        return None
    required = {
        "mode": ENGINE_CALL_MODE,
        "entry_rva": 0x52650,
        "invocation_evidence": "checked_wrapper_enter_and_return",
        "call_id_scope": "process_controlled_calls_only",
        "counter_bits": 64,
        "initialization_calls_counted": False,
        "ordinary_clock_delta": 1,
        "terminal_zero_clock_update": True,
        "zero_terminal_ui_pairs": [[3, 4]],
        "board_identity_basis": "non-null App.Board pointer preserved across the checked call",
        "internal_board_tick_count_verified": False,
        "raw_evidence": ENGINE_CALL_RAW,
    }
    if (
        not isinstance(spec, dict)
        or any(first_difference(value, spec.get(key)) for key, value in required.items())
        or type(spec.get("live_verified")) is not bool
        or draw_mode(manifest) != DRAW_SCHEDULE_MODE
    ):
        raise EvidenceError("unsupported controlled engine call contract")
    return ENGINE_CALL_MODE


def validate_engine_origin(value):
    expected = {"mode": ENGINE_CALL_MODE, "active_call_id": None, "healthy": True}
    expected.update(
        dict.fromkeys(
            (
                "reserved_calls",
                "entered_calls",
                "returned_calls",
                "written_post_boundaries",
                "verified_clock_steps",
                "verified_terminal_zero_calls",
                "terminal_clock_steps",
                "faults",
                "reentrant_calls",
                "wrong_thread_calls",
                "aborted_calls",
            ),
            0,
        )
    )
    if not isinstance(value, dict) or any(
        key not in value or first_difference(item, value[key]) for key, item in expected.items()
    ):
        raise EvidenceError("initial engine call origin is not an actual healthy fresh tracker")


def hosted_fire_mode(manifest):
    """Hosted AvZ cannon shots (issue #84): audit-only, never journaled actions.

    A DLL compiled with `LVZ_AVZ_HOSTED_FIRE_AUDIT` declares the mode below and
    writes one `kind == "hosted_fire"` event per shot plus a comparable
    per-boundary state component. This is deliberately not the `action` kind: a
    hosted shot has no request id, no ordinal and no journal entry, and
    equating it with an action would let a script's fire order pass as a
    replayed request. Any build without the switch writes neither, and both
    halves of the evidence are refused here unless the mode declares them.
    """
    spec = manifest.get("hosted_fire")
    if spec is None:
        return None
    required = {
        "mode": HOSTED_FIRE_MODE,
        "installed": True,
        "kind": HOSTED_FIRE_KIND,
        "source": "hosted_avz_script",
        "hook": "avz_cob_manager._BasicFire after the reviewed AAsm::Fire call",
        "original_engine_bitwise_unmodified": False,
        "semantic_change": "none: audit-only; no engine call is added, removed or reordered",
        "boundary": "the shot is bound to the next audited pre_step (its version)",
    }
    if (
        not isinstance(spec, dict)
        or set(spec) != set(required)
        or any(type(spec.get(key)) is not type(value) or spec[key] != value for key, value in required.items())
    ):
        raise EvidenceError("unsupported or undeclared hosted fire audit mode")
    return HOSTED_FIRE_MODE


def audit_files(directory: Path, manifest: dict) -> tuple[str, ...]:
    """Resolve supported evidence files without trusting a manifest path."""
    directory = Path(directory)
    store = evidence_codec.EvidenceStore(directory, error=EvidenceError)

    def has_evidence(name: str) -> bool:
        # A sealed archive only declares the JSONL evidence it actually
        # contains; asking about an optional name must not raise for an
        # undeclared one. Non-JSONL evidence is never compressed, so it keeps
        # its plain-file check.
        entry = store.entry(name)
        if entry is not None:
            return store.exists(name)
        if store.compressed and name.endswith(".jsonl"):
            return False
        return (directory / name).is_file()

    draw_mode(manifest)
    app_update_anchor.mode(manifest)
    mj_clock_anchor.mode(manifest)
    hosted_fire_mode(manifest)
    counter_mode = sound_counter.mode(manifest)
    fp_mode = fp_environment.mode(manifest)
    coverage = manifest.get("coverage", {})
    if not isinstance(coverage, dict):
        raise EvidenceError("invalid native audit coverage")
    animations = coverage.get("reanimations", {})
    if not isinstance(animations, dict):
        raise EvidenceError("invalid animation coverage")
    if "owner_retirement_rules" in animations and animations["owner_retirement_rules"] not in (
        _OWNER_RETIREMENT_RULES,
        _FLAG_RETIREMENT_RULES,
    ):
        raise EvidenceError("unsupported animation owner retirement rules")
    descriptor = animations.get("raw_handle_evidence")
    required = coverage.get("animation_normalization") is True or manifest.get("animation_normalization") is True
    if descriptor is not None:
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("path") != "audit/" + RAW_ANIMATIONS
            or descriptor.get("encoding") != "initial_plus_json_patch"
            or descriptor.get("binding") != ["seq", "kind", "version"]
            or type(descriptor.get("required")) is not bool
        ):
            raise EvidenceError("unsupported raw animation evidence descriptor")
        required |= descriptor["required"]
    present = has_evidence(RAW_ANIMATIONS)
    if required and not present:
        raise EvidenceError("required raw animation evidence is missing")
    result = AUDIT_FILES + ((RAW_ANIMATIONS,) if present else ())
    particle = manifest.get("particle_shake")
    particle_file = has_evidence(PARTICLE_SHAKE_RAW)
    if particle is not None:
        if (
            not isinstance(particle, dict)
            or particle.get("mode") != PARTICLE_SHAKE_MODE
            or particle.get("installed") is not True
            or particle.get("original_engine_bitwise_unmodified") is not False
            or particle.get("raw_evidence") != PARTICLE_SHAKE_RAW
            or not isinstance(particle.get("semantic_change"), str)
        ):
            raise EvidenceError("unsupported or undeclared particle shake semantics")
        if not particle_file:
            raise EvidenceError("required raw particle shake evidence is missing")
        result += (PARTICLE_SHAKE_RAW,)
    elif particle_file:
        raise EvidenceError("raw particle shake evidence lacks an explicit engine mode")
    call_file = has_evidence(ENGINE_CALL_RAW)
    if engine_call_mode(manifest):
        if not call_file:
            raise EvidenceError("required raw engine call evidence is missing")
        result += (ENGINE_CALL_RAW,)
    elif call_file:
        raise EvidenceError("raw engine call evidence lacks an explicit engine mode")
    audio_file = has_evidence(sound_effects.EVIDENCE)
    if sound_effects.mode(manifest):
        if not audio_file:
            raise EvidenceError("required sound effects activation evidence is missing")
        result += (sound_effects.EVIDENCE,)
    elif audio_file:
        raise EvidenceError("sound effects activation lacks an explicit engine mode")
    counter_file = has_evidence(sound_counter.RAW_FILE)
    if counter_mode:
        if not counter_file:
            raise EvidenceError("required raw sound counter evidence is missing")
        result += (sound_counter.RAW_FILE,)
    elif counter_file:
        raise EvidenceError("raw sound counter evidence lacks an explicit mode")
    fp_file = has_evidence(fp_environment.RAW_FILE)
    if fp_mode:
        if not fp_file:
            raise EvidenceError("required raw FP evidence is missing")
        result += (fp_environment.RAW_FILE,)
    elif fp_file:
        raise EvidenceError("raw FP evidence lacks an explicit mode")
    # Lifecycle recording is opt-in per run. An enabled declaration must ship
    # both the events stream and its close receipt; an undeclared stray stream
    # is refused instead of being silently ignored or treated as zero events.
    try:
        lifecycle = lifecycle_events.mode(manifest)
    except lifecycle_events.LifecycleError as error:
        raise EvidenceError(str(error)) from error

    def lifecycle_present(name: str) -> bool:
        return has_evidence(name)

    declared = (lifecycle_events.EVENTS_FILE, lifecycle_events.RECEIPT_FILE)
    present = [name for name in declared if lifecycle_present(name)]
    if lifecycle == "enabled":
        # The events stream exists from initialization; the close receipt only
        # exists after a successful finish. A live tail therefore reads a
        # recording before its receipt exists; the closed reader and the
        # offline validator are the ones that refuse a missing receipt.
        if not lifecycle_present(lifecycle_events.EVENTS_FILE):
            raise EvidenceError("declared lifecycle recording is missing its events stream")
        result += (lifecycle_events.EVENTS_FILE,)
        if lifecycle_present(lifecycle_events.RECEIPT_FILE):
            result += (lifecycle_events.RECEIPT_FILE,)
    elif present:
        raise EvidenceError("lifecycle evidence lacks an explicitly enabled capability declaration")
    return result


def lifecycle_report(directory: Path, manifest: dict, *, require_close: bool):
    """Run the full lifecycle contract check for a strict evidence reader.

    Any invalid stream, receipt or declared capability becomes an
    ``EvidenceError``; only ``unavailable``/``disabled``/``open`` (live reader)
    pass through. This is the single integration point so ``AuditLog`` and
    ``AuditTail.verify_closed`` cannot accept a merely present receipt.
    """
    try:
        report = lifecycle_events.validate(directory, manifest=manifest, require_close=require_close)
    except lifecycle_events.LifecycleError as error:
        raise EvidenceError(f"lifecycle recording contract error: {error}") from error
    if report["status"] == "failed":
        problems = "; ".join(report["problems"]) or "unspecified failure"
        raise EvidenceError(f"lifecycle recording is invalid: {problems}")
    return report


def _check_state_values(item):
    # Native captures use integer bits, not JSON floating-point values. Reject
    # floats rather than assuming Python and nlohmann serialize them identically.
    if isinstance(item, float):
        raise EvidenceError("audit state must encode floats as integer bits")
    if isinstance(item, dict):
        for child in item.values():
            _check_state_values(child)
    elif isinstance(item, list):
        for child in item:
            _check_state_values(child)


def digest(value: Any) -> str:
    _check_state_values(value)
    return _fnv(canonical(value))


def digests(state: dict) -> dict[str, str]:
    if not isinstance(state, dict):
        raise EvidenceError("audit state must be an object")
    _check_state_values(state)
    return _state_digests(state, {}, set(state))


def _state_digests(state: dict, cache: dict, changed: set[str], *, with_encoded=False):
    """Reuse unchanged component bytes; still verify every stored digest."""
    parts, result = [], {}
    for key in sorted(state):
        if key in changed or key not in cache:
            encoded = canonical(state[key])
            cache[key] = (canonical(key) + b":" + encoded, _fnv(encoded))
        part, result[key] = cache[key]
        parts.append(part)
    for key in list(cache):
        if key not in state:
            del cache[key]
    encoded_state = b"{" + b",".join(parts) + b"}"
    result["all"] = _fnv(encoded_state)
    return (result, encoded_state) if with_encoded else result


def version(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"epoch", "tick", "revision"}:
        raise EvidenceError("invalid version fields")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise EvidenceError("invalid version values")
    return value


def _parse_tokens(pointer: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise EvidenceError("invalid JSON Pointer")
    result = []
    for part in pointer[1:].split("/"):
        index = 0
        while index < len(part):
            if part[index] == "~":
                if index + 1 >= len(part) or part[index + 1] not in "01":
                    raise EvidenceError("invalid JSON Pointer escape")
                index += 1
            index += 1
        result.append(part.replace("~1", "/").replace("~0", "~"))
    return tuple(result)


@lru_cache(maxsize=_POINTER_CACHE_SIZE)
def _cached_tokens(pointer: str) -> tuple[str, ...]:
    # lru_cache stores successful returns only. Tuples cannot be changed by a
    # caller; the cache never stores a target container or dynamic validity.
    return _parse_tokens(pointer)


def _tokens(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str):
        raise EvidenceError("invalid JSON Pointer")
    # Avoid hashing malformed/unhashable inputs and retaining arbitrarily long
    # valid paths. Long paths still receive the same complete strict parsing.
    if type(pointer) is str and len(pointer) <= _POINTER_CACHE_MAX_CHARS:
        return _cached_tokens(pointer)
    return _parse_tokens(pointer)


def _index(container, token: str, *, add=False):
    if isinstance(container, dict):
        return token
    if not isinstance(container, list):
        raise EvidenceError("JSON Pointer traverses a scalar")
    if add and token == "-":
        return len(container)
    if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
        raise EvidenceError("invalid JSON Pointer array index")
    index = int(token)
    if index >= len(container) + int(add):
        raise EvidenceError("JSON Pointer array index out of bounds")
    return index


def patch(document: Any, operations: list[dict], *, in_place: bool = False) -> Any:
    """Apply RFC 6902 add/remove/replace emitted by nlohmann::json::diff.

    Other operations are rejected explicitly; this is a native-format reader,
    not a permissive general JSON Patch implementation.
    """
    if not isinstance(operations, list):
        raise EvidenceError("patch must be an array")
    result = document if in_place else copy.deepcopy(document)
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in {"add", "remove", "replace"}:
            raise EvidenceError("unsupported JSON Patch operation")
        op = operation["op"]
        if set(operation) != ({"op", "path"} if op == "remove" else {"op", "path", "value"}):
            raise EvidenceError("invalid JSON Patch fields")
        tokens = _tokens(operation["path"])
        if not tokens:
            if op == "remove":
                raise EvidenceError("native state root cannot be removed")
            result = copy.deepcopy(operation["value"])
            continue
        parent = result
        try:
            for token in tokens[:-1]:
                parent = parent[_index(parent, token)]
            key = _index(parent, tokens[-1], add=op == "add")
            if op == "add":
                if isinstance(parent, list):
                    parent.insert(key, copy.deepcopy(operation["value"]))
                else:
                    parent[key] = copy.deepcopy(operation["value"])
            elif op == "remove":
                del parent[key]
            else:
                parent[key]  # Replacement requires an existing target.
                parent[key] = copy.deepcopy(operation["value"])
        except (IndexError, KeyError, TypeError) as error:
            raise EvidenceError(f"invalid patch target: {operation['path']}") from error
    return result


def first_difference(expected: Any, actual: Any, path: str = "") -> dict | None:
    if type(expected) is not type(actual):
        return {"path": path, "expected": expected, "actual": actual, "reason": "type"}
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in expected or key not in actual:
                return {
                    "path": child,
                    "reason": "missing_field",
                    "expected_present": key in expected,
                    "actual_present": key in actual,
                    "expected": expected.get(key),
                    "actual": actual.get(key),
                }
            difference = first_difference(expected[key], actual[key], child)
            if difference:
                return difference
    elif isinstance(expected, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}/{index}")
            if difference:
                return difference
        if len(expected) != len(actual):
            return {"path": path, "reason": "array_length", "expected": len(expected), "actual": len(actual)}
    elif expected != actual:
        return {"path": path, "expected": expected, "actual": actual, "reason": "value"}
    return None


@dataclass(frozen=True)
class AuditFrame:
    seq: int
    kind: str
    version: dict
    payload: dict
    state: dict
    digests: dict
    canonical_state: bytes | None = None
    raw_animations: dict | None = None
    particle_seeds: tuple[dict, ...] = ()
    spawn_events: tuple[dict, ...] = ()
    raw_engine_call: dict | None = None


def spawn_payload_semantics(event, frame):
    """Retain initializer-exit scalars/RNG, mapping only four proven handles.

    The initializer's caller may replace a handle before the next boundary.
    That case cannot be normalized from this evidence and must fail closed.
    Global ordinal includes menu/preview history; controlled order is checked
    separately. The duplicate boundary is checked against event.version.
    """
    payload = copy.deepcopy(event["payload"])
    payload.pop("boundary", None)
    payload.pop("ordinal", None)
    fields = payload.get("initial", {}).get("raw_scalar_fields")
    if not isinstance(fields, dict) or type(payload.get("slot")) is not int:
        raise EvidenceError("spawn lacks exact initializer raw fields/slot")
    raw = frame.raw_animations
    if not isinstance(raw, dict):
        raise EvidenceError("spawn normalization requires checked raw animation evidence")
    by_path = {link["path"]: link for link in raw["links"]}
    for offset in ("00000118", "00000140", "00000144", "00000150"):
        if offset not in fields:
            raise EvidenceError("spawn lacks an audited zombie animation role")
        handle = fields[offset]
        if type(handle) is not int or not 0 <= handle <= 0xFFFFFFFF:
            raise EvidenceError("spawn animation handle is not uint32")
        if handle == 0:
            fields[offset] = {"status": "null"}
            continue
        link = by_path.get(f"/zombies/slots/{payload['slot']}/fields/{offset}")
        if link is None or link["raw_handle"] != handle or not link["lookup_matches"]:
            raise EvidenceError("initializer animation handle cannot be proven at the following boundary")
        fields[offset] = copy.deepcopy(link["normalized_reference"])
    return payload


def spawn_semantics(event, frame, *, map_version=lambda value: value):
    """Compare the assigned boundary and complete normalized birth payload."""
    AuditLog._envelope(event)
    if event.get("phase") != "controlled_boundary":
        raise EvidenceError("unassigned initialization history has no replay boundary")
    result = {
        "version": map_version(event["version"]),
        "phase": event["phase"],
        "native_phase": event["native_phase"],
        "payload": spawn_payload_semantics(event, frame),
    }
    if "engine_call_id" in event:
        result["engine_call_id"] = event["engine_call_id"]
    return result


def particle_semantics(event, *, map_version=lambda value: value):
    """Exclude raw addresses/global ordinal, retaining actual call order."""
    result = {
        "version": map_version(event["version"]),
        "phase": event["phase"],
        "native_phase": event["native_phase"],
        "payload": {key: value for key, value in event["payload"].items() if key != "ordinal"},
    }
    if "engine_call_id" in event:
        result["engine_call_id"] = event["engine_call_id"]
    return result


def engine_call_semantics(metadata, *, map_version=lambda value: value):
    if metadata is None:
        return None
    result = copy.deepcopy(metadata)
    result["pre_version"] = map_version(result["pre_version"])
    return result


def render_semantics(receipt, *, map_version=lambda value: value):
    if receipt is None:
        return None
    result = copy.deepcopy(receipt)
    result["frame_version"] = map_version(result["frame_version"])
    return result


def _state_clocks(state):
    try:
        return {
            "schema": SCHEMA,
            "target": state["rng"]["target"],
            "game_clock": state["board"]["00005568"],
            "effect_clock": state["board"]["0000556c"],
            "mj_clock": state["app"]["mj_clock"],
        }
    except (KeyError, TypeError) as error:
        raise EvidenceError("controlled draw state lacks audited clocks") from error


def _seeded_rng(seed):
    words = [seed or 4357]
    for index in range(1, 624):
        previous = words[-1]
        words.append((1812433253 * (previous ^ (previous >> 30)) + index) & 0xFFFFFFFF)
    return {
        "global_mt": {"algorithm": "sexy_mt19937_31", "words": words, "cursor": 624},
        "game_thread_crt": {"algorithm": "msvc_lcg_15", "state": seed},
    }


class _DrawEvidence:
    """One warm operation, one checked receipt per post, bounded terminal tail."""

    def __init__(self, manifest):
        self.target = manifest["target"]
        self.engine_calls = engine_call_mode(manifest)
        self.preparing = self.warm = self.terminal = self.health = None
        self.warm_frames = self.step_frames = self.posts = self.terminal_skips = 0
        self.terminal_completed = False
        self.seen_frame = False

    def counts(self):
        return {"mode": DRAW_SCHEDULE_MODE, "warm_frames": self.warm_frames, "step_frames": self.step_frames}

    def _receipt(self, receipt, boundary, phase):
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "lvz.controlled-render.v1"
            or receipt.get("mode") != DRAW_SCHEDULE_MODE
            or receipt.get("phase") != phase
            or version(receipt.get("frame_version")) != boundary
            or type(receipt.get("native_clock")) is not int
            or receipt["native_clock"] < 0
        ):
            raise EvidenceError("controlled draw receipt identity/phase/version is invalid")
        if phase == "terminal":
            expected = {
                "schema",
                "mode",
                "phase",
                "frame_version",
                "native_clock",
                "skipped",
                "reason",
                "cache_invalidated",
            }
            if (
                set(receipt) != expected
                or receipt.get("skipped") is not True
                or receipt.get("reason") != "left_ready_fight"
                or receipt.get("cache_invalidated") is not True
            ):
                raise EvidenceError("terminal draw skip is not explicit or invents draw evidence")
            return
        if (
            any(key in receipt for key in ("skipped", "reason", "cache_invalidated"))
            or type(receipt.get("width")) is not int
            or receipt["width"] != 800
            or type(receipt.get("height")) is not int
            or receipt["height"] != 600
            or receipt.get("pixel_format") != "bgr24"
            or receipt.get("rng_restored") is not False
            or type(receipt.get("rng_unchanged")) is not bool
        ):
            raise EvidenceError("controlled draw receipt format/guard is invalid")
        clocks = receipt.get("clocks_before")
        if (
            not isinstance(clocks, dict)
            or set(clocks) != {"schema", "target", "game_clock", "effect_clock", "mj_clock"}
            or clocks["schema"] != SCHEMA
            or clocks["target"] != self.target
            or any(
                type(clocks[key]) is not int or clocks[key] < 0 for key in ("game_clock", "effect_clock", "mj_clock")
            )
            or clocks != receipt.get("clocks_after")
            or clocks["game_clock"] != receipt["native_clock"]
        ):
            raise EvidenceError("controlled draw changed or omitted clock evidence")
        for name in ("rng_before", "rng_after"):
            hashes = receipt.get(name)
            if (
                not isinstance(hashes, dict)
                or set(hashes) != {"all", "global_mt", "game_thread_crt"}
                or any(
                    not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{16}", value) is None
                    for value in hashes.values()
                )
            ):
                raise EvidenceError("controlled draw lacks complete RNG digests")
        if receipt["rng_unchanged"] != (receipt["rng_before"] == receipt["rng_after"]):
            raise EvidenceError("controlled draw RNG guard disagrees with digests")
        if first_difference(receipt.get("counts"), self.counts()):
            raise EvidenceError("controlled draw receipt count is missing, duplicated, or reordered")

    def event(self, event):
        kind, payload = event["kind"], event["payload"]
        auxiliary = (
            self.terminal is not None
            and kind == "zombie_first_boundary_observed"
            and not self.terminal.get("transition_seen")
            and event["seq"] >= self.terminal["seq"]
            and event["version"] == self.terminal["version"]
            and payload.get("exact_spawn") is False
        )
        if self.preparing is not None and self.warm is None and kind != "render_prepared":
            if kind not in {"zombie_initialized", "particle_shake_seed"} or event.get("phase") != "initialization":
                raise EvidenceError("warm drawing contains an unexpected controlled mutation")
        if self.terminal_completed and kind not in {
            "recording_closed",
            "engine_call_closed",
            "fp_environment_closed",
            "sound_effects_closed",
            "draw_schedule_closed",
            "particle_shake_closed",
            "spawn_hook_closed",
            "lifecycle_probes_closed",
        }:
            raise EvidenceError("simulation event follows terminal completion")
        if kind == "render_preparing":
            if (
                self.preparing is not None
                or self.warm is not None
                or self.seen_frame
                or event["version"]["tick"] != 0
                or not isinstance(payload.get("request_id"), str)
            ):
                raise EvidenceError("warm drawing must occur exactly once before the first audited step")
            self.preparing = event
        elif kind == "render_prepared":
            if (
                self.preparing is None
                or self.warm is not None
                or self.seen_frame
                or event["version"] != self.preparing["version"]
                or payload.get("request_id") != self.preparing["payload"]["request_id"]
            ):
                raise EvidenceError("warm drawing receipt has no matching preparation")
            self.warm_frames = 1
            receipt = payload.get("render")
            self._receipt(receipt, event["version"], "warm")
            seed = receipt.get("seed_readback", {}).get("seed")
            if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
                raise EvidenceError("warm drawing lacks an explicit uint32 seed")
            expected = {
                "seed": seed,
                "global_mt_words": 624,
                "global_mt_cursor": 624,
                "game_thread_crt": seed,
                "verified_before_draw": True,
            }
            if first_difference(expected, receipt["seed_readback"]) or receipt["rng_before"] != digests(
                _seeded_rng(seed)
            ):
                raise EvidenceError("warm drawing seed readback does not match the captured RNG digests")
            self.warm = receipt
        elif kind == "terminal_transition":
            if (
                self.terminal is None
                or self.terminal.get("transition_seen")
                or event["version"] != self.terminal["version"]
                or payload.get("request_id") != self.terminal["request_id"]
                or payload.get("tick_delta_verified") is not (self.terminal["delta"] == 1)
                or payload.get("board_identity_preserved") is not True
                or type(payload.get("native_tick_delta")) is not int
                or payload["native_tick_delta"] != self.terminal["delta"]
            ):
                raise EvidenceError("terminal draw skip lacks a verified same-Board transition")
            self.terminal["transition_seen"] = True
        elif self.terminal is not None and kind == "request_completed":
            result = payload.get("result", {})
            observation = result.get("observation", {})
            if (
                self.terminal_completed
                or not self.terminal.get("transition_seen")
                or payload.get("request_id") != self.terminal["request_id"]
                or event["version"] != self.terminal["version"]
                or observation.get("version") != event["version"]
                or result.get("stop_reason") != "scene_changed"
                or observation.get("game_ui") == 3
            ):
                raise EvidenceError("terminal draw skip lacks its final scene_changed completion")
            self.terminal_completed = True
        elif self.terminal is not None and kind in {"request_started", "action", "render_preparing", "render_prepared"}:
            raise EvidenceError("simulation mutation follows terminal draw skip")
        if (
            self.terminal is not None
            and not self.terminal_completed
            and kind not in {"terminal_transition", "request_completed"}
            and not auxiliary
        ):
            raise EvidenceError("terminal draw skip has an intervening event")

    def state(self, state, receipt=None):
        if first_difference(state.get("draw_schedule"), self.counts()):
            raise EvidenceError("audited draw count differs from checked receipts")
        if receipt is not None:
            clocks = _state_clocks(state)
            if receipt["native_clock"] != clocks["game_clock"]:
                raise EvidenceError("draw receipt native clock differs from audited state")
            if receipt["phase"] != "terminal":
                if receipt["clocks_after"] != clocks or receipt["rng_after"] != digests(state["rng"]["instances"]):
                    raise EvidenceError("draw receipt clocks/RNG differ from actual post state")

    def frame(self, frame):
        if self.warm is None or self.terminal is not None:
            raise EvidenceError("audited update lacks warm preparation or follows terminal draw skip")
        self.seen_frame = True
        if frame.version["epoch"] != self.warm["frame_version"]["epoch"]:
            raise EvidenceError("controlled drawing crosses an unprepared epoch")
        if frame.kind == "pre_step":
            if "render" in frame.payload or frame.version["tick"] != self.posts:
                raise EvidenceError("draw receipt is misplaced or update ordinal differs")
            self.state(frame.state)
            return
        receipt = frame.payload.get("render")
        if not isinstance(receipt, dict):
            raise EvidenceError("verified post_step requires exactly one controlled draw receipt")
        terminal = receipt.get("phase") == "terminal"
        self.posts += 1
        if terminal:
            self.terminal_skips += 1
        else:
            self.step_frames += 1
        self._receipt(receipt, frame.version, "terminal" if terminal else "step")
        if (frame.state.get("app", {}).get("ui") != 3) != terminal:
            raise EvidenceError("terminal draw skip and audited fight UI disagree")
        self.state(frame.state, receipt)
        if terminal:
            delta = frame.payload["native_tick_delta"]
            if delta != 1 and not (self.engine_calls and delta == 0):
                raise EvidenceError("terminal draw skip has no supported measured call")
            self.terminal = {
                "version": frame.version,
                "request_id": frame.payload["request_id"],
                "seq": frame.seq,
                "delta": delta,
            }

    def initial(self, initial):
        if self.warm is None or initial["observation"]["version"] != self.warm["frame_version"]:
            raise EvidenceError("initial marker is not the prepared warm boundary")
        if initial["observation"].get("render_prepared") is not True:
            raise EvidenceError("initial observation does not confirm render preparation")
        # Validate B0 independently of counters advanced by later frames.
        expected = {"mode": DRAW_SCHEDULE_MODE, "warm_frames": 1, "step_frames": 0}
        if first_difference(initial["state"].get("draw_schedule"), expected):
            raise EvidenceError("initial draw state is not warm1/step0")
        if self.warm["clocks_after"] != _state_clocks(initial["state"]) or self.warm["rng_after"] != digests(
            initial["state"]["rng"]["instances"]
        ):
            raise EvidenceError("initial marker differs from the actual postwarm clocks/RNG")
        if "initialization" in initial:
            recipe = initial["initialization"]
            seed = self.warm["seed_readback"]["seed"]
            preparation = {
                "warm_frames": 1,
                "step_frames": 0,
                "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(seed))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(initial["state"]["rng"]["instances"])).hexdigest(),
            }
            if (
                type(recipe.get("seed")) is not int
                or recipe["seed"] != seed
                or recipe.get("clock_anchor") != self.warm["clocks_before"]
                or recipe.get("postwarm_clock") != self.warm["clocks_after"]
                or first_difference(recipe.get("render_preparation"), preparation)
            ):
                raise EvidenceError("initialization recipe differs from actual warm seed/clock/RNG evidence")

    def finish(self):
        if self.preparing is not None and self.warm is None:
            raise EvidenceError("warm drawing never completed")
        if self.terminal is not None and not self.terminal_completed:
            raise EvidenceError("terminal draw skip is missing its transition/completion")

    def close(self, event):
        health = event["payload"]
        required = {
            "mode": DRAW_SCHEDULE_MODE,
            "installed": True,
            "latched": True,
            "sealed": True,
            "active": False,
            "healthy": True,
            "faults": 0,
            "wrong_thread_calls": 0,
            "controlled_calls": self.warm_frames + self.step_frames,
            "warm_frames": self.warm_frames,
            "step_frames": self.step_frames,
        }
        if (
            self.warm is None
            or any(type(health.get(key)) is not type(value) or health[key] != value for key, value in required.items())
            or any(
                type(health.get(key)) is not int or health[key] < 0 for key in ("automatic_allowed", "automatic_denied")
            )
        ):
            raise EvidenceError("draw schedule close health/count is incomplete or unhealthy")
        self.health = copy.deepcopy(health)


class _EngineCallEvidence:
    """Checked wrapper lifecycle and raw pointers, retaining one pending call."""

    def __init__(self):
        self.pre = self.terminal = self.health = None
        self.board_address = None
        self.request_id = None
        self.request_calls = self.request_ticks = self.request_zero = 0
        self.calls = self.ticks = self.zero = self.terminal_ticks = 0
        self.terminal_completed = False

    def event(self, event, raw=None):
        kind, payload = event["kind"], event["payload"]
        footer = {
            "recording_closed",
            "engine_call_closed",
            "fp_environment_closed",
            "sound_effects_closed",
            "draw_schedule_closed",
            "particle_shake_closed",
            "spawn_hook_closed",
            "lifecycle_probes_closed",
        }
        if self.terminal_completed and kind not in footer:
            raise EvidenceError("engine event follows terminal completion")
        if kind in {"particle_shake_seed", "zombie_initialized"}:
            expected = self.pre["metadata"]["engine_call_id"] if self.pre else None
            if "engine_call_id" not in payload or first_difference(payload["engine_call_id"], expected):
                raise EvidenceError("hook engine call identity is missing or assigned to another call")
            if self.pre and (
                event.get("phase") != "controlled_boundary" or event.get("version") != self.pre["version"]
            ):
                raise EvidenceError("hook call lost the active pre-update version")
            if kind == "particle_shake_seed" and (
                not isinstance(raw, dict)
                or "engine_call_id" not in raw.get("payload", {})
                or first_difference(raw["payload"]["engine_call_id"], expected)
            ):
                raise EvidenceError("raw particle call identity differs from its semantic evidence")
        if kind == "request_started":
            if (
                self.pre
                or self.request_id is not None
                or self.terminal
                or not isinstance(payload.get("request_id"), str)
                or not payload["request_id"]
            ):
                raise EvidenceError("engine call request overlaps or follows a terminal")
            self.request_id = payload["request_id"]
            self.request_calls = self.request_ticks = self.request_zero = 0
        elif kind == "action" and self.pre:
            raise EvidenceError("action interleaved with an active engine call")
        elif kind == "terminal_transition":
            terminal = self.terminal
            if (
                terminal is None
                or terminal.get("transition_seen")
                or self.pre
                or event["version"] != terminal["version"]
                or payload.get("request_id") != self.request_id
                or first_difference(payload.get("engine_call"), terminal["metadata"])
                or payload.get("terminal_call_verified") is not True
                or payload.get("clock_delta_measured") is not True
                or payload.get("board_identity_preserved") is not True
                or type(payload.get("native_tick_delta")) is not int
                or payload["native_tick_delta"] != terminal["delta"]
                or payload.get("tick_delta_verified") is not (terminal["delta"] == 1)
                or payload.get("transition_kind") != terminal["metadata"]["transition_kind"]
            ):
                raise EvidenceError("terminal transition lacks its exact returned engine call")
            terminal["transition_seen"] = True
        elif kind == "request_completed":
            result = payload.get("result", {})
            expected = {
                "executed_ticks": self.request_ticks,
                "executed_engine_calls": self.request_calls,
                "terminal_zero_clock_calls": self.request_zero,
                "last_engine_call_id": self.calls if self.request_calls else None,
            }
            if (
                self.pre
                or self.request_id is None
                or payload.get("request_id") != self.request_id
                or any(key not in result or first_difference(value, result[key]) for key, value in expected.items())
            ):
                raise EvidenceError("completed request counters differ from actual returned call pairs")
            if self.terminal:
                if (
                    not self.terminal.get("transition_seen")
                    or self.terminal_completed
                    or result.get("stop_reason") != "scene_changed"
                    or result.get("terminal_kind") != self.terminal["metadata"]["transition_kind"]
                    or event["version"] != self.terminal["version"]
                    or result.get("observation", {}).get("version") != event["version"]
                ):
                    raise EvidenceError("terminal call lacks its exact completed result")
                self.terminal_completed = True
            elif result.get("stop_reason") == "scene_changed" or "terminal_kind" in result:
                raise EvidenceError("scene_changed result has no returned terminal call")
            self.request_id = None
        if self.terminal and not self.terminal_completed and kind not in {"terminal_transition", "request_completed"}:
            auxiliary = (
                kind == "zombie_first_boundary_observed"
                and not self.terminal.get("transition_seen")
                and event["version"] == self.terminal["version"]
                and payload.get("exact_spawn") is False
            )
            if not auxiliary:
                raise EvidenceError("unexpected event before terminal call completion")

    def frame(self, frame, raw):
        metadata = frame.payload.get("engine_call")
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema") != "lvz.engine-call.v1"
            or type(metadata.get("engine_call_id")) is not int
            or not 1 <= metadata["engine_call_id"] <= 0xFFFFFFFFFFFFFFFF
            or self.terminal
            or self.request_id is None
            or frame.payload.get("request_id") != self.request_id
        ):
            raise EvidenceError("missing, misplaced, or invalid engine call metadata")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "lvz.engine-call-raw.v1"
            or any(
                key not in raw or first_difference(raw[key], value)
                for key, value in {
                    "seq": frame.seq,
                    "kind": frame.kind,
                    "version": frame.version,
                    "engine_call_id": metadata["engine_call_id"],
                }.items()
            )
            or not isinstance(raw.get("payload"), dict)
            or set(raw["payload"]) != {"board_address_before", "board_address_after"}
        ):
            raise EvidenceError("raw engine call boundary/ID mismatch")
        address, after_address = raw["payload"]["board_address_before"], raw["payload"]["board_address_after"]
        if type(address) is not int or not 1 <= address <= 0xFFFFFFFF:
            raise EvidenceError("raw engine call Board address is not non-null uint32")
        clocks = _state_clocks(frame.state)
        ui = frame.state.get("app", {}).get("ui")
        if any(
            type(clocks[key]) is not int or not 0 <= clocks[key] <= 0xFFFFFFFF
            for key in ("game_clock", "effect_clock", "mj_clock")
        ):
            raise EvidenceError("engine call lacks captured uint32 clocks")
        if frame.kind == "pre_step":
            expected = {
                "engine_call_id": self.calls + 1,
                "request_call_index": self.request_calls + 1,
                "pre_version": frame.version,
                "lifecycle": "prepared",
                "native_clock_before": clocks["game_clock"],
                "native_clock_after": None,
                "native_tick_delta": None,
                "clock_delta_measured": False,
                "engine_call_entered": False,
                "engine_call_completed": False,
                "board_identity_preserved": None,
                "ready_before": True,
                "game_ui_before": 3,
                "clocks_before": clocks,
                "clocks_after": None,
                "entered_calls_total": self.calls,
                "returned_calls_total": self.calls,
            }
            budget = frame.payload.get("requested_ticks")
            if (
                self.pre
                or ui != 3
                or after_address is not None
                or type(budget) is not int
                or budget <= self.request_ticks
                or (self.board_address is not None and address != self.board_address)
                or frame.payload.get("executed_ticks") != self.request_ticks
                or any(key not in metadata or first_difference(value, metadata[key]) for key, value in expected.items())
            ):
                raise EvidenceError("engine call preparation, budget, or counter evidence is inconsistent")
            self.board_address = address
            self.pre = {
                "metadata": copy.deepcopy(metadata),
                "version": copy.deepcopy(frame.version),
                "address": address,
                "budget": budget,
            }
            return
        if self.pre is None:
            raise EvidenceError("returned engine call has no prepared pair")
        before = self.pre["metadata"]
        delta = clocks["game_clock"] - before["native_clock_before"]
        terminal = ui != 3
        zero = delta == 0 and ui == 4
        if delta != 1 and not zero:
            raise EvidenceError("unsupported zero/negative/multiple engine clock update")
        transition = "terminal_zero_clock_update" if zero else "terminal_clock_step" if terminal else "clock_step"
        expected = dict(
            before,
            lifecycle="returned",
            native_clock_after=clocks["game_clock"],
            native_tick_delta=delta,
            clock_delta_measured=True,
            engine_call_entered=True,
            engine_call_completed=True,
            board_identity_preserved=True,
            ready_after=not terminal,
            game_ui_after=ui,
            clocks_after=clocks,
            transition_kind=transition,
            entered_calls_total=self.calls + 1,
            returned_calls_total=self.calls + 1,
        )
        if (
            any(key not in metadata or first_difference(value, metadata[key]) for key, value in expected.items())
            or address != self.pre["address"]
            or type(after_address) is not int
            or after_address != address
            or type(frame.payload.get("native_tick_delta")) is not int
            or frame.payload["native_tick_delta"] != delta
            or frame.payload.get("executed_ticks") != self.request_ticks + delta
        ):
            raise EvidenceError("returned engine call facts differ from its pre/post/raw state")
        self.calls += 1
        self.ticks += delta
        self.zero += int(zero)
        self.terminal_ticks += int(terminal and delta == 1)
        self.request_calls += 1
        self.request_ticks += delta
        self.request_zero += int(zero)
        self.pre = None
        if terminal:
            self.terminal = {
                "metadata": copy.deepcopy(metadata),
                "version": copy.deepcopy(frame.version),
                "delta": delta,
            }

    def finish(self):
        if self.pre or (self.terminal and not self.terminal_completed):
            raise EvidenceError("engine call has an unfinished returned/terminal boundary")

    def close(self, event, draw):
        value = event["payload"]
        expected = {
            "mode": ENGINE_CALL_MODE,
            "reserved_calls": self.calls,
            "entered_calls": self.calls,
            "returned_calls": self.calls,
            "written_post_boundaries": self.calls,
            "verified_clock_steps": self.ticks,
            "verified_terminal_zero_calls": self.zero,
            "terminal_clock_steps": self.terminal_ticks,
            "active_call_id": None,
            "faults": 0,
            "reentrant_calls": 0,
            "wrong_thread_calls": 0,
            "aborted_calls": 0,
            "healthy": True,
        }
        if (
            self.request_id is not None
            or self.zero + self.terminal_ticks > 1
            or any(key not in value or first_difference(item, value[key]) for key, item in expected.items())
            or draw is None
            or draw.step_frames != self.ticks - self.terminal_ticks
        ):
            raise EvidenceError("engine call close health does not account for every returned call and draw")
        self.health = copy.deepcopy(value)


class _ParticleEvidence:
    def __init__(self):
        self.captured = 0
        self.controlled_calls = 0

    def accept(self, event, raw):
        AuditLog._envelope(event)
        if not isinstance(raw, dict) or raw.get("schema") != "lvz.particle-shake-raw.v1":
            raise EvidenceError("invalid raw particle shake evidence schema")
        if any(raw.get(key) != event.get(key) for key in ("seq", "kind", "version", "phase", "native_phase")):
            raise EvidenceError("particle shake raw/semantic envelope mismatch")
        payload, original = event["payload"], raw.get("payload")
        extras = {
            "particle_address",
            "emitter_address",
            "system_address",
            "holder_address",
            "pool_block_address",
            "original_seed",
        }
        if not isinstance(original, dict) or set(original) != set(payload) | extras:
            raise EvidenceError("particle shake raw payload must retain complete semantic and pointer evidence")
        if any(original[key] != value for key, value in payload.items()):
            raise EvidenceError("particle shake raw/semantic payload mismatch")
        uint = lambda value: type(value) is int and 0 <= value <= 0xFFFFFFFF
        if (
            payload.get("mode") != PARTICLE_SHAKE_MODE
            or payload.get("pool_verified") is not True
            or type(payload.get("ordinal")) is not int
            or payload["ordinal"] != self.captured
            or any(
                not uint(payload.get(key)) for key in ("particle_id", "slot", "generation", "factor", "canonical_seed")
            )
            or any(not uint(original[key]) for key in extras)
        ):
            raise EvidenceError("invalid particle shake mode, identity, ordinal, or seed")
        expected_phases = (
            {"pre_step", "request_started", "action"} if event["phase"] == "controlled_boundary" else {"initialization"}
        )
        if payload.get("control_phase") not in expected_phases:
            raise EvidenceError("particle shake control phase is inconsistent")
        age, duration, site = payload.get("age"), payload.get("duration"), payload.get("callsite_rva")
        crossfade = payload.get("crossfade_duration")
        if (
            type(age) is not int
            or type(duration) is not int
            or type(crossfade) is not int
            or not 0 <= age <= 0x7FFFFFFF
            or not 1 <= duration <= 0x7FFFFFFF
            or not 0 <= crossfade <= 0x7FFFFFFF
            or (age >= duration and crossfade == 0)
        ):
            raise EvidenceError("invalid particle shake age/duration/crossfade")
        if type(site) is not int or site not in {0x116B3C, 0x116BA5}:
            raise EvidenceError("particle shake event came from an unsupported callsite")
        factor = (duration - 1 if age == 0 else age - 1) if site == 0x116B3C else age
        identity = payload["particle_id"]
        if (
            payload["factor"] != factor
            or payload["slot"] != identity & 0xFFFF
            or payload["generation"] != identity >> 16
            or payload["generation"] == 0
            or payload["canonical_seed"] != (identity * factor) & 0xFFFFFFFF
            or original["original_seed"] != (original["particle_address"] * factor) & 0xFFFFFFFF
            or any(original[key] == 0 for key in extras - {"original_seed"})
            or original["particle_address"] != original["pool_block_address"] + payload["slot"] * 0xA0
        ):
            raise EvidenceError("particle shake generation/factor/seed relation is invalid")
        pool = payload.get("pool")
        if (
            not isinstance(pool, dict)
            or set(pool) != {"used", "capacity", "count", "free_head", "next_key"}
            or any(not uint(value) for value in pool.values())
            or not 1 <= pool["count"] <= pool["used"] <= pool["capacity"] <= 1024
            or pool["free_head"] > pool["used"]
            or not 1 <= pool["next_key"] <= 65535
            or payload["slot"] >= pool["used"]
        ):
            raise EvidenceError("invalid verified particle pool evidence")
        self.captured += 1
        self.controlled_calls += event["phase"] == "controlled_boundary"

    def close(self, event):
        health = event["payload"]
        if (
            health.get("installed") is not True
            or health.get("healthy") is not True
            or any(
                type(health.get(key)) is not int or health[key] != 0
                for key in ("queued", "wrong_thread_calls", "faults", "overflow")
            )
            or type(health.get("captured")) is not int
            or health["captured"] != self.captured
            or type(health.get("controlled_calls")) is not int
            or health["controlled_calls"] != self.controlled_calls
            or (
                "last_fault_code" in health
                and (type(health["last_fault_code"]) is not int or health["last_fault_code"] != 0)
            )
        ):
            raise EvidenceError("particle shake hook health/count is incomplete or unhealthy")


class _HostedFireEvidence:
    """One audit record per hosted cannon shot, bound to its actual boundary.

    The hook runs inside the script coroutine, between two audited boundaries:
    it is written with the controller version the runtime reported at the engine
    call, and it must therefore be followed by that version's audited pre_step.
    Every frame then has to show the running count/digest, so a shot whose
    record was dropped, duplicated or reordered cannot pass as a clean run.
    """

    def __init__(self):
        self.count = 0
        self.rolling = 14695981039346656037
        self.pending = []

    def event(self, event):
        payload = event["payload"]
        if (
            event.get("phase") != "controlled_boundary"
            or event.get("native_phase") != "avz_basic_fire"
            or not isinstance(payload, dict)
            or set(payload) != _HOSTED_FIRE_PAYLOAD_KEYS
        ):
            raise EvidenceError("invalid hosted fire record envelope or payload fields")
        if payload.get("source") != "hosted" or payload.get("op") != "fire":
            raise EvidenceError("hosted fire record lost its source/op marker")
        if any(type(payload.get(key)) is not int for key in _HOSTED_FIRE_FIELDS):
            raise EvidenceError("hosted fire record fields must be integers")
        if any(
            not 0 <= payload.get(key) <= 0xFFFFFFFF
            for key in ("plant_index", "plant_id", "plant_row", "plant_col", "target_col_bits", "tick")
        ):
            raise EvidenceError("hosted fire record has a negative or oversized identity field")
        if not -0x80000000 <= payload["target_row"] <= 0x7FFFFFFF:
            raise EvidenceError("hosted fire target row is out of range")
        if payload["tick"] != event["version"]["tick"]:
            raise EvidenceError("hosted fire tick differs from the version it is bound to")
        readable = struct.unpack("<f", struct.pack("<I", payload["target_col_bits"]))[0]
        text = payload.get("target_col_text")
        if not isinstance(text, str) or text != f"{readable:.3f}":
            raise EvidenceError("hosted fire readable column differs from its IEEE-754 bits")
        self.count += 1
        self.rolling = _hosted_fire_digest(self.rolling, payload)
        self.pending.append(event)

    def frame(self, frame):
        for event in self.pending:
            before = event["version"]
            if (
                frame.kind != "pre_step"
                or before["epoch"] != frame.version["epoch"]
                or before["tick"] != frame.version["tick"]
                or before["revision"] > frame.version["revision"]
            ):
                raise EvidenceError("hosted fire record is not bound to its next audited pre-step")
        self.pending.clear()
        state = frame.state.get("hosted_fire")
        if (
            not isinstance(state, dict)
            or state.get("mode") != HOSTED_FIRE_MODE
            or type(state.get("count")) is not int
            or state["count"] != self.count
            or type(state.get("digest")) is not int
            or state["digest"] != self.rolling
        ):
            raise EvidenceError("hosted fire state count/digest differs from the verified records")

    def finish(self):
        if self.pending:
            raise EvidenceError("hosted fire record has no following audited boundary")


class _EventSummary:
    def __init__(self):
        self.tail = deque(maxlen=8)
        self.birth_count = 0
        self.controlled_birth_count = 0
        self.initialization_birth_count = 0
        self.count = 0
        self.last_seq = -1
        self.recording_closed = False

    def accept(self, event):
        AuditLog._envelope(event)
        if event["kind"] == "reanimation_link_fault":
            raise EvidenceError("native animation link fault invalidates strict evidence")
        if event["seq"] < self.last_seq:
            raise EvidenceError("audit event sequence moved backwards")
        if self.recording_closed and event["kind"] not in {
            "engine_call_closed",
            "fp_environment_closed",
            "sound_effects_closed",
            "draw_schedule_closed",
            "particle_shake_closed",
            "spawn_hook_closed",
            "lifecycle_probes_closed",
        }:
            raise EvidenceError("native event after recording close")
        if not self.recording_closed and event["kind"] in {
            "engine_call_closed",
            "fp_environment_closed",
            "sound_effects_closed",
            "draw_schedule_closed",
            "particle_shake_closed",
            "spawn_hook_closed",
            "lifecycle_probes_closed",
        }:
            raise EvidenceError("native hook close precedes recording close")
        self.last_seq = event["seq"]
        if event["kind"] == "zombie_initialized":
            if type(event["payload"].get("ordinal")) is not int or event["payload"]["ordinal"] != self.birth_count:
                raise EvidenceError("spawn ordinal is missing or duplicated")
            self.birth_count += 1
            if event["phase"] == "controlled_boundary":
                self.controlled_birth_count += 1
            else:
                self.initialization_birth_count += 1
        self.recording_closed |= event["kind"] == "recording_closed"
        self.tail.append(event)
        self.count += 1


PROBE_CLOSE_FAULT_COUNTERS = (
    "overflow",
    "wrong_thread",
    "unmatched_commits",
    "pair_mismatch",
    "overwritten_pending",
    "faults",
    "read_failed",
    "classify_refused",
    "inactive_suppressed",
)


def _validate_probes_close(event, manifest, summary):
    """Strict validation of the declared lifecycle_probes_closed footer."""
    health = event.get("payload")
    if not isinstance(health, dict) or set(health) != {
        "installed",
        "active",
        "healthy",
        "pending_candidate",
        "patched_sites",
        "counters",
        "sites",
        "reader_protected",
        "pending_callbacks",
    }:
        raise EvidenceError("lifecycle probe close payload is missing or malformed")
    if (
        health.get("healthy") is not True
        or health.get("installed") is not False
        or health.get("active") is not False
        or health.get("pending_candidate") is not False
        or health.get("reader_protected") is not False
        or health.get("pending_callbacks") != 0
    ):
        raise EvidenceError("final lifecycle probe evidence is unhealthy or still installed")
    counters = health.get("counters")
    if not isinstance(counters, dict) or any(type(counters.get(key)) is not int for key in PROBE_CLOSE_FAULT_COUNTERS):
        raise EvidenceError("lifecycle probe close counters are malformed")
    if any(counters[key] != 0 for key in PROBE_CLOSE_FAULT_COUNTERS) or counters.get("queued") != 0:
        raise EvidenceError("lifecycle probe close counters are not clean")
    if counters.get("captured") != counters.get("delivered"):
        raise EvidenceError("lifecycle probe capture/delivery mismatch at close")
    block = manifest.get("lifecycle_probes") if isinstance(manifest, dict) else None
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise EvidenceError("lifecycle probe close has no enabled capability")
    final = block.get("probe_counters")
    if (
        not isinstance(final, dict)
        or final.get("captured") != counters.get("captured")
        or final.get("delivered") != counters.get("delivered")
        or final.get("queued") != 0
    ):
        raise EvidenceError("lifecycle probe close differs from the final manifest counters")
    if block.get("healthy") is not True or block.get("installed") is not False:
        raise EvidenceError("lifecycle probe capability final health is not clean")
    if len(summary.tail) < 2 or event["version"] != summary.tail[-2]["version"]:
        raise EvidenceError("lifecycle probe close boundary differs from spawn hook close")


def _closed_events(
    summary,
    *,
    particle=None,
    draw=None,
    calls=None,
    audio=None,
    fp=None,
    spawn_required=False,
    probes_required=False,
    probes_allowed=False,
    manifest=None,
):
    """Validate the declared close sequence and all hook health summaries."""
    final = list(summary.tail)
    if final and final[-1]["kind"] == "lifecycle_probes_closed":
        if not probes_allowed:
            raise EvidenceError("lifecycle probe close is not declared by the capability")
        _validate_probes_close(final[-1], manifest, summary)
        final = final[:-1]
    elif probes_required:
        raise EvidenceError("required lifecycle probe close health is missing")
    if spawn_required and (not final or final[-1]["kind"] != "spawn_hook_closed"):
        raise EvidenceError("required spawn hook close health is missing")
    if final and final[-1]["kind"] == "spawn_hook_closed":
        health = final[-1]["payload"]
        if (
            health.get("healthy") is not True
            or any(
                type(health.get(key)) is not int or health[key] != 0
                for key in ("faults", "overflow", "wrong_thread_calls", "queued", "active_initializers")
            )
            or type(health.get("captured")) is not int
            or health["captured"] != summary.birth_count
        ):
            raise EvidenceError("final spawn hook evidence is unhealthy or incomplete")
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("spawn hook close boundary differs from recording close")
        final = final[:-1]
    if particle is not None:
        if not final or final[-1]["kind"] != "particle_shake_closed":
            raise EvidenceError("required particle shake close health is missing")
        particle.close(final[-1])
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("particle shake close boundary differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "particle_shake_closed" for event in final):
        raise EvidenceError("particle shake close has no declared engine mode")
    if draw is not None:
        if not final or final[-1]["kind"] != "draw_schedule_closed":
            raise EvidenceError("required draw schedule close health is missing")
        draw.close(final[-1])
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("draw schedule close boundary differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "draw_schedule_closed" for event in final):
        raise EvidenceError("draw close has no declared engine mode")
    if audio is not None and audio.enabled:
        if not final or final[-1]["kind"] != "sound_effects_closed" or audio.health is None:
            raise EvidenceError("required sound effects close health is missing")
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("sound effects close differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "sound_effects_closed" for event in final):
        raise EvidenceError("sound effects close lacks an explicit mode")
    if fp is not None and fp.enabled:
        if not final or final[-1]["kind"] != fp_environment.CLOSED or fp.health is None:
            raise EvidenceError("required fixed FP close health is missing")
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("FP close boundary differs from recording close")
        fp.close(draw)
        final = final[:-1]
    elif any(event["kind"] == fp_environment.CLOSED for event in final):
        raise EvidenceError("FP close lacks an explicit mode")
    if calls is not None:
        if not final or final[-1]["kind"] != "engine_call_closed":
            raise EvidenceError("required engine call close health is missing")
        calls.close(final[-1], draw)
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("engine call close differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "engine_call_closed" for event in final):
        raise EvidenceError("engine call close lacks an explicit mode")
    if not final or final[-1]["kind"] != "recording_closed":
        raise EvidenceError("native recording lacks a completed recording_closed boundary")


def _particle_boundary(event, frame, previous):
    before = event["version"]
    if event["payload"]["control_phase"] == "pre_step":
        if previous is None or previous.kind != "pre_step" or frame.kind != "post_step" or before != previous.version:
            raise EvidenceError("particle shake call is not bound to its actual pre/post update")
    elif (
        frame.kind != "pre_step"
        or before["epoch"] != frame.version["epoch"]
        or before["tick"] != frame.version["tick"]
        or before["revision"] > frame.version["revision"]
    ):
        raise EvidenceError("particle shake action call is not bound to its next audited pre-step")


def _particle_digest(previous, payload):
    """Native canonical word order; runtime epoch/revision never enter state."""
    values = [
        payload[key]
        for key in ("callsite_rva", "particle_id", "factor", "canonical_seed", "age", "duration", "crossfade_duration")
    ]
    values += [payload["pool"][key] for key in ("used", "capacity", "free_head", "count", "next_key")]
    return _fnv_continue(previous, struct.pack("<12Q", *values))


def _hosted_fire_digest(previous, payload):
    """Native canonical word order; runtime epoch/revision never enter state."""
    values = [payload[key] for key in _HOSTED_FIRE_FIELDS]
    return _fnv_continue(previous, struct.pack("<7Q", *[value & 0xFFFFFFFFFFFFFFFF for value in values]))


class _AnimationDecoder:
    """Validate raw allocation evidence against each normalized frame.

    Raw handles are retained and checked locally, never equated across runs.
    The decoder retains one reconstructed raw snapshot, not the full history.
    """

    def __init__(self, manifest=None):
        self.state = None
        rules = (manifest or {}).get("coverage", {}).get("reanimations", {}).get("owner_retirement_rules")
        self.owner_retirement_rules = rules in (_OWNER_RETIREMENT_RULES, _FLAG_RETIREMENT_RULES)
        self.flag_retirement_rule = rules == _FLAG_RETIREMENT_RULES

    def accept(self, record, frame):
        AuditLog._envelope(record)
        envelope = {
            "schema": SCHEMA,
            "seq": frame.seq,
            "kind": frame.kind,
            "version": frame.version,
            "payload": frame.payload,
        }
        if any(record[key] != value for key, value in envelope.items()):
            raise EvidenceError("raw animation/frame envelope mismatch")
        if self.state is None:
            if "initial" not in record or "patch" in record:
                raise EvidenceError("raw animation evidence requires initial state")
            self.state = record["initial"]
        else:
            if "initial" in record or not isinstance(record.get("patch"), list):
                raise EvidenceError("raw animation evidence requires subsequent patch")
            self.state = patch(self.state, record["patch"], in_place=True)
        raw = self.state
        if raw is None and frame.state.get("board") is None and "reanimations" not in frame.state:
            return
        if not isinstance(raw, dict) or raw.get("schema") != "lvz.reanimation-raw.v1":
            raise EvidenceError("invalid raw animation state schema")
        pool, slots, links = raw.get("pool"), raw.get("actual_slot_ids"), raw.get("links")
        uint = lambda value: type(value) is int and 0 <= value <= 0xFFFFFFFF
        if (
            not isinstance(pool, dict)
            or set(pool) != {"capacity", "used", "count", "free_head", "next_key"}
            or not all(uint(value) for value in pool.values())
            or not pool["used"] <= pool["capacity"] <= 65536
            or not isinstance(slots, dict)
            or pool["count"] != len(slots)
            or not isinstance(links, list)
        ):
            raise EvidenceError("invalid raw animation pool")
        for slot, identity in slots.items():
            if (
                not slot.isascii()
                or not slot.isdecimal()
                or str(int(slot)) != slot
                or int(slot) >= pool["used"]
                or not uint(identity)
                or identity >> 16 == 0
                or identity & 0xFFFF != int(slot)
            ):
                raise EvidenceError("raw animation slot/generation mismatch")
        normalized = frame.state.get("reanimations")
        if (
            not isinstance(normalized, dict)
            or normalized.get("schema") != "lvz.reanimation-links.v1"
            or normalized.get("valid") is not True
            or normalized.get("issues") != []
            or not isinstance(normalized.get("nodes"), dict)
        ):
            raise EvidenceError("invalid comparable animation state")
        paths, anchors, owners, checked_flags = set(), set(), {}, {}
        for link in links:
            if not isinstance(link, dict):
                raise EvidenceError("invalid raw animation link")
            path, anchor, handle = link.get("path"), link.get("anchor"), link.get("raw_handle")
            if (
                not isinstance(path, str)
                or not path
                or path in paths
                or not isinstance(anchor, str)
                or not anchor
                or anchor in anchors
                or not uint(handle)
                or type(link.get("slot")) is not int
                or link["slot"] != handle & 0xFFFF
                or type(link.get("owner_dead")) is not bool
                or type(link.get("lookup_matches")) is not bool
            ):
                raise EvidenceError("invalid raw animation link identity")
            paths.add(path)
            anchors.add(anchor)
            actual = slots.get(str(link["slot"]))
            matches = handle != 0 and actual == handle
            if link.get("actual_slot_id") != actual or link["lookup_matches"] != matches:
                raise EvidenceError("raw animation lookup evidence mismatch")
            reference = link.get("normalized_reference")
            if not isinstance(reference, dict):
                raise EvidenceError("invalid normalized animation reference")
            try:
                field = frame.state
                tokens = _tokens(path)
                for token in tokens:
                    field = field[_index(field, token)]
            except (KeyError, TypeError) as error:
                raise EvidenceError("raw animation owner path is missing") from error
            if reference != field:
                raise EvidenceError("raw animation reference differs from comparable state")
            owner_squished = False
            plant_path = bool(tokens and tokens[0] == "plants")
            flag_path = (
                len(tokens) == 5
                and tokens[0] == "zombies"
                and tokens[1] == "slots"
                and tokens[3:] == ("fields", "00000144")
            )
            flag_dropped = False
            flag_fields_present = "owner_zombie_type" in link or "owner_has_object" in link
            if flag_fields_present and not (self.flag_retirement_rule and flag_path):
                raise EvidenceError("flag retirement evidence requires its declared zombie role")
            if self.flag_retirement_rule and flag_path:
                flags = frame.state["zombies"]["slots"][tokens[2]]["fields"]
                zombie_type, has_object = flags.get("00000024"), flags.get("000000bc")
                if (
                    not uint(zombie_type)
                    or type(has_object) is not int
                    or has_object not in (0, 1)
                    or type(link.get("owner_zombie_type")) is not int
                    or link["owner_zombie_type"] != zombie_type
                    or type(link.get("owner_has_object")) is not bool
                    or link["owner_has_object"] != bool(has_object)
                ):
                    raise EvidenceError("raw flag owner fields differ from actual captured fields")
                flag_dropped = (
                    zombie_type == 1 and has_object == 0 and handle >> 16 != 0 and link["slot"] < pool["used"]
                )
            if "owner_squished" in link and (not plant_path or not self.owner_retirement_rules):
                raise EvidenceError("owner_squished requires a plant owner and declared retirement rules")
            if self.owner_retirement_rules:
                dead_offsets = {
                    "plants": "00000141",
                    "zombies": "000000ec",
                    "mowers": "00000030",
                    "grid_items": "00000020",
                }
                if tokens and tokens[0] in dead_offsets:
                    if len(tokens) != 5 or tokens[1] != "slots" or tokens[3] != "fields":
                        raise EvidenceError("invalid entity animation owner path")
                    owner_key = tokens[:3]
                    if owner_key not in checked_flags:
                        fields = frame.state[tokens[0]]["slots"][tokens[2]]["fields"]
                        dead = fields.get(dead_offsets[tokens[0]])
                        squished = fields.get("00000142") if plant_path else 0
                        if not uint(dead) or not uint(squished):
                            raise EvidenceError("animation owner retirement fields are missing or invalid")
                        checked_flags[owner_key] = (dead != 0, squished != 0)
                    actual_dead, actual_squished = checked_flags[owner_key]
                    if link["owner_dead"] != actual_dead:
                        raise EvidenceError("raw owner_dead differs from the actual owner field")
                    if plant_path:
                        if type(link.get("owner_squished")) is not bool or link["owner_squished"] != actual_squished:
                            raise EvidenceError("raw owner_squished differs from the actual plant field")
                        owner_squished = actual_squished
                elif link["owner_dead"]:
                    raise EvidenceError("non-entity animation owner cannot claim owner_dead")
            if "retirement_reason" in link and (
                not self.owner_retirement_rules or reference.get("status") != "expired"
            ):
                raise EvidenceError("unexpected animation retirement reason")
            if not handle:
                if reference != {"status": "null"}:
                    raise EvidenceError("non-null reference for zero animation handle")
            elif not matches:
                reason = (
                    "out_of_range"
                    if link["slot"] >= pool["capacity"]
                    else ("not_allocated" if actual is None else "generation_mismatch")
                )
                if (
                    not (link["owner_dead"] or owner_squished or flag_dropped)
                    or reference != {"status": "expired"}
                    or link.get("lookup_failure") != reason
                ):
                    raise EvidenceError("invalid or dangling live animation link")
                if self.owner_retirement_rules:
                    retirement = (
                        "owner_dead"
                        if link["owner_dead"]
                        else "plant_squished_remove_effects"
                        if owner_squished
                        else "zombie_flag_dropped"
                    )
                    if link.get("retirement_reason") != retirement:
                        raise EvidenceError("animation retirement reason differs from actual lifecycle flags")
            else:
                node = link.get("logical_node")
                if (
                    not isinstance(node, str)
                    or reference.get("node") != node
                    or reference.get("status") not in {"live", "retiring"}
                    or node not in normalized["nodes"]
                ):
                    raise EvidenceError("raw animation logical node mismatch")
                owners.setdefault(node, []).append(anchor)
        if set(owners) != set(normalized["nodes"]):
            raise EvidenceError("raw animation node evidence is incomplete")
        expected_paths = set()
        board = frame.state.get("board", {})
        for key, value in board.items() if isinstance(board, dict) else ():
            if isinstance(value, dict) and "status" in value:
                expected_paths.add("/board/" + key.replace("~", "~0").replace("/", "~1"))
        for pool_name in ("zombies", "plants", "mowers", "grid_items"):
            for slot, entity in frame.state.get(pool_name, {}).get("slots", {}).items():
                for key, value in entity.get("fields", {}).items():
                    if isinstance(value, dict) and "status" in value:
                        expected_paths.add(f"/{pool_name}/slots/{slot}/fields/{key}")
        if paths != expected_paths:
            raise EvidenceError("raw animation owner evidence is incomplete")
        for node, anchors_for_node in owners.items():
            if normalized["nodes"][node].get("owners") != anchors_for_node:
                raise EvidenceError("raw animation owner alias graph mismatch")


class _FrameDecoder:
    def __init__(self, *, reuse_state=False, engine_calls=False):
        self.state = None
        self.previous = None
        self.cache = {}
        self.reuse_state = reuse_state
        self.engine_calls = engine_calls

    def accept(self, hashes, delta):
        AuditLog._envelope(hashes)
        AuditLog._envelope(delta)
        for key in ("schema", "seq", "kind", "version", "payload"):
            if hashes[key] != delta[key]:
                raise EvidenceError(f"checksum/delta envelope mismatch: {key}")
        if hashes["kind"] not in {"pre_step", "post_step"}:
            raise EvidenceError("unexpected audit frame kind")
        previous = self.previous
        if previous and hashes["seq"] <= previous.seq:
            raise EvidenceError("audit frame sequence is not increasing")
        if self.state is None:
            if "initial" not in delta or "patch" in delta or not isinstance(delta["initial"], dict):
                raise EvidenceError("first audit frame requires initial state")
            self.state = delta["initial"]
            _check_state_values(self.state)
            changed = set(self.state)
        else:
            if "initial" in delta or not isinstance(delta.get("patch"), list):
                raise EvidenceError("subsequent audit frame requires patch")
            changed, root_changed = set(), False
            for operation in delta["patch"]:
                if not isinstance(operation, dict):
                    raise EvidenceError("invalid JSON Patch operation")
                tokens = _tokens(operation.get("path"))
                if tokens:
                    changed.add(tokens[0])
                else:
                    root_changed = True
                if "value" in operation:
                    _check_state_values(operation["value"])
            self.state = patch(self.state, delta["patch"], in_place=self.reuse_state)
            if not isinstance(self.state, dict):
                raise EvidenceError("audit state root must remain an object")
            if root_changed:
                changed = set(self.state)
        computed, encoded = _state_digests(self.state, self.cache, changed, with_encoded=True)
        if self.state.get("schema") != SCHEMA or hashes.get("digests") != computed:
            raise EvidenceError("state digest/schema mismatch after JSON Patch reconstruction")
        if "reanimations" in self.state and (
            not isinstance(self.state["reanimations"], dict) or self.state["reanimations"].get("valid") is not True
        ):
            raise EvidenceError("native animation links are invalid")
        frame = AuditFrame(
            hashes["seq"], hashes["kind"], hashes["version"], hashes["payload"], self.state, hashes["digests"], encoded
        )
        if frame.kind == "pre_step":
            if previous and previous.kind != "post_step":
                raise EvidenceError("missing post_step")
        else:
            if previous is None or previous.kind != "pre_step":
                raise EvidenceError("missing pre_step")
            before, after = previous.version, frame.version
            zero_candidate = self.engine_calls and frame.payload.get("native_tick_delta") == 0 and after == before
            if not zero_candidate and (
                after["epoch"] != before["epoch"]
                or after["tick"] != before["tick"] + 1
                or after["revision"] != 0
                or frame.payload.get("native_tick_delta") != 1
                or frame.payload.get("request_id") != previous.payload.get("request_id")
            ):
                raise EvidenceError("invalid actual one-tick audit boundary")
        self.previous = frame
        return frame

    def finish(self):
        if self.previous and self.previous.kind != "post_step":
            raise EvidenceError("audit ends in an unfinished step")


class _AuditStreamDecoder:
    """Single merge cursor: one state, one frame of seeds, and small counters."""

    def __init__(self, manifest, files, *, reuse_state, audio_activation=None):
        self.audio = sound_effects.Evidence(manifest, audio_activation)
        self.sound_counter = sound_counter.Evidence(manifest, audio_activation)
        self.fp = fp_environment.Evidence(manifest)
        self.app_anchor = app_update_anchor.Evidence(manifest)
        self.mj_anchor = mj_clock_anchor.Evidence(manifest)
        self.b0 = b0_normalization.Evidence(manifest)
        self.calls = _EngineCallEvidence() if engine_call_mode(manifest) else None
        self.frames = _FrameDecoder(reuse_state=reuse_state, engine_calls=self.calls is not None)
        self.animation = _AnimationDecoder(manifest) if RAW_ANIMATIONS in files else None
        self.particle = _ParticleEvidence() if PARTICLE_SHAKE_RAW in files else None
        self.hosted_fire = _HostedFireEvidence() if hosted_fire_mode(manifest) else None
        self.draw = _DrawEvidence(manifest) if draw_mode(manifest) else None
        self.summary = _EventSummary()
        self.pending = []
        self.pending_spawns = []
        self.next_seq = 0
        self.last_frame = None
        self.controlled_calls = 0
        self.rolling = 14695981039346656037
        self.reuse_state = reuse_state
        self.peak_pending = 0

    def event(self, event, raw):
        self.summary.accept(event)
        self.audio.event(event)
        self.sound_counter.event(event)
        self.fp.event(event)
        self.app_anchor.event(event)
        self.mj_anchor.event(event)
        self.b0.event(event)
        if self.calls:
            self.calls.event(event, raw)
        elif (
            event["kind"] == "engine_call_closed"
            or "engine_call" in event["payload"]
            or "engine_call_id" in event["payload"]
        ):
            raise EvidenceError("engine call metadata lacks its declared mode")
        if self.draw:
            self.draw.event(event)
        elif event["kind"] in {"render_preparing", "render_prepared", "draw_schedule_closed"}:
            raise EvidenceError("controlled draw event lacks an explicit engine mode")
        shared = (
            event["kind"] == "zombie_first_boundary_observed"
            and self.last_frame is not None
            and event["seq"] == self.last_frame.seq
        )
        if shared:
            if event["version"] != self.last_frame.version:
                raise EvidenceError("birth annotation/frame version mismatch")
        elif event["seq"] != self.next_seq:
            raise EvidenceError("native audit sequence is missing or duplicated")
        else:
            self.next_seq += 1
        if event["kind"] == "particle_shake_seed":
            if self.particle is None or raw is None:
                raise EvidenceError("particle shake semantic/raw evidence or declared engine mode is missing")
            self.particle.accept(event, raw)
            if event["phase"] == "controlled_boundary":
                if len(self.pending) >= _PARTICLE_BOUNDARY_LIMIT:
                    raise EvidenceError("particle calls before an audited boundary exceed reader bound 8192")
                self.pending.append(event)
                self.peak_pending = max(self.peak_pending, len(self.pending))
            elif self.last_frame is not None:
                raise EvidenceError("particle shake lost its controlled boundary after recording started")
        elif event["kind"] == HOSTED_FIRE_KIND:
            if self.hosted_fire is None:
                raise EvidenceError("hosted fire record lacks its declared audit mode")
            self.hosted_fire.event(event)
        elif event["kind"] == "particle_shake_closed":
            if self.particle is None:
                raise EvidenceError("particle shake close has no declared engine mode")
            self.particle.close(event)
        elif event["kind"] == "zombie_initialized":
            if event["phase"] == "controlled_boundary":
                if len(self.pending_spawns) >= _SPAWN_BOUNDARY_LIMIT:
                    raise EvidenceError("spawns before an audited boundary exceed reader bound 1024")
                self.pending_spawns.append(event)
            elif self.last_frame is not None and self.last_frame.kind == "pre_step":
                raise EvidenceError("spawn lost its controlled boundary during an audited update")

    def frame(self, records):
        if self.summary.recording_closed:
            raise EvidenceError("native frame after recording close")
        frame = self.frames.accept(records[0], records[1])
        engine_raw_index = 2 + int(self.animation is not None)
        counter_raw = records[engine_raw_index + int(self.calls is not None)] if self.sound_counter.enabled else None
        self.sound_counter.frame_with_audio(self.audio, frame, counter_raw)
        fp_raw_index = engine_raw_index + int(self.calls is not None) + int(self.sound_counter.enabled)
        self.fp.frame(frame, records[fp_raw_index] if self.fp.enabled else None)
        self.app_anchor.frame(frame)
        self.mj_anchor.frame(frame)
        self.b0.frame(frame)
        if self.calls:
            self.calls.frame(frame, records[engine_raw_index])
            frame = replace(frame, raw_engine_call=records[engine_raw_index])
        elif "engine_call" in frame.payload:
            raise EvidenceError("engine call frame lacks its declared mode")
        if self.draw:
            self.draw.frame(frame)
        elif "draw_schedule" in frame.state or "render" in frame.payload:
            raise EvidenceError("controlled draw state/receipt requires an explicit engine mode")
        if frame.seq != self.next_seq:
            raise EvidenceError("native audit sequence is missing or duplicated")
        self.next_seq += 1
        if self.animation:
            self.animation.accept(records[2], frame)
            frame = replace(
                frame, raw_animations=self.animation.state if self.reuse_state else copy.deepcopy(self.animation.state)
            )
        for event in self.pending:
            _particle_boundary(event, frame, self.last_frame)
            self.controlled_calls += 1
            self.rolling = _particle_digest(self.rolling, event["payload"])
        if self.particle:
            state = frame.state.get("particle_shake")
            if (
                not isinstance(state, dict)
                or state.get("mode") != PARTICLE_SHAKE_MODE
                or type(state.get("controlled_calls")) is not int
                or state["controlled_calls"] != self.controlled_calls
                or type(state.get("controlled_digest")) is not int
                or state["controlled_digest"] != self.rolling
            ):
                raise EvidenceError("particle shake state count/digest differs from verified seed events")
        elif "particle_shake" in frame.state:
            raise EvidenceError("particle shake state requires an explicit engine mode")
        for event in self.pending_spawns:
            before = event["version"]
            if frame.kind == "post_step":
                if self.last_frame is None or self.last_frame.kind != "pre_step" or before != self.last_frame.version:
                    raise EvidenceError("spawn is not bound to its actual pre/post update")
            elif (
                before["epoch"] != frame.version["epoch"]
                or before["tick"] != frame.version["tick"]
                or before["revision"] > frame.version["revision"]
            ):
                raise EvidenceError("spawn action is not bound to its next audited pre-step")
        result = replace(frame, particle_seeds=tuple(self.pending), spawn_events=tuple(self.pending_spawns))
        self.pending.clear()
        self.pending_spawns.clear()
        if self.hosted_fire is not None:
            self.hosted_fire.frame(frame)
        elif "hosted_fire" in frame.state:
            raise EvidenceError("hosted fire state requires an explicit audit mode")
        self.last_frame = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
        return result

    def finish(self, *, final=False):
        self.frames.finish()
        if self.calls:
            self.calls.finish()
        if self.draw:
            self.draw.finish()
        if final and self.hosted_fire is not None:
            self.hosted_fire.finish()
        if final and self.pending:
            raise EvidenceError("particle shake calls have no following audited boundary")
        if final and self.pending_spawns:
            raise EvidenceError("controlled spawns have no following audited boundary")


def _walk_audit(decoder, read, *, retain_event=None, request_id=None, constrain_request=False):
    """Merge sorted event/frame sequences without retaining the event history."""
    from itertools import zip_longest

    event_reader = iter(read("events.jsonl"))
    names = ["checksums.jsonl", "state-deltas.jsonl"]
    if decoder.animation:
        names.append(RAW_ANIMATIONS)
    if decoder.calls:
        names.append(ENGINE_CALL_RAW)
    if decoder.sound_counter.enabled:
        names.append(sound_counter.RAW_FILE)
    if decoder.fp.enabled:
        names.append(fp_environment.RAW_FILE)
    frame_readers = [iter(read(name)) for name in names]
    rows = zip_longest(*frame_readers)
    raw = iter(read(PARTICLE_SHAKE_RAW)) if decoder.particle else iter(())

    def next_event():
        event = next(event_reader, None)
        if event is not None:
            AuditLog._envelope(event)
        return event

    def next_frame():
        records = next(rows, None)
        if records is not None:
            if any(record is None for record in records):
                raise EvidenceError("checksum/delta/raw animation record count mismatch")
            AuditLog._envelope(records[0])
        return records

    try:
        pending_terminal = None
        event, records = next_event(), next_frame()
        while event is not None or records is not None:
            if records is not None and (event is None or records[0]["seq"] <= event["seq"]):
                if pending_terminal is not None:
                    raise EvidenceError("new frame precedes zero terminal verification")
                frame = decoder.frame(records)
                if constrain_request and frame.payload.get("request_id") != request_id:
                    raise EvidenceError("unexpected intervening request in live frame audit")
                if decoder.calls and frame.kind == "post_step" and frame.payload["native_tick_delta"] == 0:
                    pending_terminal = frame
                else:
                    yield frame
                records = next_frame()
            else:
                decoder.event(event, next(raw, None) if event["kind"] == "particle_shake_seed" else None)
                if retain_event is not None and event["kind"] != "particle_shake_seed":
                    retain_event(event)
                if pending_terminal is not None and decoder.calls.terminal_completed:
                    yield pending_terminal
                    pending_terminal = None
                event = next_event()
        if next(raw, None) is not None:
            raise EvidenceError("particle shake semantic/raw record count mismatch")
        decoder.finish()
    finally:
        first_error = None
        for reader in [event_reader, *frame_readers, raw]:
            close = getattr(reader, "close", None)
            if close:
                try:
                    close()
                except Exception as error:
                    first_error = first_error or error
        if first_error is not None:
            raise first_error


class FrameSelection:
    """Reconstruct lazily so a full match does not retain every full state."""

    def __init__(self, owner, indices=None):
        self.owner = owner
        self.indices = range(len(owner._frame_headers)) if indices is None else indices

    def __len__(self):
        return len(self.indices)

    def __iter__(self):
        wanted = iter(self.indices)
        current = next(wanted, None)
        if current is None:
            return
        frames = self.owner._frames()
        try:
            for index, frame in enumerate(frames):
                if index == current:
                    yield frame
                    current = next(wanted, None)
                    if current is None:
                        return
        finally:
            frames.close()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return FrameSelection(self.owner, self.indices[index])
        wanted = self.indices[index]
        selection = iter(FrameSelection(self.owner, [wanted]))
        try:
            return next(selection)
        finally:
            selection.close()


class AuditLog:
    def __init__(self, directory: str | Path, *, require_closed: bool = False):
        self.directory = Path(directory)
        self.manifest = read_json(self.directory / "manifest.json")
        if self.manifest.get("schema") != SCHEMA or not isinstance(self.manifest.get("target"), str):
            raise EvidenceError("invalid native audit manifest")
        if self.manifest.get("loaded_signatures_match") is not True:
            raise EvidenceError("native target signatures did not match")
        self.evidence_files = audit_files(self.directory, self.manifest)
        self.store = evidence_codec.EvidenceStore(self.directory, error=EvidenceError)
        self._files = {name: EventStream(self.store, name) for name in self.evidence_files}
        self.audio_activation = (
            decode(self.store.stored_path(sound_effects.EVIDENCE).read_bytes())
            if sound_effects.EVIDENCE in self.evidence_files
            else None
        )
        if self.audio_activation is not None:
            self._files[sound_effects.EVIDENCE].verify()
        self.events = self._files["events.jsonl"]
        self._control_events, self._headers, self._frame_headers = [], [], []
        self._requests, self._request_frames, self._frame_indices = {}, {}, {}
        decoder = _AuditStreamDecoder(
            self.manifest, self.evidence_files, reuse_state=True, audio_activation=self.audio_activation
        )
        for index, frame in enumerate(_walk_audit(decoder, self._files.__getitem__, retain_event=self._retain_event)):
            header = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
            rid = frame.payload.get("request_id")
            self._headers.append(header)
            self._frame_headers.append((frame.seq, rid))
            self._request_frames.setdefault(rid, []).append(header)
            self._frame_indices.setdefault(rid, []).append(index)
        decoder.finish(final=True)
        self._particle, self._summary, self._draw, self._calls = (
            decoder.particle,
            decoder.summary,
            decoder.draw,
            decoder.calls,
        )
        self._audio = decoder.audio
        self._app_anchor = decoder.app_anchor
        self._mj_anchor = decoder.mj_anchor
        self._b0 = decoder.b0
        self._sound_counter = decoder.sound_counter
        self._fp = decoder.fp
        self.peak_pending_particle_calls = decoder.peak_pending
        self.frames = FrameSelection(self)
        if require_closed:
            _closed_events(
                self._summary,
                particle=self._particle,
                draw=self._draw,
                calls=self._calls,
                audio=self._audio,
                fp=self._fp,
                spawn_required=self.manifest.get("spawn_hook", {}).get("installed") is True,
                probes_required=self.manifest.get("lifecycle_probes", {}).get("enabled") is True,
                probes_allowed=self.manifest.get("lifecycle_probes", {}).get("enabled") is True,
                manifest=self.manifest,
            )
        # Full semantic lifecycle validation, not a receipt-existence check. A
        # live snapshot (require_closed=False) may be missing the receipt but
        # its present records must still satisfy the contract.
        lifecycle_report(self.directory, self.manifest, require_close=require_closed)

    def _retain_event(self, event):
        self._control_events.append(event)
        self._requests.setdefault(event["payload"].get("request_id"), []).append(event)

    def verify_files(self):
        """Explicit success barrier, including sources never opened by a seek."""
        if audit_files(self.directory, self.manifest) != self.evidence_files:
            raise EvidenceError("closed audit evidence file set changed")
        for evidence in self._files.values():
            evidence.verify()

    @property
    def birth_counts(self):
        return {
            "controlled": self._summary.controlled_birth_count,
            "initialization": self._summary.initialization_birth_count,
        }

    def validate_draw_initial(self, initial):
        if self._draw:
            self._draw.initial(initial)

    def validate_audio_initial(self, initial):
        self._audio.initial(initial)

    def validate_app_anchor_initial(self, initial):
        self._app_anchor.initial(initial)

    @property
    def app_anchor_receipt(self):
        return copy.deepcopy(self._app_anchor.anchor)

    def validate_mj_clock_initial(self, initial):
        self._mj_anchor.initial(initial)

    @property
    def mj_clock_receipt(self):
        return copy.deepcopy(self._mj_anchor.anchor)

    def validate_b0_normalization_initial(self, initial):
        self._b0.initial(initial)

    @property
    def b0_normalization_receipt(self):
        return copy.deepcopy(self._b0.anchor)

    def validate_fp_initial(self, initial):
        self._fp.initial(initial)

    @property
    def fp_activation(self):
        return copy.deepcopy(self._fp.activation)

    @property
    def fp_health(self):
        return copy.deepcopy(self._fp.health)

    def validate_sound_counter_initial(self, initial):
        self._sound_counter.initial(initial)

    @property
    def sound_counter_receipt(self):
        return copy.deepcopy(self._sound_counter.origin)

    @property
    def sound_effects_health(self):
        return copy.deepcopy(self._audio.health)

    @property
    def warm_render(self):
        return copy.deepcopy(self._draw.warm) if self._draw else None

    @property
    def draw_health(self):
        return copy.deepcopy(self._draw.health) if self._draw else None

    @property
    def engine_call_health(self):
        return copy.deepcopy(self._calls.health) if self._calls else None

    @property
    def control_events(self):
        """Verified control/birth index, excluding high-volume particle calls."""
        self.events.verify()
        return iter(self._control_events)

    @staticmethod
    def _envelope(record):
        if record.get("schema") != SCHEMA or type(record.get("seq")) is not int or record["seq"] < 0:
            raise EvidenceError("invalid native audit schema/sequence")
        if not isinstance(record.get("payload"), dict) or not isinstance(record.get("kind"), str):
            raise EvidenceError("invalid native audit envelope")
        if record["kind"] in {
            "spawn_hook_fault",
            "particle_shake_fault",
            "reanimation_link_fault",
            "render_failed",
            "draw_schedule_fault",
            "engine_call_fault",
            "app_update_anchor_failed",
            "mj_clock_anchor_failed",
            "b0_normalization_failed",
            "sound_counter_origin_failed",
            "fp_environment_fault",
        }:
            raise EvidenceError("native hook fault invalidates strict evidence")
        if record["kind"] == "particle_shake_seed":
            if record.get("native_phase") != "before_srand" or record.get("phase") not in {
                "controlled_boundary",
                "initialization",
            }:
                raise EvidenceError("invalid particle shake capture phase")
            if record["phase"] == "initialization":
                if "version" not in record or record["version"] is not None:
                    raise EvidenceError("initialization particle shake must have explicitly unassigned version")
                return
        if record["kind"] == "zombie_initialized":
            spawn = record["payload"]
            if (
                record.get("native_phase") != "zombie_initialize_exit"
                or spawn.get("schema") != "lvz.spawn.v1"
                or spawn.get("kind") != "zombie_initialized"
                or spawn.get("phase") != "zombie_initialize_exit"
            ):
                raise EvidenceError("invalid exact zombie initialization evidence")
            if record.get("phase") == "initialization":
                if record.get("version") is not None or "boundary" not in spawn or spawn["boundary"] is not None:
                    raise EvidenceError("initialization spawn must have an explicitly unassigned boundary")
                return
            boundary = spawn.get("boundary")
            if (
                record.get("phase") != "controlled_boundary"
                or not isinstance(boundary, dict)
                or record.get("version")
                != {
                    "epoch": boundary.get("segment"),
                    "tick": boundary.get("tick"),
                    "revision": boundary.get("revision"),
                }
            ):
                raise EvidenceError("controlled spawn boundary/version mismatch")
        version(record.get("version"))

    def _frames(self, *, reuse_state=False):
        self._files["manifest.json"].verify()
        if audit_files(self.directory, self.manifest) != self.evidence_files:
            raise EvidenceError("closed audit evidence file set changed")
        if self.audio_activation is not None:
            self._files[sound_effects.EVIDENCE].verify()
        decoder = _AuditStreamDecoder(
            self.manifest, self.evidence_files, reuse_state=reuse_state, audio_activation=self.audio_activation
        )
        yield from _walk_audit(decoder, self._files.__getitem__)
        decoder.finish(final=True)

    def request_frames(self, request_id: str) -> FrameSelection:
        return FrameSelection(self, self._frame_indices.get(request_id, []))

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        """Metadata only; useful for ordering checks without state reconstruction."""
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


class _ConsumedEventStream:
    """Repeatable view of the consumed live prefix; never retains its rows."""

    def __init__(self, owner):
        self.owner = owner

    def __iter__(self):
        yield from self.owner._prefix_records("events.jsonl")


class AuditTail:
    """Verify append-only evidence with one state and at most 8192 pending seeds.

    Control/birth records and small frame headers remain indexed. Every seed's
    raw and semantic records are verified as they arrive, then released after
    its frame is consumed. Closing rechecks consumed byte hashes, without
    decoding or patching the history again.

    This reader follows a *live* recording with byte offsets, so it accepts only
    plain evidence. A sealed archive whose containers were compressed is read
    through AuditLog instead and is rejected here instead of misreading offsets.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.store = evidence_codec.EvidenceStore(self.directory, error=EvidenceError)
        if self.store.compressed:
            raise EvidenceError("live audit tail requires plain evidence; this archive is compressed")
        self.manifest = read_json(self.directory / "manifest.json")
        if (
            self.manifest.get("schema") != SCHEMA
            or not isinstance(self.manifest.get("target"), str)
            or self.manifest.get("loaded_signatures_match") is not True
        ):
            raise EvidenceError("invalid live audit target manifest")
        self._manifest_file = EventStream(self.store, "manifest.json")
        # The live tail follows engine-frame evidence only. Lifecycle evidence is
        # a separate offline contract (the offline validator and
        # AuditLog(require_closed) own it): it is excluded from frame positions
        # and from the frame file-set check, and the receipt may legitimately
        # appear while the tail is still being read.
        self.evidence_files = audit_files(self.directory, self.manifest)
        self._frame_files = self._frame_evidence_files()
        self._static_files = {
            name: EventStream(self.store, name) for name in self.evidence_files if name == sound_effects.EVIDENCE
        }
        self.audio_activation = (
            decode(self.store.stored_path(sound_effects.EVIDENCE).read_bytes()) if self._static_files else None
        )
        for evidence in self._static_files.values():
            evidence.verify()
        self._headers, self._control_events = [], []
        self._positions = {
            name: 0 for name in self._frame_files if name != "manifest.json" and name not in self._static_files
        }
        self._hashes = {name: hashlib.sha256() for name in self._positions}
        self._stream = _AuditStreamDecoder(
            self.manifest, self.evidence_files, reuse_state=True, audio_activation=self.audio_activation
        )
        self._decoder, self._animation, self._particle = (
            self._stream.frames,
            self._stream.animation,
            self._stream.particle,
        )
        self._draw = self._stream.draw
        self._calls = self._stream.calls
        self._audio = self._stream.audio
        self._app_anchor = self._stream.app_anchor
        self._mj_anchor = self._stream.mj_anchor
        self._b0 = self._stream.b0
        self._sound_counter = self._stream.sound_counter
        self._fp = self._stream.fp
        self._identities = {}
        self._requests, self._request_frames = {}, {}
        self.events = _ConsumedEventStream(self)

    @property
    def peak_pending_particle_calls(self):
        return self._stream.peak_pending

    @property
    def birth_counts(self):
        return {
            "controlled": self._stream.summary.controlled_birth_count,
            "initialization": self._stream.summary.initialization_birth_count,
        }

    def validate_draw_initial(self, initial):
        if self._draw:
            self._draw.initial(initial)

    def validate_audio_initial(self, initial):
        self._audio.initial(initial)

    def validate_app_anchor_initial(self, initial):
        self._app_anchor.initial(initial)

    @property
    def app_anchor_receipt(self):
        return copy.deepcopy(self._app_anchor.anchor)

    def validate_mj_clock_initial(self, initial):
        self._mj_anchor.initial(initial)

    @property
    def mj_clock_receipt(self):
        return copy.deepcopy(self._mj_anchor.anchor)

    def validate_b0_normalization_initial(self, initial):
        self._b0.initial(initial)

    @property
    def b0_normalization_receipt(self):
        return copy.deepcopy(self._b0.anchor)

    def validate_fp_initial(self, initial):
        self._fp.initial(initial)

    @property
    def fp_activation(self):
        return copy.deepcopy(self._fp.activation)

    @property
    def fp_health(self):
        return copy.deepcopy(self._fp.health)

    def validate_sound_counter_initial(self, initial):
        self._sound_counter.initial(initial)

    @property
    def sound_counter_receipt(self):
        return copy.deepcopy(self._sound_counter.origin)

    @property
    def sound_effects_health(self):
        return copy.deepcopy(self._audio.health)

    @property
    def warm_render(self):
        return copy.deepcopy(self._draw.warm) if self._draw else None

    @property
    def draw_health(self):
        return copy.deepcopy(self._draw.health) if self._draw else None

    @property
    def engine_call_health(self):
        return copy.deepcopy(self._calls.health) if self._calls else None

    def _frame_evidence_files(self):
        return tuple(
            name
            for name in audit_files(self.directory, self.manifest)
            if name not in (lifecycle_events.EVENTS_FILE, lifecycle_events.RECEIPT_FILE)
        )

    def _check_file(self, name, *, required_size=None):
        stat = (self.directory / name).stat()
        identity = (stat.st_dev, stat.st_ino)
        if name in self._identities and self._identities[name] != identity:
            raise EvidenceError("live audit file was replaced")
        self._identities[name] = identity
        if stat.st_size < (self._positions[name] if required_size is None else required_size):
            raise EvidenceError("live audit file was truncated")
        return stat

    def _new_records(self, name):
        # A completed response flushes its evidence. Read exactly this prefix;
        # an unrelated later request cannot extend a cursor indefinitely.
        limit = self._check_file(name).st_size
        with (self.directory / name).open("rb") as stream:
            stream.seek(self._positions[name])
            while stream.tell() < limit:
                line = stream.readline(min(limit - stream.tell(), _JSONL_MAX_RECORD_BYTES + 1))
                if len(line) > _JSONL_MAX_RECORD_BYTES:
                    raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                if not line.endswith(b"\n"):
                    raise EvidenceError("live audit contains an incomplete flushed record")
                value = decode(line)
                if not isinstance(value, dict):
                    raise EvidenceError("live audit record must be an object")
                self._hashes[name].update(line)
                self._positions[name] = stream.tell()
                yield value
        self._check_file(name, required_size=limit)

    def _verify_prefix(self, name, limit, expected):
        self._check_file(name, required_size=limit)
        digest = hashlib.sha256()
        with (self.directory / name).open("rb") as stream:
            remaining = limit
            while remaining:
                block = stream.read(min(1 << 20, remaining))
                if not block:
                    raise EvidenceError("live audit file was truncated")
                digest.update(block)
                remaining -= len(block)
        self._check_file(name, required_size=limit)
        if digest.hexdigest() != expected:
            raise EvidenceError("previously consumed live audit prefix SHA-256 changed")

    def _prefix_records(self, name):
        limit, expected = self._positions[name], self._hashes[name].hexdigest()
        self._check_file(name, required_size=limit)
        digest, complete = hashlib.sha256(), False
        try:
            with (self.directory / name).open("rb") as stream:
                while stream.tell() < limit:
                    line = stream.readline(min(limit - stream.tell(), _JSONL_MAX_RECORD_BYTES + 1))
                    if len(line) > _JSONL_MAX_RECORD_BYTES:
                        raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                    if not line.endswith(b"\n"):
                        raise EvidenceError("live audit contains an incomplete consumed record")
                    digest.update(line)
                    value = decode(line)
                    if not isinstance(value, dict):
                        raise EvidenceError("live audit record must be an object")
                    yield value
            complete = True
            self._check_file(name, required_size=limit)
            if digest.hexdigest() != expected:
                raise EvidenceError("previously consumed live audit prefix SHA-256 changed")
        finally:
            if not complete:
                self._verify_prefix(name, limit, expected)

    def _retain_event(self, event):
        self._control_events.append(event)
        self._requests.setdefault(event["payload"].get("request_id"), []).append(event)

    def read_request(self, request_id: str | None):
        self._manifest_file.verify()
        for evidence in self._static_files.values():
            evidence.verify()
        if self._frame_evidence_files() != self._frame_files:
            raise EvidenceError("live audit evidence file set changed")
        for frame in _walk_audit(
            self._stream,
            self._new_records,
            retain_event=self._retain_event,
            request_id=request_id,
            constrain_request=True,
        ):
            header = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
            self._headers.append(header)
            self._request_frames.setdefault(request_id, []).append(header)
            yield frame

    def verify_closed(self):
        for _ in self.read_request(None):
            raise EvidenceError("unexpected unconsumed frames at recording close")
        self._stream.finish(final=True)
        _closed_events(
            self._stream.summary,
            particle=self._particle,
            draw=self._draw,
            calls=self._calls,
            audio=self._audio,
            fp=self._fp,
            spawn_required=self.manifest.get("spawn_hook", {}).get("installed") is True,
            probes_required=self.manifest.get("lifecycle_probes", {}).get("enabled") is True,
            probes_allowed=self.manifest.get("lifecycle_probes", {}).get("enabled") is True,
            manifest=self.manifest,
        )
        lifecycle_report(self.directory, self.manifest, require_close=True)
        for name, position in self._positions.items():
            if self._check_file(name).st_size != position:
                raise EvidenceError("unconsumed bytes after recording close")
            self._verify_prefix(name, position, self._hashes[name].hexdigest())

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


def lifecycle_identity_normalized_manifest(manifest: dict) -> dict:
    """Normalize only the permitted lifecycle identity fields for comparison.

    ``session_id`` is a per-process identity and may differ between two cold
    runs; every other capability/build/counter fact stays in the comparison.
    """
    if not isinstance(manifest, dict):
        return manifest
    block = manifest.get(lifecycle_events.CAPABILITY_KEY)
    if not isinstance(block, dict):
        return manifest
    normalized = copy.deepcopy(manifest)
    normalized[lifecycle_events.CAPABILITY_KEY]["session_id"] = 0
    return normalized


def common_manifest_normalized(manifest: dict) -> dict:
    """Narrow, declared instrumentation normalization for scoped comparison.

    Only the lifecycle identity (run/branch/session) and the exact-store probe
    capability block are removed; every gameplay/state/RNG/action/audio field
    stays in the comparison. The default comparator above remains strict.
    """
    normalized = lifecycle_identity_normalized_manifest(manifest)
    if not isinstance(normalized, dict):
        return normalized
    import copy as _copy

    normalized = _copy.deepcopy(normalized)
    if "lifecycle_probes" in normalized:
        normalized["lifecycle_probes"] = "<instrumentation-scope>"
    return normalized


def compare_common_audits(expected: AuditLog, actual: AuditLog, *, request_map: dict[str, str] | None = None) -> dict:
    """Compare common evidence with the narrow instrumentation normalization.

    The two arms may legitimately differ in their exact-store probe streams and
    final probe counters; those are compared separately by the lifecycle
    semantic comparator. Everything else keeps the full strict comparison.
    """
    result = compare_audits(expected, actual, request_map=request_map, manifest_normalizer=common_manifest_normalized)
    result["normalized"] = ["run_id", "branch_id", "session_id", "lifecycle_probes manifest"]
    result["scope"] = (
        "captured audit state/render/engine-call/particle evidence only; action/result, "
        "endpoint and exact-store probe streams are compared by their own comparators"
    )
    return result


def compare_audits(
    expected: AuditLog, actual: AuditLog, *, request_map: dict[str, str] | None = None, manifest_normalizer=None
) -> dict:
    if manifest_normalizer is None:
        manifest_normalizer = lifecycle_identity_normalized_manifest
    expected_manifest = manifest_normalizer(expected.manifest)
    actual_manifest = manifest_normalizer(actual.manifest)
    if expected_manifest != actual_manifest:
        return {
            "equal": False,
            "reason": "audit_manifest",
            "difference": first_difference(expected_manifest, actual_manifest),
        }
    unname_epoch = lambda value: {"tick": value["tick"], "revision": value["revision"]}
    difference = first_difference(
        render_semantics(expected.warm_render, map_version=unname_epoch),
        render_semantics(actual.warm_render, map_version=unname_epoch),
    )
    if difference:
        return {"equal": False, "reason": "warm_render", "difference": difference}
    left, right = expected.frames, actual.frames
    if request_map is not None:
        left = FrameSelection(expected, [i for i, (_, rid) in enumerate(expected._frame_headers) if rid in request_map])
        wanted = set(request_map.values())
        right = FrameSelection(actual, [i for i, (_, rid) in enumerate(actual._frame_headers) if rid in wanted])
    for index, (a, b) in enumerate(zip(left, right)):
        if a.kind != b.kind or a.version["tick"] != b.version["tick"]:
            return {"equal": False, "reason": "boundary", "index": index}
        if request_map and request_map[a.payload["request_id"]] != b.payload["request_id"]:
            return {"equal": False, "reason": "request_order", "index": index}
        difference = first_difference(
            render_semantics(a.payload.get("render"), map_version=unname_epoch),
            render_semantics(b.payload.get("render"), map_version=unname_epoch),
        )
        if difference:
            return {"equal": False, "reason": "render", "index": index, "difference": difference}
        difference = first_difference(
            engine_call_semantics(a.payload.get("engine_call"), map_version=unname_epoch),
            engine_call_semantics(b.payload.get("engine_call"), map_version=unname_epoch),
        )
        if difference:
            return {"equal": False, "reason": "engine_call", "index": index, "difference": difference}
        difference = first_difference(
            [particle_semantics(event, map_version=unname_epoch) for event in a.particle_seeds],
            [particle_semantics(event, map_version=unname_epoch) for event in b.particle_seeds],
        )
        if difference:
            return {"equal": False, "reason": "particle_shake", "index": index, "difference": difference}
        difference = first_difference(a.state, b.state) if a.canonical_state != b.canonical_state else None
        if difference:
            return {
                "equal": False,
                "reason": "state",
                "index": index,
                "tick": a.version["tick"],
                "phase": a.kind,
                "expected_seq": a.seq,
                "actual_seq": b.seq,
                "difference": difference,
            }
    if len(left) != len(right):
        return {"equal": False, "reason": "frame_count", "expected": len(left), "actual": len(right)}
    expected.verify_files()
    actual.verify_files()
    return {
        "equal": True,
        "frames": len(left),
        "scope": "captured audit fields only",
        "original_engine_replay_verified": False,
    }
