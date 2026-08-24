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

    The trailing `s?` makes every keyword also match its regular plural, so
    "senior" catches "Seniors" and "lead" catches "Leads" -- both real
    postings that slipped through -- while an "analyst" include still catches
    "Analysts". Done in the matcher rather than by listing plural forms in
    filters.yml because it covers all four keyword lists at once, needs no
    upkeep when a keyword is added, and cannot drift out of sync the way a
    hand-maintained parallel list would.

    It does not weaken the boundary guarantees above: "sql" still fails
    against "MySQL" on the lookbehind, and "lead" still fails against
    "Leadership" because the following "e" fails the trailing lookahead.

    Known limitation, deliberate: this handles singular keyword -> plural
    text, not the reverse (a "sanctions" keyword will not match "sanction"),
    and not irregular plurals. Neither has shown up in real data, and the
    one-character fix is worth more here than a stemmer.
    """
    normalized = _normalize(text)
    for kw in keywords:
        pattern = rf"(?<!\w){re.escape(_normalize(kw))}s?(?!\w)"
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


# ISO alpha-2 codes treated as "US-based" when a source states the country
# structurally. Only "US" today; kept as a set so US territories could be
# added without touching the matching logic.
US_COUNTRY_CODES = frozenset({"US"})


def matches_location(
    location: str | None, filters: Filters, country_code: str | None = None
) -> bool:
    """True if the posting looks US-based.

    When `country_code` is present the answer comes from it alone -- a
    structured country from the source is strictly better evidence than
    string-matching a free-text location, and it is also authoritative in the
    negative: a posting Workday says is in India is in India, regardless of
    what its `locationsText` happens to spell. Only Workday supplies this
    today, and only for postings that were enriched via its job-detail
    endpoint (see job_ingest/ats/workday.py).

    Everything else -- Greenhouse/Lever/Ashby, and Workday rows stored
    list-only -- falls back to the unchanged keyword/state-code matching
    below. Workday's free-text location is notably poor for this ("2
    Locations", "ATLANTA FDC/BDC - 5865"), which is exactly why the enriched
    path exists.
    """
    if country_code:
        return country_code.strip().upper() in US_COUNTRY_CODES
    if not location:
        return False
    if _contains_any(location, filters.location_include_keywords):
        return True
    return _contains_state_code(location, filters.location_include_state_codes)


def matches(
    title: str, location: str | None, filters: Filters, country_code: str | None = None
) -> bool:
    return matches_title(title, filters) and matches_location(
        location, filters, country_code
    )
