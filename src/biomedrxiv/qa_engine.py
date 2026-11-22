"""DuckDB analytical engine + Gemini / Ollama tool-calling Q&A assistant.

Security & accuracy principles:
- **Deterministic numbers:** The LLM never invents counts or percentages. All
  quantitative answers are backed by read-only DuckDB SQL executed against the
  materialized Parquet tables (``preprints``, ``clinical_trials``,
  ``downstream_clinical``).
- **Strict SQL sandbox:** Only single-statement ``SELECT`` / ``WITH ... SELECT``
  queries targeting the registered Parquet views are permitted. DDL/DML,
  pragmas, multi-statement queries, and arbitrary filesystem table functions
  (``read_csv``, ``read_parquet``, ``glob``, etc.) are blocked.
- **Provider resilience:** Uses the modern ``google-genai`` Interactions API
  (default ``gemini-4-hydrogen``) when ``GEMINI_API_KEY`` is configured, with
  automatic or explicit fallback to a local Ollama server.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import requests

from biomedrxiv.config import ProjectPaths

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"
MAX_RETURNED_ROWS = 200

FORBIDDEN_SQL_PATTERNS = [
    re.compile(
        r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|"
        r"COPY|EXPORT|IMPORT|PRAGMA|CALL|LOAD|INSTALL|SET|RESET|VACUUM|CHECKPOINT|"
        r"MERGE|GRANT|REVOKE)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(read_csv|read_csv_auto|read_parquet|read_json|read_json_auto|read_text|"
        r"read_blob|glob|parquet_scan|csv_scan|sqlite_scan|postgres_scan|httpfs)\s*\(",
        re.IGNORECASE,
    ),
]


def validate_readonly_sql(sql: str) -> str:
    """Validate and normalize a read-only SELECT/WITH SQL query."""
    if not sql or not sql.strip():
        raise ValueError("SQL query must not be empty.")

    cleaned = sql.strip()
    # Strip markdown code fences if an LLM wrapped the query
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Strip single trailing semicolon, reject embedded semicolons (multi-statement)
    cleaned = cleaned.rstrip(";").strip()
    # Remove single-quoted string literals before checking for semicolons/keywords
    unquoted = re.sub(r"'(?:''|[^'])*'", "''", cleaned)
    if ";" in unquoted:
        raise ValueError("Multiple SQL statements are not permitted.")

    if not re.match(r"^(?:SELECT|WITH)\b", unquoted, re.IGNORECASE):
        raise ValueError("Only read-only SELECT or WITH ... SELECT queries are allowed.")

    for pat in FORBIDDEN_SQL_PATTERNS:
        m = pat.search(unquoted)
        if m:
            raise ValueError(f"Disallowed SQL operation or function detected: '{m.group(0)}'")

    return cleaned


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


class DuckDBQueryEngine:
    """In-memory DuckDB engine exposing materialized Parquet files as read-only views."""

    TABLE_FILES = {
        "preprints": "preprints_canonical.parquet",
        "clinical_trials": "clinical_trials_enriched.parquet",
        "downstream_clinical": "downstream_clinical_impact.parquet",
        "institution_ror_map": "institution_ror_map.parquet",
    }

    def __init__(self, paths: ProjectPaths | None = None) -> None:
        self.paths = paths or ProjectPaths.from_root()
        self.registered_tables: dict[str, Path] = {}
        self._discover_tables()

    def _discover_tables(self) -> None:
        self.registered_tables.clear()
        for view_name, filename in self.TABLE_FILES.items():
            fpath = self.paths.processed_dir / filename
            if fpath.exists():
                self.registered_tables[view_name] = fpath

    def _connect(self) -> duckdb.DuckDBPyConnection:
        self._discover_tables()
        conn = duckdb.connect(database=":memory:")
        for view_name, fpath in self.registered_tables.items():
            escaped = str(fpath).replace("'", "''")
            conn.execute(f"CREATE VIEW {view_name} AS SELECT * FROM read_parquet('{escaped}')")
        return conn

    def get_schema_summary(self) -> dict[str, Any]:
        """Return registered views, row counts, and column types."""
        conn = self._connect()
        try:
            tables: dict[str, Any] = {}
            for view_name in self.registered_tables:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]
                cols = conn.execute(f"DESCRIBE {view_name}").fetchall()
                tables[view_name] = {
                    "row_count": int(cnt),
                    "columns": [{"name": str(c[0]), "type": str(c[1])} for c in cols],
                }
            return tables
        finally:
            conn.close()

    def run_sql(self, sql: str, *, max_rows: int = MAX_RETURNED_ROWS) -> dict[str, Any]:
        """Execute a validated read-only SQL query and return JSON-serializable rows."""
        validated = validate_readonly_sql(sql)
        conn = self._connect()
        try:
            # Wrap in outer LIMIT only if query does not already specify a small limit
            limited_sql = f"SELECT * FROM ({validated}) AS _q LIMIT {int(max_rows) + 1}"
            cursor = conn.execute(limited_sql)
            columns = [desc[0] for desc in cursor.description]
            raw_rows = cursor.fetchall()
            truncated = len(raw_rows) > max_rows
            rows = [
                {col: _json_safe(val) for col, val in zip(columns, row, strict=False)}
                for row in raw_rows[:max_rows]
            ]
            return {
                "sql": validated,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            }
        finally:
            conn.close()

    def search_papers(
        self,
        query: str,
        *,
        server: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        published_only: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Fast parameterized keyword search over preprint titles, categories, and institutions."""
        if "preprints" not in self.registered_tables:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

        limit = max(1, min(int(limit), 100))
        conditions = ["(title ILIKE ? OR category_clean ILIKE ? OR institution_clean ILIKE ?)"]
        like_pat = f"%{query.strip()}%"
        params: list[Any] = [like_pat, like_pat, like_pat]

        if server and server.lower() in {"biorxiv", "medrxiv"}:
            conditions.append("server_clean = ?")
            params.append(server.lower())
        if year_start is not None:
            conditions.append("EXTRACT(YEAR FROM first_posted_date) >= ?")
            params.append(int(year_start))
        if year_end is not None:
            conditions.append("EXTRACT(YEAR FROM first_posted_date) <= ?")
            params.append(int(year_end))
        if published_only:
            conditions.append("snapshot_is_published = TRUE")

        sql = (
            "SELECT normalized_doi, title, server_clean, category_clean, "
            "CAST(first_posted_date AS DATE) AS first_posted_date, num_versions, "
            "author_count, snapshot_is_published, snapshot_published_doi, publisher_name, "
            "institution_clean, doi_link "
            f"FROM preprints WHERE {' AND '.join(conditions)} "
            f"ORDER BY first_posted_date DESC LIMIT {limit}"
        )
        conn = self._connect()
        try:
            cursor = conn.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = [
                {col: _json_safe(val) for col, val in zip(columns, r, strict=False)}
                for r in cursor.fetchall()
            ]
            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": False,
            }
        finally:
            conn.close()


def _build_system_instruction(schema: dict[str, Any]) -> str:
    schema_lines: list[str] = []
    for table_name, info in schema.items():
        cols = ", ".join(f"{c['name']} ({c['type']})" for c in info["columns"])
        schema_lines.append(f"- Table `{table_name}` ({info['row_count']:,} rows): {cols}")
    schema_block = "\n".join(schema_lines) if schema_lines else "(No tables materialized yet)"

    return (
        "You are a senior biomedical data analyst answering questions about the BioRxiv & MedRxiv "
        "preprint dataset, peer-reviewed journal outcomes, and linked clinical trials.\n\n"
        "AVAILABLE DUCKDB VIEWS:\n"
        f"{schema_block}\n\n"
        "RULES:\n"
        "1. ALWAYS call `run_sql_query` (or `search_papers`) to compute exact numbers — never guess statistics.\n"
        "2. `preprints` is ALREADY deduplicated to 1 row per unique DOI. Use `first_posted_date` for cohort years.\n"
        "3. Remember right-censoring: preprints posted in 2025–2026 have lower snapshot publication rates because peer review takes 6–18+ months. Mention this caveat when comparing recent years.\n"
        "4. `server_clean` values are lowercase: `'biorxiv'` and `'medrxiv'`. `category_clean` is lowercase.\n"
        "5. Join `preprints` with `clinical_trials` on `normalized_doi`, and with `downstream_clinical` on `preprints.snapshot_published_doi = downstream_clinical.published_doi`.\n"
        "6. Keep answers clear, concise, and grounded in the exact returned rows. Format numbers with commas and 1–2 decimal places for percentages."
    )


GEMINI_TOOLS = [
    {
        "type": "function",
        "name": "run_sql_query",
        "description": (
            "Execute a read-only DuckDB SELECT query against the registered views "
            "(`preprints`, `clinical_trials`, `downstream_clinical`) and return up to 200 rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Valid read-only DuckDB SQL SELECT or WITH ... SELECT statement.",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "type": "function",
        "name": "search_papers",
        "description": (
            "Search preprint titles, categories, and institutions by keyword with optional filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for."},
                "server": {
                    "type": "string",
                    "enum": ["biorxiv", "medrxiv"],
                    "description": "Optional server filter.",
                },
                "year_start": {"type": "integer", "description": "Optional start year (inclusive)."},
                "year_end": {"type": "integer", "description": "Optional end year (inclusive)."},
                "published_only": {
                    "type": "boolean",
                    "description": "Only return preprints linked to a peer-reviewed journal DOI.",
                },
                "limit": {"type": "integer", "description": "Max papers to return (default 15)."},
            },
            "required": ["query"],
        },
    },
]


@dataclass
class QAResponse:
    """Structured answer returned by ``BioMedRxivAssistant.ask``."""

    answer: str
    provider: str
    model: str
    sql_queries: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    interaction_id: str | None = None


def check_ollama_available(host: str = DEFAULT_OLLAMA_HOST, timeout: float = 1.5) -> tuple[bool, list[str]]:
    """Probe whether a local Ollama server is reachable and list installed model names."""
    try:
        resp = requests.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        if resp.status_code == 200:
            models = [m.get("name", "") for m in (resp.json().get("models") or []) if m.get("name")]
            return True, models
    except requests.RequestException:
        pass
    return False, []


class BioMedRxivAssistant:
    """Tool-calling Q&A assistant backed by DuckDB + Gemini API (with Ollama fallback)."""

    def __init__(
        self,
        engine: DuckDBQueryEngine,
        *,
        gemini_api_key: str | None = None,
        gemini_model: str = DEFAULT_GEMINI_MODEL,
        ollama_host: str = DEFAULT_OLLAMA_HOST,
        ollama_model: str = DEFAULT_OLLAMA_MODEL,
        gemini_client: Any = None,
    ) -> None:
        self.engine = engine
        self.gemini_api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.gemini_model = gemini_model
        self.ollama_host = ollama_host
        self.ollama_model = ollama_model
        self._gemini_client = gemini_client

    def _get_gemini_client(self) -> Any:
        if self._gemini_client is not None:
            return self._gemini_client
        if not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        from google import genai

        self._gemini_client = genai.Client(api_key=self.gemini_api_key)
        return self._gemini_client

    def _execute_tool(
        self, name: str, arguments: dict[str, Any], sql_log: list[str], table_log: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            if name == "run_sql_query":
                sql = str(arguments.get("sql", ""))
                res = self.engine.run_sql(sql)
                sql_log.append(res["sql"])
                table_log.append(res)
                return res
            if name == "search_papers":
                res = self.engine.search_papers(
                    query=str(arguments.get("query", "")),
                    server=arguments.get("server"),
                    year_start=arguments.get("year_start"),
                    year_end=arguments.get("year_end"),
                    published_only=bool(arguments.get("published_only", False)),
                    limit=int(arguments.get("limit", 15)),
                )
                table_log.append(res)
                return res
            return {"error": f"Unknown tool: {name}"}
        except Exception as exc:
            logger.warning("Tool %s failed: %s", name, exc)
            return {"error": str(exc)}

    def ask_gemini(
        self,
        question: str,
        *,
        model: str | None = None,
        previous_interaction_id: str | None = None,
        max_tool_rounds: int = 4,
    ) -> QAResponse:
        """Answer using Gemini Interactions API with multi-turn DuckDB function calling."""
        client = self._get_gemini_client()
        model_name = model or self.gemini_model
        schema = self.engine.get_schema_summary()
        system_instruction = _build_system_instruction(schema)

        sql_log: list[str] = []
        table_log: list[dict[str, Any]] = []

        create_kwargs: dict[str, Any] = {
            "model": model_name,
            "input": question,
            "system_instruction": system_instruction,
            "tools": GEMINI_TOOLS,
        }
        if previous_interaction_id:
            create_kwargs["previous_interaction_id"] = previous_interaction_id

        interaction = client.interactions.create(**create_kwargs)

        for _round in range(max_tool_rounds):
            steps = getattr(interaction, "steps", None) or []
            fn_calls = [s for s in steps if getattr(s, "type", None) == "function_call"]
            if not fn_calls:
                break

            tool_results = []
            for call in fn_calls:
                raw_args = getattr(call, "arguments", None) or {}
                if isinstance(raw_args, str):
                    raw_args = json.loads(raw_args)
                result_obj = self._execute_tool(call.name, raw_args, sql_log, table_log)
                tool_results.append(
                    {
                        "type": "function_result",
                        "call_id": call.id,
                        "name": call.name,
                        "result": json.dumps(result_obj, default=str),
                    }
                )

            interaction = client.interactions.create(
                model=model_name,
                input=tool_results,
                previous_interaction_id=interaction.id,
                system_instruction=system_instruction,
                tools=GEMINI_TOOLS,
            )

        answer_text = getattr(interaction, "output_text", None) or ""
        return QAResponse(
            answer=answer_text.strip() or "No text response generated.",
            provider="gemini",
            model=model_name,
            sql_queries=sql_log,
            tables=table_log,
            interaction_id=getattr(interaction, "id", None),
        )

    def ask_ollama(self, question: str, *, model: str | None = None) -> QAResponse:
        """Two-step grounded Q&A using a local Ollama model (generate SQL -> run DuckDB -> summarize)."""
        model_name = model or self.ollama_model
        schema = self.engine.get_schema_summary()
        sys_prompt = _build_system_instruction(schema)

        sql_prompt = (
            f"Question: {question}\n\n"
            "Write ONE single read-only DuckDB SQL SELECT query to answer this question. "
            "Return ONLY the SQL query (no explanation, no prose)."
        )
        resp1 = requests.post(
            f"{self.ollama_host.rstrip('/')}/api/chat",
            json={
                "model": model_name,
                "stream": False,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": sql_prompt},
                ],
            },
            timeout=90,
        )
        resp1.raise_for_status()
        raw_sql = (resp1.json().get("message") or {}).get("content", "").strip()

        sql_log: list[str] = []
        table_log: list[dict[str, Any]] = []
        query_result = self._execute_tool("run_sql_query", {"sql": raw_sql}, sql_log, table_log)

        summary_prompt = (
            f"Original question: {question}\n\n"
            f"Executed DuckDB SQL:\n```sql\n{raw_sql}\n```\n\n"
            f"Query result JSON:\n{json.dumps(query_result, default=str)[:12000]}\n\n"
            "Provide a clear, accurate, concise answer grounded strictly in these results."
        )
        resp2 = requests.post(
            f"{self.ollama_host.rstrip('/')}/api/chat",
            json={
                "model": model_name,
                "stream": False,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": summary_prompt},
                ],
            },
            timeout=90,
        )
        resp2.raise_for_status()
        answer = (resp2.json().get("message") or {}).get("content", "").strip()
        return QAResponse(
            answer=answer,
            provider="ollama",
            model=model_name,
            sql_queries=sql_log,
            tables=table_log,
        )

    def ask(
        self,
        question: str,
        *,
        provider: str = "auto",
        model: str | None = None,
        previous_interaction_id: str | None = None,
    ) -> QAResponse:
        """Route a question to Gemini or Ollama (with automatic provider selection)."""
        chosen = provider.lower().strip()
        if chosen == "gemini":
            return self.ask_gemini(
                question, model=model, previous_interaction_id=previous_interaction_id
            )
        if chosen == "ollama":
            return self.ask_ollama(question, model=model)

        # Auto mode: prefer Gemini when API key is present, otherwise try local Ollama
        if self.gemini_api_key or self._gemini_client is not None:
            return self.ask_gemini(
                question, model=model, previous_interaction_id=previous_interaction_id
            )

        ollama_ok, ollama_models = check_ollama_available(self.ollama_host)
        if ollama_ok:
            chosen_model = model or (ollama_models[0] if ollama_models else self.ollama_model)
            return self.ask_ollama(question, model=chosen_model)

        raise RuntimeError(
            "No LLM provider configured. Set GEMINI_API_KEY in your environment for Gemini "
            f"({DEFAULT_GEMINI_MODEL}) or start a local Ollama server at {self.ollama_host}. "
            "You can still use the Direct SQL Playground and Paper Search tabs offline."
        )
