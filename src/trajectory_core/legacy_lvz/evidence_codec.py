"""Lossless gzip storage for sealed audit JSONL evidence.

The native recorder always writes plain JSONL; compression happens in the
host, after the recorder closed and before the run is sealed. Every container
is decompressed and compared with the plain bytes it replaces before the plain
file is removed, and the receipt inside the audit directory binds
logical name -> stored name, both digests and both sizes. Readers refuse a
missing receipt with stored containers, a plain file that a receipt still
declares, a container that does not reproduce its plain digest, and a directory
that mixes both forms. Archives without a receipt keep their plain contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

RECEIPT = "evidence-codec.json"
SCHEMA = "lvz.evidence-codec.v1"
CODEC = "gzip-6-v1"
SUFFIX = ".gz"
_CHUNK = 1 << 22


class CodecError(ValueError):
    pass


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _compress_file(source: Path, destination: Path) -> None:
    import gzip

    with source.open("rb") as plain, destination.open("xb") as raw:
        # mtime=0 keeps the container byte-identical for identical input, so two
        # runs with equal evidence produce equal stored hashes.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as packed:
            while block := plain.read(_CHUNK):
                packed.write(block)
        raw.flush()
        os.fsync(raw.fileno())


def _plain_digest(path: Path) -> tuple[str, int]:
    import gzip

    digest, size = hashlib.sha256(), 0
    with gzip.open(path, "rb") as stream:
        while block := stream.read(_CHUNK):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def read_receipt(directory: Path) -> dict | None:
    path = Path(directory) / RECEIPT
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    files = value.get("files")
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or value.get("codec") != CODEC
        or value.get("state") != "complete"
        or value.get("method") != "replace_in_place_after_verify"
        or not isinstance(files, dict)
        or not files
    ):
        raise CodecError("unsupported audit evidence codec receipt")
    for name, entry in files.items():
        if (
            not isinstance(entry, dict)
            or entry.get("stored") != name + SUFFIX
            or entry.get("codec") != CODEC
            or not name.endswith(".jsonl")
            or any(type(entry.get(key)) is not int or entry[key] < 0 for key in ("plain_bytes", "stored_bytes"))
            or any(
                not isinstance(entry.get(key), str) or len(entry[key]) != 64
                for key in ("plain_sha256", "stored_sha256")
            )
        ):
            raise CodecError("malformed audit evidence codec entry")
    return value


def compress_evidence(directory: Path) -> dict:
    """Replace every audit JSONL file with a verified gzip container.

    Containers are written and verified while the plain files are still in
    place, so an interrupted attempt can be rolled back and never leaves a
    half-read archive. The receipt is written only after every container proved
    it reproduces the recorded plain digest and size; the plain files are then
    reclaimed. A leftover plain file beside a declared container is tolerated by
    the readers and still checked against the recorded plain digest.
    """
    directory = Path(directory)
    existing = read_receipt(directory)
    if existing is not None:
        raise CodecError("audit evidence is already compressed")
    names = sorted(path.name for path in directory.iterdir() if path.is_file() and path.name.endswith(".jsonl"))
    if not names:
        raise CodecError("audit directory has no JSONL evidence to compress")
    if any((directory / (name + SUFFIX)).exists() for name in names):
        raise CodecError("audit directory already contains gzip containers")
    receipt = {
        "schema": SCHEMA,
        "codec": CODEC,
        "state": "complete",
        "method": "replace_in_place_after_verify",
        "files": {},
    }
    written = []
    try:
        for name in names:
            plain, stored = directory / name, directory / (name + SUFFIX)
            temporary = directory / (name + SUFFIX + ".partial")
            _compress_file(plain, temporary)
            plain_sha, plain_bytes = _sha256(plain), plain.stat().st_size
            stored_sha, stored_bytes = _sha256(temporary), temporary.stat().st_size
            recovered_sha, recovered_bytes = _plain_digest(temporary)
            if (recovered_sha, recovered_bytes) != (plain_sha, plain_bytes):
                raise CodecError(f"compressed container does not reproduce {name}")
            os.replace(temporary, stored)
            written.append(stored)
            receipt["files"][name] = {
                "stored": name + SUFFIX,
                "codec": CODEC,
                "plain_sha256": plain_sha,
                "plain_bytes": plain_bytes,
                "stored_sha256": stored_sha,
                "stored_bytes": stored_bytes,
            }
    except BaseException:
        # Roll back every container so the archive keeps its plain contract.
        for path in written:
            path.unlink(missing_ok=True)
        for path in directory.glob("*" + SUFFIX + ".partial"):
            path.unlink(missing_ok=True)
        raise
    _write_receipt(directory, receipt)
    for name in names:
        (directory / name).unlink(missing_ok=True)
    _fsync_directory(directory)
    return receipt


def _write_receipt(directory: Path, receipt: dict) -> None:
    target = directory / RECEIPT
    temporary = directory / (RECEIPT + ".partial")
    with temporary.open("wb") as stream:
        stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:  # Directory handles are not openable on every platform.
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


class EvidenceStore:
    """Resolve logical audit evidence names to their stored form."""

    def __init__(self, directory, *, error=CodecError):
        self.directory = Path(directory)
        self.error = error
        self.receipt = read_receipt(self.directory)
        self.entries = self.receipt["files"] if self.receipt is not None else {}
        if self.receipt is None and any(self.directory.glob("*" + SUFFIX)):
            raise self.error("compressed audit evidence lacks its codec receipt")

    @property
    def compressed(self) -> bool:
        return self.receipt is not None

    def entry(self, name: str) -> dict | None:
        return self.entries.get(name)

    def stored_path(self, name: str) -> Path:
        entry = self.entries.get(name)
        if entry is not None:
            stored = self.directory / entry["stored"]
            if stored.is_file():
                return stored
            return self.directory / name
        if self.receipt is not None and name.endswith(".jsonl"):
            raise self.error(f"compressed archive does not declare evidence: {name}")
        return self.directory / name

    def path(self, name: str) -> Path:
        return self.stored_path(name)

    def exists(self, name: str) -> bool:
        entry = self.entries.get(name)
        if entry is not None:
            return (self.directory / entry["stored"]).is_file() or (self.directory / name).is_file()
        if self.receipt is not None and name.endswith(".jsonl"):
            raise self.error(f"compressed archive does not declare evidence: {name}")
        return (self.directory / name).is_file()

    def plain_sha256(self, name: str) -> str:
        entry = self.entries.get(name)
        if entry is not None:
            return entry["plain_sha256"]
        path = self.directory / name
        if not path.is_file():
            raise self.error(f"missing audit evidence: {name}")
        return _sha256(path)

    def stored_sha256(self, name: str) -> str:
        entry = self.entries.get(name)
        if entry is not None:
            return entry["stored_sha256"]
        path = self.directory / name
        if not path.is_file():
            raise self.error(f"missing audit evidence: {name}")
        return _sha256(path)

    def size(self, name: str) -> int:
        path = self.stored_path(name)
        if not path.is_file():
            raise self.error(f"missing audit evidence: {name}")
        return path.stat().st_size

    def open(self, name: str):
        entry = self.entries.get(name)
        path = self.stored_path(name)
        if not path.is_file():
            raise self.error(f"missing audit evidence: {name}")
        if entry is None:
            return path.open("rb")
        import gzip

        return gzip.open(path, "rb")

    def verify(self, name: str) -> None:
        """Stored bytes and, for a container, the plain bytes it reproduces."""
        entry = self.entries.get(name)
        path = self.stored_path(name)
        if not path.is_file() or _sha256(path) != self.stored_sha256(name):
            raise self.error(f"stored audit evidence SHA-256 changed: {path.name}")
        if entry is None:
            return
        if _plain_digest(path) != (entry["plain_sha256"], entry["plain_bytes"]):
            raise self.error(f"compressed audit evidence no longer reproduces {name}")
        leftover = self.directory / name
        if leftover.is_file() and (entry["plain_sha256"], entry["plain_bytes"]) != (
            _sha256(leftover),
            leftover.stat().st_size,
        ):
            raise self.error(f"leftover plain evidence differs from its recorded bytes: {name}")
