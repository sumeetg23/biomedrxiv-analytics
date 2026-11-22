"""Unit tests for dataset versioning, deduplication, diffing, and rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from biomedrxiv.config import ProjectPaths
from biomedrxiv.versioning import DatasetVersionManager


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def test_ingest_deduplicates_identical_snapshots(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    manager = DatasetVersionManager(paths=paths)

    src = tmp_path / "kaggle_cache" / "versions" / "1"
    _write_csv(src / "papers.csv", "doi,title", ["10.1101/001,Paper A", "10.1101/002,Paper B"])

    first = manager.ingest_directory(src, notes="v1")
    assert first.status == "created"
    assert paths.current_link.is_symlink()
    assert paths.current_link.resolve() == first.snapshot_dir.resolve()
    assert (first.snapshot_dir / "manifest.json").exists()

    second = manager.ingest_directory(src, notes="recheck")
    assert second.status == "unchanged"
    assert second.version_id == first.version_id
    assert len(manager.list_versions()) == 1


def test_ingest_tracks_new_version_diff_and_rollback(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    manager = DatasetVersionManager(paths=paths)

    src_v1 = tmp_path / "src_v1"
    _write_csv(src_v1 / "papers.csv", "doi,title", ["10.1101/001,Paper A"])
    r1 = manager.ingest_directory(src_v1)

    src_v2 = tmp_path / "src_v2"
    _write_csv(
        src_v2 / "papers.csv",
        "doi,title,category",
        ["10.1101/001,Paper A,genomics", "10.1101/002,Paper B,neuroscience"],
    )
    _write_csv(src_v2 / "extra.csv", "id", ["1"])
    r2 = manager.ingest_directory(src_v2)

    assert r2.status == "created"
    assert r2.version_id != r1.version_id
    assert r2.diff is not None
    assert r2.diff["added_files"] == ["extra.csv"]
    assert r2.diff["modified_files"][0]["row_delta"] == 1
    assert r2.diff["modified_files"][0]["added_columns"] == ["category"]

    # Rollback to v1
    manager.switch_version(r1.version_id)
    assert manager.get_current_version_id() == r1.version_id
    assert paths.current_link.resolve() == r1.snapshot_dir.resolve()

    # Diff API works symmetrically
    explicit_diff = manager.diff_versions(r1.version_id, r2.version_id)
    assert explicit_diff["from_version"] == r1.version_id
    assert explicit_diff["to_version"] == r2.version_id


def test_switch_unknown_version_raises(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    manager = DatasetVersionManager(paths=paths)
    with pytest.raises(KeyError):
        manager.switch_version("v_does_not_exist")
