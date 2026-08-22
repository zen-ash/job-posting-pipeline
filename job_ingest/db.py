"""Postgres access: schema setup, company sync, and the upsert/new/closed logic.

Design notes (see also job_ingest/schema.sql):

- Rows in `jobs` are never deleted. A posting's lifecycle is entirely
  first_seen_at / last_seen_at / closed_at / notified_at.
- content_hash is always computed from the FULL description fetched this run,
  before it gets truncated for storage — so truncation never masks an edit.
- Closing is only ever done from a *successful* fetch's results. If a board
  fetch fails, `sync_company_postings` for it is simply never called — its
  existing jobs are left exactly as they were. See `main.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Json

from job_ingest.config import Company
from job_ingest.models import Posting, posting_content_hash

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Neon's free tier is 0.5GB total; full HTML/plain descriptions across hundreds
# of postings per company add up fast, and the digest never needs more than a
# title/location/link anyway. `url` always has the full text.
MAX_DESCRIPTION_CHARS = 4000
_TRUNCATION_SUFFIX = "…[truncated]"


def get_connection() -> psycopg.Connection:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in "
            "(or export it directly, e.g. in GitHub Actions)."
        )
    return psycopg.connect(database_url)


def ensure_schema(conn: psycopg.Connection) -> None:
    """Idempotent: safe to call at the start of every run."""
    sql = SCHEMA_PATH.read_text()
    with conn.cursor() as cur:
        for statement in _split_statements(sql):
            cur.execute(statement)
    conn.commit()


def _split_statements(sql: str) -> list[str]:
    # Our DDL has no semicolons inside string literals or function bodies, so a
    # naive split is safe and keeps this dependency-free. Executed one at a time
    # because psycopg3's extended query protocol doesn't allow multiple commands
    # in a single prepared statement.
    return [s.strip() for s in sql.split(";") if s.strip()]


def sync_companies(conn: psycopg.Connection, companies: list[Company]) -> None:
    """Upsert the companies table from companies.yml. Run before syncing any
    postings, since jobs.company_slug references companies.slug."""
    with conn.cursor() as cur:
        for c in companies:
            cur.execute(
                """
                INSERT INTO companies (slug, name, ats, board_token)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    ats = EXCLUDED.ats,
                    board_token = EXCLUDED.board_token,
                    updated_at = now()
                """,
                (c.slug, c.name, c.ats, c.board_token),
            )
    conn.commit()


def _truncate_description(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    keep = MAX_DESCRIPTION_CHARS - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    reopened: int = 0
    unchanged: int = 0
    closed: int = 0


def sync_company_postings(
    conn: psycopg.Connection, company: Company, postings: list[Posting]
) -> SyncStats:
    """Upsert `postings` (this run's full fetch for `company`) and close any
    previously-open posting for this company that's now absent.

    Only call this for a company whose fetch actually succeeded — closing here
    is unconditional on absence, which is only safe to trust when the fetch
    itself is trustworthy. An empty `postings` list (a real, successful fetch
    that returned zero jobs) correctly closes everything still open.
    """
    stats = SyncStats(seen=len(postings))
    fetched_ids = [p.external_id for p in postings]

    with conn.cursor() as cur:
        for posting in postings:
            new_hash = posting_content_hash(posting)
            stored_description = _truncate_description(posting.description)

            cur.execute(
                """
                SELECT content_hash, closed_at FROM jobs
                WHERE source = %s AND company_slug = %s AND external_id = %s
                """,
                (posting.source, posting.company_slug, posting.external_id),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    INSERT INTO jobs (
                        source, company_slug, external_id, title, location,
                        department, url, source_updated_at, description,
                        content_hash, first_seen_at, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                    """,
                    (
                        posting.source,
                        posting.company_slug,
                        posting.external_id,
                        posting.title,
                        posting.location,
                        posting.department,
                        posting.url,
                        posting.source_updated_at,
                        stored_description,
                        new_hash,
                    ),
                )
                stats.new += 1
                continue

            existing_hash, closed_at = row
            reopened = closed_at is not None
            if reopened:
                stats.reopened += 1
            if existing_hash != new_hash:
                stats.updated += 1
            else:
                stats.unchanged += 1

            cur.execute(
                """
                UPDATE jobs
                SET title = %s,
                    location = %s,
                    department = %s,
                    url = %s,
                    source_updated_at = %s,
                    description = %s,
                    content_hash = %s,
                    last_seen_at = now(),
                    closed_at = NULL,
                    -- Reopening surfaces a posting as newly-pending again, since
                    -- from the user's perspective it's newly active. A same-hash
                    -- re-sighting or a content edit on an already-open posting
                    -- never touches notified_at.
                    notified_at = CASE WHEN %s THEN NULL ELSE notified_at END
                WHERE source = %s AND company_slug = %s AND external_id = %s
                """,
                (
                    posting.title,
                    posting.location,
                    posting.department,
                    posting.url,
                    posting.source_updated_at,
                    stored_description,
                    new_hash,
                    reopened,
                    posting.source,
                    posting.company_slug,
                    posting.external_id,
                ),
            )

        cur.execute(
            """
            UPDATE jobs
            SET closed_at = now()
            WHERE source = %s AND company_slug = %s AND closed_at IS NULL
              AND external_id <> ALL(%s)
            """,
            (company.ats, company.slug, fetched_ids),
        )
        stats.closed = cur.rowcount

    return stats


def record_run(
    conn: psycopg.Connection,
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    companies_total: int,
    companies_succeeded: int,
    companies_failed: int,
    jobs_seen: int,
    jobs_new: int,
    jobs_updated: int,
    jobs_reopened: int,
    jobs_closed: int,
    error: str | None,
    board_errors: list[dict],
    digest_pending_total: int = 0,
    digest_matched_total: int = 0,
    digest_sent: bool = False,
    digest_postings_sent: int = 0,
    digest_error: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (
                started_at, finished_at, status, companies_total, companies_succeeded,
                companies_failed, jobs_seen, jobs_new, jobs_updated, jobs_reopened,
                jobs_closed, error, board_errors, digest_pending_total, digest_matched_total,
                digest_sent, digest_postings_sent, digest_error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                started_at,
                finished_at,
                status,
                companies_total,
                companies_succeeded,
                companies_failed,
                jobs_seen,
                jobs_new,
                jobs_updated,
                jobs_reopened,
                jobs_closed,
                error,
                Json(board_errors),
                digest_pending_total,
                digest_matched_total,
                digest_sent,
                digest_postings_sent,
                digest_error,
            ),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id
