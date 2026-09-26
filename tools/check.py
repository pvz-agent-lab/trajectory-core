"""Single required check entry point for trajectory-core.

    python tools/check.py

Runs, in order:

1. ruff lint and format check;
2. mypy over the public package (legacy closure relaxed, see ``pyproject.toml``);
3. the test suite with the pure-Python hash backend forced;
4. sdist + wheel build into ``dist/``;
5. a fresh ``venv`` install of the wheel with ``--no-deps`` and ``pip check``;
6. fixture creation and CLI execution from a temporary directory outside the
   repository, using only the installed package;
7. the public two-branch + two-rerun formal-closure example and the installed
   ``verify-closure`` CLI, again from outside the repository.

The last three steps are what make the build claim real: nothing from the
source tree is importable from the clean environment.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def run(command: list[str | Path], *, cwd: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(f"check failed ({result.returncode}): {printable}")
    return result


def clean_env(**extra: str) -> dict:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["TRAJECTORY_CORE_FNV"] = "python"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def fixture_script() -> str:
    return (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from support.synthetic import record_session\n"
        "from trajectory_core import build_trajectory\n"
        "root = Path(sys.argv[1])\n"
        "trace = record_session(root / 'source')\n"
        "bundle = build_trajectory(trace, root / 'source' / 'audit', root / 'bundle')\n"
        "print(bundle.trajectory_id)\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="stop after the test suite")
    args = parser.parse_args(argv)

    run([sys.executable, "-m", "ruff", "check", "src", "tests", "tools", "examples"])
    run([sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "tools", "examples"])
    run([sys.executable, "-m", "mypy"])
    run([sys.executable, "-X", "dev", "-m", "pytest", "tests"], env=clean_env())
    print("static checks, types and tests passed", flush=True)
    if args.skip_build:
        return 0

    if DIST.exists():
        shutil.rmtree(DIST)
    run([sys.executable, "-m", "build", "--outdir", DIST])
    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(f"expected exactly one wheel and one sdist in {DIST}")

    with tempfile.TemporaryDirectory(prefix="trajectory-core-check-") as temporary:
        workspace = Path(temporary)
        environment = workspace / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "Scripts" / "python.exe" if os.name == "nt" else environment / "bin" / "python"
        run([python, "-m", "pip", "install", "--quiet", "--no-deps", wheels[0]], cwd=workspace, env=clean_env())
        run([python, "-m", "pip", "check"], cwd=workspace, env=clean_env())

        script = workspace / "make_fixture.py"
        script.write_text(fixture_script(), encoding="utf-8", newline="\n")
        created = run([python, script, workspace, ROOT / "tests"], cwd=workspace, env=clean_env())
        trajectory_id = created.stdout.strip().splitlines()[-1]

        bin_directory = "Scripts" if os.name == "nt" else "bin"
        executable_name = "trajectory-core.exe" if os.name == "nt" else "trajectory-core"
        executable = environment / bin_directory / executable_name
        summary = run([executable, "summary", workspace / "bundle"], cwd=workspace, env=clean_env())
        if trajectory_id not in summary.stdout:
            raise SystemExit("installed CLI summary does not match the built trajectory")
        version = run([executable, "--version"], cwd=workspace, env=clean_env())
        print(version.stdout.strip())

        example = ROOT / "examples" / "formal_closure_two_branches.py"
        demo = run(
            [python, example, workspace / "closure-demo", "--support", ROOT / "tests"],
            cwd=workspace,
            env=clean_env(),
        )
        for expected in (
            "content_integrity=verified",
            "producer_attested=False",
            "synthetic_receipt=True",
            "read_only_inventory_stable=True",
        ):
            if expected not in demo.stdout:
                raise SystemExit(f"formal-closure example output is missing {expected!r}")
        closure = run(
            [executable, "verify-closure", workspace / "closure-demo" / "stage" / "closure.json"],
            cwd=workspace,
            env=clean_env(),
        )
        if '"status": "valid"' not in closure.stdout:
            raise SystemExit("installed CLI did not validate the example formal closure")
        print("clean-venv wheel install and outside-repository CLI checks passed", flush=True)
        print("installed formal-closure example and verify-closure CLI passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
