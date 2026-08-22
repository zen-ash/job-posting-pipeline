"""Pure-function tests for db.py logic that doesn't need a live Postgres."""

from __future__ import annotations

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
