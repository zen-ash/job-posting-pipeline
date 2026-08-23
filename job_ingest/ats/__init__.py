"""Registry mapping an `ats` value from companies.yml to its fetcher module.

greenhouse/lever/ashby each expose:
    fetch(board_token: str, *, session=None, timeout=...) -> raw JSON
    normalize(raw, company: Company) -> list[Posting]

workday differs -- it is addressed by a tenant/host/site triple rather than a
single token, and it takes the filters so it can decide which postings are
worth a second (detail) request. `fetch_postings` below absorbs that
difference so callers stay uniform and the original three modules keep their
existing signatures unchanged.
"""

from __future__ import annotations

import requests

from job_ingest.ats import ashby, greenhouse, lever, workday
from job_ingest.config import Company
from job_ingest.filters import Filters
from job_ingest.http import DEFAULT_TIMEOUT
from job_ingest.models import Posting

FETCHERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
}


def fetch_postings(
    company: Company,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    filters: Filters | None = None,
) -> list[Posting]:
    """Fetch and normalize one company's board, whichever ATS it is on."""
    module = FETCHERS[company.ats]
    if company.ats == "workday":
        raw = module.fetch(company, session=session, timeout=timeout, filters=filters)
    else:
        raw = module.fetch(company.board_token, session=session, timeout=timeout)
    return module.normalize(raw, company)
