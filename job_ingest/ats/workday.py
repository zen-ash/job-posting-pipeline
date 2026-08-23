"""Workday CXS job-board API.

Two-phase, unlike the other three sources:

  1. LIST   POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
            body {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
            -> {"total": N, "jobPostings": [...]}
  2. DETAIL GET  https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
            -> {"jobPostingInfo": {...}}

The list response is thin -- title, externalPath, locationsText, postedOn,
bulletFields, and nothing else. No description, no real timestamp (postedOn is
prose: "Posted 2 Days Ago"), and a free-text location that is frequently
unusable for filtering ("2 Locations", "ATLANTA FDC/BDC - 5865"). Measured
against this project's own filter, ~72% of Home Depot's postings failed the
location check on list data alone.

The detail endpoint has everything missing: a real `startDate`, the full
description, and -- most importantly -- a STRUCTURED country
(`country.alpha2Code == "US"`), which is better location evidence than any of
the other three ATSs provide.

Fetching detail for every posting is not affordable: ~0.56s each measured,
which is ~9.5 minutes for Home Depot's 1020 postings alone, against a 15-minute
workflow timeout for the entire run. So detail is fetched only for postings
whose TITLE already passes the filter -- typically a few percent of a board.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import requests

from job_ingest.config import Company
from job_ingest.filters import Filters, matches_title
from job_ingest.http import DEFAULT_TIMEOUT, get_session
from job_ingest.models import Posting

# Workday hard-caps page size at 20. Asking for more does NOT clamp and does
# NOT error -- it returns {"jobPostings": []} with HTTP 200, so a fetcher using
# limit=100 silently reports "this board has no jobs". Verified: limit 19 -> 19
# postings, 20 -> 20, 21 -> 0, 25 -> 0.
PAGE_SIZE = 20

# Safety valve so a misconfigured tenant can't spin forever. See the wrap-around
# note in fetch(); 500 pages * 20 = 10,000 postings, far above any real board here.
DEFAULT_MAX_PAGES = 500

# Politeness pause between detail requests, on top of the between-boards delay
# the run loop already applies. Detail is the high-volume phase, so it gets its
# own smaller delay rather than reusing the per-board one.
DETAIL_DELAY_SECONDS = 0.2

LIST_URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
# Human-facing apply URL. Note this one has NO /wday/cxs/{tenant} segment --
# verified byte-identical to the `externalUrl` the detail endpoint returns, so
# it can be built from list data alone without paying for a detail request.
APPLY_URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/{site}{path}"


def _require_workday_fields(company: Company) -> None:
    missing = [f for f in ("tenant", "wd_host", "site") if not getattr(company, f)]
    if missing:
        raise ValueError(
            f"company '{company.slug}' is ats=workday but missing {missing} "
            f"(see companies.yml)"
        )


def apply_url(company: Company, external_path: str) -> str:
    return APPLY_URL.format(
        tenant=company.tenant, wd_host=company.wd_host, site=company.site, path=external_path
    )


def fetch(
    company: Company,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    filters: Filters | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Returns raw list rows, each optionally carrying an enriched detail
    payload under the "_detail" key (see `_enrich`).

    Takes the whole Company rather than a board token because a Workday board
    is addressed by a tenant/host/site triple; the registry in
    job_ingest/ats/__init__.py handles that difference so the other three
    fetchers keep their existing single-token signature.
    """
    _require_workday_fields(company)
    session = session or get_session()
    url = LIST_URL.format(tenant=company.tenant, wd_host=company.wd_host, site=company.site)

    rows: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0

    for _ in range(max_pages):
        response = session.post(
            url,
            json={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()

        # `total` is populated ONLY on the first page; every later page reports
        # total: 0. Capture it once and never overwrite it.
        if total is None:
            total = int(payload.get("total") or 0)

        page = payload.get("jobPostings") or []
        if not page:
            break
        rows.extend(page)

        offset += PAGE_SIZE
        # THE termination condition, and it must be this one. Workday WRAPS
        # rather than ending: requesting offset >= total returns page 1 again
        # (verified -- offset=1020 on a 1020-posting board returned an
        # identical ID list to offset=0). So "stop when a page comes back
        # empty" never becomes true and would loop until max_pages, silently
        # re-ingesting the first page over and over.
        if total <= 0 or offset >= total:
            break

    if filters is not None:
        _enrich(rows, company, session=session, timeout=timeout, filters=filters)
    return rows


def _enrich(
    rows: list[dict[str, Any]],
    company: Company,
    *,
    session: requests.Session,
    timeout: float,
    filters: Filters,
) -> None:
    """Fetch the detail payload for rows whose title already passes the filter,
    attaching it in place under "_detail".

    This calls the SAME matches_title() the digest uses rather than
    reimplementing the rule, so a filters.yml edit moves both together. It is
    purely a cost optimisation -- the digest still applies the complete filter
    independently over what got stored, and never trusts this pass.

    CONSEQUENCE, deliberate and worth knowing: a posting whose title does not
    pass at fetch time is stored LIST-ONLY -- no description, no country_code,
    no source_updated_at. If filters.yml is later loosened so that posting
    would now qualify, its stored row stays thin until something re-enriches
    it. Its location will then be judged by the weak free-text path rather than
    the structured country, so it may be filtered out on location even though
    an enriched version of the same row would have passed. There is no backfill
    yet; re-enrichment happens only if the posting is edited upstream such that
    a later run re-fetches it. Building that backfill is deferred, not
    overlooked.
    """
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title or not matches_title(title, filters):
            continue
        path = row.get("externalPath")
        if not path:
            continue
        detail_url = DETAIL_URL.format(
            tenant=company.tenant, wd_host=company.wd_host, site=company.site, path=path
        )
        try:
            response = session.get(
                detail_url, headers={"Accept": "application/json"}, timeout=timeout
            )
            response.raise_for_status()
            row["_detail"] = response.json().get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError):
            # One posting's detail failing must not fail the whole board -- the
            # row simply stays list-only, exactly like a title that didn't
            # match. Board-level failures still propagate from fetch() above.
            continue
        time.sleep(DETAIL_DELAY_SECONDS)


def _external_id(row: dict[str, Any]) -> str | None:
    """Stable per-posting identifier, taken from LIST data only.

    Uses `externalPath` -- e.g.
    "/job/STORE-SUPPORT-CENTER-ATLANTA---9090/Lead-Data-Scientist_Req184175-1".

    Two candidates were rejected, both for reasons verified against live
    tenants rather than assumed:

    - The detail endpoint's opaque GUID is the best identifier in isolation,
      but only title-matching postings get enriched (see _enrich), so a
      "GUID when available" scheme would give one posting one ID while
      list-only and a different one once enriched. Against a permanent
      (source, company_slug, external_id) primary key in a table that never
      deletes, that forks a single posting into two rows -- the original
      closed, the new one reported as new.

    - bulletFields[0] is the requisition ID on some tenants ("Req184175" on
      homedepot, unique across all 120 sampled) but bulletFields is a
      tenant-configurable DISPLAY field, and it is NOT that everywhere.
      Verified on worldpay, where it is configured as
      [location, reqId] -- so bulletFields[0] there is "ATLANTA, GEORGIA",
      which collapsed 20 distinct postings into 9 distinct values. Silently
      merging unrelated jobs onto one primary key is far worse than the
      alternative below.

    externalPath is structural rather than display-configured, is present on
    every row, and was unique across every posting sampled on both tenants.
    Its one weakness is that it embeds a slug of the title and location, so an
    upstream title edit changes the ID and the posting reappears as new while
    the old row is closed. That is a visible duplicate rather than a silent
    merge, and is the tradeoff deliberately accepted here.
    """
    path = row.get("externalPath")
    return str(path).strip() if path and str(path).strip() else None


def _parse_start_date(value: str | None) -> datetime | None:
    """Workday's detail `startDate` is a plain "YYYY-MM-DD".

    The list response's `postedOn` is deliberately ignored: it is display prose
    ("Posted 2 Days Ago"), not a timestamp, and guessing a date from it would
    put a fabricated value in source_updated_at.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _country_code(detail: dict[str, Any]) -> str | None:
    """Pull the ISO alpha-2 country out of a detail payload.

    Order matters. The top-level `country` object carries only `descriptor`
    ("United States of America") and an opaque `id` -- NOT alpha2Code. The
    code lives on `jobRequisitionLocation.country`. Both are checked anyway,
    preferring the one actually observed to have it, since this is a Workday
    response shape rather than a documented contract.
    """
    nested = (detail.get("jobRequisitionLocation") or {}).get("country") or {}
    code = nested.get("alpha2Code")
    if code:
        return str(code).strip().upper()
    code = (detail.get("country") or {}).get("alpha2Code")
    return str(code).strip().upper() if code else None


def normalize(raw: list[dict[str, Any]], company: Company) -> list[Posting]:
    postings = []
    for row in raw:
        external_id = _external_id(row)
        if not external_id:
            continue
        detail = row.get("_detail") or {}
        location = detail.get("location") or row.get("locationsText")
        country = _country_code(detail)

        postings.append(
            Posting(
                source="workday",
                company_slug=company.slug,
                external_id=external_id,
                title=(row.get("title") or "").strip(),
                location=location,
                # Workday exposes no department/team on either endpoint.
                department=None,
                url=apply_url(company, row.get("externalPath") or ""),
                source_updated_at=_parse_start_date(detail.get("startDate")),
                description=detail.get("jobDescription") or "",
                country_code=country,
            )
        )
    return postings
