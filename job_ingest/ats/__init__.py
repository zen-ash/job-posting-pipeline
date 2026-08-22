"""Registry mapping an `ats` value from companies.yml to its fetcher module.

Every module here exposes the same two functions:
    fetch(board_token: str, *, session=None, timeout=...) -> raw JSON
    normalize(raw, company: Company) -> list[Posting]
"""

from __future__ import annotations

from job_ingest.ats import ashby, greenhouse, lever

FETCHERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}
