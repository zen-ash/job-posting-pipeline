"""Digest email: find postings pending notification, filter them, pick a fair
sample, send one email via Resend, then mark exactly what was sent.

Ordering matters here, deliberately: send the email FIRST, and only mark
notified_at afterward — and as a separate DB commit, not inside the same
transaction as the send, since an HTTP call to Resend isn't a transactional
resource in the first place.

That ordering is a choice between two failure modes, and we're picking one on
purpose:

  - send-then-mark (what this does): if the process dies after a successful
    send but before the notified_at commit, the same posting can appear again
    in tomorrow's digest. Mildly annoying, self-correcting, costs nothing.
  - mark-then-send: if the process dies after marking notified_at but before
    (or during) the send, a posting is permanently marked "notified" without
    the email ever having gone out. Silent, permanent, and the whole point of
    this pipeline is not missing a posting.

At-least-once beats at-most-once here, so send-then-mark it is.

Selection pipeline, in order:
  1. fetch every posting pending notification (no cap yet)
  2. filter by job_ingest.filters (title include/exclude keywords, location)
  3. split into priority tiers (filters.yml: priority_keywords)
  4. round-robin across companies WITHIN each tier, so one prolific board
     can't crowd the others out of the part of the digest that gets read
  5. fill the MAX_POSTINGS_PER_DIGEST cap from tier 1 first. Tier 1 is never
     truncated -- if it alone exceeds the cap, the cap is exceeded and the
     email says so.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

import psycopg
import resend

from job_ingest import filters as filters_module
from job_ingest.filters import DEFAULT_FILTERS_PATH, Filters

MAX_POSTINGS_PER_DIGEST = 50
# Safety cap on the initial fetch itself — not the digest cap. High enough to
# never matter at this project's scale, just a backstop against an unbounded
# query if the pending set ever grew huge.
MAX_PENDING_FETCH = 10_000


@dataclass(frozen=True, slots=True)
class PendingPosting:
    source: str
    company_slug: str
    external_id: str
    company_name: str
    title: str
    location: str | None
    url: str
    first_seen_at: datetime
    # Structured country from the source, when it gave one (Workday only
    # today). Preferred over string-matching `location` -- see
    # job_ingest/filters.matches_location.
    country_code: str | None = None


@dataclass
class DigestResult:
    pending_total: int
    # How many postings matched at least one title_include_keyword. Sits
    # between pending_total and matched_total in the funnel, so the report can
    # separate "never looked relevant" from "looked relevant but was rejected".
    include_matched_total: int
    matched_total: int
    # Tier 1 is never truncated, so tier1_total is both "how many priority
    # postings matched" and "how many were sent".
    tier1_total: int
    tier2_total: int
    included: int
    sent: bool
    error: str | None = None
    # [(location_string, count), ...] — postings that passed the title filter
    # but were excluded by location, grouped by their raw location string so
    # filters.yml's location_include_keywords can be tuned against what's
    # actually being missed. Sorted by count, most-excluded first.
    location_excluded: list[tuple[str, int]] | None = None
    # [(exclude_keyword, count), ...] — postings that matched a title include
    # keyword but were killed by a title exclude keyword, grouped by WHICH
    # exclude keyword fired. The title-side symmetric counterpart to
    # location_excluded: exclude keywords were otherwise unobservable — you'd
    # never know a posting existed to be excluded in the first place. Sorted
    # by count, most-excluded first.
    title_excluded: list[tuple[str, int]] | None = None


def count_pending_postings(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE notified_at IS NULL AND closed_at IS NULL")
        return cur.fetchone()[0]


def fetch_pending_postings(
    conn: psycopg.Connection, limit: int = MAX_PENDING_FETCH
) -> list[PendingPosting]:
    """Every posting pending notification, oldest-first within each company
    (by first_seen_at) — the ordering `_round_robin_by_company` relies on to
    keep FIFO fairness per company once it interleaves them.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.source, j.company_slug, j.external_id, c.name, j.title,
                   j.location, j.url, j.first_seen_at, j.country_code
            FROM jobs j
            JOIN companies c ON c.slug = j.company_slug
            WHERE j.notified_at IS NULL AND j.closed_at IS NULL
            ORDER BY j.company_slug, j.first_seen_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [PendingPosting(*row) for row in cur.fetchall()]


def apply_filters(
    postings: list[PendingPosting], filters: Filters
) -> tuple[list[PendingPosting], list[tuple[str, int]], list[tuple[str, int]], int]:
    """Returns (matched, location_excluded, title_excluded, include_matched).

    location_excluded covers postings that matched a title include keyword
    and passed the title exclude check, but failed on location — those are
    the ones worth reviewing to tune location_include_keywords.

    title_excluded covers postings that matched a title include keyword but
    were then killed by a title exclude keyword, grouped by which exclude
    keyword fired — the symmetric report for tuning title_exclude_keywords,
    the same way location_excluded is for location_include_keywords.

    A posting that never matched any include keyword in the first place isn't
    a signal for either report — there's nothing to tune from "didn't match
    any of the include keywords at all".
    """
    matched = []
    include_matched = 0
    location_excluded_counts: Counter[str] = Counter()
    title_excluded_counts: Counter[str] = Counter()

    for p in postings:
        if not filters_module.title_include_match(p.title, filters):
            continue
        include_matched += 1

        exclude_hit = filters_module.title_exclude_hit(p.title, filters)
        if exclude_hit is not None:
            title_excluded_counts[exclude_hit] += 1
            continue

        if filters_module.matches_location(p.location, filters, p.country_code):
            matched.append(p)
        else:
            location_excluded_counts[p.location or "(no location given)"] += 1

    location_excluded = sorted(
        location_excluded_counts.items(), key=lambda kv: kv[1], reverse=True
    )
    title_excluded = sorted(title_excluded_counts.items(), key=lambda kv: kv[1], reverse=True)
    return matched, location_excluded, title_excluded, include_matched


def _round_robin_by_company(postings: list[PendingPosting]) -> list[PendingPosting]:
    """Interleaves postings across companies (alphabetically by slug) so that
    a single board with a large matched count can't monopolize a capped
    digest. Each company's own relative order (oldest-first, from the query)
    is preserved within its turns.
    """
    by_company: dict[str, list[PendingPosting]] = defaultdict(list)
    for p in postings:
        by_company[p.company_slug].append(p)

    queues = [by_company[slug] for slug in sorted(by_company)]
    result: list[PendingPosting] = []
    while any(queues):
        for q in queues:
            if q:
                result.append(q.pop(0))
    return result


def mark_notified(
    conn: psycopg.Connection, postings: list[PendingPosting], *, notified_at: datetime
) -> None:
    """Marks exactly the postings that were actually included in a sent email —
    never the full pending or matched set, since anything filtered out or cut
    by the cap needs to stay genuinely un-notified for a future run.
    """
    with conn.cursor() as cur:
        for p in postings:
            cur.execute(
                """
                UPDATE jobs SET notified_at = %s
                WHERE source = %s AND company_slug = %s AND external_id = %s
                """,
                (notified_at, p.source, p.company_slug, p.external_id),
            )
    conn.commit()


def _group_by_company(postings: list[PendingPosting]) -> dict[str, list[PendingPosting]]:
    groups: dict[str, list[PendingPosting]] = {}
    for p in postings:
        groups.setdefault(p.company_name, []).append(p)
    for group in groups.values():
        group.sort(key=lambda p: p.title)
    return dict(sorted(groups.items()))


def build_subject(matched_total: int) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    noun = "posting" if matched_total == 1 else "postings"
    return f"Job digest {today}: {matched_total} new {noun}"


TIER1_HEADING = "TIER 1 - PRIORITY (internships, co-ops, campus & rotational programs)"
TIER2_HEADING = "TIER 2 - OTHER MATCHES"


@dataclass
class TieredSelection:
    """What the digest will actually send, split by priority tier.

    tier1 is never truncated: if priority postings alone exceed the cap, all
    of them go out and `tier1_over_cap` records by how much. Missing an
    internship deadline costs more than a long email, and unlike a full-time
    posting an internship req does not come back next quarter.
    """

    tier1: list[PendingPosting]
    tier2: list[PendingPosting]
    tier2_omitted: int
    tier1_over_cap: int

    @property
    def all_postings(self) -> list[PendingPosting]:
        return self.tier1 + self.tier2


def select_tiered(
    matched: list[PendingPosting], filters: Filters, cap: int = MAX_POSTINGS_PER_DIGEST
) -> TieredSelection:
    """Split matched postings into tiers, round-robin WITHIN each tier, then
    fill the cap from tier 1 first.

    Round-robin runs per tier rather than once overall on purpose: doing it
    once and then splitting would let a single company's tier-1 postings sit
    consecutively, and the fairness guarantee is wanted inside the tier that
    actually gets read first.
    """
    tier1 = _round_robin_by_company(
        [p for p in matched if filters_module.is_priority(p.title, filters)]
    )
    tier2 = _round_robin_by_company(
        [p for p in matched if not filters_module.is_priority(p.title, filters)]
    )
    remaining = max(0, cap - len(tier1))
    selected2 = tier2[:remaining]
    return TieredSelection(
        tier1=tier1,
        tier2=selected2,
        tier2_omitted=len(tier2) - len(selected2),
        tier1_over_cap=max(0, len(tier1) - cap),
    )


def _overflow_sentence(sel: TieredSelection) -> str:
    """One line reconciling what was left out, broken down by tier.

    Tier 1 is always 0 here by construction; it is still stated rather than
    omitted so the reader can tell "no priority postings were held back" from
    "the breakdown forgot about them".
    """
    n = sel.tier2_omitted
    return (
        f"...and {n} more new posting{'s' if n != 1 else ''} not shown here "
        f"(0 from Tier 1 - priority postings are never held back; "
        f"{n} from Tier 2, over the {MAX_POSTINGS_PER_DIGEST} cap). "
        f"They'll appear in an upcoming digest."
    )


def _tier1_warning_sentence(sel: TieredSelection) -> str:
    n = sel.tier1_over_cap
    return (
        f"NOTE: {len(sel.tier1)} priority postings exceed the "
        f"{MAX_POSTINGS_PER_DIGEST}-posting cap by {n}. Sending all of them "
        f"anyway rather than truncating Tier 1."
    )


def build_text_body(sel: TieredSelection) -> str:
    lines: list[str] = []
    for heading, group in ((TIER1_HEADING, sel.tier1), (TIER2_HEADING, sel.tier2)):
        if not group:
            continue
        lines.append(heading)
        lines.append("=" * len(heading))
        for company_name, jobs in _group_by_company(group).items():
            lines.append(company_name)
            for j in jobs:
                lines.append(f"  - {j.title} ({j.location or 'n/a'})")
                lines.append(f"    {j.url}")
            lines.append("")
    if sel.tier1_over_cap > 0:
        lines.append(_tier1_warning_sentence(sel))
    if sel.tier2_omitted > 0:
        lines.append(_overflow_sentence(sel))
    return "\n".join(lines).strip() + "\n"


def build_html_body(sel: TieredSelection) -> str:
    parts: list[str] = []
    for heading, group in ((TIER1_HEADING, sel.tier1), (TIER2_HEADING, sel.tier2)):
        if not group:
            continue
        parts.append(f"<h1>{escape(heading)}</h1>")
        for company_name, jobs in _group_by_company(group).items():
            items = "\n".join(
                f'<li><a href="{escape(j.url)}">{escape(j.title)}</a> '
                f"&mdash; {escape(j.location or 'n/a')}</li>"
                for j in jobs
            )
            parts.append(f"<h2>{escape(company_name)}</h2>\n<ul>\n{items}\n</ul>")
    if sel.tier1_over_cap > 0:
        parts.append(f"<p><strong>{escape(_tier1_warning_sentence(sel))}</strong></p>")
    if sel.tier2_omitted > 0:
        parts.append(f"<p><em>{escape(_overflow_sentence(sel))}</em></p>")
    return (
        '<html><body style="font-family: sans-serif; max-width: 640px;">\n'
        + "\n".join(parts)
        + "\n</body></html>"
    )


@dataclass
class _Selection:
    pending_total: int
    include_matched_total: int
    matched_total: int
    tiered: TieredSelection
    location_excluded: list[tuple[str, int]]
    title_excluded: list[tuple[str, int]]


def _select_for_digest(conn: psycopg.Connection, filters_path: str) -> _Selection:
    """The pure selection pipeline (fetch -> filter -> round-robin -> cap),
    shared by both `preview_digest` (no send, no DB write) and `send_digest`.
    """
    pending_total = count_pending_postings(conn)
    if pending_total == 0:
        return _Selection(0, 0, 0, TieredSelection([], [], 0, 0), [], [])

    all_pending = fetch_pending_postings(conn)
    filters = filters_module.load_filters(filters_path)
    matched, location_excluded, title_excluded, include_matched = apply_filters(
        all_pending, filters
    )
    matched_total = len(matched)

    if matched_total == 0:
        return _Selection(
            pending_total,
            include_matched,
            0,
            TieredSelection([], [], 0, 0),
            location_excluded,
            title_excluded,
        )

    tiered = select_tiered(matched, filters)
    return _Selection(
        pending_total,
        include_matched,
        matched_total,
        tiered,
        location_excluded,
        title_excluded,
    )


def preview_digest(
    conn: psycopg.Connection, filters_path: str = DEFAULT_FILTERS_PATH
) -> DigestResult:
    """Runs the exact same fetch/filter/round-robin/cap pipeline as
    `send_digest`, for visibility into the funnel counts, but never calls
    Resend and never marks anything notified. Use this (e.g. via
    `--skip-digest`) to see what a real send would include without sending it.
    """
    selection = _select_for_digest(conn, filters_path)
    return DigestResult(
        pending_total=selection.pending_total,
        include_matched_total=selection.include_matched_total,
        matched_total=selection.matched_total,
        tier1_total=len(selection.tiered.tier1),
        tier2_total=len(selection.tiered.tier2),
        included=len(selection.tiered.all_postings),
        sent=False,
        location_excluded=selection.location_excluded,
        title_excluded=selection.title_excluded,
    )


def send_digest(
    conn: psycopg.Connection, filters_path: str = DEFAULT_FILTERS_PATH
) -> DigestResult:
    selection = _select_for_digest(conn, filters_path)
    pending_total = selection.pending_total
    include_matched_total = selection.include_matched_total
    matched_total = selection.matched_total
    tiered = selection.tiered
    postings = tiered.all_postings
    location_excluded = selection.location_excluded
    title_excluded = selection.title_excluded

    if pending_total == 0:
        return DigestResult(
            pending_total=0,
            include_matched_total=0,
            matched_total=0,
            tier1_total=0,
            tier2_total=0,
            included=0,
            sent=False,
        )

    if matched_total == 0:
        return DigestResult(
            pending_total=pending_total,
            include_matched_total=include_matched_total,
            matched_total=0,
            tier1_total=0,
            tier2_total=0,
            included=0,
            sent=False,
            location_excluded=location_excluded,
            title_excluded=title_excluded,
        )

    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("DIGEST_FROM_EMAIL", "")
    to_email = os.environ.get("DIGEST_TO_EMAIL", "")
    if not resend.api_key or not from_email or not to_email:
        return DigestResult(
            pending_total=pending_total,
            include_matched_total=include_matched_total,
            matched_total=matched_total,
            tier1_total=len(tiered.tier1),
            tier2_total=len(tiered.tier2),
            included=len(postings),
            sent=False,
            error="RESEND_API_KEY / DIGEST_FROM_EMAIL / DIGEST_TO_EMAIL not fully set",
            location_excluded=location_excluded,
            title_excluded=title_excluded,
        )

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to_email],
        "subject": build_subject(matched_total),
        "html": build_html_body(tiered),
        "text": build_text_body(tiered),
    }

    try:
        resend.Emails.send(params)
    except resend.exceptions.ResendError as exc:
        # Send failed outright — nothing gets marked notified, so every one of
        # these postings is picked up again by the next run's digest. Correct:
        # we chose at-least-once, and this is that choice paying off.
        return DigestResult(
            pending_total=pending_total,
            include_matched_total=include_matched_total,
            matched_total=matched_total,
            tier1_total=len(tiered.tier1),
            tier2_total=len(tiered.tier2),
            included=len(postings),
            sent=False,
            error=str(exc),
            location_excluded=location_excluded,
            title_excluded=title_excluded,
        )

    # Send succeeded. Mark exactly what was sent, as its own commit — see the
    # module docstring for why this is deliberately not wrapped with the send.
    mark_notified(conn, postings, notified_at=datetime.now(UTC))
    return DigestResult(
        pending_total=pending_total,
        include_matched_total=include_matched_total,
        matched_total=matched_total,
        tier1_total=len(tiered.tier1),
        tier2_total=len(tiered.tier2),
        included=len(postings),
        sent=True,
        location_excluded=location_excluded,
        title_excluded=title_excluded,
    )
