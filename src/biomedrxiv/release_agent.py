"""Release-digest generator with optional Gemini API agentic commentary & triage.

Always generates a deterministic, auditable Markdown report (suitable as a
GitHub Pull Request body or release changelog). When ``GEMINI_API_KEY`` is
provided and ``use_gemini=True``, appends a concise executive summary (or
automated failure triage if any quality gate failed) using the ``google-genai``
Interactions API (``gemini-4-hydrogen``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from biomedrxiv.qa_engine import DEFAULT_GEMINI_MODEL
from biomedrxiv.quality_gates import QualityReport

logger = logging.getLogger(__name__)


def _status_badge(status: str) -> str:
    return {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}.get(status, status)


def render_deterministic_markdown(report: QualityReport) -> str:
    """Render a clean GitHub-flavored Markdown scorecard & diff table."""
    overall = "PASSED" if report.passed else "FAILED"
    lines = [
        f"## Automated Dataset Update & Quality Gate Report — **{overall}**",
        "",
        f"- **Checked at (UTC):** `{report.checked_at_utc}`",
        f"- **Checks:** {len(report.checks)} total | **{report.fail_count}** failed | **{report.warn_count}** warnings",
        "",
        "### Quality Gate Scorecard",
        "",
        "| Check | Status | Details |",
        "| :--- | :---: | :--- |",
    ]
    for chk in report.checks:
        lines.append(f"| `{chk.name}` | **{_status_badge(chk.status)}** | {chk.message} |")

    if report.diff:
        d = report.diff
        lines.extend(
            [
                "",
                "### Snapshot Delta vs. Baseline",
                "",
                f"- **Version:** `{d.get('baseline_version') or 'previous'}` → `{d.get('candidate_version') or 'current'}`",
                f"- **Latest Posting Date:** `{d.get('date_max_before') or '—'}` → `{d.get('date_max_after') or '—'}`",
                "",
                "| Metric | Before | After | Delta |",
                "| :--- | ---: | ---: | ---: |",
            ]
        )
        for key, m in (d.get("metrics") or {}).items():
            before = m.get("before")
            after = m.get("after")
            delta = m.get("delta")
            b_str = f"{before:,}" if isinstance(before, int) else (str(before) if before is not None else "—")
            a_str = f"{after:,}" if isinstance(after, int) else (str(after) if after is not None else "—")
            if isinstance(delta, int):
                d_str = f"{delta:+,}"
            elif isinstance(delta, float):
                d_str = f"{delta:+.2f}"
            else:
                d_str = "—"
            lines.append(f"| `{key}` | {b_str} | {a_str} | {d_str} |")

        frontiers = d.get("frontier_deltas") or []
        if frontiers:
            lines.extend(["", "### Fastest-Growing Tracked Frontiers Since Last Release", ""])
            for f in frontiers:
                lines.append(f"- **{f['topic']}**: {f['before']:,} → {f['after']:,} (`{f['delta']:+,}`)")

    return "\n".join(lines) + "\n"


def generate_agent_commentary(
    report: QualityReport,
    *,
    gemini_api_key: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    gemini_client: Any = None,
) -> str | None:
    """Use Gemini Interactions API to produce an executive digest or failure triage."""
    api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_client is None and not api_key:
        logger.info("GEMINI_API_KEY not set; skipping AI release commentary.")
        return None

    try:
        if gemini_client is None:
            from google import genai

            gemini_client = genai.Client(api_key=api_key)

        payload_json = json.dumps(report.to_dict(), indent=2)
        if report.passed:
            prompt = (
                "You are a senior biomedical data engineer reviewing an automated Kaggle dataset "
                "update Pull Request for the BioRxiv & MedRxiv analytics pipeline.\n\n"
                "Based ONLY on this JSON quality report and snapshot diff, write a concise 3-5 bullet "
                "executive 'What Changed' summary suitable for a GitHub PR reviewer. Highlight notable "
                "volume/publication/clinical-trial changes and any warnings. Do not invent numbers.\n\n"
                f"```json\n{payload_json}\n```"
            )
        else:
            prompt = (
                "You are a senior data reliability engineer triaging a FAILED automated dataset "
                "quality gate for the BioRxiv & MedRxiv pipeline.\n\n"
                "Based ONLY on this JSON quality report, write a concise triage summary:\n"
                "1. Which gate(s) failed and why.\n"
                "2. Likely upstream cause (e.g. schema drift, partial Kaggle upload, local path leak).\n"
                "3. Exact recommended remediation steps before merging.\n\n"
                f"```json\n{payload_json}\n```"
            )

        interaction = gemini_client.interactions.create(
            model=model,
            input=prompt,
        )
        text = (getattr(interaction, "output_text", None) or "").strip()
        return text or None
    except Exception as exc:
        logger.warning("Gemini commentary generation failed (continuing deterministically): %s", exc)
        return None


def build_release_digest_markdown(
    report: QualityReport,
    *,
    use_gemini: bool = False,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    gemini_client: Any = None,
) -> str:
    """Build complete PR / release Markdown combining deterministic gates + optional AI digest."""
    base_md = render_deterministic_markdown(report)
    if not use_gemini:
        return base_md

    ai_text = generate_agent_commentary(
        report, model=gemini_model, gemini_client=gemini_client
    )
    if not ai_text:
        return base_md

    heading = (
        "### AI Agent Executive Summary"
        if report.passed
        else "### AI Agent Failure Triage & Remediation"
    )
    return f"{base_md}\n{heading}\n\n{ai_text}\n"
