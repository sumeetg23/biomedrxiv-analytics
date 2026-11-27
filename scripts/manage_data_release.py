#!/usr/bin/env python3
"""Manage versioned monthly data releases on GitHub Releases (Option A).

Subcommands:
  package        Stage Web Parquet, raw CSV (.gz), RAG corpus (.gz), caches (.tar.gz), and sha256sums.txt
  publish        Create/update a GitHub Release (`data-vYYYY.MM`) and upload staged assets via GitHub API
  hydrate-base   Download the latest published `data-v*` release into `data/current/` and `data/cache/`
  hydrate-web    Download `web/data/parquet/` for `active_release_tag` in `web/data/releases.json`
  record-ledger  Update `web/data/releases.json` provenance ledger
  promote        Promote a staged prerelease (`data-vYYYY.MM`) to Latest Release after PR merge

Uses pure Python (`requests`) + `GITHUB_TOKEN` (no extra CLI required; works on both macOS & CI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from biomedrxiv.config import ProjectPaths
from biomedrxiv.release_store import (
    WEB_PARQUET_FILENAMES,
    hydrate_base_state_from_dir,
    hydrate_web_parquet_from_dir,
    load_releases_ledger,
    package_release_bundle,
    update_releases_ledger,
)

DEFAULT_REPO = "sumeetg23/biomedrxiv-analytics"
GH_API = "https://api.github.com"


def _gh_headers(token: str | None = None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "biomedrxiv-release-manager",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_repo(args_repo: str | None) -> str:
    return (args_repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO).strip()


def _get_token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _fetch_release_by_tag(repo: str, tag: str, token: str | None) -> dict[str, Any] | None:
    url = f"{GH_API}/repos/{repo}/releases/tags/{tag}"
    resp = requests.get(url, headers=_gh_headers(token), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _fetch_latest_data_release(repo: str, token: str | None, include_prereleases: bool = False) -> dict[str, Any] | None:
    url = f"{GH_API}/repos/{repo}/releases?per_page=30"
    resp = requests.get(url, headers=_gh_headers(token), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for rel in resp.json():
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith("data-v"):
            continue
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_prereleases:
            continue
        return rel
    return None


def _download_release_assets(
    release: dict[str, Any],
    dest_dir: Path,
    only_names: set[str] | None,
    token: str | None,
) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if only_names is not None and name not in only_names:
            continue
        dl_url = asset.get("browser_download_url")
        if not dl_url:
            continue
        print(f"Downloading {name} ({round((asset.get('size') or 0) / (1024 * 1024), 2)} MiB)...")
        with requests.get(dl_url, headers=_gh_headers(token, accept="application/octet-stream"), stream=True, timeout=300) as r:
            r.raise_for_status()
            out_path = dest_dir / name
            with out_path.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        downloaded.append(name)
    return downloaded


def cmd_package(args: argparse.Namespace) -> int:
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    info = package_release_bundle(
        paths=paths,
        web_parquet_dir=args.web_parquet_dir,
        stage_dir=args.stage_dir,
        include_raw_csv=not args.no_raw_csv,
        include_caches=not args.no_caches,
    )
    print(json.dumps(info, indent=2))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    token = _get_token()
    if not token:
        print("ERROR: Set GITHUB_TOKEN (or GH_TOKEN) with `contents: write` scope.", file=sys.stderr)
        return 2
    repo = _get_repo(args.repo)
    stage_dir = Path(args.stage_dir)
    if not stage_dir.is_dir():
        print(f"ERROR: Stage directory not found: {stage_dir}", file=sys.stderr)
        return 1

    rel = _fetch_release_by_tag(repo, args.tag, token)
    if rel is None:
        notes = (
            args.notes_file.read_text(encoding="utf-8")
            if args.notes_file and args.notes_file.is_file()
            else f"Automated versioned BioRxiv & MedRxiv dataset release `{args.tag}`."
        )
        create_resp = requests.post(
            f"{GH_API}/repos/{repo}/releases",
            headers=_gh_headers(token),
            json={
                "tag_name": args.tag,
                "name": f"Dataset Snapshot {args.tag}",
                "body": notes,
                "draft": False,
                "prerelease": bool(args.prerelease),
            },
            timeout=30,
        )
        create_resp.raise_for_status()
        rel = create_resp.json()
        print(f"[created] GitHub Release {args.tag} (prerelease={args.prerelease})")
    else:
        print(f"[exists] Updating assets on GitHub Release {args.tag}")

    existing_assets = {a["name"]: a["id"] for a in rel.get("assets", [])}
    upload_base = rel["upload_url"].split("{", 1)[0]

    for asset_file in sorted(stage_dir.iterdir()):
        if not asset_file.is_file():
            continue
        if asset_file.name in existing_assets:
            del_url = f"{GH_API}/repos/{repo}/releases/assets/{existing_assets[asset_file.name]}"
            requests.delete(del_url, headers=_gh_headers(token), timeout=30).raise_for_status()
        size_mib = asset_file.stat().st_size / (1024.0 * 1024.0)
        print(f"Uploading {asset_file.name} ({size_mib:.2f} MiB)...")
        with asset_file.open("rb") as fh:
            up_resp = requests.post(
                f"{upload_base}?name={asset_file.name}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": "biomedrxiv-release-manager",
                },
                data=fh,
                timeout=600,
            )
            up_resp.raise_for_status()
    print(f"Published release '{args.tag}' -> {rel.get('html_url')}")
    return 0


def cmd_hydrate_base(args: argparse.Namespace) -> int:
    repo = _get_repo(args.repo)
    token = _get_token() or None
    paths = ProjectPaths.from_root(PROJECT_ROOT)

    rel = (
        _fetch_release_by_tag(repo, args.tag, token)
        if args.tag and args.tag != "latest"
        else _fetch_latest_data_release(repo, token, include_prereleases=args.include_prereleases)
    )
    if rel is None:
        print("[info] No prior data-v* GitHub Release found; clean bootstrap will be used.")
        return 0

    print(f"Hydrating base state from release '{rel['tag_name']}'...")
    want = {"biorxiv_medrxiv_preprints.csv.gz", "caches.tar.gz", "rag_corpus.jsonl.gz", "sha256sums.txt"}
    if args.rag_only:
        want = {"caches.tar.gz", "rag_corpus.jsonl.gz", "sha256sums.txt"}

    with tempfile.TemporaryDirectory() as td:
        dl_dir = Path(td)
        _download_release_assets(rel, dl_dir, only_names=want, token=token)
        restored = hydrate_base_state_from_dir(dl_dir, paths)
    print(json.dumps({"release_tag": rel["tag_name"], "restored": restored}, indent=2))
    return 0


def cmd_hydrate_web(args: argparse.Namespace) -> int:
    repo = _get_repo(args.repo)
    token = _get_token() or None
    ledger = load_releases_ledger(args.ledger)
    tag = (args.tag or ledger.get("active_release_tag") or "").strip()

    if not tag:
        if all((args.web_parquet_dir / f).is_file() for f in WEB_PARQUET_FILENAMES):
            print("[ok] No active_release_tag in releases.json yet; using existing local web/data/parquet/.")
            return 0
        print("ERROR: No active_release_tag configured and local web/data/parquet/ is incomplete.", file=sys.stderr)
        return 1

    rel = _fetch_release_by_tag(repo, tag, token)
    if rel is None:
        if args.allow_local_fallback and all((args.web_parquet_dir / f).is_file() for f in WEB_PARQUET_FILENAMES):
            print(f"[warn] GitHub Release '{tag}' not found yet; falling back to local web/data/parquet/.")
            return 0
        print(f"ERROR: GitHub Release '{tag}' not found in {repo}.", file=sys.stderr)
        return 1

    want = set(WEB_PARQUET_FILENAMES) | {"sha256sums.txt"}
    with tempfile.TemporaryDirectory() as td:
        dl_dir = Path(td)
        _download_release_assets(rel, dl_dir, only_names=want, token=token)
        copied = hydrate_web_parquet_from_dir(dl_dir, args.web_parquet_dir)
    print(json.dumps({"release_tag": tag, "hydrated_web_parquet": copied}, indent=2))
    return 0


def cmd_record_ledger(args: argparse.Namespace) -> int:
    ledger = update_releases_ledger(
        ledger_path=args.ledger,
        tag=args.tag,
        dashboard_json_path=args.dashboard_json,
        web_parquet_dir=args.web_parquet_dir,
        gates_passed=not args.gates_failed,
        set_active=not args.no_set_active,
    )
    print(json.dumps({"active_release_tag": ledger["active_release_tag"], "total_releases": len(ledger["releases"])}, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    token = _get_token()
    if not token:
        print("ERROR: Set GITHUB_TOKEN with `contents: write` scope.", file=sys.stderr)
        return 2
    repo = _get_repo(args.repo)
    ledger = load_releases_ledger(args.ledger)
    tag = (args.tag or ledger.get("active_release_tag") or "").strip()
    if not tag:
        print("[skip] No active_release_tag to promote.")
        return 0
    rel = _fetch_release_by_tag(repo, tag, token)
    if rel is None:
        print(f"[skip] Release '{tag}' does not exist in {repo}.")
        return 0
    if not rel.get("prerelease"):
        print(f"[ok] Release '{tag}' is already a full release.")
        return 0
    resp = requests.patch(
        f"{GH_API}/repos/{repo}/releases/{rel['id']}",
        headers=_gh_headers(token),
        json={"prerelease": False, "make_latest": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"[promoted] Release '{tag}' is now the Latest Release.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage monthly GitHub Releases for Bio&Med-Rxiv analytics.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pkg = sub.add_parser("package", help="Stage release assets and SHA-256 checksums.")
    p_pkg.add_argument("--stage-dir", type=Path, required=True)
    p_pkg.add_argument("--web-parquet-dir", type=Path, default=PROJECT_ROOT / "web" / "data" / "parquet")
    p_pkg.add_argument("--no-raw-csv", action="store_true")
    p_pkg.add_argument("--no-caches", action="store_true")
    p_pkg.set_defaults(func=cmd_package)

    p_pub = sub.add_parser("publish", help="Upload staged assets to a GitHub Release.")
    p_pub.add_argument("--tag", required=True)
    p_pub.add_argument("--stage-dir", type=Path, required=True)
    p_pub.add_argument("--repo", default=None)
    p_pub.add_argument("--prerelease", action="store_true")
    p_pub.add_argument("--notes-file", type=Path, default=None)
    p_pub.set_defaults(func=cmd_publish)

    p_hb = sub.add_parser("hydrate-base", help="Download prior release raw CSV & caches for incremental CI.")
    p_hb.add_argument("--tag", default="latest")
    p_hb.add_argument("--repo", default=None)
    p_hb.add_argument("--include-prereleases", action="store_true")
    p_hb.add_argument("--rag-only", action="store_true", help="Only download caches.tar.gz + rag_corpus.jsonl.gz")
    p_hb.set_defaults(func=cmd_hydrate_base)

    p_hw = sub.add_parser("hydrate-web", help="Download active release Web Parquet files for GitHub Pages.")
    p_hw.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "web" / "data" / "releases.json")
    p_hw.add_argument("--web-parquet-dir", type=Path, default=PROJECT_ROOT / "web" / "data" / "parquet")
    p_hw.add_argument("--tag", default=None)
    p_hw.add_argument("--repo", default=None)
    p_hw.add_argument("--allow-local-fallback", action="store_true")
    p_hw.set_defaults(func=cmd_hydrate_web)

    p_rec = sub.add_parser("record-ledger", help="Record release metadata into web/data/releases.json.")
    p_rec.add_argument("--tag", required=True)
    p_rec.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "web" / "data" / "releases.json")
    p_rec.add_argument("--dashboard-json", type=Path, default=PROJECT_ROOT / "web" / "data" / "dashboard_data.json")
    p_rec.add_argument("--web-parquet-dir", type=Path, default=PROJECT_ROOT / "web" / "data" / "parquet")
    p_rec.add_argument("--gates-failed", action="store_true")
    p_rec.add_argument("--no-set-active", action="store_true")
    p_rec.set_defaults(func=cmd_record_ledger)

    p_pro = sub.add_parser("promote", help="Promote prerelease tag to Latest Release on PR merge.")
    p_pro.add_argument("--tag", default=None)
    p_pro.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "web" / "data" / "releases.json")
    p_pro.add_argument("--repo", default=None)
    p_pro.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
