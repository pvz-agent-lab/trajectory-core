# Changelog

## 0.2.0 — outcome and formal-closure contracts

* Public `trajectory-core.outcome.v2` contract with `Outcome`,
  `validate_outcome`, `read_outcome`, `outcome_document` and
  `outcome_from_legacy`.  It separates the plan unit, measured progress,
  one-cycle completion, declared-goal completion, run lifecycle status,
  execution extent (not executed / partial / complete / unknown),
  termination/truncation reasons and verification status; missing facts stay
  `unknown`, booleans are rejected where counts belong, contradictory
  declarations fail, `full_cycle` is never goal evidence and a completed
  invocation is never goal success.  The legacy adapter reads
  `flags_to_complete` as the historical **rounds** field (one round = two
  flags = twenty waves, never rescaled), derives the execution extent from
  proven source-stage facts only (a completed-round delta, or the source
  play loop's `full_cycle_completed` outcome; the case lifecycle status and
  an absolute wave prove nothing), and records its source, input schema and
  evidence scope.
* Formal producer closure (`trajectory-core.formal-closure.v2`) with
  `FormalClosure`, `seal_formal_closure`, `load_formal_closure`,
  `validate_formal_closure` and `validate_closure_record`, plus the
  `producer-closure-receipt.v2` and `rerun-report.v1` documents.  The closure
  record names the `trajectory-core.controlled-family.v1` profile (every node
  one outcome; every node exactly one baseline/rerun pair role), while the
  generic tree APIs keep accepting larger and unpaired trees.  The receipt
  binds the complete producer-owned artifact inventory (tree evidence,
  outcomes, rerun reports) by normalized path and digest, excluding receipt
  and closure; the closure validator requires exact equality.  The writer and
  loader share one validation contract; missing, duplicated, omitted or extra
  references, bad digests, unknown schemas, conflicting identities, inventory
  changes, symlinks and path escapes all fail closed.  Existing destinations
  are never overwritten, competing writers' files are never deleted (cleanup
  is scoped to this invocation's exclusive acquisition and file identity,
  never byte equality), and only an output this call created is cleaned up.
  The closure record itself is contained before it is opened, including
  explicit-root and symlink cases.
* A loaded closure reports from an isolated validated record/receipt snapshot:
  `report()` never re-reads the package, so post-load file or record mutation
  cannot elevate a synthetic or changed claim.
* The closure report separates core-verified `content_integrity` from
  `producer_attested_closure` and from the runtime claims core cannot prove.
  A `synthetic: true` receipt is accepted only as a clearly marked fixture and
  never presented as a live producer's proof.
* Typed `OutcomeContractError` and `ClosureError`.
* CLI: `adapt-outcome`, `seal-closure`, `verify-closure`.
* Public synthetic two-branch + two-rerun example
  (`examples/formal_closure_two_branches.py`), exercised by
  `tools/check.py` from a clean installed wheel; the read-only inventory is
  proven unchanged across verification.
* `docs/outcome-closure-contract.md` records the contract authority, the
  env/rollout producer responsibilities and the old-entry exit conditions.

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
* Every tree-index node path, every node manifest file and the index file are
  validated for containment (absolute, drive/UNC, `..` and resolved symlink
  escapes) before any of them is opened; a standalone trajectory manifest and
  the evidence references a bundle declares are bounded the same way.
  `inspect_tree`, full validation, seal attribution and export all use the
  validated resolved paths. A failed full validation is never cached, so a
  retry re-runs every check.
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
  an artifact; existing destinations are never touched, and cleanup is scoped
  to an output this call exclusively created, so a competing writer's file is
  never deleted.
* Repeated `validate_tree`/`load_tree(...).verify()` and
  `validate_trajectory`/`load_trajectory(...).verify()` calls reuse the already
  derived result instead of re-reading the private evidence.
* `python tools/check.py` runs lint, format check, public-API typing, tests,
  sdist/wheel build and a clean-venv outside-repository CLI check.
* Legacy readers and game-state semantics are isolated under
  `trajectory_core.legacy_lvz`; see `NOTICES.md` and `COMPATIBILITY.md`.

Not included in this version: Agent/rollout/env integration, live replay,
dataset or registry services.
