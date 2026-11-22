"""Local FastAPI web server for the DuckDB + Gemini/Ollama Q&A interface.

Keeps ``web/`` purely static for GitHub Pages while serving:
- ``/``            -> Interactive Q&A + Direct SQL Playground UI (``app/index.html``)
- ``/dashboard``   -> Local mirror of the static Plotly dashboard (``web/``)
- ``/api/health``  -> Provider availability & DuckDB table stats
- ``/api/schema``  -> DuckDB view schemas
- ``/api/ask``     -> Grounded natural-language Q&A (Gemini Interactions API / Ollama)
- ``/api/sql``     -> Direct read-only DuckDB SQL execution
- ``/api/search``  -> Fast keyword search across 449k+ preprints
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from biomedrxiv.config import ProjectPaths
from biomedrxiv.qa_engine import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    BioMedRxivAssistant,
    DuckDBQueryEngine,
    check_ollama_available,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=2000)
    provider: str = Field(default="auto", pattern="^(auto|gemini|ollama)$")
    model: str | None = None
    previous_interaction_id: str | None = None


class SqlRequest(BaseModel):
    sql: str = Field(..., min_length=6, max_length=10000)
    max_rows: int = Field(default=100, ge=1, le=200)


def create_app(
    paths: ProjectPaths | None = None,
    assistant: BioMedRxivAssistant | None = None,
) -> FastAPI:
    """Create and configure the local Q&A FastAPI application."""
    resolved_paths = paths or ProjectPaths.from_root()
    engine = assistant.engine if assistant is not None else DuckDBQueryEngine(resolved_paths)
    qa_assistant = assistant or BioMedRxivAssistant(engine=engine)

    app = FastAPI(
        title="BioRxiv & MedRxiv DuckDB + LLM Analyst",
        version="0.1.0",
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        schema = engine.get_schema_summary()
        ollama_ok, ollama_models = check_ollama_available(qa_assistant.ollama_host)
        return {
            "status": "ok",
            "tables": {k: v["row_count"] for k, v in schema.items()},
            "providers": {
                "gemini": {
                    "configured": bool(
                        qa_assistant.gemini_api_key or qa_assistant._gemini_client is not None
                    ),
                    "default_model": qa_assistant.gemini_model or DEFAULT_GEMINI_MODEL,
                },
                "ollama": {
                    "reachable": ollama_ok,
                    "host": qa_assistant.ollama_host or DEFAULT_OLLAMA_HOST,
                    "default_model": qa_assistant.ollama_model or DEFAULT_OLLAMA_MODEL,
                    "installed_models": ollama_models[:10],
                },
            },
        }

    @app.get("/api/schema")
    def get_schema() -> dict[str, Any]:
        return {"tables": engine.get_schema_summary()}

    @app.post("/api/sql")
    def run_sql(req: SqlRequest) -> dict[str, Any]:
        try:
            return engine.run_sql(req.sql, max_rows=req.max_rows)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"SQL execution error: {exc}") from exc

    @app.get("/api/search")
    def search_papers(
        q: str = Query(..., min_length=2),
        server: str | None = Query(default=None),
        year_start: int | None = Query(default=None),
        year_end: int | None = Query(default=None),
        published_only: bool = Query(default=False),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return engine.search_papers(
            query=q,
            server=server,
            year_start=year_start,
            year_end=year_end,
            published_only=published_only,
            limit=limit,
        )

    @app.post("/api/ask")
    def ask_question(req: AskRequest) -> dict[str, Any]:
        try:
            result = qa_assistant.ask(
                req.question,
                provider=req.provider,
                model=req.model,
                previous_interaction_id=req.previous_interaction_id,
            )
            return asdict(result)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Assistant error: {exc}") from exc

    # Serve the local Q&A UI at / and the static Plotly dashboard at /dashboard
    app_ui = resolved_paths.root / "app" / "index.html"
    if app_ui.exists():

        @app.get("/", include_in_schema=False)
        def serve_home() -> FileResponse:
            return FileResponse(app_ui)

    web_dir: Path = resolved_paths.root / "web"
    if web_dir.is_dir():

        @app.get("/dashboard", include_in_schema=False)
        def redirect_dashboard() -> RedirectResponse:
            return RedirectResponse(url="/dashboard/")

        app.mount("/dashboard", StaticFiles(directory=str(web_dir), html=True), name="dashboard")

    return app
