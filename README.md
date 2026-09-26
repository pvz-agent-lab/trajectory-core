# trajectory-core

Offline trajectory and evidence-tree contracts, readers and validation for the
`lvz.*` evidence formats. The package reads and re-derives existing sealed
evidence; it never launches a game, never opens a socket and has no runtime
dependencies beyond the Python standard library.

It is the first deliverable of
[pvz-agent-lab/trajectory-core#1](https://github.com/pvz-agent-lab/trajectory-core/issues/1)
under the migration accepted by
[guajun/llm-vs-zombies#104](https://github.com/guajun/llm-vs-zombies/issues/104).

## Install

```console
python -m pip install trajectory-core        # from a built wheel / index
python -m pip install -e ".[dev]"            # editable checkout with dev tools
```

Supported: Python 3.11, 3.12, 3.13 (and newer CPython, standard library only).
Licence: **GPL-3.0-only**, inherited from the source repository; see
[LICENSE](LICENSE) and [NOTICES.md](NOTICES.md).

## Read a sealed trajectory

```python
import trajectory_core as tc

trajectory = tc.load_trajectory("runs/issue99-control-a")   # full verification
summary = trajectory.summary()
print(summary.trajectory_id, summary.steps, summary.end_boundary)
print(summary.branch_id, summary.parent)                    # tree placement, if any
```

`load_trajectory` re-derives, from the bytes on disk:

* the manifest content identity (`trajectory_id`) with the old canonical JSON
  rules;
* every evidence file SHA-256 recorded in the manifest;
* the sealed native audit stream (events, checksums, state deltas, codec
  receipt, optional raw evidence) and the session trace;
* the recorded steps against both sources, including action outcomes and
  stop reasons.

`summary()` is a projection of that verified object: identity, source, steps,
initial and end boundaries, tree placement, parent reference, per-step stop
reasons, failed actions and evidence digests. It never replaces verification.

## Read a packaged tree

```python
tree = tc.load_tree("experiments/trees/issue99-fc2-shovel-fork")  # full chains
report = tree.validate()             # root identity + every parent-to-child chain
print(report["tree_id"], report["branches"], report["trunk_nodes"])

index_only = tc.inspect_tree("...") # explicit partial mode: verification == "index_only"
```

A packaged tree adds exactly one manifest section (`tree`); each recording keeps
its own content identity. Validation re-derives the root identity and every
chain from the root down, checks branch scope (a `branch_id` cannot fork inside
one tree, only the root may omit a parent) and rejects a node that claims the
real continuation baseline off the trunk. A child may depart from any boundary
its parent actually reached, including action revisions and per-tick versions
inside a multi-tick request. The whole tree index is validated for containment
and uniqueness before a node manifest is opened; the node manifest files (and
the index file itself), a standalone trajectory manifest and the evidence a
bundle declares are bounded the same way, so an escaped file — not just an
escaped directory — is a `PathContractError` on every host before any byte is
read.

## Verify a seal

```python
report = tc.verify_seal("work/issue99-fc2-seal.json", source_root="F:/old-checkout")
assert report["matches"]
```

The seal's relative, possibly backslash-separated references are interpreted
against the explicit `source_root`; nothing in the library hard-codes a private
path. The verifier recomputes the tree identity, every node `manifest_sha256`
and every report digest, and (by default) fully re-reads each node. Use
`verify_nodes=False` for a `"structure_only"` report. Declarations are checked
before anything is compared: duplicate node keys, duplicate report references
(including normalized aliases such as `a\b.json`, `a/b.json` and `./a/b.json`),
non-object entries and entries missing required fields raise `SealError`
instead of being silently overwritten or dropped. An empty `reports` list is
valid: the seal may bind the tree without binding a report.

## Outcome contract and formal closure

```python
import trajectory_core as tc

# Adapt a pre-migration plan/case pair without changing its historical unit.
outcome = tc.outcome_from_legacy(plan_json, case_json, source="runs/issue97", expected_scene=3)
print(outcome.plan_rounds, outcome.cycle_completed, outcome.goal_reached)
print(outcome.run_status, outcome.execution_extent)   # lifecycle and extent are separate axes

# Bind a complete experimental family: tree + outcomes + rerun reports +
# producer receipt + the immutable input inventory.
closure = tc.seal_formal_closure(
    "stage", "stage/closure.json",
    tree="tree",
    outcomes={"root": "outcomes/root.json", "branch": "outcomes/branch.json"},
    pairs=[{"id": "branch-rerun", "baseline": "root", "rerun": "branch", "report": "reports/branch.json"}],
    receipt="receipt.json",
)
report = tc.load_formal_closure("stage/closure.json").report()
print(report["profile"])                              # trajectory-core.controlled-family.v1
print(report["content_integrity"]["status"])          # verified: re-derived from bytes
print(report["producer_attested_closure"]["synthetic"])  # true for a fixture receipt
```

The outcome schema keeps the plan unit, the measured progress, one-cycle
completion, target completion, run status, execution extent (not executed /
partial / complete / unknown), termination/truncation and verification
separate; unknown facts stay unknown, a completed invocation with an unmet
target is never success, and a complete cycle is never read as goal
completion.  The formal closure is a *producer protocol*, not a process check:
the producer supplies the receipt (a `synthetic: true` receipt is a fixture,
never runtime proof), the receipt must bind the complete producer-owned
artifact inventory by path and digest, and the report separates what core
proved from bytes, what the producer attested, and what core cannot prove.
A loaded closure reports from an isolated validated snapshot: later file or
record mutation cannot change a reported decision.  The closure v2 profile is
the named `trajectory-core.controlled-family.v1` shape (every node one outcome,
one baseline/rerun pair role); the generic tree/trajectory APIs place no such
restriction on larger or unpaired trees.

A self-contained synthetic demo (two branches, two rerun reports, formal
sealing and read-only verification) lives in
[`examples/formal_closure_two_branches.py`](examples/formal_closure_two_branches.py):

```console
python examples/formal_closure_two_branches.py WORKSPACE --support tests
```

See [docs/outcome-closure-contract.md](docs/outcome-closure-contract.md) for the
full contract, the responsibility split and the old-entry exit conditions.

## CLI

```console
trajectory-core summary PATH [--tree]       # verify then print the summary as JSON
trajectory-core validate-trajectory PATH
trajectory-core validate-tree PATH
trajectory-core inspect-tree PATH           # index-only projection
trajectory-core verify-seal SEAL --source-root DIR [--structure-only]
trajectory-core export TREE OUTPUT.zip      # deterministic ZIP, refuses to overwrite
trajectory-core adapt-outcome PLAN CASE [--expected-scene N]
trajectory-core seal-closure ROOT DEST --tree PATH --receipt PATH \
    --outcome KEY=PATH --pair ID BASELINE RERUN REPORT
    # writes a formal closure; never overwrites
    # (repeat --outcome once per node and --pair once per baseline/rerun pair)
trajectory-core verify-closure CLOSURE [--root DIR]
```

`package_tree` and `export_tree` apply the same full public validation as the
readers (including parent departure boundaries and the path contract) before a
tree is returned or a ZIP is created. Existing destinations are never touched,
and an output created by the failing call is removed when validation or writing
fails.

Exit codes: `0` valid, `1` invalid evidence, `2` usage error, `3` unsupported
schema/capability.

## Error semantics and verification levels

`TrajectoryCoreError` is the base; `EvidenceError`,
`UnsupportedSchemaError`, `IncompleteEvidenceError`, `PathContractError` and
`SealError` separate wrong bytes, unknown formats, missing parts, unsafe paths
and seal mismatches. Full, partial and unsupported outcomes are never mixed:
see [COMPATIBILITY.md](COMPATIBILITY.md) for the complete mapping, the old-to-new
API table and the deliberately unsupported capabilities (live replay,
env/rollout, dataset services).

## Development checks

One command runs everything:

```console
python tools/check.py
```

* `ruff check` and `ruff format --check`;
* `mypy` over the public package (the vendored legacy closure keeps its
  historical dynamic style; see `pyproject.toml` and `NOTICES.md`);
* the test suite (`pytest`) with the pure-Python hash backend forced;
* `python -m build` sdist + wheel;
* a fresh `venv` install of the wheel with `--no-deps`, `pip check`, fixture
  creation and CLI execution from a temporary directory outside the repository.

The tests build all fixtures themselves from a synthetic, socket-free recorder
(`tests/support/synthetic.py`); no game, save file or toolchain is required.
Negative cases cover tampering, missing files, unknown schemas, broken chains,
duplicate identities, cross-root links, wrong parent boundaries, unsealed
recordings, truncation, path traversal and read-only source inventories.
Outcome and closure cases additionally cover boolean-as-count rejection,
contradictory cycle/goal declarations, execution-extent contradictions,
receipt artifact-scope mismatches, re-sealing with a stale receipt,
post-load report snapshot isolation, output-ownership races (including a
same-payload competing writer), closure-file containment before any read, and
failure cleanup.

## Layout

```text
src/trajectory_core/          public API, CLI, path contract, outcome and closure contracts
src/trajectory_core/legacy_lvz/
                              adapted old readers and validators (see NOTICES.md)
tests/                        synthetic fixtures, behavior and negative tests
examples/                     installed-package formal-closure demo
docs/                         the outcome/closure contract and responsibility split
tools/check.py                the single required check entry point
```

Old format identity is preserved by construction: the hash and validation
formulas are not re-implemented, they are the same code from the source commit,
adapted only where the old module imported the runtime client.
