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

## CLI

```console
trajectory-core summary PATH [--tree]       # verify then print the summary as JSON
trajectory-core validate-trajectory PATH
trajectory-core validate-tree PATH
trajectory-core inspect-tree PATH           # index-only projection
trajectory-core verify-seal SEAL --source-root DIR [--structure-only]
trajectory-core export TREE OUTPUT.zip      # deterministic ZIP, refuses to overwrite
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

## Layout

```text
src/trajectory_core/          public API, CLI, path contract, seal verification
src/trajectory_core/legacy_lvz/
                              adapted old readers and validators (see NOTICES.md)
tests/                        synthetic fixtures, behavior and negative tests
tools/check.py                the single required check entry point
```

Old format identity is preserved by construction: the hash and validation
formulas are not re-implemented, they are the same code from the source commit,
adapted only where the old module imported the runtime client.
