#!/usr/bin/env python3
"""Launch the local DuckDB + Gemini/Ollama Q&A web app.

Usage:
    export GEMINI_API_KEY="your-key-here"
    .venv/bin/python scripts/run_qa_app.py --port 8000

    # Or use a local Ollama model without any API key:
    .venv/bin/python scripts/run_qa_app.py --ollama-model llama3.2 --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.config import ProjectPaths
from biomedrxiv.qa_engine import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    BioMedRxivAssistant,
    DuckDBQueryEngine,
)
from biomedrxiv.qa_server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start the local DuckDB + Gemini/Ollama Q&A web interface."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument(
        "--gemini-model",
        default=DEFAULT_GEMINI_MODEL,
        help=f"Gemini model ID (default: {DEFAULT_GEMINI_MODEL}).",
    )
    parser.add_argument(
        "--ollama-host",
        default=DEFAULT_OLLAMA_HOST,
        help=f"Local Ollama base URL (default: {DEFAULT_OLLAMA_HOST}).",
    )
    parser.add_argument(
        "--ollama-model",
        default=DEFAULT_OLLAMA_MODEL,
        help=f"Fallback Ollama model name (default: {DEFAULT_OLLAMA_MODEL}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    engine = DuckDBQueryEngine(paths=paths)
    assistant = BioMedRxivAssistant(
        engine=engine,
        gemini_model=args.gemini_model,
        ollama_host=args.ollama_host,
        ollama_model=args.ollama_model,
    )
    app = create_app(paths=paths, assistant=assistant)
    print(f"Starting DuckDB + LLM Q&A App at http://{args.host}:{args.port}/")
    print(f"Visual dashboard also available at http://{args.host}:{args.port}/dashboard/")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
