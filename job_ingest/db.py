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
from dataclasses import dataclass, replace
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
                INSERT INTO companies (slug, name, ats, board_token, tenant, wd_host, site)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    ats = EXCLUDED.ats,
                    board_token = EXCLUDED.board_token,
                    tenant = EXCLUDED.tenant,
                    wd_host = EXCLUDED.wd_host,
                    site = EXCLUDED.site,
                    updated_at = now()
                """,
                (c.slug, c.name, c.ats, c.board_token, c.tenant, c.wd_host, c.site),
            )
    conn.commit()


def enriched_external_ids(conn: psycopg.Connection, company: Company) -> set[str]:
    """external_ids for this company that already have a stored description.

    Handed to the Workday fetcher so it can skip re-requesting job detail for
    postings whose body we already have -- the difference between ~750 detail
    requests a night and only the day's new arrivals. Returned as a plain set
    so the fetcher itself never touches the database and stays offline-testable.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_id FROM jobs
            WHERE source = %s AND company_slug = %s AND coalesce(description, '') <> ''
            """,
            (company.ats, company.slug),
        )
        return {row[0] for row in cur.fetchall()}


def _truncate_description(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    keep = MAX_DESCRIPTION_CHARS - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


def _resolve_against_stored(posting: Posting, row: tuple) -> tuple[str, str, str | None]:
    """Returns (content_hash, description_to_store, location_to_store) for a
    posting that already exists in the table.

    Exists because of the Workday "don't re-enrich" optimisation: when a
    posting's body was fetched on an earlier run, later runs deliberately skip
    the detail request, so the incoming Posting arrives with description="".
    Written naively that empty string would overwrite the stored description
    and change the content_hash on every single run -- destroying the body text
    and reporting the whole board as "updated" forever.

    So an empty incoming description against a non-empty stored one means "no
    new information about the body", not "the body is now empty":

      - the stored description is kept, and
      - the hash is recomputed against the STORED body, so a title or location
        edit (both of which still arrive from the list response every run) is
        still detected, while
      - if the list-derived fields are unchanged too, the stored hash is
        returned verbatim rather than recomputed. That last part matters: the
        original hash was taken over the FULL body, but only the truncated body
        was stored, so recomputing would produce a different hash and flag
        every enriched row as updated exactly once for no real reason.

    TRADEOFF, deliberate: description-body edits on Workday postings are no
    longer detected, because the body is never re-read. Title and location
    edits still are. Catching body edits would mean re-fetching every enriched
    posting nightly -- ~750 requests to find the rare reworded paragraph --
    which is the cost this whole change exists to remove. Revisit with a TTL
    (re-enrich anything older than N days) if body edits ever start mattering.
    """
    stored_hash, _closed_at, s_title, s_location, s_department, s_description = row
    stored_description = s_description or ""

    # A None location means the fetcher learned nothing new this run (Workday
    # skipped enrichment), NOT that the posting lost its location -- so keep
    # what is stored rather than overwriting it with a worse value.
    location = posting.location if posting.location is not None else s_location

    if posting.description or not stored_description:
        resolved = replace(posting, location=location)
        return (
            posting_content_hash(resolved),
            _truncate_description(resolved.description),
            location,
        )

    list_fields_changed = (posting.title, location, posting.department) != (
        s_title,
        s_location,
        s_department,
    )
    if not list_fields_changed:
        return stored_hash, stored_description, location
    carried = replace(posting, description=stored_description, location=location)
    return posting_content_hash(carried), stored_description, location


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    reopened: int = 0
    unchanged: int = 0
    closed: int = 0


def sync_company_postings(
    conn: psycopg.Connection,
    company: Company,
    postings: list[Posting],
    complete: bool = True,
) -> SyncStats:
    """Upsert `postings` (this run's full fetch for `company`) and close any
    previously-open posting for this company that's now absent.

    Only call this for a company whose fetch actually succeeded — closing here
    is unconditional on absence, which is only safe to trust when the fetch
    itself is trustworthy. An empty `postings` list (a real, successful fetch
    that returned zero jobs) correctly closes everything still open.

    `complete=False` suppresses closure detection entirely for this call. The
    general rule is that closing a posting requires a COMPLETE observation of
    the board: absence only means "closed" if we would have seen it had it
    been open. A failed fetch is one way to lose that guarantee (handled by
    never calling this function at all), and a fetch that SUCCEEDS but is
    truncated is another — Workday caps `total` at 2000, so a larger board
    returns a sliding window and postings drop out of view without changing.
    Both produce the same false closure, so both must suppress it. Postings
    that WERE seen are still upserted normally; only the closing step is
    skipped.
    """
    stats = SyncStats(seen=len(postings))
    fetched_ids = [p.external_id for p in postings]

    with conn.cursor() as cur:
        for posting in postings:
            cur.execute(
                """
                SELECT content_hash, closed_at, title, location, department, description
                FROM jobs
                WHERE source = %s AND company_slug = %s AND external_id = %s
                """,
                (posting.source, posting.company_slug, posting.external_id),
            )
            row = cur.fetchone()

            if row is not None:
                new_hash, stored_description, resolved_location = _resolve_against_stored(
                    posting, row
                )
            else:
                new_hash = posting_content_hash(posting)
                stored_description = _truncate_description(posting.description)
                resolved_location = posting.location

            if row is None:
                cur.execute(
                    """
                    INSERT INTO jobs (
                        source, company_slug, external_id, title, location,
                        department, url, source_updated_at, description,
                        country_code, content_hash, first_seen_at, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
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
                        posting.country_code,
                        new_hash,
                    ),
                )
                stats.new += 1
                continue

            existing_hash, closed_at = row[0], row[1]
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
                    -- COALESCE so a later list-only re-fetch of an
                    -- already-enriched posting can't erase its country. New
                    -- structured data overwrites; absent data leaves the
                    -- stored value alone.
                    country_code = COALESCE(%s, country_code),
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
                    resolved_location,
                    posting.department,
                    posting.url,
                    posting.source_updated_at,
                    stored_description,
                    posting.country_code,
                    new_hash,
                    reopened,
                    posting.source,
                    posting.company_slug,
                    posting.external_id,
                ),
            )

        if complete:
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
    incomplete_observations: list[dict] | None = None,
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
                jobs_closed, error, board_errors, incomplete_observations,
                digest_pending_total, digest_matched_total,
                digest_sent, digest_postings_sent, digest_error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                Json(incomplete_observations or []),
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
