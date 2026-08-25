"""Pure-function tests for db.py logic that doesn't need a live Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Json

from job_ingest.config import Company, ConfigError, load_companies
from job_ingest.db import (
    MAX_DESCRIPTION_CHARS,
    _resolve_against_stored,
    _truncate_description,
    record_run,
    sync_company_postings,
)
from job_ingest.main import compute_run_status
from job_ingest.models import Posting, posting_content_hash


def test_truncate_description_leaves_short_text_untouched():
    text = "short description"
    assert _truncate_description(text) == text


def test_truncate_description_truncates_long_text_and_stays_within_budget():
    text = "x" * 5000
    result = _truncate_description(text)

    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert result.endswith("…[truncated]")
    assert result.startswith("x" * 100)


def test_truncate_description_boundary_exactly_at_limit_is_untouched():
    text = "x" * MAX_DESCRIPTION_CHARS
    assert _truncate_description(text) == text


def test_compute_run_status_all_succeeded():
    assert compute_run_status(companies_total=5, companies_failed=0) == "success"


def test_compute_run_status_all_failed():
    assert compute_run_status(companies_total=5, companies_failed=5) == "failure"


def test_compute_run_status_some_failed():
    assert compute_run_status(companies_total=5, companies_failed=2) == "partial_failure"


def test_compute_run_status_no_companies_configured():
    # Empty companies.yml isn't a failure, just a no-op run.
    assert compute_run_status(companies_total=0, companies_failed=0) == "success"


# --- companies.yml: workday rows alongside the existing three ATSs ----------


def _write(tmp_path, body):
    p = tmp_path / "companies.yml"
    p.write_text(body)
    return p


def test_existing_board_token_rows_remain_valid(tmp_path):
    path = _write(tmp_path, """
companies:
  - slug: gitlab
    name: GitLab
    ats: greenhouse
    board_token: gitlab
""")
    companies = load_companies(path)
    assert companies[0].board_token == "gitlab"
    assert companies[0].tenant is None


def test_workday_row_loads_with_tenant_host_site(tmp_path):
    path = _write(tmp_path, """
companies:
  - slug: homedepot
    name: Home Depot
    ats: workday
    tenant: homedepot
    wd_host: wd5
    site: CareerDepot
""")
    c = load_companies(path)[0]
    assert (c.tenant, c.wd_host, c.site) == ("homedepot", "wd5", "CareerDepot")
    assert c.board_token is None


def test_workday_row_missing_wd_host_is_rejected(tmp_path):
    path = _write(tmp_path, """
companies:
  - slug: homedepot
    name: Home Depot
    ats: workday
    tenant: homedepot
    site: CareerDepot
""")
    with pytest.raises(ConfigError, match="wd_host"):
        load_companies(path)


def test_greenhouse_row_missing_board_token_is_rejected(tmp_path):
    path = _write(tmp_path, """
companies:
  - slug: gitlab
    name: GitLab
    ats: greenhouse
""")
    with pytest.raises(ConfigError, match="board_token"):
        load_companies(path)


# --- skipped-enrichment resolution (Workday "don't re-enrich") --------------
#
# row layout matches the SELECT in sync_company_postings:
#   (content_hash, closed_at, title, location, department, description)


def _posting(**over):
    base = dict(
        source="workday", company_slug="homedepot", external_id="/job/j1",
        title="Data Analyst", location="Atlanta, GA", department=None,
        url="https://example.com/j1", source_updated_at=None, description="",
    )
    base.update(over)
    return Posting(**base)


def _row(description="stored body", title="Data Analyst",
         location="Atlanta, GA", department=None, content_hash="STOREDHASH"):
    return (content_hash, None, title, location, department, description)


def test_skipped_enrichment_keeps_stored_description_and_hash():
    # Nothing changed and no new body was fetched -> the row must look
    # untouched, not "updated", and must not lose its stored description.
    h, desc, _loc = _resolve_against_stored(_posting(), _row())
    assert desc == "stored body"
    assert h == "STOREDHASH"


def test_skipped_enrichment_still_detects_a_title_edit():
    # Title arrives from the list response every run, so an edit must change
    # the hash even though the body was never re-read.
    h, desc, _loc = _resolve_against_stored(_posting(title="Lead Data Analyst"), _row())
    assert desc == "stored body"
    assert h != "STOREDHASH"


def test_skipped_enrichment_still_detects_a_location_edit():
    h, _, _loc = _resolve_against_stored(_posting(location="Austin, TX"), _row())
    assert h != "STOREDHASH"


def test_incoming_description_wins_when_present():
    # A genuine re-enrichment (or a first enrichment) must overwrite.
    h, desc, _loc = _resolve_against_stored(_posting(description="fresh body"), _row())
    assert desc == "fresh body"
    assert h == posting_content_hash(_posting(description="fresh body"))


def test_empty_incoming_and_empty_stored_is_a_normal_list_only_row():
    # Both empty -> nothing special; hash over the empty body as usual.
    h, desc, _loc = _resolve_against_stored(_posting(), _row(description=""))
    assert desc == ""
    assert h == posting_content_hash(_posting())


def test_stored_null_description_is_treated_as_empty():
    h, desc, _loc = _resolve_against_stored(_posting(), _row(description=None))
    assert desc == ""
    assert h == posting_content_hash(_posting())


def test_skipped_enrichment_is_stable_across_repeated_runs():
    # The failure this guards: an empty incoming description overwriting the
    # stored one would change the hash every night and flap the row forever.
    row = _row()
    h1, d1, _l1 = _resolve_against_stored(_posting(), row)
    h2, d2, _l2 = _resolve_against_stored(_posting(), (h1, None, "Data Analyst",
                                                       "Atlanta, GA", None, d1))
    assert (h1, d1) == (h2, d2) == ("STOREDHASH", "stored body")


def test_skipped_enrichment_does_not_downgrade_a_stored_location():
    # The regression this guards, observed live: an already-enriched posting
    # stored location "Raleigh, NC" from its detail payload. On the next run
    # its detail was (correctly) skipped, the fetcher emitted the LIST value
    # "2 Locations", and that placeholder overwrote the specific location --
    # so the digest would show "2 Locations" to the reader. The fetcher now
    # emits None for a skipped row, meaning "no new information".
    h, desc, loc = _resolve_against_stored(
        _posting(location=None), _row(location="Raleigh, NC")
    )
    assert loc == "Raleigh, NC"
    assert desc == "stored body"
    assert h == "STOREDHASH"  # and it must not read as an edit


def test_none_location_is_kept_from_storage_even_on_a_fresh_enrichment():
    h, desc, loc = _resolve_against_stored(
        _posting(location=None, description="fresh body"), _row(location="Raleigh, NC")
    )
    assert loc == "Raleigh, NC"
    assert desc == "fresh body"


def test_a_real_list_location_edit_still_overwrites():
    # None means "no info"; an actual string means the list reported a change.
    _h, _desc, loc = _resolve_against_stored(
        _posting(location="Austin, TX"), _row(location="Raleigh, NC")
    )
    assert loc == "Austin, TX"


# --- closure requires a COMPLETE observation --------------------------------


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return None  # every posting looks new; we only care about the close step


class _FakeConn:
    def __init__(self):
        self._cur = _FakeCursor()

    def cursor(self):
        return self._cur

    def commit(self):
        pass


def _closing_statements(cur):
    return [s for s in cur.executed if "closed_at = now()" in s]


def _wd_posting(external_id):
    return Posting(
        source="workday", company_slug="citi", external_id=external_id,
        title="Data Analyst", location="New York, NY", department=None,
        url="https://example.com/x", source_updated_at=None, description="",
    )


CITI = Company(slug="citi", name="Citi", ats="workday",
               tenant="citi", wd_host="wd5", site="2")


def test_complete_observation_still_closes_absent_postings():
    conn = _FakeConn()
    sync_company_postings(conn, CITI, [_wd_posting("/job/a")], complete=True)
    assert len(_closing_statements(conn._cur)) == 1


def test_truncated_observation_closes_nothing():
    # The whole point: a posting absent from a truncated fetch is
    # indistinguishable from one that closed, so nothing may be closed.
    conn = _FakeConn()
    stats = sync_company_postings(conn, CITI, [_wd_posting("/job/a")], complete=False)
    assert _closing_statements(conn._cur) == []
    assert stats.closed == 0


def test_truncated_observation_still_upserts_what_it_saw():
    # Suppressing closure must not suppress ingestion.
    conn = _FakeConn()
    stats = sync_company_postings(
        conn, CITI, [_wd_posting("/job/a"), _wd_posting("/job/b")], complete=False
    )
    assert stats.seen == 2
    assert stats.new == 2
    assert any("INSERT INTO jobs" in s for s in conn._cur.executed)


def test_complete_defaults_to_true_so_existing_callers_are_unchanged():
    conn = _FakeConn()
    sync_company_postings(conn, CITI, [_wd_posting("/job/a")])
    assert len(_closing_statements(conn._cur)) == 1


class _RecordingCursor(_FakeCursor):
    def __init__(self):
        super().__init__()
        self.params = []

    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.params.append(params)

    def fetchone(self):
        return (1,)  # the RETURNING id from the runs insert


class _RecordingConn(_FakeConn):
    def __init__(self):
        self._cur = _RecordingCursor()


def test_incomplete_observations_are_recorded_on_the_run():
    # jobs_closed under-reports on a suppressed run by design, so the run row
    # has to say WHY -- otherwise "nothing closed" is ambiguous after the fact.
    conn = _RecordingConn()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    record_run(
        conn, started_at=now, finished_at=now, status="success",
        companies_total=1, companies_succeeded=1, companies_failed=0,
        jobs_seen=2000, jobs_new=0, jobs_updated=0, jobs_reopened=0, jobs_closed=0,
        error=None, board_errors=[],
        incomplete_observations=[{"company_slug": "citi", "reason": "total=2000 at cap"}],
    )
    sql = conn._cur.executed[0]
    assert "incomplete_observations" in sql
    payload = [p for p in conn._cur.params[0] if isinstance(p, Json)]
    dumped = [p.obj for p in payload]
    assert [{"company_slug": "citi", "reason": "total=2000 at cap"}] in dumped


def test_incomplete_observations_defaults_to_empty():
    conn = _RecordingConn()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    record_run(
        conn, started_at=now, finished_at=now, status="success",
        companies_total=1, companies_succeeded=1, companies_failed=0,
        jobs_seen=1, jobs_new=0, jobs_updated=0, jobs_reopened=0, jobs_closed=0,
        error=None, board_errors=[],
    )
    dumped = [p.obj for p in conn._cur.params[0] if isinstance(p, Json)]
    assert [] in dumped


# --- reopen path and notification state -------------------------------------


class _ExistingRowCursor(_RecordingCursor):
    """Cursor whose SELECT reports an existing row, optionally already closed."""

    def __init__(self, closed_at):
        super().__init__()
        self._closed_at = closed_at

    def fetchone(self):
        # (content_hash, closed_at, title, location, department, description)
        return ("HASH", self._closed_at, "Data Analyst", "New York, NY", None, "body")


class _ExistingRowConn(_FakeConn):
    def __init__(self, closed_at):
        self._cur = _ExistingRowCursor(closed_at)


def _update_params(conn):
    for sql, params in zip(conn._cur.executed, conn._cur.params, strict=False):
        if "UPDATE jobs" in sql and "notified_at = CASE" in sql:
            return params
    raise AssertionError("no upsert UPDATE issued")


def test_reopening_a_closed_posting_clears_notified_at():
    """Documents real current behaviour, deliberate but sharp-edged.

    A posting that was closed and comes back has its notified_at reset, so it
    is delivered again -- the intent being that a genuinely re-opened role is
    newly actionable. The hazard is that it makes any FALSE closure become a
    duplicate delivery, which is precisely what notified_at exists to prevent.
    That is why closure now requires a complete observation (see
    sync_company_postings): removing the false closures removes the spurious
    reopens, rather than weakening the reopen semantics themselves.
    """
    conn = _ExistingRowConn(closed_at=datetime(2026, 8, 1, tzinfo=UTC))
    sync_company_postings(conn, CITI, [_wd_posting("/job/a")], complete=True)
    params = _update_params(conn)
    assert True in params, "the reopened flag should be set, clearing notified_at"


def test_an_already_open_posting_does_not_touch_notified_at():
    conn = _ExistingRowConn(closed_at=None)
    sync_company_postings(conn, CITI, [_wd_posting("/job/a")], complete=True)
    params = _update_params(conn)
    assert True not in params, "an unclosed posting must not reset notification state"


def test_reopen_is_counted_in_stats():
    conn = _ExistingRowConn(closed_at=datetime(2026, 8, 1, tzinfo=UTC))
    stats = sync_company_postings(conn, CITI, [_wd_posting("/job/a")], complete=True)
    assert stats.reopened == 1
