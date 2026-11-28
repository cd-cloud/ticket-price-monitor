"""Optional HTML/text cleanup helpers for noisy flight result pages.

Trafilatura is used when it is installed. The fallback intentionally stays
small and dependency-free so the main tracker can still run without optional
AI/HTML-cleaning packages.
"""

from __future__ import annotations

import html
import importlib.util
import re
from typing import Any


def cleaner_status() -> dict[str, Any]:
    available = importlib.util.find_spec("trafilatura") is not None
    return {
        "available": available,
        "engine": "trafilatura" if available else "fallback",
    }


def clean_html_text(source: str, *, url: str | None = None) -> dict[str, Any]:
    raw = str(source or "")
    reason = ""

    if importlib.util.find_spec("trafilatura") is not None:
        try:
            import trafilatura

            extracted = trafilatura.extract(
                raw,
                url=url,
                include_tables=True,
                include_comments=False,
                favor_precision=True,
            )
            cleaned = normalize_text(extracted or "")
            if cleaned:
                return {
                    "available": True,
                    "engine": "trafilatura",
                    "text": cleaned,
                    "length": len(cleaned),
                }
        except Exception as exc:
            reason = str(exc)

    cleaned = normalize_text(strip_html(raw))
    return {
        "available": False,
        "engine": "fallback",
        "reason": reason or "trafilatura is not installed or produced no text",
        "text": cleaned,
        "length": len(cleaned),
    }


def strip_html(source: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", source or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|section|article|header|footer)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def normalize_text(text: str) -> str:
    lines = []
    for line in str(text or "").replace("\xa0", " ").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)
