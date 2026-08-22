"""Lever Postings API.

GET https://api.lever.co/v0/postings/{slug}?mode=json
Returns a bare JSON array (not wrapped in an object). Public, unauthenticated,
no list-of-boards endpoint. Note Lever only exposes a creation time (createdAt),
not a separate last-edited time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from job_ingest.config import Company
from job_ingest.http import DEFAULT_TIMEOUT, get_session
from job_ingest.models import Posting

BASE_URL = "https://api.lever.co/v0/postings/{slug}"


def fetch(
    board_token: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    session = session or get_session()
    response = session.get(
        BASE_URL.format(slug=board_token),
        params={"mode": "json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def normalize(raw: list[dict[str, Any]], company: Company) -> list[Posting]:
    postings = []
    for job in raw:
        categories = job.get("categories") or {}

        postings.append(
            Posting(
                source="lever",
                company_slug=company.slug,
                external_id=str(job["id"]),
                title=(job.get("text") or "").strip(),
                location=categories.get("location"),
                department=categories.get("team"),
                url=job.get("hostedUrl") or "",
                source_updated_at=_parse_epoch_ms(job.get("createdAt")),
                description=job.get("descriptionPlain") or "",
            )
        )
    return postings


def _parse_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
