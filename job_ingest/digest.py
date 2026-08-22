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
  3. round-robin across companies, so one prolific board can't crowd the
     others out of a capped digest
  4. cap at MAX_POSTINGS_PER_DIGEST, report the overflow
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


@dataclass
class DigestResult:
    pending_total: int
    matched_total: int
    included: int
    sent: bool
    error: str | None = None
    # [(location_string, count), ...] — postings that passed the title filter
    # but were excluded by location, grouped by their raw location string so
    # filters.yml's location_include_keywords can be tuned against what's
    # actually being missed. Sorted by count, most-excluded first.
    location_excluded: list[tuple[str, int]] | None = None


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
                   j.location, j.url, j.first_seen_at
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
) -> tuple[list[PendingPosting], list[tuple[str, int]]]:
    """Returns (matched, location_excluded).

    location_excluded covers only postings that passed the TITLE filter but
    failed on location — those are the ones actually worth reviewing to tune
    location_include_keywords; postings that never passed the title filter in
    the first place aren't a location-tuning signal.
    """
    matched = []
    location_excluded_counts: Counter[str] = Counter()

    for p in postings:
        if not filters_module.matches_title(p.title, filters):
            continue
        if filters_module.matches_location(p.location, filters):
            matched.append(p)
        else:
            location_excluded_counts[p.location or "(no location given)"] += 1

    location_excluded = sorted(
        location_excluded_counts.items(), key=lambda kv: kv[1], reverse=True
    )
    return matched, location_excluded


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


def build_text_body(postings: list[PendingPosting], overflow: int) -> str:
    lines = []
    for company_name, jobs in _group_by_company(postings).items():
        lines.append(company_name)
        for j in jobs:
            lines.append(f"  - {j.title} ({j.location or 'n/a'})")
            lines.append(f"    {j.url}")
        lines.append("")
    if overflow > 0:
        lines.append(
            f"...and {overflow} more new posting{'s' if overflow != 1 else ''} not shown "
            f"here (digest capped at {MAX_POSTINGS_PER_DIGEST}) — they'll appear in an "
            f"upcoming digest."
        )
    return "\n".join(lines).strip() + "\n"


def build_html_body(postings: list[PendingPosting], overflow: int) -> str:
    sections = []
    for company_name, jobs in _group_by_company(postings).items():
        items = "\n".join(
            f'<li><a href="{escape(j.url)}">{escape(j.title)}</a> '
            f"&mdash; {escape(j.location or 'n/a')}</li>"
            for j in jobs
        )
        sections.append(f"<h2>{escape(company_name)}</h2>\n<ul>\n{items}\n</ul>")

    overflow_html = ""
    if overflow > 0:
        overflow_html = (
            f"<p><em>...and {overflow} more new posting{'s' if overflow != 1 else ''} not "
            f"shown here (digest capped at {MAX_POSTINGS_PER_DIGEST}) — they'll appear in "
            f"an upcoming digest.</em></p>"
        )

    return (
        "<html><body style=\"font-family: sans-serif; max-width: 640px;\">\n"
        + "\n".join(sections)
        + overflow_html
        + "\n</body></html>"
    )


@dataclass
class _Selection:
    pending_total: int
    matched_total: int
    postings: list[PendingPosting]  # already round-robined + capped
    overflow: int
    location_excluded: list[tuple[str, int]]


def _select_for_digest(conn: psycopg.Connection, filters_path: str) -> _Selection:
    """The pure selection pipeline (fetch -> filter -> round-robin -> cap),
    shared by both `preview_digest` (no send, no DB write) and `send_digest`.
    """
    pending_total = count_pending_postings(conn)
    if pending_total == 0:
        return _Selection(0, 0, [], 0, [])

    all_pending = fetch_pending_postings(conn)
    filters = filters_module.load_filters(filters_path)
    matched, location_excluded = apply_filters(all_pending, filters)
    matched_total = len(matched)

    if matched_total == 0:
        return _Selection(pending_total, 0, [], 0, location_excluded)

    ordered = _round_robin_by_company(matched)
    postings = ordered[:MAX_POSTINGS_PER_DIGEST]
    overflow = max(0, matched_total - len(postings))
    return _Selection(pending_total, matched_total, postings, overflow, location_excluded)


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
        matched_total=selection.matched_total,
        included=len(selection.postings),
        sent=False,
        location_excluded=selection.location_excluded,
    )


def send_digest(
    conn: psycopg.Connection, filters_path: str = DEFAULT_FILTERS_PATH
) -> DigestResult:
    selection = _select_for_digest(conn, filters_path)
    pending_total = selection.pending_total
    matched_total = selection.matched_total
    postings = selection.postings
    overflow = selection.overflow
    location_excluded = selection.location_excluded

    if pending_total == 0:
        return DigestResult(pending_total=0, matched_total=0, included=0, sent=False)

    if matched_total == 0:
        return DigestResult(
            pending_total=pending_total,
            matched_total=0,
            included=0,
            sent=False,
            location_excluded=location_excluded,
        )

    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("DIGEST_FROM_EMAIL", "")
    to_email = os.environ.get("DIGEST_TO_EMAIL", "")
    if not resend.api_key or not from_email or not to_email:
        return DigestResult(
            pending_total=pending_total,
            matched_total=matched_total,
            included=len(postings),
            sent=False,
            error="RESEND_API_KEY / DIGEST_FROM_EMAIL / DIGEST_TO_EMAIL not fully set",
            location_excluded=location_excluded,
        )

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to_email],
        "subject": build_subject(matched_total),
        "html": build_html_body(postings, overflow),
        "text": build_text_body(postings, overflow),
    }

    try:
        resend.Emails.send(params)
    except resend.exceptions.ResendError as exc:
        # Send failed outright — nothing gets marked notified, so every one of
        # these postings is picked up again by the next run's digest. Correct:
        # we chose at-least-once, and this is that choice paying off.
        return DigestResult(
            pending_total=pending_total,
            matched_total=matched_total,
            included=len(postings),
            sent=False,
            error=str(exc),
            location_excluded=location_excluded,
        )

    # Send succeeded. Mark exactly what was sent, as its own commit — see the
    # module docstring for why this is deliberately not wrapped with the send.
    mark_notified(conn, postings, notified_at=datetime.now(UTC))
    return DigestResult(
        pending_total=pending_total,
        matched_total=matched_total,
        included=len(postings),
        sent=True,
        location_excluded=location_excluded,
    )
