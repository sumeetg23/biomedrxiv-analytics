#!/usr/bin/env python3
"""CLI for downloading, versioning, listing, diffing, and rolling back the Kaggle dataset.

Usage examples:
    .venv/bin/python scripts/download_dataset.py sync
    .venv/bin/python scripts/download_dataset.py list
    .venv/bin/python scripts/download_dataset.py diff <from_version> <to_version>
    .venv/bin/python scripts/download_dataset.py switch <version_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.config import DEFAULT_KAGGLE_DATASET, ProjectPaths
from biomedrxiv.versioning import DatasetVersionManager


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def _cmd_sync(manager: DatasetVersionManager, args: argparse.Namespace) -> int:
    result = manager.sync_from_kaggle(
        force_download=not args.use_cache,
        force_snapshot=args.force_snapshot,
        notes=args.notes,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "version_id": result.version_id,
                "content_sha256": result.content_sha256,
                "snapshot_dir": str(result.snapshot_dir),
                "current_symlink": str(manager.paths.current_link),
                "checked_at_utc": result.checked_at_utc,
                "diff": result.diff,
            },
            indent=2,
        )
    )
    return 0


def _cmd_list(manager: DatasetVersionManager, _args: argparse.Namespace) -> int:
    registry = manager.load_registry()
    current = registry.get("current_version")
    versions = registry.get("versions", [])
    if not versions:
        print("No dataset versions stored yet. Run: scripts/download_dataset.py sync")
        return 0

    print(f"Dataset: {registry.get('dataset_handle')}")
    print(f"Active version: {current}\n")
    for v in versions:
        marker = "*" if v["version_id"] == current else " "
        size_mb = v["total_size_bytes"] / (1024 * 1024)
        print(
            f"{marker} {v['version_id']}  |  {v['created_at_utc']}  |  "
            f"{v['file_count']} file(s)  |  {size_mb:.2f} MiB  |  sha256:{v['content_sha256'][:12]}"
        )
        for f in v.get("files", []):
            rows = f"{f['row_count']:,} rows" if f.get("row_count") is not None else "non-tabular"
            print(f"      - {f['relative_path']} ({rows})")
    return 0


def _cmd_diff(manager: DatasetVersionManager, args: argparse.Namespace) -> int:
    diff = manager.diff_versions(args.from_version, args.to_version)
    print(json.dumps(diff, indent=2))
    return 0


def _cmd_switch(manager: DatasetVersionManager, args: argparse.Namespace) -> int:
    target = manager.switch_version(args.version_id)
    print(f"Active version switched to {args.version_id} -> {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and manage versioned snapshots of the Bio&Med-Rxiv Kaggle dataset."
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_KAGGLE_DATASET,
        help=f"Kaggle dataset handle (default: {DEFAULT_KAGGLE_DATASET}).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Download from Kaggle and create snapshot if changed.")
    p_sync.add_argument(
        "--use-cache",
        action="store_true",
        help="Reuse existing kagglehub cache instead of forcing a fresh remote check.",
    )
    p_sync.add_argument(
        "--force-snapshot",
        action="store_true",
        help="Create a new snapshot even if SHA-256 matches the active version.",
    )
    p_sync.add_argument("--notes", default="", help="Optional annotation stored in manifest.")
    p_sync.set_defaults(func=_cmd_sync)

    p_list = sub.add_parser("list", help="List all recorded dataset versions.")
    p_list.set_defaults(func=_cmd_list)

    p_diff = sub.add_parser("diff", help="Diff two recorded versions.")
    p_diff.add_argument("from_version", help="Baseline version_id.")
    p_diff.add_argument("to_version", help="Target version_id.")
    p_diff.set_defaults(func=_cmd_diff)

    p_switch = sub.add_parser("switch", help="Point data/current at a historical version.")
    p_switch.add_argument("version_id", help="Version ID to activate.")
    p_switch.set_defaults(func=_cmd_switch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    manager = DatasetVersionManager(
        paths=ProjectPaths.from_root(PROJECT_ROOT),
        dataset_handle=args.dataset,
    )
    return args.func(manager, args)


if __name__ == "__main__":
    raise SystemExit(main())
