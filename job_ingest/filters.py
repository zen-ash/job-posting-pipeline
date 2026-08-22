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
    # US state 2-letter codes, matched separately and more strictly than the
    # phrases above — see _contains_state_code.
    location_include_state_codes: tuple[str, ...] = ()


def load_filters(path: str | Path = DEFAULT_FILTERS_PATH) -> Filters:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    return Filters(
        title_include_keywords=tuple(raw.get("title_include_keywords") or []),
        title_exclude_keywords=tuple(raw.get("title_exclude_keywords") or []),
        location_include_keywords=tuple(raw.get("location_include_keywords") or []),
        location_include_state_codes=tuple(raw.get("location_include_state_codes") or []),
    )


def _normalize(text: str) -> str:
    """Lowercase and strip periods, so "U.S." and "US" compare equal, "Sr."
    and "Sr" compare equal, etc. Applied to both the searched text and every
    keyword before matching, so no keyword ever needs a separate
    punctuation-bearing variant to catch the punctuation-free spelling — this
    replaced an earlier fix that instead special-cased the regex boundary for
    individual punctuation-ending keywords (it worked, but only ever grew one
    instance at a time; this kills the whole class up front).
    """
    return text.lower().replace(".", "")


def _find_match(text: str, keywords: tuple[str, ...]) -> str | None:
    """Returns the first keyword (in list order) that matches `text` as a
    whole word/phrase, or None if none do.

    Whole-word/phrase (not plain substring) matching on purpose: a bare
    substring check would match "us" inside "Russia" or "Australia", and
    "sql" inside "MySQL". Uses (?<!\\w)...(?!\\w) rather than \\b\\b: \\b
    requires an actual transition between a word char and a non-word char,
    and if a keyword's last character is non-word (e.g. a keyword ending in
    "(" or ")"), there's no such transition left when the next real character
    is -also- non-word — so a plain \\b silently never matches there. (?!\\w)
    has no such requirement: it only asserts the next character isn't a word
    character, true at end-of-string or before more punctuation.
    """
    normalized = _normalize(text)
    for kw in keywords:
        pattern = rf"(?<!\w){re.escape(_normalize(kw))}(?!\w)"
        if re.search(pattern, normalized):
            return kw
    return None


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return _find_match(text, keywords) is not None


def _contains_state_code(text: str, codes: tuple[str, ...]) -> bool:
    """Matches a US state code only as ", XX" — a comma, optional whitespace,
    then the code exactly as written (case-SENSITIVE) — e.g. "Romeoville, IL".

    Deliberately stricter than _contains_any's plain whole-word matching: a
    case-insensitive bare-word match on a 2-letter code risks colliding with
    common English words a permissive location field could plausibly contain
    ("in", "or", "hi", "me", "de", ...). Comma-anchoring plus requiring the
    exact uppercase form removes that risk, at the cost of missing a state
    code written without a preceding comma (e.g. "US IL - Remote") — that
    specific shape is instead covered by the plain "us" entry in
    location_include_keywords, and spelled-out state names cover the rest of
    the recall this stricter matching gives up.
    """
    return any(re.search(rf",\s*{re.escape(code)}(?!\w)", text) for code in codes)


def title_include_match(title: str, filters: Filters) -> bool:
    return _contains_any(title, filters.title_include_keywords)


def title_exclude_hit(title: str, filters: Filters) -> str | None:
    """The specific exclude keyword that matches `title`, or None if none do.

    Exposed separately from matches_title (rather than folded into a plain
    bool) so a caller can report WHICH exclude keyword killed a posting —
    the title-side counterpart to knowing which raw location string failed
    the location filter. See digest.apply_filters.
    """
    return _find_match(title, filters.title_exclude_keywords)


def matches_title(title: str, filters: Filters) -> bool:
    if not title_include_match(title, filters):
        return False
    return title_exclude_hit(title, filters) is None


def matches_location(location: str | None, filters: Filters) -> bool:
    if not location:
        return False
    if _contains_any(location, filters.location_include_keywords):
        return True
    return _contains_state_code(location, filters.location_include_state_codes)


def matches(title: str, location: str | None, filters: Filters) -> bool:
    return matches_title(title, filters) and matches_location(location, filters)
