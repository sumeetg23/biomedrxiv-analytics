"""Unit tests for macOS launchd and cron schedule generation."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from biomedrxiv.config import ProjectPaths
from biomedrxiv.scheduler import (
    build_launchd_plist,
    format_cron_entry,
    write_launchd_plist,
)


def test_build_and_write_launchd_plist(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    plist_file = write_launchd_plist(
        paths,
        output_path=tmp_path / "test.plist",
        hour=4,
        minute=30,
        weekday=1,
    )
    loaded = plistlib.loads(plist_file.read_bytes())
    assert loaded["StartCalendarInterval"] == {"Hour": 4, "Minute": 30, "Weekday": 1}
    assert loaded["WorkingDirectory"] == str(paths.root)
    assert loaded["ProgramArguments"][1].endswith("scripts/update_dataset.py")


def test_daily_schedule_omits_weekday(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    plist = build_launchd_plist(paths, hour=2, minute=15, weekday=None)
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}

    cron = format_cron_entry(paths, hour=2, minute=15, weekday=None)
    assert cron.startswith("15 2 * * * ")


def test_invalid_schedule_raises(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "project")
    with pytest.raises(ValueError):
        build_launchd_plist(paths, hour=25, minute=0)
    with pytest.raises(ValueError):
        build_launchd_plist(paths, hour=3, minute=0, weekday=7)
