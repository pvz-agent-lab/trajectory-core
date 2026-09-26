# Outcome and formal-closure contracts

This document is the authority for the two versioned contracts added for
[guajun/llm-vs-zombies#104](https://github.com/guajun/llm-vs-zombies/issues/104)
and the completion semantics recorded in
[#97](https://github.com/guajun/llm-vs-zombies/issues/97):

* `trajectory-core.outcome.v2` — the public run-outcome contract;
* `trajectory-core.formal-closure.v2` — the formal producer-closure protocol
  for the `trajectory-core.controlled-family.v1` profile, with the
  `trajectory-core.producer-closure-receipt.v2` and
  `trajectory-core.rerun-report.v1` documents it binds.

The `v1` drafts of the outcome, closure and receipt documents were developed
inside the same pre-release pull request and were never published; they are
rejected as unknown schemas, never upgraded silently.  The schema names below
are the first release shapes.

`trajectory-core` is the **contract authority**: it defines the schemas,
validates them, and reads/writes the records.  It is not a process supervisor.
It never launches a game, observes a runtime, takes a lock or proves that a
writer stopped.  Those facts remain the producer's responsibility and are
carried across the boundary as explicit, reviewable declarations.

## 1. Outcome contract (`trajectory-core.outcome.v2`)

One document separates eight questions that the legacy evaluation reports
merged:

| Field | Meaning |
|---|---|
| `plan.unit` | the unit of the declared target; `"round"` is the only unit defined in v2 (`null` = unknown) |
| `plan.rounds` | the declared target in rounds (`null` = unknown); requires `unit == "round"` |
| `progress.rounds_completed` | actually completed rounds (`null` = unknown) |
| `progress.maximum_wave` / `progress.final_scene` / `progress.expected_scene` | the other facts the cycle/goal conditions need |
| `cycle.completed` | whether **one full cycle** completed (the historical `full_cycle` gate) |
| `goal.reached` | whether the **declared target** was reached |
| `run.status` | `completed` / `failed` / `aborted` / `unknown` |
| `run.execution_extent` | how much of the invocation actually executed: `not_executed` / `partial` / `complete` / `unknown` |
| `run.termination_reason` / `run.truncation_reason` | why the run ended as declared / why it was cut short |
| `verification.status` | `verified` / `unverified` / `failed` / `unknown` and the declared scope |
| `provenance` | `kind` (`native` / `legacy-adapted` / `synthetic-demo`), source, input schema, evidence scope, adaptations |

Strict validation rules (all failures are `OutcomeContractError` or
`UnsupportedSchemaError`):

* unknown schemas, fields and units fail closed;
* counts are real integers or `null`; a boolean is never accepted as a count;
* missing facts stay `null`.  `cycle.completed` and `goal.reached` are derived
  from the known facts and may only be declared when the derivation agrees;
  a declaration of `true` while a required fact is unknown is rejected;
* a contradictory declaration (for example `cycle.completed = true` with zero
  completed rounds, or `goal.reached = true` after one of two rounds) is
  rejected;
* **`cycle.completed` is never evidence of `goal.reached`.**  The historical
  gate ("at least one round, wave ≥ 20, the declared scene") and the target
  ("completed rounds ≥ declared rounds, with the same cycle conditions")
  are computed and validated separately;
* **the lifecycle status is never evidence of goal success.**  A completed
  invocation can report `goal.reached = false`, and `run.execution_extent`
  is an independent axis: `complete` says the invocation ran to its configured
  end, not that the target was reached;
* `run.execution_extent = "not_executed"` contradicts an actual advancement
  delta (`progress.rounds_completed > 0`), and `run.execution_extent =
  "complete"` contradicts a recorded truncation reason; an unproven extent
  stays `"unknown"` — a `0` completed-round count cannot distinguish a
  failure before execution from a partial first round.  A positive
  `progress.maximum_wave` (or `final_scene`) is not advancement: the
  historical runner initializes `maximum_wave` from the loaded initial
  observation, so a saved state can already be on wave 1 or later, and a
  truncation reason alone proves nothing because a budget can stop before the
  first action;
* `synthetic-demo` and `legacy-adapted` documents cannot claim
  `verification.status = "verified"`.

### Legacy adaptation

`outcome_from_legacy(plan, case, ...)` adapts a pre-migration
`lvz.evaluation-plan.v1/v2` document plus a case report.  It always produces a
`legacy-adapted`, `unverified` document and records the adaptations.  In
particular:

* `flags_to_complete` is the pre-#97 **name** of `rounds_to_complete`; it
  always counted rounds (one round = two flags = twenty waves) and is read as
  rounds, never divided or rescaled;
* an absent target keeps the historical default of one round;
* declaring both the alias and `rounds_to_complete` is rejected — the old
  reader rejected the mix, and a unit change is never silent;
* `full_cycle` is reported as `cycle.completed` only;
* `tick_budget_exhausted` / `wall_budget_exhausted` / `disk_reserve_stop`
  become `run.truncation_reason`; other outcomes become
  `run.termination_reason`;
* `run.execution_extent` describes the **source execution** and is derived
  from proven source-stage facts unless the caller declares it.  It never
  follows the case/suite lifecycle: the historical suite marks a case
  `failed` after a later cold replay or retention failure even though the
  source play loop already completed, and marks it `incomplete` for a strict
  suite while the source outcome carries the real end.  `startup_failed`
  (the source run never existed) proves `not_executed`; the source play
  loop's own `outcome: full_cycle_completed` (its configured end) proves
  `complete`; a positive `rounds_completed` delta proves only that
  execution started, so it becomes `partial` only together with a documented
  explicit premature-stop/truncation source outcome (`terminal_before_complete`
  or one of `tick_budget_exhausted` / `wall_budget_exhausted` /
  `disk_reserve_stop`).  A positive delta with an absent, unknown or
  arbitrary outcome stays `unknown`: a complete source with a missing reason
  is possible, and an arbitrary non-null string is not proof.  A positive
  absolute `maximum_wave`, a recorded truncation reason alone, or any other
  fact likewise leaves the extent `unknown`.  Callers that know more (for
  example a producer that observed a failure inside the first round) pass
  `execution_extent="partial"` explicitly.

## 2. Formal producer closure (`trajectory-core.formal-closure.v2`)

A formal closure binds, exactly and completely:

```text
trajectory-core.formal-closure.v2   profile: trajectory-core.controlled-family.v1
├── tree            tree path, tree_id and one binding per node
│                   (key, branch_id, trunk, parent_key, trajectory_id, manifest_sha256)
├── outcomes        one trajectory-core.outcome.v2 document per node
├── pairs           one rerun report per baseline/rerun pair; every node appears
│                   in exactly one pair and one role
├── producer_receipt  the producer's declaration, as a separate file
└── inputs          the complete SHA-256 inventory of the package
```

`seal_formal_closure()` builds the record from the bytes on disk, validates it
with the same `validate_closure_record()` the loader uses, then writes it with
an exclusive create.  `load_formal_closure()` / `validate_formal_closure()`
re-derive everything.  Missing, duplicated, omitted or extra references, bad
digests, unknown schemas, conflicting identities and any inventory change are
`ClosureError`s; an unknown closure schema or profile is an
`UnsupportedSchemaError`.

### The controlled-family v1 profile

The closure profile is named and versioned: `trajectory-core.controlled-family.v1`.
It deliberately restricts the *closure* shape for this narrow experiment
family:

* every tree node carries exactly one outcome document;
* every tree node belongs to exactly one baseline/rerun pair, in exactly one
  role.  A node cannot be both a baseline and a rerun, and cannot appear in two
  pairs.  All nodes must be covered.

This is a decision, not an unresolved upstream question.  It is **not** a
restriction on the generic tree/trajectory readers: `load_tree`,
`validate_tree`, `inspect_tree`, `package_tree`, `export_tree`,
`load_trajectory` and the other trajectory APIs accept larger trees and
unpaired nodes with no pairing requirement at all.  A family that needs
single-arm closures or multi-pair designs needs a new profile version (and a
review of what the receipt must attest), not a change to the generic core.

### Producer receipt: who attests what

The producer (env/rollout) must confirm, outside this package, that no writer
is active, and must say so in a receipt that the caller supplies:

```json
{
  "schema": "trajectory-core.producer-closure-receipt.v2",
  "producer": {"name": "...", "version": "..."},
  "synthetic": false,
  "attestation": "no_active_writers",
  "statement": "human-readable claim",
  "method": "how the producer confirmed it",
  "scope": {
    "tree_id": "...",
    "nodes": [{"key": "...", "trajectory_id": "...", "manifest_sha256": "..."}],
    "artifacts": [
      {"path": "tree/tree.json", "sha256": "..."},
      {"path": "outcomes/root.json", "sha256": "..."},
      {"path": "reports/control-rerun.json", "sha256": "..."}
    ]
  }
}
```

* The receipt is the producer's declaration of **which artifacts it closes**.
  `scope.artifacts` must bind the complete producer-owned inventory — tree
  index and node evidence, outcome documents and rerun reports — by normalized
  relative path and SHA-256.  The receipt itself and the generated closure
  record are excluded from that scope (a file cannot bind its own digest), and
  they can never be listed: the closure validator computes the producer-owned
  inventory as the full package inventory minus the receipt, minus the closure
  file, and requires **exact path and digest equality**.  A missing, extra,
  reused (aliased or duplicated), traversal-bearing or digest-changed entry
  fails the closure.
* `synthetic` is required and must be an explicit boolean.  There is no
  default and no blanket `attested: true` parameter: core never fabricates a
  receipt and never claims a producer fact on its own.
* A receipt whose tree/node `scope` does not match the bound tree nodes
  exactly is rejected.
* A `synthetic: true` receipt (fixtures, demos, tests) still produces a valid
  closure, but its report says `attested: false`, `synthetic: true` and lists
  the runtime claims as unverifiable.  A fixture can never be read as a live
  producer's proof.

Because the receipt binds the outcome/report/tree bytes, editing an outcome
(and recomputing `outcome_id`) or a rerun report after the receipt was written
no longer closes: re-sealing with the stale receipt is rejected until the
producer regenerates the receipt over the new inventory.

### What the closure report distinguishes

`FormalClosure.report()` separates three epistemic levels:

* **`content_integrity`** — what the package re-derived from the bytes:
  tree identity and chains, every node manifest digest, every outcome
  identity, every rerun report binding, the receipt tree/node scope, the
  receipt artifact scope and the complete input inventory
  (`status: "verified"`, `runtime_facts_verified_by_core: false`);
* **`producer_attested_closure`** — the receipt's claim and whether it is
  synthetic; core only checks the schema, the scopes and the equality of the
  declared bytes, never the runtime fact;
* **`unverifiable_runtime_claims`** — the statements core cannot independently
  prove, always including "there was no active writer".

A loaded closure keeps an isolated snapshot of the validated record and
receipt.  `report()` never re-reads the package: mutating `receipt.json`,
outcome bytes or any other file after a successful load, or mutating the
dictionary returned by `record`, cannot change the reported decisions or turn
a synthetic receipt into `attested: true`.  The report is the snapshot that
was actually verified; if the package was never loaded, reporting fails
closed.

### Immutability, ownership and containment

The sealing package must be an immutable input while it is sealed:

* every reference is relative and is resolved under the package root after
  symlink resolution; traversal, drive/UNC prefixes, absolute paths and
  symlinks inside the package are rejected;
* the closure record file itself is bounded **before** `read_json`: an
  explicit root must contain it after symlink resolution, a symlinked closure
  record (even one that stays inside the package) is rejected, and no byte of
  a record outside the declared package is ever opened;
* the destination is refused when it exists and is created exclusively, so a
  competing writer is never overwritten;
* cleanup is scoped to this invocation's exclusive acquisition: only after
  this call acquired the destination with an exclusive create is a failed
  output considered for removal, and it is re-checked against the file
  identity captured at acquisition — never against byte equality with a
  competing writer's file.  A competing writer that created the same
  deterministic payload first is left untouched;
* the complete inventory is hashed before validation and re-checked after the
  write; a change before or during sealing aborts the seal.

## 3. Responsibility split

| Concern | Owner |
|---|---|
| Schema, validation, binding, read-only verification, deterministic inventory | `trajectory-core` (this package) |
| Recording runtime evidence; writing outcome documents; confirming its writers stopped; producing the receipt with the complete artifact inventory | env / rollout adapters |
| Adapting old evaluation plans/cases without changing historical units | the caller via `outcome_from_legacy` (adapter stays in this package) |
| Live replay, game process detection, locks, dataset/reward/training services | explicitly out of scope; never simulated here |

## 4. Old-entry exit conditions

* `verify_seal()` keeps reading `lvz.issue99-shovel-fork-seal.v1` records
  unchanged.  A legacy seal is never upgraded into a formal closure, and a
  formal closure is never read through the legacy seal entry
  (`UnsupportedSchemaError`).
* `outcome_from_legacy()` remains the only accepted path for old
  `lvz.evaluation-plan.v1/v2` and case reports; it marks its output
  `legacy-adapted` and `unverified`.
* The legacy entries may be retired when (a) env/rollout publish formal
  closures for all active experiments, (b) no consumer still reads the old
  report shapes, and (c) the old records are retained read-only for
  provenance.  Until then both entries stay; deleting the old readers is a
  separate, explicitly reviewed change.
