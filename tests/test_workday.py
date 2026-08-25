"""Workday fetcher tests — fixtures only, no network.

Pagination is exercised against a fake session that reproduces the two real
behaviours that make a naive loop wrong: `total` appearing only on page 1, and
offset >= total WRAPPING back to page 1 instead of returning an empty page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_ingest.ats import workday
from job_ingest.config import Company
from job_ingest.filters import Filters
from job_ingest.models import Posting

FIXTURES = Path(__file__).parent / "fixtures"

HOMEDEPOT = Company(
    slug="homedepot", name="Home Depot", ats="workday",
    tenant="homedepot", wd_host="wd5", site="CareerDepot",
)

FILTERS = Filters(
    title_include_keywords=("data scientist", "analyst"),
    title_exclude_keywords=("lead",),
    location_include_keywords=("united states", "us"),
    location_include_state_codes=("GA", "CA"),
)


def load(name):
    return json.loads((FIXTURES / name).read_text())


# --- normalize ---------------------------------------------------------------


def test_normalize_maps_list_only_rows():
    raw = load("workday_homedepot_list.json")["jobPostings"]
    postings = workday.normalize(raw, HOMEDEPOT)

    assert len(postings) == len(raw)
    assert all(isinstance(p, Posting) for p in postings)
    assert all(p.source == "workday" for p in postings)
    assert all(p.company_slug == "homedepot" for p in postings)
    # List-only rows carry no description, country, or real timestamp.
    assert all(p.description == "" for p in postings)
    assert all(p.country_code is None for p in postings)
    assert all(p.source_updated_at is None for p in postings)


def test_normalize_uses_external_path_as_external_id():
    raw = load("workday_homedepot_list.json")["jobPostings"]
    first = workday.normalize(raw, HOMEDEPOT)[0]
    assert first.external_id == raw[0]["externalPath"]


def test_external_id_never_uses_bulletfields():
    """bulletFields is a tenant-configurable DISPLAY field. On worldpay it is
    [location, reqId], so bulletFields[0] is a city name shared by many
    postings -- verified live, 20 postings collapsing to 9 distinct values.
    Using it as a primary key would silently merge unrelated jobs."""
    worldpay = Company(slug="gp-worldpay", name="Worldpay", ats="workday",
                       tenant="worldpay", wd_host="wd5",
                       site="Worldpay_External_Careers_Site")
    rows = [
        {"title": "A", "externalPath": "/job/ATLANTA-GEORGIA/A_JR1",
         "bulletFields": ["ATLANTA, GEORGIA", "JR1"]},
        {"title": "B", "externalPath": "/job/ATLANTA-GEORGIA/B_JR2",
         "bulletFields": ["ATLANTA, GEORGIA", "JR2"]},
    ]
    ids = [p.external_id for p in workday.normalize(rows, worldpay)]
    assert len(ids) == len(set(ids)) == 2, "colliding IDs would merge two jobs"
    assert "ATLANTA, GEORGIA" not in ids


def test_normalize_still_works_when_bulletfields_absent():
    raw = [r for r in load("workday_homedepot_list.json")["jobPostings"]
           if not r.get("bulletFields")]
    assert raw, "fixture should contain a row with empty bulletFields"
    posting = workday.normalize(raw, HOMEDEPOT)[0]
    assert posting.external_id == raw[0]["externalPath"]


def test_normalize_builds_apply_url_without_the_cxs_segment():
    raw = load("workday_homedepot_list.json")["jobPostings"]
    posting = workday.normalize(raw, HOMEDEPOT)[0]
    assert posting.url.startswith("https://homedepot.wd5.myworkdayjobs.com/CareerDepot/job/")
    assert "/wday/cxs/" not in posting.url


def test_normalize_handles_row_with_no_locations_text():
    raw = [r for r in load("workday_homedepot_list.json")["jobPostings"]
           if "locationsText" not in r]
    assert raw, "fixture should contain a row with no locationsText"
    assert workday.normalize(raw, HOMEDEPOT)[0].location is None


def test_normalize_prefers_detail_fields_when_enriched():
    raw = load("workday_homedepot_list.json")["jobPostings"][:1]
    raw[0]["_detail"] = load("workday_homedepot_detail.json")["jobPostingInfo"]
    posting = workday.normalize(raw, HOMEDEPOT)[0]

    assert posting.country_code == "US"
    assert posting.description.startswith("<")
    assert posting.source_updated_at is not None
    assert posting.source_updated_at.strftime("%Y-%m-%d") == "2026-08-21"


def test_external_id_is_identical_enriched_or_not():
    """The PK must not change when a list-only row later gets enriched --
    otherwise one posting forks into two rows in a never-deleting table."""
    raw = load("workday_homedepot_list.json")["jobPostings"][:1]
    bare = workday.normalize([dict(raw[0])], HOMEDEPOT)[0]

    enriched_row = dict(raw[0])
    enriched_row["_detail"] = load("workday_homedepot_detail.json")["jobPostingInfo"]
    enriched = workday.normalize([enriched_row], HOMEDEPOT)[0]

    assert bare.external_id == enriched.external_id
    # ...and specifically is NOT the detail endpoint's opaque GUID.
    assert enriched.external_id != enriched_row["_detail"]["id"]


def test_posted_on_prose_is_never_parsed_into_a_timestamp():
    raw = load("workday_homedepot_list.json")["jobPostings"]
    assert any("Posted" in r.get("postedOn", "") for r in raw)
    assert all(p.source_updated_at is None for p in workday.normalize(raw, HOMEDEPOT))


def test_normalize_handles_empty_list():
    assert workday.normalize([], HOMEDEPOT) == []


# --- pagination --------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeWorkdaySession:
    """Reproduces the real API's two pagination quirks.

    - `total` is returned only when offset == 0; later pages report total: 0.
    - offset >= total WRAPS to the first page rather than returning [].
    """

    def __init__(self, total, page_size=workday.PAGE_SIZE):
        self.total = total
        self.page_size = page_size
        self.offsets = []

    def post(self, url, json=None, headers=None, timeout=None):
        offset = json["offset"]
        self.offsets.append(offset)
        effective = offset % self.total if self.total else 0
        count = min(self.page_size, max(0, self.total - effective))
        postings = [
            {
                "title": f"Job {effective + i}",
                "externalPath": f"/job/j{effective + i}",
                "locationsText": "ATLANTA - 9090",
                "postedOn": "Posted 1 Day Ago",
                "bulletFields": [f"Req{effective + i}"],
            }
            for i in range(count)
        ]
        return FakeResponse(
            {"total": self.total if offset == 0 else 0, "jobPostings": postings}
        )


def test_pagination_stops_at_total_and_does_not_wrap():
    session = FakeWorkdaySession(total=45)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)

    assert len(rows) == 45
    assert session.offsets == [0, 20, 40]
    # The killer regression: if termination were "stop on an empty page" this
    # would keep going, since offset=45+ returns page 1 again rather than [].
    assert len(rows) == len(set(r["externalPath"] for r in rows))


def test_pagination_captures_total_from_first_page_only():
    session = FakeWorkdaySession(total=25)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    # Page 2 reports total: 0. If that overwrote the captured total, the loop
    # would stop after one page and silently drop the last 5 postings.
    assert len(rows) == 25


def test_pagination_exact_multiple_of_page_size_does_not_refetch_page_one():
    session = FakeWorkdaySession(total=40)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    assert len(rows) == 40
    assert session.offsets == [0, 20]
    assert len(set(r["externalPath"] for r in rows)) == 40


def test_pagination_respects_hard_cap():
    session = FakeWorkdaySession(total=10_000)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None, max_pages=3)
    assert len(rows) == 60
    assert len(session.offsets) == 3


def test_pagination_handles_empty_board():
    session = FakeWorkdaySession(total=0)
    assert workday.fetch(HOMEDEPOT, session=session, filters=None) == []


def test_page_size_is_twenty():
    # >20 returns an empty array with HTTP 200 rather than erroring, so this
    # constant is load-bearing, not a tuning knob.
    assert workday.PAGE_SIZE == 20


# --- enrichment gating -------------------------------------------------------


class FakeEnrichSession(FakeWorkdaySession):
    def __init__(self, total, titles):
        super().__init__(total)
        self.titles = titles
        self.detail_urls = []

    def post(self, url, json=None, headers=None, timeout=None):
        offset = json["offset"]
        self.offsets.append(offset)
        postings = [
            {
                "title": t,
                "externalPath": f"/job/j{i}",
                "locationsText": "ATLANTA - 9090",
                "postedOn": "Posted 1 Day Ago",
                "bulletFields": [f"Req{i}"],
            }
            for i, t in enumerate(self.titles)
        ]
        return FakeResponse({"total": self.total, "jobPostings": postings})

    def get(self, url, headers=None, timeout=None):
        self.detail_urls.append(url)
        return FakeResponse(
            {"jobPostingInfo": {"jobDescription": "<p>d</p>", "startDate": "2026-08-21",
                                "country": {"alpha2Code": "US"}, "location": "Atlanta, GA"}}
        )


def test_enrichment_only_fetches_detail_for_title_matching_postings(monkeypatch):
    monkeypatch.setattr(workday.time, "sleep", lambda s: None)
    titles = ["Data Scientist", "Warehouse Associate", "Lead Data Scientist", "Data Analyst"]
    session = FakeEnrichSession(total=len(titles), titles=titles)

    rows = workday.fetch(HOMEDEPOT, session=session, filters=FILTERS)

    # "Data Scientist" and "Data Analyst" match; "Warehouse Associate" has no
    # include keyword; "Lead Data Scientist" is killed by the "lead" exclude.
    assert len(session.detail_urls) == 2
    enriched = [r for r in rows if "_detail" in r]
    assert sorted(r["title"] for r in enriched) == ["Data Analyst", "Data Scientist"]


def test_enrichment_is_skipped_entirely_when_no_filters_given():
    session = FakeEnrichSession(total=1, titles=["Data Scientist"])
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    assert session.detail_urls == []
    assert all("_detail" not in r for r in rows)


def test_detail_urls_use_the_cxs_path():
    session = FakeEnrichSession(total=1, titles=["Data Scientist"])
    workday.fetch(HOMEDEPOT, session=session, filters=FILTERS)
    assert session.detail_urls[0] == (
        "https://homedepot.wd5.myworkdayjobs.com/wday/cxs/homedepot/CareerDepot/job/j0"
    )


def test_missing_workday_config_raises_clearly():
    bad = Company(slug="x", name="X", ats="workday", tenant="t")  # no wd_host/site
    with pytest.raises(ValueError, match="wd_host"):
        workday.fetch(bad, session=FakeWorkdaySession(total=1), filters=None)


# --- skipping re-enrichment of postings already stored ----------------------


def test_already_enriched_postings_are_not_refetched(monkeypatch):
    monkeypatch.setattr(workday.time, "sleep", lambda s: None)
    titles = ["Data Scientist", "Data Analyst"]
    session = FakeEnrichSession(total=len(titles), titles=titles)

    # /job/j0 is already stored; only /job/j1 should cost a request.
    workday.fetch(HOMEDEPOT, session=session, filters=FILTERS,
                  already_enriched={"/job/j0"})

    assert len(session.detail_urls) == 1
    assert session.detail_urls[0].endswith("/job/j1")


def test_empty_already_enriched_set_enriches_everything(monkeypatch):
    monkeypatch.setattr(workday.time, "sleep", lambda s: None)
    titles = ["Data Scientist", "Data Analyst"]
    session = FakeEnrichSession(total=len(titles), titles=titles)
    workday.fetch(HOMEDEPOT, session=session, filters=FILTERS, already_enriched=set())
    assert len(session.detail_urls) == 2


def test_already_enriched_defaults_to_enriching_when_not_supplied(monkeypatch):
    monkeypatch.setattr(workday.time, "sleep", lambda s: None)
    session = FakeEnrichSession(total=1, titles=["Data Scientist"])
    workday.fetch(HOMEDEPOT, session=session, filters=FILTERS)
    assert len(session.detail_urls) == 1


def test_skipping_uses_the_same_id_scheme_as_normalize(monkeypatch):
    """The skip set is keyed on external_id, so it must be built from the same
    field normalize() uses -- externalPath, not bulletFields."""
    monkeypatch.setattr(workday.time, "sleep", lambda s: None)
    session = FakeEnrichSession(total=1, titles=["Data Scientist"])
    rows = workday.fetch(HOMEDEPOT, session=session, filters=FILTERS)
    external_id = workday.normalize(rows, HOMEDEPOT)[0].external_id

    session2 = FakeEnrichSession(total=1, titles=["Data Scientist"])
    workday.fetch(HOMEDEPOT, session=session2, filters=FILTERS,
                  already_enriched={external_id})
    assert session2.detail_urls == []


# --- completeness / truncation detection ------------------------------------


def test_board_under_the_cap_is_a_complete_observation():
    session = FakeWorkdaySession(total=45)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    assert workday.incomplete_reason(rows) is None


def test_board_at_the_cap_is_flagged_incomplete():
    # Workday reports total == TOTAL_CAP for any board larger than the cap, so
    # the result set is a sliding window and absence proves nothing.
    session = FakeWorkdaySession(total=workday.TOTAL_CAP)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    reason = workday.incomplete_reason(rows)
    assert reason is not None
    assert str(workday.TOTAL_CAP) in reason


def test_hitting_the_page_budget_is_also_flagged_incomplete():
    # The other way an observation goes partial: our own max_pages ran out.
    session = FakeWorkdaySession(total=500)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None, max_pages=3)
    assert "max_pages" in (workday.incomplete_reason(rows) or "")


def test_incomplete_flag_does_not_leak_into_normalized_postings():
    session = FakeWorkdaySession(total=workday.TOTAL_CAP)
    rows = workday.fetch(HOMEDEPOT, session=session, filters=None)
    postings = workday.normalize(rows, HOMEDEPOT)
    assert len(postings) == len(rows)
    assert all(p.external_id.startswith("/job/") for p in postings)
