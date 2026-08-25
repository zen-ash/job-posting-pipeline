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

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class BoardFetch:
    """One board's postings plus whether they are a COMPLETE observation.

    `incomplete_reason` is None when the fetch saw the whole board. When it is
    set, the postings are still valid and still get upserted -- but a posting
    absent from a partial observation cannot be distinguished from one that
    closed, so closure detection must be suppressed for this company on this
    run. See db.sync_company_postings.
    """

    postings: list[Posting]
    incomplete_reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.incomplete_reason is None


def fetch_postings(
    company: Company,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    filters: Filters | None = None,
    already_enriched: set[str] | None = None,
) -> BoardFetch:
    """Fetch and normalize one company's board, whichever ATS it is on.

    `already_enriched` is Workday-only (see job_ingest/ats/workday.py) and is
    ignored by the other three, which have no second-phase request to skip.
    """
    module = FETCHERS[company.ats]
    if company.ats == "workday":
        raw = module.fetch(
            company,
            session=session,
            timeout=timeout,
            filters=filters,
            already_enriched=already_enriched,
        )
        reason = module.incomplete_reason(raw)
    else:
        # Greenhouse/Lever/Ashby each return their whole board in one
        # unpaginated response, so there is no partial-observation case for
        # them: either the request succeeded and we saw everything, or it
        # raised and sync is never called at all.
        raw = module.fetch(company.board_token, session=session, timeout=timeout)
        reason = None
    return BoardFetch(module.normalize(raw, company), reason)
