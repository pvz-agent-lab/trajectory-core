# Outcome and formal-closure contracts

This document is the authority for the two versioned contracts added for
[guajun/llm-vs-zombies#104](https://github.com/guajun/llm-vs-zombies/issues/104)
and the completion semantics recorded in
[#97](https://github.com/guajun/llm-vs-zombies/issues/97):

* `trajectory-core.outcome.v1` — the public run-outcome contract;
* `trajectory-core.formal-closure.v1` — the formal producer-closure protocol,
  with the `trajectory-core.producer-closure-receipt.v1` and
  `trajectory-core.rerun-report.v1` documents it binds.

`trajectory-core` is the **contract authority**: it defines the schemas,
validates them, and reads/writes the records.  It is not a process supervisor.
It never launches a game, observes a runtime, takes a lock or proves that a
writer stopped.  Those facts remain the producer's responsibility and are
carried across the boundary as explicit, reviewable declarations.

## 1. Outcome contract (`trajectory-core.outcome.v1`)

One document separates seven questions that the legacy evaluation reports
merged:

| Field | Meaning |
|---|---|
| `plan.unit` | the unit of the declared target; `"round"` is the only unit defined in v1 (`null` = unknown) |
| `plan.rounds` | the declared target in rounds (`null` = unknown); requires `unit == "round"` |
| `progress.rounds_completed` | actually completed rounds (`null` = unknown) |
| `progress.maximum_wave` / `progress.final_scene` / `progress.expected_scene` | the other facts the cycle/goal conditions need |
| `cycle.completed` | whether **one full cycle** completed (the historical `full_cycle` gate) |
| `goal.reached` | whether the **declared target** was reached |
| `run.status` | `completed` / `failed` / `aborted` / `unknown` |
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
  `run.termination_reason`.

## 2. Formal producer closure (`trajectory-core.formal-closure.v1`)

A formal closure binds, exactly and completely:

```text
trajectory-core.formal-closure.v1
├── tree            tree path, tree_id and one binding per node
│                   (key, branch_id, trunk, parent_key, trajectory_id, manifest_sha256)
├── outcomes        one trajectory-core.outcome.v1 document per node
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
`ClosureError`s; an unknown closure schema is `UnsupportedSchemaError`.

### Producer receipt: who attests what

The producer (env/rollout) must confirm, outside this package, that no writer
is active, and must say so in a receipt that the caller supplies:

```json
{
  "schema": "trajectory-core.producer-closure-receipt.v1",
  "producer": {"name": "...", "version": "..."},
  "synthetic": false,
  "attestation": "no_active_writers",
  "statement": "human-readable claim",
  "method": "how the producer confirmed it",
  "scope": {"tree_id": "...", "nodes": [{"key": "...", "trajectory_id": "...", "manifest_sha256": "..."}]}
}
```

* `synthetic` is required and must be an explicit boolean.  There is no
  default and no blanket `attested: true` parameter: core never fabricates a
  receipt and never claims a producer fact on its own.
* A receipt whose `scope` does not match the bound tree nodes exactly is
  rejected.
* A `synthetic: true` receipt (fixtures, demos, tests) still produces a valid
  closure, but its report says `attested: false`, `synthetic: true` and lists
  the runtime claims as unverifiable.  A fixture can never be read as a live
  producer's proof.

### What the closure report distinguishes

`FormalClosure.report()` separates three epistemic levels:

* **`content_integrity`** — what the package re-derived from the bytes:
  tree identity and chains, every node manifest digest, every outcome
  identity, every rerun report binding, the receipt scope and the complete
  input inventory (`status: "verified"`);
* **`producer_attested_closure`** — the receipt's claim and whether it is
  synthetic; core only checks the schema and the scope equality;
* **`unverifiable_runtime_claims`** — the statements core cannot independently
  prove, always including "there was no active writer".

`content_integrity.runtime_facts_verified_by_core` is always `false`.

### Immutability and cleanup

The sealing package must be an immutable input while it is sealed:

* the destination is refused when it exists and is created exclusively, so a
  competing writer is never overwritten and its file is never deleted;
* the complete inventory is hashed before validation and re-checked after the
  write; a change before or during sealing aborts the seal;
* on failure only an output this call created (verified by payload digest) is
  removed;
* every reference is relative and is resolved under the package root after
  symlink resolution; traversal, drive/UNC prefixes, absolute paths and
  symlinks inside the package are rejected before any byte is read.

## 3. Responsibility split

| Concern | Owner |
|---|---|
| Schema, validation, binding, read-only verification, deterministic inventory | `trajectory-core` (this package) |
| Recording runtime evidence; writing outcome documents; confirming its writers stopped; producing the receipt | env / rollout adapters |
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
