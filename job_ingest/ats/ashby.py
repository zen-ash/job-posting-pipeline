"""Ashby Job Board API.

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
Returns {"jobs": [...]}. Public, unauthenticated, no list-of-boards endpoint.
Ashby only exposes a publish time, not a separate last-edited time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from job_ingest.config import Company
from job_ingest.http import DEFAULT_TIMEOUT, get_session
from job_ingest.models import Posting

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(
    board_token: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    session = session or get_session()
    response = session.get(
        BASE_URL.format(slug=board_token),
        params={"includeCompensation": "true"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def normalize(raw: dict[str, Any], company: Company) -> list[Posting]:
    postings = []
    for job in raw.get("jobs", []):
        postings.append(
            Posting(
                source="ashby",
                company_slug=company.slug,
                external_id=str(job["id"]),
                title=(job.get("title") or "").strip(),
                location=job.get("location"),
                department=job.get("department") or job.get("team"),
                url=job.get("jobUrl") or "",
                source_updated_at=_parse_dt(job.get("publishedAt")),
                description=job.get("descriptionPlain") or "",
            )
        )
    return postings


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
