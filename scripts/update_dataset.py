#!/usr/bin/env python3
"""Automated dataset update entrypoint and macOS launchd / cron schedule manager.

Run manually or from a scheduler (macOS launchd, cron, GitHub Actions):
    .venv/bin/python scripts/update_dataset.py
    .venv/bin/python scripts/update_dataset.py --print-cron
    .venv/bin/python scripts/update_dataset.py --write-launchd-plist ./config/com.biomedrxiv.dataset.updater.plist
    .venv/bin/python scripts/update_dataset.py --install-launchd --weekday 0 --hour 3 --minute 0
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
from biomedrxiv.scheduler import (
    DEFAULT_LAUNCHD_LABEL,
    format_cron_entry,
    install_launchd_agent,
    uninstall_launchd_agent,
    write_launchd_plist,
)
from biomedrxiv.versioning import DatasetVersionManager


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Kaggle for dataset updates and manage automatic schedules."
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_KAGGLE_DATASET,
        help=f"Kaggle dataset handle (default: {DEFAULT_KAGGLE_DATASET}).",
    )
    parser.add_argument("--hour", type=int, default=3, help="Scheduled hour (0-23, default: 3).")
    parser.add_argument("--minute", type=int, default=0, help="Scheduled minute (0-59, default: 0).")
    parser.add_argument(
        "--weekday",
        type=int,
        default=0,
        help="Day of week (0=Sun..6=Sat, default: 0). Use -1 for daily.",
    )
    parser.add_argument(
        "--print-cron",
        action="store_true",
        help="Print a ready-to-paste crontab entry and exit.",
    )
    parser.add_argument(
        "--write-launchd-plist",
        type=Path,
        default=None,
        help="Write a macOS launchd plist to the given path (does not load it) and exit.",
    )
    parser.add_argument(
        "--install-launchd",
        action="store_true",
        help="Write and load a macOS LaunchAgent in ~/Library/LaunchAgents.",
    )
    parser.add_argument(
        "--uninstall-launchd",
        action="store_true",
        help="Unload and remove the macOS LaunchAgent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    weekday = None if args.weekday == -1 else args.weekday

    if args.print_cron:
        print(format_cron_entry(paths, hour=args.hour, minute=args.minute, weekday=weekday))
        return 0

    if args.write_launchd_plist is not None:
        out = write_launchd_plist(
            paths,
            output_path=args.write_launchd_plist,
            hour=args.hour,
            minute=args.minute,
            weekday=weekday,
        )
        print(f"Wrote launchd plist to {out}")
        return 0

    if args.install_launchd:
        out = install_launchd_agent(paths, hour=args.hour, minute=args.minute, weekday=weekday)
        print(f"Installed and loaded macOS LaunchAgent: {out}")
        return 0

    if args.uninstall_launchd:
        removed = uninstall_launchd_agent(DEFAULT_LAUNCHD_LABEL)
        print(f"Removed LaunchAgent: {removed}" if removed else "No LaunchAgent installed.")
        return 0

    _setup_logging(paths.logs_dir / "updates.log")
    logger = logging.getLogger("biomedrxiv.update")

    manager = DatasetVersionManager(paths=paths, dataset_handle=args.dataset)
    result = manager.sync_from_kaggle(force_download=True, notes="automated update check")
    summary = {
        "status": result.status,
        "version_id": result.version_id,
        "content_sha256": result.content_sha256,
        "checked_at_utc": result.checked_at_utc,
        "diff": result.diff,
    }
    logger.info("Update run finished: %s", json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
