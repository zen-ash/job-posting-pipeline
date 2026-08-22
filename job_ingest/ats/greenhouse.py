"""Greenhouse Job Board API.

GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
Returns {"jobs": [...]}. Public, unauthenticated, no list-of-boards endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from job_ingest.config import Company
from job_ingest.http import DEFAULT_TIMEOUT, get_session
from job_ingest.models import Posting

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"


def fetch(
    board_token: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    session = session or get_session()
    response = session.get(
        BASE_URL.format(board_token=board_token),
        params={"content": "true"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def normalize(raw: dict[str, Any], company: Company) -> list[Posting]:
    postings = []
    for job in raw.get("jobs", []):
        location = (job.get("location") or {}).get("name")
        departments = job.get("departments") or []
        department = departments[0].get("name") if departments else None

        postings.append(
            Posting(
                source="greenhouse",
                company_slug=company.slug,
                external_id=str(job["id"]),
                title=(job.get("title") or "").strip(),
                location=location,
                department=department,
                url=job.get("absolute_url") or "",
                source_updated_at=_parse_dt(job.get("updated_at")),
                description=job.get("content") or "",
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
