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


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word/phrase, case-insensitive match of any keyword in `text`.

    Word-boundary (not plain substring) matching on purpose: a bare substring
    check would match "us" inside "Russia" or "Australia", and "sql" inside
    "MySQL".

    Uses (?<!\\w)...(?!\\w) rather than \\b\\b. They're equivalent for a keyword
    that starts and ends with a word character, but \\b breaks for one that
    ends in punctuation: \\b requires an actual transition between a word char
    and a non-word char, and if the keyword's last character is already
    non-word (e.g. "u.s." ending in ".", or "remote (us)" ending in ")"),
    there's no such transition left to find when the next real character is
    -also- non-word (a comma, a space, end of string) — so the trailing \\b
    silently never matches and the keyword is dead. (?!\\w) has no such
    requirement: it only asserts the next character isn't a word character,
    which is true at end-of-string or before another punctuation character.
    Confirmed empirically: "u.s." and "remote (us)" were both silently dead
    under \\b before this fix — neither matched a single real posting.
    """
    lowered = text.lower()
    return any(
        re.search(rf"(?<!\w){re.escape(kw.lower())}(?!\w)", lowered) for kw in keywords
    )


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


def matches_title(title: str, filters: Filters) -> bool:
    if not _contains_any(title, filters.title_include_keywords):
        return False
    return not _contains_any(title, filters.title_exclude_keywords)


def matches_location(location: str | None, filters: Filters) -> bool:
    if not location:
        return False
    if _contains_any(location, filters.location_include_keywords):
        return True
    return _contains_state_code(location, filters.location_include_state_codes)


def matches(title: str, location: str | None, filters: Filters) -> bool:
    return matches_title(title, filters) and matches_location(location, filters)
