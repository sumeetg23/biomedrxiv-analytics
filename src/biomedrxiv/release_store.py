"""Versioned monthly release packaging, hydration, and provenance ledger (Option A).

Keeps binary Parquet tables (~21.7 MiB), compressed raw CSV snapshots (~380 MiB),
and incremental caches (~19 MiB) inside immutable GitHub Releases (`data-vYYYY.MM`)
instead of `.git` history, while tracking a tiny provenance ledger at
`web/data/releases.json` for atomic Pages deploys and 1-click rollbacks.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from biomedrxiv.config import ProjectPaths

WEB_PARQUET_FILENAMES = (
    "preprints.parquet",
    "clinical_trials.parquet",
    "downstream_clinical.parquet",
    "manifest.json",
)

CACHE_MEMBER_PATHS = (
    "clinical_trials.sqlite",
    "publication_status.sqlite",
    "ror_api_cache.sqlite",
    "rag_embeddings_bge_small.jsonl",
)


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute hex SHA-256 digest of a file using streaming reads."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(files: list[Path], out_path: Path) -> dict[str, str]:
    """Write standard `sha256sum` formatted manifest and return `{filename: digest}`."""
    digests: dict[str, str] = {}
    lines: list[str] = []
    for f in sorted(files, key=lambda p: p.name):
        if not f.is_file():
            continue
        digest = compute_sha256(f)
        digests[f.name] = digest
        lines.append(f"{digest}  {f.name}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return digests


def parse_sha256sums(sums_path: Path) -> dict[str, str]:
    """Parse a `sha256sums.txt` file into `{filename: expected_hex}`."""
    expected: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            expected[parts[-1]] = parts[0].lower()
    return expected


def verify_sha256sums(directory: Path, sums_path: Path, required_files: tuple[str, ...] = ()) -> list[str]:
    """Verify SHA-256 digests for files present in `directory` against `sums_path`.

    Raises ValueError on any mismatch or missing required file.
    Returns list of verified filenames.
    """
    expected = parse_sha256sums(sums_path)
    verified: list[str] = []

    for req in required_files:
        if req not in expected:
            raise ValueError(f"Required file '{req}' missing from {sums_path.name}")
        target = directory / req
        if not target.is_file():
            raise ValueError(f"Required file '{req}' missing in {directory}")

    for name, exp_digest in expected.items():
        target = directory / name
        if not target.is_file():
            continue
        actual = compute_sha256(target).lower()
        if actual != exp_digest:
            raise ValueError(f"SHA-256 mismatch for {name}: expected {exp_digest}, got {actual}")
        verified.append(name)
    return verified


def _gzip_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as f_in, gzip.open(dst, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)


def _gunzip_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src, "rb") as f_in, dst.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)


def package_release_bundle(
    paths: ProjectPaths,
    web_parquet_dir: Path,
    stage_dir: Path,
    *,
    include_raw_csv: bool = True,
    include_caches: bool = True,
) -> dict[str, Any]:
    """Stage all monthly release assets into `stage_dir` with `sha256sums.txt`."""
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged_files: list[Path] = []

    # 1. Web Parquet tables for browser DuckDB-Wasm
    for fname in WEB_PARQUET_FILENAMES:
        src = web_parquet_dir / fname
        if not src.is_file():
            raise FileNotFoundError(f"Missing required Web Parquet file: {src}")
        dst = stage_dir / fname
        shutil.copy2(src, dst)
        staged_files.append(dst)

    # 2. Compressed canonical raw CSV for next month's incremental API sync
    if include_raw_csv:
        csv_src = paths.current_link / "biorxiv_medrxiv_preprints.csv"
        if csv_src.is_file():
            csv_gz = stage_dir / "biorxiv_medrxiv_preprints.csv.gz"
            _gzip_file(csv_src, csv_gz)
            staged_files.append(csv_gz)

    # 3. Compressed RAG corpus (for atomic Vectorize sync on PR merge)
    rag_src = paths.processed_dir / "rag_corpus.jsonl"
    if rag_src.is_file():
        rag_gz = stage_dir / "rag_corpus.jsonl.gz"
        _gzip_file(rag_src, rag_gz)
        staged_files.append(rag_gz)

    # 4. Incremental caches archive (NCT SQLite, pubs SQLite, ROR API cache, local BGE embeddings)
    if include_caches:
        cache_members = [paths.cache_dir / name for name in CACHE_MEMBER_PATHS if (paths.cache_dir / name).is_file()]
        if cache_members:
            tar_path = stage_dir / "caches.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tf:
                for member in cache_members:
                    tf.add(member, arcname=member.name)
            staged_files.append(tar_path)

    sums_path = stage_dir / "sha256sums.txt"
    digests = write_sha256sums(staged_files, sums_path)
    return {
        "stage_dir": str(stage_dir),
        "assets": sorted([f.name for f in staged_files] + [sums_path.name]),
        "sha256": digests,
    }


def hydrate_web_parquet_from_dir(release_dir: Path, web_parquet_dir: Path) -> list[str]:
    """Verify and copy Web Parquet files from a downloaded release directory."""
    release_dir = Path(release_dir)
    web_parquet_dir = Path(web_parquet_dir)
    sums_path = release_dir / "sha256sums.txt"
    if sums_path.is_file():
        verify_sha256sums(release_dir, sums_path, required_files=WEB_PARQUET_FILENAMES)

    web_parquet_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for fname in WEB_PARQUET_FILENAMES:
        src = release_dir / fname
        if not src.is_file():
            raise FileNotFoundError(f"Release directory missing {fname}: {src}")
        shutil.copy2(src, web_parquet_dir / fname)
        copied.append(fname)
    return copied


def hydrate_base_state_from_dir(release_dir: Path, paths: ProjectPaths) -> dict[str, bool]:
    """Restore raw CSV snapshot, RAG corpus, and SQLite/embedding caches from a release dir."""
    release_dir = Path(release_dir)
    paths.ensure_directories()
    sums_path = release_dir / "sha256sums.txt"
    if sums_path.is_file():
        verify_sha256sums(release_dir, sums_path)

    restored = {"raw_csv": False, "caches": False, "rag_corpus": False}

    csv_gz = release_dir / "biorxiv_medrxiv_preprints.csv.gz"
    if csv_gz.is_file():
        seed_dir = paths.versions_dir / "hydrated_base"
        seed_dir.mkdir(parents=True, exist_ok=True)
        csv_dst = seed_dir / "biorxiv_medrxiv_preprints.csv"
        _gunzip_file(csv_gz, csv_dst)
        # Point data/current symlink (or directory) to hydrated_base
        if paths.current_link.is_symlink() or paths.current_link.is_file():
            paths.current_link.unlink()
        elif paths.current_link.is_dir():
            shutil.rmtree(paths.current_link)
        paths.current_link.symlink_to(seed_dir)
        restored["raw_csv"] = True

    tar_path = release_dir / "caches.tar.gz"
    if tar_path.is_file():
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                # Strict path traversal guard: only allow flat expected cache filenames
                if member.name in CACHE_MEMBER_PATHS and member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        dst = paths.cache_dir / member.name
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        with dst.open("wb") as out_f:
                            shutil.copyfileobj(extracted, out_f)
        restored["caches"] = True

    rag_gz = release_dir / "rag_corpus.jsonl.gz"
    if rag_gz.is_file():
        _gunzip_file(rag_gz, paths.processed_dir / "rag_corpus.jsonl")
        restored["rag_corpus"] = True

    return restored


def load_releases_ledger(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.is_file():
        return {"schema_version": "1.0", "active_release_tag": "", "releases": []}
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def update_releases_ledger(
    ledger_path: Path,
    *,
    tag: str,
    dashboard_json_path: Path,
    web_parquet_dir: Path,
    gates_passed: bool = True,
    set_active: bool = True,
) -> dict[str, Any]:
    """Upsert a release entry into `web/data/releases.json` with KPIs & Parquet SHA-256 digests."""
    ledger = load_releases_ledger(ledger_path)
    dash = json.loads(dashboard_json_path.read_text(encoding="utf-8"))
    k_all = dash.get("kpis", {}).get("all", {})
    prov = dash.get("dataset_provenance", {})
    ct_summary = (dash.get("clinical_trials") or {}).get("summary") or {}

    parquet_sha: dict[str, str] = {}
    total_bytes = 0
    for fname in WEB_PARQUET_FILENAMES:
        fp = web_parquet_dir / fname
        if fp.is_file():
            parquet_sha[fname] = compute_sha256(fp)
            total_bytes += fp.stat().st_size

    entry = {
        "tag": tag,
        "recorded_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "snapshot_version_id": prov.get("version_id", "local"),
        "unique_dois": int(k_all.get("unique_dois", 0)),
        "total_version_rows": int(k_all.get("total_version_rows", 0)),
        "date_min": k_all.get("date_min"),
        "date_max": k_all.get("date_max"),
        "overall_pub_rate_pct": k_all.get("overall_pub_rate_pct"),
        "clinical_trial_links": int(ct_summary.get("total_trial_links", 0)),
        "parquet_total_mib": round(total_bytes / (1024.0 * 1024.0), 2),
        "gates_passed": bool(gates_passed),
        "sha256": parquet_sha,
    }

    existing = [r for r in ledger.get("releases", []) if r.get("tag") != tag]
    existing.insert(0, entry)
    ledger["schema_version"] = "1.0"
    if set_active:
        ledger["active_release_tag"] = tag
    ledger["releases"] = existing[:36]  # Keep 3 years of monthly provenance

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger
