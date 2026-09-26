"""Static and dynamic import-boundary checks for the offline core.

The package must not import a game runtime, a socket client, a launcher, an
environment/rollout package, an AvZ module or a Win32 binding -- not even as an
optional or dynamically-created import. Only the standard library and
``trajectory_core`` itself are allowed.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "trajectory_core"
FORBIDDEN_ROOTS = {
    "llm_vs_zombies",
    "client",
    "launcher",
    "runtime",
    "env",
    "rollout",
    "avz",
    "win32",
    "win32api",
    "win32con",
    "pywin32",
    "socket",
    "http",
    "urllib",
}
STDLIB = set(sys.stdlib_module_names) | {"__future__", "trajectory_core"}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_bytes(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_standard_library_and_package_imports() -> None:
    unexpected: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        roots = _imported_roots(path)
        bad = roots - STDLIB
        if bad:
            unexpected[str(path.relative_to(SRC))] = bad
    assert unexpected == {}, f"unexpected third-party or sibling imports: {unexpected}"


def test_forbidden_sibling_names_never_appear() -> None:
    hits: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        bad = _imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            hits[str(path.relative_to(SRC))] = bad
    assert hits == {}, f"forbidden runtime imports: {hits}"


def test_fresh_interpreter_loads_without_runtime_modules() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["TRAJECTORY_CORE_FNV"] = "python"
    script = (
        "import sys, trajectory_core\n"
        "from trajectory_core.legacy_lvz.audit_compare import hash_backend\n"
        "forbidden = {'socket', 'subprocess', 'ctypes', 'llm_vs_zombies', 'win32api'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, loaded\n"
        "assert hash_backend() == 'python_fnv1a', hash_backend()\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_pure_python_fnv_matches_native_switch() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["TRAJECTORY_CORE_FNV"] = "python"
    script = (
        "from trajectory_core.legacy_lvz.audit_compare import _python_fnv\n"
        "assert _python_fnv(b'') == 0xcbf29ce484222325\n"
        "assert _python_fnv(b'hello') == 0xa430d84680aabd0b\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
