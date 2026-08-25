"""Entrypoint.

  python -m job_ingest.main --company <slug>   -- fetch+print one company, no DB
                                                   (step 2 debug mode)
  python -m job_ingest.main                    -- full run: every company in
                                                   companies.yml, written to Postgres
                                                   (this is what the daily cron runs)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

import requests
from dotenv import load_dotenv

from job_ingest import db, digest
from job_ingest.ats import BoardFetch, fetch_postings
from job_ingest.config import Company, load_companies
from job_ingest.filters import Filters, load_filters
from job_ingest.http import BETWEEN_BOARDS_DELAY_SECONDS


def fetch_company(
    company: Company,
    filters: Filters | None = None,
    already_enriched: set[str] | None = None,
) -> BoardFetch:
    """Both extra arguments are Workday-only and are cost optimisations inside
    the fetch, never a substitute for the digest's own independent filtering:

    - `filters` decides which postings justify a second (detail) request.
    - `already_enriched` skips that request for postings whose body is already
      stored.

    They are passed in rather than looked up inside the fetcher so that
    job_ingest/ats/* stays database-unaware and testable offline.
    """
    return fetch_postings(company, filters=filters, already_enriched=already_enriched)


def _load_filters_or_none(path: str) -> Filters | None:
    """Filters are only needed by Workday's fetch-time optimisation. A missing
    filters.yml shouldn't stop a run that has no Workday companies in it, so
    this degrades to None (Workday then stores every posting list-only) rather
    than raising.
    """
    try:
        return load_filters(path)
    except (OSError, ValueError):
        return None


def compute_run_status(companies_total: int, companies_failed: int) -> str:
    if companies_total == 0 or companies_failed == 0:
        return "success"
    if companies_failed >= companies_total:
        return "failure"
    return "partial_failure"


def run_single_company(args: argparse.Namespace) -> int:
    """Step 2 debug mode: fetch + print, no DB."""
    companies = {c.slug: c for c in load_companies(args.companies_file)}
    if args.company not in companies:
        print(
            f"Unknown company slug '{args.company}'. Known slugs: {sorted(companies)}",
            file=sys.stderr,
        )
        return 1

    company = companies[args.company]
    postings = fetch_company(company, _load_filters_or_none(args.filters_file)).postings

    board_id = company.board_token or f"{company.tenant}/{company.wd_host}/{company.site}"
    header = f"{company.name} ({company.ats}, {board_id})"
    print(f"{header}: {len(postings)} postings\n")
    for p in postings[:10]:
        print(f"  [{p.external_id}] {p.title}")
        print(f"      location: {p.location or 'n/a'}   department: {p.department or 'n/a'}")
        print(f"      {p.url}")
    if len(postings) > 10:
        print(f"\n  ... and {len(postings) - 10} more")

    return 0


def run_full_ingest(args: argparse.Namespace) -> int:
    """Full run: every company in companies.yml, upserted into Postgres."""
    companies = load_companies(args.companies_file)
    # Loaded once here and handed to each fetch. Only Workday uses it, to skip
    # detail requests for postings whose title can't pass anyway; the digest
    # re-loads and re-applies the full filter itself over what got stored.
    filters = _load_filters_or_none(args.filters_file)
    started_at = datetime.now(UTC)

    conn = db.get_connection()
    try:
        db.ensure_schema(conn)
        db.sync_companies(conn, companies)

        companies_succeeded = 0
        board_errors: list[dict] = []
        incomplete_observations: list[dict] = []
        totals = {"seen": 0, "new": 0, "updated": 0, "reopened": 0, "closed": 0}

        for i, company in enumerate(companies):
            print(f"[{company.slug}] fetching ({company.ats})...")
            try:
                # Only Workday has a second-phase request worth skipping; the
                # other three would pay for a query whose result they ignore.
                already_enriched = (
                    db.enriched_external_ids(conn, company)
                    if company.ats == "workday"
                    else None
                )
                fetched = fetch_company(company, filters, already_enriched)
            except requests.RequestException as exc:
                message = str(exc)
                print(f"[{company.slug}] FAILED: {message}", file=sys.stderr)
                board_errors.append(
                    {"company_slug": company.slug, "ats": company.ats, "error": message}
                )
            else:
                if not fetched.complete:
                    incomplete_observations.append(
                        {"company_slug": company.slug, "reason": fetched.incomplete_reason}
                    )
                    print(
                        f"[{company.slug}] INCOMPLETE observation, closure detection "
                        f"suppressed: {fetched.incomplete_reason}",
                        file=sys.stderr,
                    )
                stats = db.sync_company_postings(
                    conn, company, fetched.postings, complete=fetched.complete
                )
                conn.commit()
                companies_succeeded += 1
                for key in totals:
                    totals[key] += getattr(stats, key)
                print(
                    f"[{company.slug}] seen={stats.seen} new={stats.new} "
                    f"updated={stats.updated} reopened={stats.reopened} closed={stats.closed}"
                )

            if i < len(companies) - 1:
                time.sleep(BETWEEN_BOARDS_DELAY_SECONDS)

        companies_failed = len(board_errors)
        status = compute_run_status(len(companies), companies_failed)

        if args.skip_digest:
            # Still run the full fetch/filter/round-robin pipeline so the
            # funnel counts are real -- just never call Resend or mark
            # anything notified. --skip-digest is for seeing what a send
            # would include without actually sending it.
            digest_result = digest.preview_digest(conn)
        else:
            digest_result = digest.send_digest(conn)

        label = "preview (--skip-digest)" if args.skip_digest else "digest"
        title_killed = sum(n for _, n in digest_result.title_excluded or [])
        location_killed = sum(n for _, n in digest_result.location_excluded or [])
        no_include = digest_result.pending_total - digest_result.include_matched_total

        # Reported as a funnel rather than three bare totals: each line is the
        # stage's survivor count plus what that stage removed, so the numbers
        # reconcile top to bottom instead of having to be inferred.
        print(f"{label} funnel:")
        print(f"  fetched (pending)          {digest_result.pending_total:>6}")
        print(
            f"  title-include matched      {digest_result.include_matched_total:>6}"
            f"   (-{no_include} matched no include keyword)"
        )
        print(
            f"  survived title excludes    "
            f"{digest_result.include_matched_total - title_killed:>6}   (-{title_killed})"
        )
        print(
            f"  survived location filter   {digest_result.matched_total:>6}"
            f"   (-{location_killed})"
        )
        print(f"    tier 1 (priority)        {digest_result.tier1_total:>6}   (never truncated)")
        print(f"    tier 2 (other)           {digest_result.tier2_total:>6}")
        capped = digest_result.matched_total - digest_result.included
        suffix = f"   (-{capped} over the {digest.MAX_POSTINGS_PER_DIGEST} cap)" if capped else ""
        print(f"  selected                   {digest_result.included:>6}{suffix}")

        if digest_result.title_excluded:
            print("\n  killed by title exclude (grouped by keyword, tune filters.yml):")
            for keyword, count in digest_result.title_excluded[:15]:
                print(f'    {count:>3}x  "{keyword}"')
        if digest_result.location_excluded:
            print("\n  failed location (passed title filter, tune filters.yml):")
            for location, count in digest_result.location_excluded[:15]:
                print(f"    {count:>3}x  {location}")
        if args.skip_digest:
            pass  # never attempts a send, nothing more to report
        elif digest_result.error:
            print(f"digest FAILED: {digest_result.error}", file=sys.stderr)
        elif digest_result.sent:
            print(f"digest sent: {digest_result.included} postings")
        elif digest_result.pending_total == 0:
            print("digest: nothing pending, skipped")
        elif digest_result.matched_total == 0:
            print("digest: nothing matched the filters, skipped")

        finished_at = datetime.now(UTC)

        run_id = db.record_run(
            conn,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            companies_total=len(companies),
            companies_succeeded=companies_succeeded,
            companies_failed=companies_failed,
            jobs_seen=totals["seen"],
            jobs_new=totals["new"],
            jobs_updated=totals["updated"],
            jobs_reopened=totals["reopened"],
            jobs_closed=totals["closed"],
            error=None,
            board_errors=board_errors,
            incomplete_observations=incomplete_observations,
            digest_pending_total=digest_result.pending_total,
            digest_matched_total=digest_result.matched_total,
            digest_sent=digest_result.sent,
            digest_postings_sent=digest_result.included if digest_result.sent else 0,
            digest_error=digest_result.error,
        )

        print(
            f"\nrun #{run_id} [{status}]: {companies_succeeded}/{len(companies)} boards ok, "
            f"jobs seen={totals['seen']} new={totals['new']} updated={totals['updated']} "
            f"reopened={totals['reopened']} closed={totals['closed']}"
        )
        if board_errors:
            print("board failures:", file=sys.stderr)
            for be in board_errors:
                print(f"  {be['company_slug']} ({be['ats']}): {be['error']}", file=sys.stderr)

        # Ingestion status and digest status are reported separately (see
        # schema.sql), but the process exit code needs to be a single signal —
        # GitHub Actions only has one red/green per run. Either kind of failure
        # should surface as a failed Action run, since both are things you'd
        # actually want to notice.
        return 0 if status != "failure" and not digest_result.error else 1

    except Exception as exc:  # noqa: BLE001 - top-level: record the crash, then re-raise
        finished_at = datetime.now(UTC)
        try:
            db.record_run(
                conn,
                started_at=started_at,
                finished_at=finished_at,
                status="failure",
                companies_total=0,
                companies_succeeded=0,
                companies_failed=0,
                jobs_seen=0,
                jobs_new=0,
                jobs_updated=0,
                jobs_reopened=0,
                jobs_closed=0,
                error=str(exc),
                board_errors=[],
            )
        except Exception:
            pass  # DB itself may be what's broken; don't mask the original error
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", help="fetch+print only this slug from companies.yml, no DB")
    parser.add_argument("--companies-file", default="companies.yml")
    parser.add_argument("--filters-file", default="filters.yml")
    parser.add_argument(
        "--skip-digest",
        action="store_true",
        help="ingest only, don't send (or attempt to send) the digest email",
    )
    args = parser.parse_args(argv)

    if args.company:
        return run_single_company(args)
    return run_full_ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
