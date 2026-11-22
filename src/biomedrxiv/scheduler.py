"""Scheduling utilities for automated dataset updates (macOS launchd & cron)."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from biomedrxiv.config import ProjectPaths

DEFAULT_LAUNCHD_LABEL = "com.biomedrxiv.dataset.updater"


def build_launchd_plist(
    paths: ProjectPaths,
    *,
    label: str = DEFAULT_LAUNCHD_LABEL,
    hour: int = 3,
    minute: int = 0,
    weekday: int | None = 0,
    python_executable: Path | None = None,
) -> dict:
    """Build a macOS LaunchAgent plist dictionary for periodic dataset updates.

    Args:
        paths: Canonical project paths.
        label: Reverse-DNS launchd job label.
        hour: Local hour (0-23) to run the update.
        minute: Local minute (0-59) to run the update.
        weekday: Day of week (0=Sunday .. 6=Saturday), or None for daily runs.
        python_executable: Python binary path (defaults to project ``.venv/bin/python``).
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour must be 0-23 and minute must be 0-59.")
    if weekday is not None and not (0 <= weekday <= 6):
        raise ValueError("weekday must be 0 (Sunday) through 6 (Saturday), or None for daily.")

    py_bin = python_executable or (paths.root / ".venv" / "bin" / "python")
    update_script = paths.root / "scripts" / "update_dataset.py"

    calendar_interval: dict[str, int] = {"Hour": hour, "Minute": minute}
    if weekday is not None:
        calendar_interval["Weekday"] = weekday

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "Label": label,
        "ProgramArguments": [str(py_bin), str(update_script)],
        "WorkingDirectory": str(paths.root),
        "EnvironmentVariables": {
            "BIOMEDRXIV_PROJECT_ROOT": str(paths.root),
            "PYTHONPATH": str(paths.root / "src"),
        },
        "StartCalendarInterval": calendar_interval,
        "StandardOutPath": str(paths.logs_dir / "launchd_stdout.log"),
        "StandardErrorPath": str(paths.logs_dir / "launchd_stderr.log"),
        "RunAtLoad": False,
    }


def write_launchd_plist(
    paths: ProjectPaths,
    output_path: Path | None = None,
    **kwargs,
) -> Path:
    """Write the LaunchAgent plist file to disk and return its path."""
    label = kwargs.get("label", DEFAULT_LAUNCHD_LABEL)
    target = output_path or (
        Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    plist_data = build_launchd_plist(paths, **kwargs)
    with target.open("wb") as fh:
        plistlib.dump(plist_data, fh)
    return target


def install_launchd_agent(paths: ProjectPaths, **kwargs) -> Path:
    """Write and load the macOS LaunchAgent via ``launchctl``."""
    plist_path = write_launchd_plist(paths, **kwargs)
    # Unload first if already loaded (ignore error if not yet loaded)
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "load", str(plist_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return plist_path


def uninstall_launchd_agent(label: str = DEFAULT_LAUNCHD_LABEL) -> Path | None:
    """Unload and remove an installed macOS LaunchAgent plist if present."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist_path.exists():
        return None
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    plist_path.unlink()
    return plist_path


def format_cron_entry(
    paths: ProjectPaths,
    *,
    hour: int = 3,
    minute: int = 0,
    weekday: int | None = 0,
) -> str:
    """Generate a portable crontab line for Linux/macOS environments."""
    py_bin = paths.root / ".venv" / "bin" / "python"
    script = paths.root / "scripts" / "update_dataset.py"
    log_file = paths.logs_dir / "cron_updates.log"
    dow = "*" if weekday is None else str(weekday)
    return f'{minute} {hour} * * {dow} cd "{paths.root}" && "{py_bin}" "{script}" >> "{log_file}" 2>&1'
