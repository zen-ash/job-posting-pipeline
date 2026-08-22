"""Digest filtering: config-driven keyword rules, loaded from filters.yml.

Deliberately dumb (whole-word/phrase, case-insensitive matching) — this is the
"SQL-style" deterministic filter step. LLM-based relevance scoring is later
and separate (see README "what's next"). Every list lives in filters.yml, not
here, so tuning the filter never means touching code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_FILTERS_PATH = "filters.yml"


@dataclass(frozen=True, slots=True)
class Filters:
    title_include_keywords: tuple[str, ...]
    title_exclude_keywords: tuple[str, ...]
    location_include_keywords: tuple[str, ...]


def load_filters(path: str | Path = DEFAULT_FILTERS_PATH) -> Filters:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    return Filters(
        title_include_keywords=tuple(raw.get("title_include_keywords") or []),
        title_exclude_keywords=tuple(raw.get("title_exclude_keywords") or []),
        location_include_keywords=tuple(raw.get("location_include_keywords") or []),
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word/phrase, case-insensitive match of any keyword in `text`.

    Word-boundary (not plain substring) matching on purpose: a bare substring
    check would match "us" inside "Russia" or "Australia", and "sql" inside
    "MySQL". \\b handles both single words and multi-word phrases the same way.
    """
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(kw.lower())}\b", lowered) for kw in keywords)


def matches_title(title: str, filters: Filters) -> bool:
    if not _contains_any(title, filters.title_include_keywords):
        return False
    return not _contains_any(title, filters.title_exclude_keywords)


def matches_location(location: str | None, filters: Filters) -> bool:
    if not location:
        return False
    return _contains_any(location, filters.location_include_keywords)


def matches(title: str, location: str | None, filters: Filters) -> bool:
    return matches_title(title, filters) and matches_location(location, filters)
