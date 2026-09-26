# Changelog

## 0.1.0 — first public offline core

* Installable `trajectory-core` package (import name `trajectory_core`) with no
  runtime dependencies beyond Python 3.11+.
* Public `load_trajectory` / `validate_trajectory` re-derive manifest identity,
  every evidence digest, the closed native audit stream, the session trace and
  every recorded step.
* Public `load_tree` / `validate_tree` re-derive the root identity, every
  parent-to-child chain and every parent boundary; `inspect_tree` is the
  explicitly partial index-only mode. A child may depart from any boundary its
  parent actually reached, including intermediate action/tick boundaries inside
  a multi-tick request.
* Every tree-index node path is validated for containment (absolute, drive/UNC,
  `..` and resolved symlink escapes) before any node manifest is opened;
  `inspect_tree`, full validation, seal attribution and export all use the
  validated directories.
* Public `verify_seal` re-derives a legacy `lvz.issue99-shovel-fork-seal.v1`
  record against an explicit `source_root`, including its reports. Duplicate or
  malformed node/report declarations raise `SealError` instead of being
  silently overwritten or dropped; an empty report list stays valid.
* Typed public errors (`EvidenceError`, `UnsupportedSchemaError`,
  `IncompleteEvidenceError`, `PathContractError`, `SealError`). Paths use the
  same host-neutral grammar on POSIX and Windows: backslashes are separators and
  drive/UNC/absolute references are always rejected.
* `package_tree` and `export_tree` apply the same full public validation as the
  readers (including parent departure boundaries) before returning or creating
  an artifact; existing destinations are never touched and a partial output is
  removed on failure.
* Repeated `validate_tree`/`load_tree(...).verify()` and
  `validate_trajectory`/`load_trajectory(...).verify()` calls reuse the already
  derived result instead of re-reading the private evidence.
* `python tools/check.py` runs lint, format check, public-API typing, tests,
  sdist/wheel build and a clean-venv outside-repository CLI check.
* Legacy readers and game-state semantics are isolated under
  `trajectory_core.legacy_lvz`; see `NOTICES.md` and `COMPATIBILITY.md`.

Not included in this version: Agent/rollout/env integration, live replay,
dataset or registry services.
