"""Pure-function tests for db.py logic that doesn't need a live Postgres."""

from __future__ import annotations

import pytest

from job_ingest.config import ConfigError, load_companies
from job_ingest.db import (
    MAX_DESCRIPTION_CHARS,
    _resolve_against_stored,
    _truncate_description,
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
