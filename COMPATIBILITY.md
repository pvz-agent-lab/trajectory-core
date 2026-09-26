# Compatibility and migration

`trajectory-core` reads the evidence formats of the old repository
`guajun/llm-vs-zombies` without changing their bytes, identities or hashes.
This document is the contract for what is accepted, what is rejected and how
the old API maps to the new one.

## Supported formats

| Schema | Meaning | Reader |
|---|---|---|
| `lvz.engine-replay.v1` | one sealed trajectory bundle (`trajectory.json` + evidence) | `load_trajectory` / `validate_trajectory` |
| `lvz.evidence-tree.v1` | a packaged tree index (`tree.json` + `nodes/`) | `load_tree` / `validate_tree` / `inspect_tree` |
| `lvz.audit.v1` | the native audit stream inside a bundle (events, checksums, state deltas, optional raw evidence) | full trajectory load |
| `lvz.evidence-codec.v1` | the gzip container receipt for audit JSONL | full trajectory load |
| `lvz.branch-scope.v1` | a branch scope declaration in the initial marker | full trajectory load |
| `lvz.reanimation-links.v1` | comparable animation state inside audit state | full trajectory load |
| `lvz.lifecycle-*` | optional lifecycle recording contract | full trajectory load |
| `lvz.issue99-shovel-fork-seal.v1` | a seal record for one tree plus reports | `verify_seal` |
| `trajectory-core.outcome.v1` | a versioned run outcome (target unit, progress, cycle, goal, run/verification status) | `read_outcome` / `validate_outcome` / `outcome_from_legacy` |
| `trajectory-core.formal-closure.v1` | the formal producer closure over a tree, outcomes, rerun pairs, receipt and inventory | `load_formal_closure` / `validate_formal_closure` / `seal_formal_closure` |
| `trajectory-core.producer-closure-receipt.v1` | the producer's own `no_active_writers` declaration and its scope | `read_producer_receipt` |
| `trajectory-core.rerun-report.v1` | one baseline/rerun comparison bound to two tree nodes | `read_rerun_report` |

Unknown schemas **fail closed** with `UnsupportedSchemaError`. A manifest is
never upgraded in place: `content_identity` covers every field except
`trajectory_id` and `tree`, so adding or removing a tree placement cannot change
what a recording *is*, and a rewritten manifest can only be re-identified, never
silently accepted as the old identity.

## Error semantics

| Exception | Raised when |
|---|---|
| `TrajectoryCoreError` | base class of everything this package raises itself |
| `EvidenceError` | bytes are valid JSON but the evidence is wrong (hash chain, digest, schema content) |
| `UnsupportedSchemaError` | the declared schema/version is not one of the formats above |
| `IncompleteEvidenceError` | a complete artifact is missing a file, a manifest or a close |
| `PathContractError` | a reference is absolute, traversing, drive/UNC-prefixed or a symlink escape; the grammar is host-neutral, so backslashes are read as separators on every OS |
| `SealError` | a seal record or `source_root` is malformed: wrong container type, duplicate node/report declarations, missing required fields or unreadable output state |
| `OutcomeContractError` | a versioned outcome document is malformed, self-contradictory or turns an unknown fact into a success claim |
| `ClosureError` | a formal closure, its receipt, its rerun reports or the package inventory are missing, duplicated, conflicting or changed |
| `UnsupportedCapabilityError` | a capability that is deliberately not part of this package (never a silent success) |

A *partial read* is always explicit: `inspect_tree()` reports
`verification: "index_only"` and `verify_seal(..., verify_nodes=False)` reports
`verification: "structure_only"`. Only a full `load_trajectory` /
`validate_tree` / `verify_seal` report says `full`.

## Old API to new API

| old (`llm_vs_zombies`) | new (`trajectory_core`) | note |
|---|---|---|
| `engine_replay.Trajectory.load(path)` | `load_trajectory(path)` | returns the typed facade; full verification is identical |
| `engine_replay.build_trajectory(...)` | `build_trajectory(...)` | same guardrails: closed source, output must not exist |
| `evidence_tree.EvidenceTree.load(path).validate()` | `load_tree(path)` / `validate_tree(path)` | same full re-derivation |
| `evidence_tree.package_tree` / `export_tree` | `package_tree` / `export_tree` | same writers plus the public reader's full validation; neither overwrites an existing destination |
| `evidence_tree.root_placement` / `branch_placement` / `attach_tree` | same names | `TreePlacement` is the same object |
| `audit_compare.AuditLog` | `trajectory_core.legacy_lvz.audit_compare.AuditLog` | advanced/legacy use only |
| old `lvz.evaluation-plan.v1/v2` + case report | `outcome_from_legacy(plan, case, ...)` | explicit adapter: keeps the historical round unit, marks the result `legacy-adapted`/`unverified`, never raises a legacy result to `verified` |
| old report digests / rerun comparison | `read_rerun_report` + a formal closure | the producer writes the report; core binds its identities and bytes |
| `root_integrity.check_state` / `check_root` | `trajectory_core.legacy_lvz.root_integrity` | game-state reference semantics, intentionally outside the public schema |
| `evidence_codec.EvidenceStore` | `trajectory_core.legacy_lvz.evidence_codec` | used by the full load; no separate public API yet |

## Legacy path adaptation

Old seal records written on Windows store relative paths with `\` separators
(for example `work\issue99-fc2-report.json`). `verify_seal(seal, source_root=...)`
resolves those references against the explicit `source_root`; the same
host-neutral grammar applies to every reference in the package, and absolute
paths, drive/UNC prefixes and `..` traversal are always rejected. The record
itself is never rewritten. Evidence references inside a bundle, the bundle's
manifest file and its audit directory are always resolved relative to that
bundle and are checked for containment before they are read.

## Seal and tree-index validation

Tree-index node paths, node manifest files, the tree index file and seal
references are validated before the first node manifest is opened. A path that
is absolute, drive/UNC-prefixed, traversing or a resolved symlink escape is
rejected (the same grammar on POSIX and Windows), so `inspect_tree`,
`validate_tree`, `verify_seal` and `export_tree` never read bytes outside the
package root. A full tree load derives every parent-to-child chain once; a
repeated `validate()`/`verify()` call reuses that result instead of re-reading
the bundles, and a failed full validation is not cached as a success.

A `lvz.issue99-shovel-fork-seal.v1` record is validated as a structure before
any comparison: non-object `nodes`/`reports` entries, duplicate node keys,
duplicate report references (after normalizing `\`, `/` and `./` aliases, and
after resolving symlinks), and entries missing `key`, `branch_id`, `trunk`,
`parent_key`, `trajectory_id`, `manifest_sha256`, `path` or `sha256` raise
`SealError`. An empty `reports` list is valid; a missing `reports` field is read
as an empty list for compatibility. Mismatches between a well-formed record and
the artifacts stay in the report's `problems` list.

`package_tree` and `export_tree` run the same full validation as the readers
before returning a tree or creating a ZIP: a child boundary the parent never
reached is rejected by both writers, an existing destination is never touched,
and only a file this call exclusively created is removed when writing fails
(a competing writer's file is left untouched).

## Formal closure versus the legacy seal

`verify_seal()` reads the legacy `lvz.issue99-shovel-fork-seal.v1` record with
an explicit `source_root` and never rewrites it.  The formal closure
(`trajectory-core.formal-closure.v1`) is a different, stricter entry: it binds
the tree, every node, one outcome per node, one baseline/rerun pair report per
node, a producer receipt and the complete package inventory, and its loader
and writer share one validation function.  A legacy seal is **never** accepted
as a formal closure (and vice versa: `UnsupportedSchemaError`), and no formal
status is ever inferred from an old record.  A formal closure's report
separates core-verified content integrity from the producer's attestation and
names the runtime claims core cannot prove; see
[docs/outcome-closure-contract.md](docs/outcome-closure-contract.md).

## Deliberately unsupported

* No live replay, process launch, socket transport or game process — this is an
  offline reader only.
* No process supervision, writer locks or "is the game still running" checks:
  the formal closure only validates the producer's receipt and records the
  runtime claims as unverifiable offline.
* No environment/rollout/Agent/LLM code; those are separate packages.
* No dataset registry, version publishing, sample selection, deduplication,
  splits, reward or training sampling.
* No silent conversion of the issue #99 diagnostic fork into a formal training
  tree; `package_tree` packages already-placed bundles and preserves each
  recording's own identity.
