# Notices, provenance and license

`trajectory-core` inherits **GPL-3.0-only** from the source repository
[`guajun/llm-vs-zombies`](https://github.com/guajun/llm-vs-zombies) and keeps the
verbatim `LICENSE` file. The offline reader closure is an adapted copy of that
repository at commit `11917a7` (`#111: validate and seal frozen lifecycle-probe
acceptance runs (#117)`). Nothing in this package is copied from a game
binary, a save file, a toolchain or a private recording.

## Source mapping

| trajectory-core file | source file at `11917a7` | changes |
|---|---|---|
| `src/trajectory_core/legacy_lvz/audit_compare.py` | `src/llm_vs_zombies/audit_compare.py` | `EvidenceError` moved to the public `errors` module; the optional native FNV toolchain is imported lazily, caches under the system temp directory, honours `TRAJECTORY_CORE_FNV`/`TRAJECTORY_CORE_FNV_COMPILER` and still falls back to the exact pure-Python implementation. No evidence-format change. |
| `src/trajectory_core/legacy_lvz/evidence_codec.py` | `src/llm_vs_zombies/evidence_codec.py` | unchanged |
| `src/trajectory_core/legacy_lvz/evidence_tree.py` | `src/llm_vs_zombies/evidence_tree.py` | the function-local `engine_replay` import now points at the adapted `trajectory` module (that module was split in this repository) |
| `src/trajectory_core/legacy_lvz/trajectory.py` | `src/llm_vs_zombies/engine_replay.py`, lines for `_write` through `build_trajectory` | reduced to the read-and-package closure: live `replay`, `ReplaySession`, `demo` and the CLI were removed; the socket-`Client` import is replaced by the pure `client_branch` module. Step/initial validation is byte-compatible. |
| `src/trajectory_core/legacy_lvz/client_branch.py` | `src/llm_vs_zombies/client.py`: `branch_scope_id` and `declared_branch_scope` | extracted as a standalone pure module; `ProtocolError` became `BranchScopeError` (an `EvidenceError`) |
| `src/trajectory_core/legacy_lvz/root_integrity.py` | `src/llm_vs_zombies/root_integrity.py` | unchanged (legacy game-state reference semantics, isolated from the public API) |
| `src/trajectory_core/legacy_lvz/lifecycle_events.py` | `src/llm_vs_zombies/lifecycle_events.py` | one typing-only annotation (`report: dict[str, Any]`) |
| `src/trajectory_core/legacy_lvz/{sound_effects,b0_normalization,app_update_anchor,mj_clock_anchor,sound_counter,fp_environment}.py` | same names under `src/llm_vs_zombies/` | unchanged |
| `tests/support/synthetic.py` | `tests/test_engine_replay.py` (`ModelTransport`, `GAME`/`BUILD`/`ARTIFACTS` constants) and `src/llm_vs_zombies/session.py` (`SessionTrace.__init__/emit/close`) | test-only adaptation: the transport is driven with request dictionaries instead of a socket; no game, client or network |
| `tests/test_legacy_root_integrity.py` | `tests/test_root_integrity.py` | imports adapted to this package; test bodies unchanged |
| `tests/test_codec.py` | `tests/test_evidence_codec.py` | converted to pytest; assertions unchanged |

Files deliberately **not** copied: `client.py`, `launcher.py`, `runtime/*`,
`recording.py`, `evaluation*.py`, `session.py` (except the test-only trace
writer), `repl.py`, `video.py` and every native, AvZ or Windows module. The core
package imports only the Python standard library and itself.

The adapted copies keep the original Apache-less/GPL-3.0 licensing: this
repository is a redistribution of that code under the same license, with the
original project credited above.

## Original modules in this repository

`outcome.py`, `closure.py` and the `tests/support/formal_demo.py` fixture are
original work of this repository, not adapted legacy code.  They document and
implement the completion/closure semantics agreed in
[`guajun/llm-vs-zombies#104`](https://github.com/guajun/llm-vs-zombies/issues/104)
and [`#97`](https://github.com/guajun/llm-vs-zombies/issues/97) but copy no
legacy implementation: the legacy readers remain the single source of the old
hashes and format parsing.  `tests/support/formal_demo.py` reuses the test-only
synthetic recorder described above; no game, runtime or private recording is
involved.
