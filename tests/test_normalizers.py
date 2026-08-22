"""Normalizer tests run entirely against saved fixture JSON — no network access.

Each fixture holds 2 real postings (pulled from the live API while building this
pipeline) plus one hand-crafted entry with missing optional fields, to check the
normalizers degrade gracefully instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from job_ingest.ats import ashby, greenhouse, lever
from job_ingest.config import Company
from job_ingest.models import Posting, posting_content_hash

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


# --- Greenhouse ---------------------------------------------------------------

GITLAB = Company(slug="gitlab", name="GitLab", ats="greenhouse", board_token="gitlab")


def test_greenhouse_normalizes_all_jobs_to_postings():
    raw = load_fixture("greenhouse_gitlab.json")
    postings = greenhouse.normalize(raw, GITLAB)

    assert len(postings) == len(raw["jobs"])
    assert all(isinstance(p, Posting) for p in postings)
    assert all(p.source == "greenhouse" for p in postings)
    assert all(p.company_slug == "gitlab" for p in postings)


def test_greenhouse_maps_real_job_fields_correctly():
    raw = load_fixture("greenhouse_gitlab.json")
    postings = greenhouse.normalize(raw, GITLAB)
    first_raw = raw["jobs"][0]
    first = postings[0]

    assert first.external_id == str(first_raw["id"])
    assert first.title == first_raw["title"]
    assert first.location == first_raw["location"]["name"]
    assert first.department == first_raw["departments"][0]["name"]
    assert first.url == first_raw["absolute_url"]
    assert first.source_updated_at is not None
    assert first.description == first_raw["content"]


def test_greenhouse_handles_missing_department_and_still_parses_timestamp():
    raw = load_fixture("greenhouse_gitlab.json")
    postings = greenhouse.normalize(raw, GITLAB)
    edge_case = next(p for p in postings if p.external_id == "9999001")

    assert edge_case.department is None
    assert edge_case.location == "Remote"
    assert edge_case.source_updated_at is not None


def test_greenhouse_handles_empty_jobs_list():
    assert greenhouse.normalize({"jobs": []}, GITLAB) == []


# --- Lever -----------------------------------------------------------------

RO = Company(slug="ro", name="Ro", ats="lever", board_token="ro")


def test_lever_normalizes_all_jobs_to_postings():
    raw = load_fixture("lever_ro.json")
    postings = lever.normalize(raw, RO)

    assert len(postings) == len(raw)
    assert all(p.source == "lever" for p in postings)
    assert all(p.company_slug == "ro" for p in postings)


def test_lever_maps_real_job_fields_correctly():
    raw = load_fixture("lever_ro.json")
    postings = lever.normalize(raw, RO)
    first_raw = raw[0]
    first = postings[0]

    assert first.external_id == first_raw["id"]
    assert first.title == first_raw["text"]
    assert first.location == first_raw["categories"]["location"]
    assert first.department == first_raw["categories"]["team"]
    assert first.url == first_raw["hostedUrl"]
    assert first.description == first_raw["descriptionPlain"]
    # createdAt is epoch milliseconds; confirm it round-trips to the right second.
    assert first.source_updated_at is not None
    assert int(first.source_updated_at.timestamp() * 1000) == first_raw["createdAt"]


def test_lever_handles_missing_team_and_null_created_at():
    raw = load_fixture("lever_ro.json")
    postings = lever.normalize(raw, RO)
    edge_case = next(p for p in postings if p.external_id == "fixture-edge-case-0001")

    assert edge_case.department is None
    assert edge_case.source_updated_at is None


def test_lever_handles_empty_list():
    assert lever.normalize([], RO) == []


# --- Ashby -------------------------------------------------------------------

LINEAR = Company(slug="linear", name="Linear", ats="ashby", board_token="linear")


def test_ashby_normalizes_all_jobs_to_postings():
    raw = load_fixture("ashby_linear.json")
    postings = ashby.normalize(raw, LINEAR)

    assert len(postings) == len(raw["jobs"])
    assert all(p.source == "ashby" for p in postings)
    assert all(p.company_slug == "linear" for p in postings)


def test_ashby_maps_real_job_fields_correctly():
    raw = load_fixture("ashby_linear.json")
    postings = ashby.normalize(raw, LINEAR)
    first_raw = raw["jobs"][0]
    first = postings[0]

    assert first.external_id == first_raw["id"]
    assert first.title == first_raw["title"]
    assert first.location == first_raw["location"]
    assert first.url == first_raw["jobUrl"]
    assert first.description == first_raw["descriptionPlain"]
    assert first.source_updated_at is not None


def test_ashby_handles_missing_location_and_null_published_at():
    raw = load_fixture("ashby_linear.json")
    postings = ashby.normalize(raw, LINEAR)
    edge_case = next(p for p in postings if p.external_id == "fixture-edge-case-0001")

    assert edge_case.location is None
    assert edge_case.department is None
    assert edge_case.source_updated_at is None


def test_ashby_handles_empty_jobs_list():
    assert ashby.normalize({"jobs": []}, LINEAR) == []


# --- Cross-cutting: content_hash ---------------------------------------------


def test_content_hash_is_stable_for_identical_postings():
    raw = load_fixture("greenhouse_gitlab.json")
    p1 = greenhouse.normalize(raw, GITLAB)[0]
    p2 = greenhouse.normalize(raw, GITLAB)[0]

    assert posting_content_hash(p1) == posting_content_hash(p2)


def test_content_hash_changes_when_title_changes():
    raw = load_fixture("greenhouse_gitlab.json")
    original = greenhouse.normalize(raw, GITLAB)[0]
    edited = Posting(
        source=original.source,
        company_slug=original.company_slug,
        external_id=original.external_id,
        title=original.title + " (Updated)",
        location=original.location,
        department=original.department,
        url=original.url,
        source_updated_at=original.source_updated_at,
        description=original.description,
    )

    assert posting_content_hash(original) != posting_content_hash(edited)


def test_content_hash_is_same_across_different_sources_for_identical_content():
    # Sanity check that the hash is purely a function of content fields, not source
    # metadata — two different ATSs describing the same posting text should collide.
    common = dict(
        external_id="1",
        title="Data Engineer",
        location="Remote",
        department="Engineering",
        url="https://example.com/1",
        source_updated_at=None,
        description="Same posting text",
    )
    a = Posting(source="greenhouse", company_slug="acme", **common)
    b = Posting(source="lever", company_slug="acme", **common)

    assert posting_content_hash(a) == posting_content_hash(b)
