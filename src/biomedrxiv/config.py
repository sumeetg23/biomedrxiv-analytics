"""Central configuration and path definitions for the Bio&Med-Rxiv project."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_KAGGLE_DATASET = "m0delcrafter/bio-and-med-dataset"


def get_project_root() -> Path:
    """Resolve the repository root directory."""
    env_root = os.environ.get("BIOMEDRXIV_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    # src/biomedrxiv/config.py -> parents[2] is project root
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProjectPaths:
    """Canonical filesystem layout for data, versions, cache, and logs."""

    root: Path
    data_dir: Path
    versions_dir: Path
    current_link: Path
    processed_dir: Path
    cache_dir: Path
    registry_file: Path
    logs_dir: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> ProjectPaths:
        resolved_root = (root or get_project_root()).resolve()
        data_dir = resolved_root / "data"
        return cls(
            root=resolved_root,
            data_dir=data_dir,
            versions_dir=data_dir / "versions",
            current_link=data_dir / "current",
            processed_dir=data_dir / "processed",
            cache_dir=data_dir / "cache",
            registry_file=data_dir / "registry.json",
            logs_dir=resolved_root / "logs",
        )

    def ensure_directories(self) -> None:
        """Create required project directories if they do not exist."""
        for directory in (
            self.data_dir,
            self.versions_dir,
            self.processed_dir,
            self.cache_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
