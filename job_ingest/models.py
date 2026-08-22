"""The shared shape every ATS normalizer produces, independent of source quirks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Posting:
    """One job posting, normalized to a common shape regardless of which ATS it
    came from. Fetchers in `job_ingest.ats` are responsible for mapping their
    source's raw JSON onto this.
    """

    source: str  # "greenhouse" | "lever" | "ashby"
    company_slug: str  # our internal slug from companies.yml, not the ATS's own name
    external_id: str  # the id the ATS assigns this posting, stringified
    title: str
    location: str | None
    department: str | None
    url: str
    # Best-effort "when this posting last changed" timestamp *as reported by the
    # source* (Greenhouse: updated_at, Lever: createdAt, Ashby: publishedAt — note
    # Lever/Ashby only ever give a creation/publish time, not a true edit time).
    # This is descriptive only. Actual new/edited detection is done in the DB layer
    # by diffing content_hash against what we stored last run, not by trusting this.
    source_updated_at: datetime | None
    # Raw text/HTML used only as hashing input to detect edits to an existing
    # posting; not rendered anywhere itself.
    description: str


def posting_content_hash(posting: Posting) -> str:
    """Hash of the fields that make up a posting's substance. Two fetches of the
    same posting produce the same hash iff nothing meaningful changed, which is
    how the DB layer (step 3) tells "unchanged, just seen again" apart from
    "this existing posting was edited".
    """
    normalized = "\x1f".join(
        [
            posting.title.strip(),
            (posting.location or "").strip(),
            (posting.department or "").strip(),
            posting.description.strip(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
