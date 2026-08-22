"""Digest email: find postings pending notification, send one email via Resend,
then mark exactly what was sent.

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
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

import psycopg
import resend

MAX_POSTINGS_PER_DIGEST = 50


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
    included: int
    sent: bool
    error: str | None = None


def count_pending_postings(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE notified_at IS NULL AND closed_at IS NULL")
        return cur.fetchone()[0]


def fetch_pending_postings(
    conn: psycopg.Connection, limit: int = MAX_POSTINGS_PER_DIGEST
) -> list[PendingPosting]:
    """Oldest-pending-first (by first_seen_at), so that if there's ever a
    backlog bigger than the cap, it drains over successive runs instead of
    older postings being perpetually crowded out by newer ones.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.source, j.company_slug, j.external_id, c.name, j.title,
                   j.location, j.url, j.first_seen_at
            FROM jobs j
            JOIN companies c ON c.slug = j.company_slug
            WHERE j.notified_at IS NULL AND j.closed_at IS NULL
            ORDER BY j.first_seen_at ASC, j.company_slug, j.title
            LIMIT %s
            """,
            (limit,),
        )
        return [PendingPosting(*row) for row in cur.fetchall()]


def mark_notified(
    conn: psycopg.Connection, postings: list[PendingPosting], *, notified_at: datetime
) -> None:
    """Marks exactly the postings that were actually included in a sent email —
    never the full pending set, since a capped digest leaves the overflow
    genuinely un-notified for a future run to pick up.
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


def build_subject(pending_total: int) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    noun = "posting" if pending_total == 1 else "postings"
    return f"Job digest {today}: {pending_total} new {noun}"


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


def send_digest(conn: psycopg.Connection) -> DigestResult:
    pending_total = count_pending_postings(conn)
    if pending_total == 0:
        return DigestResult(pending_total=0, included=0, sent=False)

    postings = fetch_pending_postings(conn, MAX_POSTINGS_PER_DIGEST)
    overflow = max(0, pending_total - len(postings))

    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("DIGEST_FROM_EMAIL", "")
    to_email = os.environ.get("DIGEST_TO_EMAIL", "")
    if not resend.api_key or not from_email or not to_email:
        return DigestResult(
            pending_total=pending_total,
            included=len(postings),
            sent=False,
            error="RESEND_API_KEY / DIGEST_FROM_EMAIL / DIGEST_TO_EMAIL not fully set",
        )

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to_email],
        "subject": build_subject(pending_total),
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
            pending_total=pending_total, included=len(postings), sent=False, error=str(exc)
        )

    # Send succeeded. Mark exactly what was sent, as its own commit — see the
    # module docstring for why this is deliberately not wrapped with the send.
    mark_notified(conn, postings, notified_at=datetime.now(UTC))
    return DigestResult(pending_total=pending_total, included=len(postings), sent=True)
