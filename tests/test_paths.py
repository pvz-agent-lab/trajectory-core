"""Path contract: explicit roots, no traversal, no cwd dependence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trajectory_core._paths import iter_files, normalize_reference, resolve_under
from trajectory_core.errors import PathContractError


@pytest.mark.parametrize(
    "reference", ["/etc/passwd", "C:/Windows/system32", "\\\\server\\share\\x", "a/../../b", "..", ".", "", "   "]
)
def test_unsafe_references_are_rejected(reference: str) -> None:
    if reference == "C:/Windows/system32":
        with pytest.raises(PathContractError):
            normalize_reference(reference, legacy_windows=True)
    with pytest.raises(PathContractError):
        normalize_reference(reference, legacy_windows=True)


def test_legacy_windows_separators_normalize_without_host_rules() -> None:
    assert normalize_reference("work\\issue99-fc2-report.json", legacy_windows=True) == "work/issue99-fc2-report.json"
    with pytest.raises(PathContractError):
        normalize_reference("work\\..\\..\\escape.json", legacy_windows=True)


def test_resolve_under_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "inside").mkdir(parents=True)
    outside.mkdir()
    (root / "inside" / "file.json").write_text("{}\n", encoding="utf-8")
    assert resolve_under(root, "inside/file.json") == (root / "inside" / "file.json")
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this account")
    with pytest.raises(PathContractError):
        resolve_under(root, "link/escaped.json")


def test_iter_files_is_deterministic_and_read_only(tmp_path: Path) -> None:
    for name in ("b.json", "a.json"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    assert [path.name for path in iter_files(tmp_path)] == ["a.json", "b.json"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.json", "b.json"]
