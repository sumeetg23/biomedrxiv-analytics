"""Dataset download, immutable snapshotting, version registry, and diffing.

Design principles:
- Content-addressed deduplication via SHA-256 digests so scheduled updates do not
  create redundant snapshots when upstream Kaggle data has not changed.
- Immutable version directories under ``data/versions/<version_id>/`` with a
  ``manifest.json`` file recording per-file checksums, sizes, and tabular row counts.
- Atomic symlink updates for ``data/current`` so readers never observe partial writes.
- Audit trail in ``data/registry.json`` tracking every version and check timestamp.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from biomedrxiv.config import DEFAULT_KAGGLE_DATASET, ProjectPaths

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB
TABULAR_EXTENSIONS = {".csv", ".tsv", ".parquet", ".jsonl", ".ndjson"}


@dataclass
class FileRecord:
    """Metadata for a single file inside a dataset snapshot."""

    relative_path: str
    size_bytes: int
    sha256: str
    row_count: int | None = None
    columns: list[str] | None = None


@dataclass
class VersionRecord:
    """Metadata for a stored dataset version."""

    version_id: str
    dataset_handle: str
    created_at_utc: str
    content_sha256: str
    total_size_bytes: int
    file_count: int
    files: list[FileRecord]
    source_path: str
    kaggle_cache_version: str | None = None
    diff_from_previous: dict[str, Any] | None = None
    notes: str = ""


@dataclass
class UpdateResult:
    """Structured outcome of a download/update run."""

    status: str  # "created", "unchanged"
    version_id: str
    content_sha256: str
    snapshot_dir: Path
    diff: dict[str, Any] | None = None
    checked_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def compute_file_sha256(path: Path) -> str:
    """Compute hex SHA-256 checksum of a file in streaming chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dataset_digest(files: list[FileRecord]) -> str:
    """Compute a deterministic dataset-level SHA-256 from sorted file records."""
    digest = hashlib.sha256()
    for record in sorted(files, key=lambda r: r.relative_path):
        digest.update(f"{record.relative_path}:{record.size_bytes}:{record.sha256}\n".encode())
    return digest.hexdigest()


def _inspect_tabular_metadata(path: Path) -> tuple[int | None, list[str] | None]:
    """Best-effort fast inspection of row counts and column headers for tabular files."""
    suffix = path.suffix.lower()
    if suffix not in TABULAR_EXTENSIONS:
        return None, None

    try:
        if suffix == ".parquet":
            import pyarrow.parquet as pq

            pf = pq.ParquetFile(path)
            return pf.metadata.num_rows, list(pf.schema_arrow.names)

        if suffix in {".csv", ".tsv"}:
            import csv

            delimiter = "\t" if suffix == ".tsv" else ","
            with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh, delimiter=delimiter)
                header = next(reader, None)
                if header is None:
                    return 0, []
                row_count = sum(1 for _ in reader)
                return row_count, header

        if suffix in {".jsonl", ".ndjson"}:
            columns: list[str] | None = None
            row_count = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if columns is None:
                        obj = json.loads(stripped)
                        if isinstance(obj, dict):
                            columns = list(obj.keys())
                    row_count += 1
            return row_count, columns
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Could not inspect tabular metadata for %s: %s", path.name, exc)

    return None, None


def scan_directory(directory: Path) -> list[FileRecord]:
    """Inventory every regular file inside ``directory`` with checksums and stats."""
    records: list[FileRecord] = []
    for file_path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = file_path.relative_to(directory).as_posix()
        if rel == "manifest.json":
            continue
        size = file_path.stat().st_size
        sha256 = compute_file_sha256(file_path)
        row_count, columns = _inspect_tabular_metadata(file_path)
        records.append(
            FileRecord(
                relative_path=rel,
                size_bytes=size,
                sha256=sha256,
                row_count=row_count,
                columns=columns,
            )
        )
    return records


def _extract_kaggle_cache_version(source_path: Path) -> str | None:
    """Extract numeric version component if kagglehub path follows .../versions/<N>."""
    parts = source_path.parts
    for idx, part in enumerate(parts):
        if part == "versions" and idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def compute_version_diff(
    previous: VersionRecord, current: VersionRecord
) -> dict[str, Any]:
    """Compare two version snapshots at the file, schema, and row-count levels."""
    prev_map = {f.relative_path: f for f in previous.files}
    curr_map = {f.relative_path: f for f in current.files}

    added_files = sorted(set(curr_map) - set(prev_map))
    removed_files = sorted(set(prev_map) - set(curr_map))
    modified_files: list[dict[str, Any]] = []
    unchanged_files: list[str] = []

    for rel in sorted(set(prev_map) & set(curr_map)):
        old_f = prev_map[rel]
        new_f = curr_map[rel]
        if old_f.sha256 == new_f.sha256:
            unchanged_files.append(rel)
            continue

        entry: dict[str, Any] = {
            "file": rel,
            "size_delta_bytes": new_f.size_bytes - old_f.size_bytes,
            "old_sha256": old_f.sha256[:12],
            "new_sha256": new_f.sha256[:12],
        }
        if old_f.row_count is not None and new_f.row_count is not None:
            entry["old_rows"] = old_f.row_count
            entry["new_rows"] = new_f.row_count
            entry["row_delta"] = new_f.row_count - old_f.row_count
        if old_f.columns is not None and new_f.columns is not None:
            old_cols = set(old_f.columns)
            new_cols = set(new_f.columns)
            if old_cols != new_cols:
                entry["added_columns"] = sorted(new_cols - old_cols)
                entry["removed_columns"] = sorted(old_cols - new_cols)
        modified_files.append(entry)

    return {
        "from_version": previous.version_id,
        "to_version": current.version_id,
        "total_size_delta_bytes": current.total_size_bytes - previous.total_size_bytes,
        "added_files": added_files,
        "removed_files": removed_files,
        "modified_files": modified_files,
        "unchanged_files_count": len(unchanged_files),
    }


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _update_current_symlink(current_link: Path, target_dir: Path) -> None:
    """Atomically point ``data/current`` at ``target_dir`` using a relative symlink."""
    rel_target = os.path.relpath(target_dir, start=current_link.parent)
    tmp_link = current_link.parent / f".current_tmp_{os.getpid()}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(rel_target, tmp_link)
    os.replace(tmp_link, current_link)


class DatasetVersionManager:
    """Manages Kaggle downloads, immutable version snapshots, and update history."""

    def __init__(
        self,
        paths: ProjectPaths | None = None,
        dataset_handle: str = DEFAULT_KAGGLE_DATASET,
    ) -> None:
        self.paths = paths or ProjectPaths.from_root()
        self.dataset_handle = dataset_handle
        self.paths.ensure_directories()

    def load_registry(self) -> dict[str, Any]:
        """Load ``data/registry.json`` or initialize an empty registry structure."""
        if not self.paths.registry_file.exists():
            return {
                "dataset_handle": self.dataset_handle,
                "current_version": None,
                "versions": [],
                "check_history": [],
            }
        with self.paths.registry_file.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_registry(self, registry: dict[str, Any]) -> None:
        """Persist ``data/registry.json`` atomically."""
        _atomic_write_json(self.paths.registry_file, registry)

    def list_versions(self) -> list[dict[str, Any]]:
        """Return all recorded versions in chronological order."""
        return self.load_registry().get("versions", [])

    def get_current_version_id(self) -> str | None:
        """Return the active version ID recorded in the registry."""
        return self.load_registry().get("current_version")

    def download_from_kaggle(self, force_download: bool = False) -> Path:
        """Download the Kaggle dataset via ``kagglehub`` and return the local cache path."""
        import kagglehub

        logger.info(
            "Downloading Kaggle dataset '%s' (force_download=%s)...",
            self.dataset_handle,
            force_download,
        )
        local_path = kagglehub.dataset_download(
            self.dataset_handle,
            force_download=force_download,
        )
        resolved = Path(local_path).resolve()
        logger.info("Kaggle download resolved to %s", resolved)
        return resolved

    def ingest_directory(
        self,
        source_dir: Path,
        *,
        notes: str = "",
        force_snapshot: bool = False,
    ) -> UpdateResult:
        """Snapshot a local directory into ``data/versions/`` if its content changed.

        Separating ``ingest_directory`` from ``download_from_kaggle`` makes the
        versioning engine deterministic and straightforward to unit-test.
        """
        source_dir = source_dir.resolve()
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source dataset directory does not exist: {source_dir}")

        files = scan_directory(source_dir)
        if not files:
            raise ValueError(f"Source dataset directory is empty: {source_dir}")

        content_sha256 = compute_dataset_digest(files)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        registry = self.load_registry()

        # Deduplicate against the latest version unless forced
        existing_versions = registry.get("versions", [])
        if existing_versions and not force_snapshot:
            latest = existing_versions[-1]
            if latest["content_sha256"] == content_sha256:
                latest_id = latest["version_id"]
                snapshot_dir = self.paths.versions_dir / latest_id
                registry.setdefault("check_history", []).append(
                    {
                        "checked_at_utc": now_iso,
                        "status": "unchanged",
                        "version_id": latest_id,
                        "content_sha256": content_sha256,
                    }
                )
                # Keep check history bounded to last 500 checks
                registry["check_history"] = registry["check_history"][-500:]
                self.save_registry(registry)
                logger.info(
                    "Dataset unchanged (digest %s matches active version %s).",
                    content_sha256[:12],
                    latest_id,
                )
                return UpdateResult(
                    status="unchanged",
                    version_id=latest_id,
                    content_sha256=content_sha256,
                    snapshot_dir=snapshot_dir,
                    diff=None,
                    checked_at_utc=now_iso,
                )

        # Build a human-readable, sortable, collision-resistant version ID
        version_id = f"v{now.strftime('%Y%m%d_%H%M%S')}_{content_sha256[:8]}"
        snapshot_dir = self.paths.versions_dir / version_id
        staging_dir = self.paths.versions_dir / f".staging_{version_id}_{os.getpid()}"

        if staging_dir.exists():
            shutil.rmtree(staging_dir)

        try:
            shutil.copytree(source_dir, staging_dir)
            total_size = sum(f.size_bytes for f in files)
            new_record = VersionRecord(
                version_id=version_id,
                dataset_handle=self.dataset_handle,
                created_at_utc=now_iso,
                content_sha256=content_sha256,
                total_size_bytes=total_size,
                file_count=len(files),
                files=files,
                source_path=str(source_dir),
                kaggle_cache_version=_extract_kaggle_cache_version(source_dir),
                notes=notes,
            )

            diff: dict[str, Any] | None = None
            if existing_versions:
                prev_dict = existing_versions[-1]
                prev_record = VersionRecord(
                    version_id=prev_dict["version_id"],
                    dataset_handle=prev_dict["dataset_handle"],
                    created_at_utc=prev_dict["created_at_utc"],
                    content_sha256=prev_dict["content_sha256"],
                    total_size_bytes=prev_dict["total_size_bytes"],
                    file_count=prev_dict["file_count"],
                    files=[FileRecord(**f) for f in prev_dict["files"]],
                    source_path=prev_dict.get("source_path", ""),
                    kaggle_cache_version=prev_dict.get("kaggle_cache_version"),
                )
                diff = compute_version_diff(prev_record, new_record)
                new_record.diff_from_previous = diff

            manifest_payload = asdict(new_record)
            _atomic_write_json(staging_dir / "manifest.json", manifest_payload)

            os.replace(staging_dir, snapshot_dir)
        except Exception:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        _update_current_symlink(self.paths.current_link, snapshot_dir)

        registry["dataset_handle"] = self.dataset_handle
        registry["current_version"] = version_id
        registry.setdefault("versions", []).append(asdict(new_record))
        registry.setdefault("check_history", []).append(
            {
                "checked_at_utc": now_iso,
                "status": "created",
                "version_id": version_id,
                "content_sha256": content_sha256,
            }
        )
        registry["check_history"] = registry["check_history"][-500:]
        self.save_registry(registry)

        logger.info(
            "Created new snapshot %s (%d files, %.2f MiB).",
            version_id,
            len(files),
            total_size / (1024 * 1024),
        )
        return UpdateResult(
            status="created",
            version_id=version_id,
            content_sha256=content_sha256,
            snapshot_dir=snapshot_dir,
            diff=diff,
            checked_at_utc=now_iso,
        )

    def sync_from_kaggle(
        self,
        *,
        force_download: bool = True,
        force_snapshot: bool = False,
        notes: str = "",
    ) -> UpdateResult:
        """Download latest Kaggle dataset and snapshot it if content changed."""
        source_dir = self.download_from_kaggle(force_download=force_download)
        return self.ingest_directory(
            source_dir,
            notes=notes,
            force_snapshot=force_snapshot,
        )

    def switch_version(self, version_id: str) -> Path:
        """Point ``data/current`` at an existing historical version (rollback/pin)."""
        registry = self.load_registry()
        known_ids = {v["version_id"] for v in registry.get("versions", [])}
        if version_id not in known_ids:
            raise KeyError(f"Unknown version_id '{version_id}'. Known: {sorted(known_ids)}")

        target_dir = self.paths.versions_dir / version_id
        if not target_dir.is_dir():
            raise FileNotFoundError(f"Snapshot directory missing on disk: {target_dir}")

        _update_current_symlink(self.paths.current_link, target_dir)
        registry["current_version"] = version_id
        self.save_registry(registry)
        logger.info("Switched active dataset version to %s.", version_id)
        return target_dir

    def diff_versions(self, from_version_id: str, to_version_id: str) -> dict[str, Any]:
        """Compute diff between any two recorded versions."""
        registry = self.load_registry()
        by_id = {v["version_id"]: v for v in registry.get("versions", [])}
        for vid in (from_version_id, to_version_id):
            if vid not in by_id:
                raise KeyError(f"Unknown version_id '{vid}'.")

        def _to_record(d: dict[str, Any]) -> VersionRecord:
            return VersionRecord(
                version_id=d["version_id"],
                dataset_handle=d["dataset_handle"],
                created_at_utc=d["created_at_utc"],
                content_sha256=d["content_sha256"],
                total_size_bytes=d["total_size_bytes"],
                file_count=d["file_count"],
                files=[FileRecord(**f) for f in d["files"]],
                source_path=d.get("source_path", ""),
                kaggle_cache_version=d.get("kaggle_cache_version"),
            )

        return compute_version_diff(_to_record(by_id[from_version_id]), _to_record(by_id[to_version_id]))
