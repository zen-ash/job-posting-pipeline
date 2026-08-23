"""Pure-function tests for db.py logic that doesn't need a live Postgres."""

from __future__ import annotations

import pytest

from job_ingest.config import ConfigError, load_companies
from job_ingest.db import MAX_DESCRIPTION_CHARS, _truncate_description
from job_ingest.main import compute_run_status


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
