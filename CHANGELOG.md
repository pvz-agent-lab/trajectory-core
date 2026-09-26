# Changelog

## 0.1.0 — first public offline core

* Installable `trajectory-core` package (import name `trajectory_core`) with no
  runtime dependencies beyond Python 3.11+.
* Public `load_trajectory` / `validate_trajectory` re-derive manifest identity,
  every evidence digest, the closed native audit stream, the session trace and
  every recorded step.
* Public `load_tree` / `validate_tree` re-derive the root identity, every
  parent-to-child chain and every parent boundary; `inspect_tree` is the
  explicitly partial index-only mode.
* Public `verify_seal` re-derives a legacy `lvz.issue99-shovel-fork-seal.v1`
  record against an explicit `source_root`, including its reports.
* Typed public errors (`EvidenceError`, `UnsupportedSchemaError`,
  `IncompleteEvidenceError`, `PathContractError`, `SealError`).
* `export_tree` writes a deterministic ZIP and refuses to overwrite.
* `python tools/check.py` runs lint, format check, public-API typing, tests,
  sdist/wheel build and a clean-venv outside-repository CLI check.
* Legacy readers and game-state semantics are isolated under
  `trajectory_core.legacy_lvz`; see `NOTICES.md` and `COMPATIBILITY.md`.

Not included in this version: Agent/rollout/env integration, live replay,
dataset or registry services.
