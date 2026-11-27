"""Unit tests for monthly GitHub Release packaging, SHA-256 verification, and ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from biomedrxiv.config import ProjectPaths
from biomedrxiv.release_store import (
    WEB_PARQUET_FILENAMES,
    hydrate_base_state_from_dir,
    hydrate_web_parquet_from_dir,
    package_release_bundle,
    update_releases_ledger,
    verify_sha256sums,
)


def _seed_workspace(tmp_path: Path) -> tuple[ProjectPaths, Path, Path]:
    paths = ProjectPaths.from_root(tmp_path / "repo")
    paths.ensure_directories()

    # Seed raw CSV in data/versions/v1 and symlink data/current -> v1
    v1 = paths.versions_dir / "v1"
    v1.mkdir(parents=True)
    (v1 / "biorxiv_medrxiv_preprints.csv").write_text("doi,title\n10.1101/1,Test\n", encoding="utf-8")
    paths.current_link.symlink_to(v1)

    # Seed caches and rag corpus
    (paths.cache_dir / "clinical_trials.sqlite").write_bytes(b"sqlite-ct-stub")
    (paths.cache_dir / "rag_embeddings_bge_small.jsonl").write_text('{"id":"a","values":[0.1]}\n', encoding="utf-8")
    (paths.processed_dir / "rag_corpus.jsonl").write_text('{"id":"a","text":"hello","metadata":{}}\n', encoding="utf-8")

    # Seed web parquet files
    web_pq = paths.root / "web" / "data" / "parquet"
    web_pq.mkdir(parents=True)
    for fname in WEB_PARQUET_FILENAMES:
        (web_pq / fname).write_bytes(f"payload-{fname}".encode("utf-8"))

    # Seed minimal dashboard_data.json
    dash_json = paths.root / "web" / "data" / "dashboard_data.json"
    dash_json.write_text(
        json.dumps(
            {
                "dataset_provenance": {"version_id": "v20261122_test"},
                "kpis": {
                    "all": {
                        "unique_dois": 454051,
                        "total_version_rows": 611986,
                        "date_min": "2013-11-07",
                        "date_max": "2026-11-22",
                        "overall_pub_rate_pct": 48.94,
                    }
                },
                "clinical_trials": {"summary": {"total_trial_links": 3034}},
            }
        ),
        encoding="utf-8",
    )
    return paths, web_pq, dash_json


def test_package_verify_and_hydrate_roundtrip(tmp_path: Path) -> None:
    paths, web_pq, _ = _seed_workspace(tmp_path)
    stage_dir = tmp_path / "stage"

    info = package_release_bundle(paths, web_pq, stage_dir)
    assert "sha256sums.txt" in info["assets"]
    assert "biorxiv_medrxiv_preprints.csv.gz" in info["assets"]
    assert "caches.tar.gz" in info["assets"]
    assert "rag_corpus.jsonl.gz" in info["assets"]
    for fname in WEB_PARQUET_FILENAMES:
        assert fname in info["assets"]

    # Hydrate into a clean CI workspace
    clean_paths = ProjectPaths.from_root(tmp_path / "ci_runner")
    clean_web_pq = clean_paths.root / "web" / "data" / "parquet"

    copied = hydrate_web_parquet_from_dir(stage_dir, clean_web_pq)
    assert set(copied) == set(WEB_PARQUET_FILENAMES)
    assert (clean_web_pq / "preprints.parquet").read_bytes() == b"payload-preprints.parquet"

    restored = hydrate_base_state_from_dir(stage_dir, clean_paths)
    assert restored == {"raw_csv": True, "caches": True, "rag_corpus": True}
    assert (clean_paths.current_link / "biorxiv_medrxiv_preprints.csv").read_text(encoding="utf-8").startswith("doi,title")
    assert (clean_paths.cache_dir / "clinical_trials.sqlite").read_bytes() == b"sqlite-ct-stub"
    assert (clean_paths.cache_dir / "rag_embeddings_bge_small.jsonl").exists()
    assert (clean_paths.processed_dir / "rag_corpus.jsonl").exists()


def test_verify_sha256sums_detects_tampering(tmp_path: Path) -> None:
    paths, web_pq, _ = _seed_workspace(tmp_path)
    stage_dir = tmp_path / "stage"
    package_release_bundle(paths, web_pq, stage_dir)

    # Corrupt one Parquet asset
    (stage_dir / "preprints.parquet").write_bytes(b"corrupted!")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_sha256sums(stage_dir, stage_dir / "sha256sums.txt", required_files=WEB_PARQUET_FILENAMES)


def test_releases_ledger_upsert_and_ordering(tmp_path: Path) -> None:
    _, web_pq, dash_json = _seed_workspace(tmp_path)
    ledger_path = tmp_path / "releases.json"

    l1 = update_releases_ledger(
        ledger_path,
        tag="data-v2026.11",
        dashboard_json_path=dash_json,
        web_parquet_dir=web_pq,
    )
    assert l1["active_release_tag"] == "data-v2026.11"
    assert len(l1["releases"]) == 1
    assert l1["releases"][0]["unique_dois"] == 454051
    assert "preprints.parquet" in l1["releases"][0]["sha256"]

    l2 = update_releases_ledger(
        ledger_path,
        tag="data-v2026.12",
        dashboard_json_path=dash_json,
        web_parquet_dir=web_pq,
    )
    assert l2["active_release_tag"] == "data-v2026.12"
    assert [r["tag"] for r in l2["releases"]] == ["data-v2026.12", "data-v2026.11"]
