#!/usr/bin/env python3
"""Zero-Node Cloudflare Vectorize v2 uploader for BioRxiv & MedRxiv RAG corpus.

Embeds `data/processed/rag_corpus.jsonl` via Cloudflare Workers AI
(`@cf/baai/bge-small-en-v1.5`, 384 dims, ~1,841 Neurons per 1M tokens) and
upserts NDJSON batches directly into Cloudflare Vectorize v2 via REST API.

Required environment variables (not needed for --dry-run):
  CLOUDFLARE_ACCOUNT_ID  — 32-char hex Account ID (visible in dash.cloudflare.com URL)
  CLOUDFLARE_API_TOKEN   — API Token with permissions:
                             Account -> Workers AI -> Read/Edit
                             Account -> Vectorize -> Edit

Usage:
  # 1. Inspect token/Neuron/dimension budget without network calls:
  .venv/bin/python scripts/upload_vectorize.py --dry-run

  # 2. Smoke-test with 100 docs first:
  export CLOUDFLARE_ACCOUNT_ID="..."
  export CLOUDFLARE_API_TOKEN="..."
  .venv/bin/python scripts/upload_vectorize.py --ensure-index --limit 100

  # 3. Upload full 12,500-doc corpus (resumable via local embedding cache):
  .venv/bin/python scripts/upload_vectorize.py --ensure-index
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = PROJECT_ROOT / "data" / "processed" / "rag_corpus.jsonl"
DEFAULT_CACHE = PROJECT_ROOT / "data" / "cache" / "rag_embeddings_bge_small.jsonl"

EMBEDDING_MODEL = "@cf/baai/bge-small-en-v1.5"
EMBEDDING_DIMS = 384
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def load_corpus(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            docs.append(json.loads(line))
            if limit is not None and len(docs) >= limit:
                break
    return docs


def load_embedding_cache(cache_path: Path) -> dict[str, list[float]]:
    cache: dict[str, list[float]] = {}
    if not cache_path.exists():
        return cache
    with cache_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj.get("id"), str) and isinstance(obj.get("values"), list):
                    cache[obj["id"]] = obj["values"]
            except json.JSONDecodeError:
                continue
    return cache


def cf_headers(api_token: str, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": content_type,
    }


def ensure_vectorize_index(account_id: str, api_token: str, index_name: str) -> None:
    """Create the Vectorize v2 index (384 dims, cosine) if absent, plus metadata indexes."""
    base = f"{CF_API_BASE}/accounts/{account_id}/vectorize/v2/indexes"
    get_resp = requests.get(f"{base}/{index_name}", headers=cf_headers(api_token), timeout=30)
    if get_resp.status_code == 200 and get_resp.json().get("success"):
        print(f"[ok] Vectorize index '{index_name}' already exists.")
    else:
        payload = {
            "name": index_name,
            "description": "BioRxiv & MedRxiv clinical trials, NIH iCite papers, and top preprints (bge-small-en-v1.5)",
            "config": {
                "dimensions": EMBEDDING_DIMS,
                "metric": "cosine",
            },
        }
        create_resp = requests.post(base, headers=cf_headers(api_token), json=payload, timeout=30)
        data = create_resp.json()
        if not create_resp.ok or not data.get("success"):
            # Handle already-exists gracefully
            errs = str(data.get("errors", []))
            if "already exists" not in errs.lower():
                raise RuntimeError(f"Failed to create Vectorize index '{index_name}': HTTP {create_resp.status_code} {data}")
        print(f"[created] Vectorize index '{index_name}' ({EMBEDDING_DIMS} dims, cosine).")

    # Create metadata indexes for optional server/doc_type/year filtering
    for prop_name, prop_type in [("doc_type", "string"), ("server", "string"), ("year", "number")]:
        mi_url = f"{base}/{index_name}/metadata_index/create"
        mi_resp = requests.post(
            mi_url,
            headers=cf_headers(api_token),
            json={"propertyName": prop_name, "indexType": prop_type},
            timeout=30,
        )
        if mi_resp.ok:
            print(f"[ok] Metadata index '{prop_name}' ({prop_type}) ensured.")
        else:
            body_text = mi_resp.text
            if "already" in body_text.lower():
                print(f"[ok] Metadata index '{prop_name}' already exists.")
            else:
                print(f"[warn] Metadata index '{prop_name}' returned HTTP {mi_resp.status_code}: {body_text[:200]}")


def embed_batch_workers_ai(
    account_id: str,
    api_token: str,
    texts: list[str],
    max_retries: int = 4,
) -> list[list[float]]:
    url = f"{CF_API_BASE}/accounts/{account_id}/ai/run/{EMBEDDING_MODEL}"
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            url,
            headers=cf_headers(api_token),
            json={"text": texts},
            timeout=90,
        )
        if resp.status_code == 200:
            payload = resp.json()
            vectors = (payload.get("result") or {}).get("data")
            if isinstance(vectors, list) and len(vectors) == len(texts):
                return vectors
            raise RuntimeError(f"Unexpected Workers AI embedding response shape: {str(payload)[:300]}")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            wait = 2.0 * attempt
            print(f"[retry {attempt}/{max_retries}] Workers AI HTTP {resp.status_code}, waiting {wait:.0f}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Workers AI embedding failed: HTTP {resp.status_code} {resp.text[:300]}")
    raise RuntimeError("Exhausted retries for Workers AI embedding.")


def upsert_batch_vectorize(
    account_id: str,
    api_token: str,
    index_name: str,
    ndjson_lines: list[str],
    max_retries: int = 4,
) -> str:
    url = f"{CF_API_BASE}/accounts/{account_id}/vectorize/v2/indexes/{index_name}/upsert"
    body = ("\n".join(ndjson_lines) + "\n").encode("utf-8")
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            url,
            headers=cf_headers(api_token, content_type="application/x-ndjson"),
            data=body,
            timeout=90,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return str((data.get("result") or {}).get("mutationId") or "ok")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            wait = 2.5 * attempt
            print(f"[retry {attempt}/{max_retries}] Vectorize upsert HTTP {resp.status_code}, waiting {wait:.0f}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Vectorize upsert failed: HTTP {resp.status_code} {resp.text[:300]}")
    raise RuntimeError("Exhausted retries for Vectorize upsert.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Embed and upload RAG corpus to Cloudflare Vectorize v2.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="Path to rag_corpus.jsonl")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="Local embedding cache JSONL")
    parser.add_argument("--index-name", default="biomedrxiv-rag", help="Vectorize index name (default: biomedrxiv-rag)")
    parser.add_argument("--embed-batch-size", type=int, default=80, help="Texts per Workers AI embed call (default: 80)")
    parser.add_argument("--upsert-batch-size", type=int, default=400, help="Vectors per Vectorize NDJSON upsert (default: 400)")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap for smoke testing (e.g., 100)")
    parser.add_argument("--ensure-index", action="store_true", help="Create Vectorize index + metadata indexes if absent")
    parser.add_argument("--dry-run", action="store_true", help="Report budget estimates without network calls")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between API calls in seconds")
    args = parser.parse_args()

    if not args.corpus.exists():
        print(f"ERROR: Corpus file not found at {args.corpus}. Run scripts/build_rag_corpus.py first.", file=sys.stderr)
        return 1

    docs = load_corpus(args.corpus, limit=args.limit)
    cache = load_embedding_cache(args.cache)
    to_embed = [d for d in docs if d["id"] not in cache]

    est_tokens = sum(len(d["text"]) for d in to_embed) // 4
    est_neurons = (est_tokens / 1_000_000.0) * 1841.0
    stored_dims = len(docs) * EMBEDDING_DIMS

    summary = {
        "index_name": args.index_name,
        "docs_selected": len(docs),
        "already_cached_embeddings": len(docs) - len(to_embed),
        "new_embeddings_needed": len(to_embed),
        "estimated_new_tokens": est_tokens,
        "estimated_new_neurons": round(est_neurons, 1),
        "daily_free_neurons_pct": round((est_neurons / 10_000.0) * 100.0, 2),
        "stored_vector_dimensions": stored_dims,
        "free_vectorize_cap_pct": round((stored_dims / 5_000_000.0) * 100.0, 2),
    }
    print(json.dumps(summary, indent=2))
    if args.dry_run:
        return 0

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not api_token:
        print(
            "ERROR: Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN environment variables first.",
            file=sys.stderr,
        )
        return 2

    if args.ensure_index:
        ensure_vectorize_index(account_id, api_token, args.index_name)

    # 1. Embed missing documents (persisting incrementally to cache)
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    if to_embed:
        print(f"Embedding {len(to_embed):,} documents via {EMBEDDING_MODEL}...")
        with args.cache.open("a", encoding="utf-8") as cf:
            for start in range(0, len(to_embed), args.embed_batch_size):
                batch = to_embed[start : start + args.embed_batch_size]
                vectors = embed_batch_workers_ai(
                    account_id,
                    api_token,
                    [d["text"] for d in batch],
                )
                for doc, vec in zip(batch, vectors):
                    cache[doc["id"]] = vec
                    cf.write(json.dumps({"id": doc["id"], "values": vec}) + "\n")
                cf.flush()
                done = min(start + len(batch), len(to_embed))
                if done % (args.embed_batch_size * 5) == 0 or done == len(to_embed):
                    print(f"  embedded {done:,}/{len(to_embed):,}")
                if args.delay > 0:
                    time.sleep(args.delay)

    # 2. Upsert all selected vectors to Vectorize v2 in NDJSON batches
    print(f"Upserting {len(docs):,} vectors into Vectorize index '{args.index_name}'...")
    for start in range(0, len(docs), args.upsert_batch_size):
        batch = docs[start : start + args.upsert_batch_size]
        ndjson_lines = [
            json.dumps(
                {
                    "id": doc["id"],
                    "values": cache[doc["id"]],
                    "metadata": doc["metadata"],
                },
                ensure_ascii=False,
            )
            for doc in batch
        ]
        mut_id = upsert_batch_vectorize(account_id, api_token, args.index_name, ndjson_lines)
        done = min(start + len(batch), len(docs))
        print(f"  upserted {done:,}/{len(docs):,} (mutationId={mut_id})")
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"Done! {len(docs):,} vectors uploaded to Cloudflare Vectorize index '{args.index_name}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
