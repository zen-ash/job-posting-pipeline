"""Tests for digest content generation — pure functions, no DB or network."""

from __future__ import annotations

from datetime import UTC, datetime

from job_ingest.digest import (
    MAX_POSTINGS_PER_DIGEST,
    PendingPosting,
    _group_by_company,
    _round_robin_by_company,
    apply_filters,
    build_html_body,
    build_subject,
    build_text_body,
)
from job_ingest.filters import Filters


def make_posting(company_name: str, title: str, **overrides) -> PendingPosting:
    defaults = dict(
        source="greenhouse",
        company_slug=company_name.lower(),
        external_id="1",
        company_name=company_name,
        title=title,
        location="Remote",
        url=f"https://example.com/{title}",
        first_seen_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PendingPosting(**defaults)


def test_build_subject_singular():
    assert build_subject(1) == build_subject(1)  # stable
    assert "1 new posting" in build_subject(1)
    assert "postings" not in build_subject(1)


def test_build_subject_plural():
    assert "5 new postings" in build_subject(5)


def test_group_by_company_groups_and_sorts_alphabetically():
    postings = [
        make_posting("Zeta Corp", "Analyst"),
        make_posting("Acme Inc", "Engineer B"),
        make_posting("Acme Inc", "Engineer A"),
    ]
    groups = _group_by_company(postings)

    assert list(groups.keys()) == ["Acme Inc", "Zeta Corp"]
    # within-company sorted by title
    assert [p.title for p in groups["Acme Inc"]] == ["Engineer A", "Engineer B"]


def test_text_body_includes_all_postings_and_their_links():
    postings = [make_posting("Acme Inc", "Data Engineer", location="NYC")]
    body = build_text_body(postings, overflow=0)

    assert "Acme Inc" in body
    assert "Data Engineer" in body
    assert "NYC" in body
    assert "https://example.com/Data Engineer" in body
    assert "more new posting" not in body


def test_text_body_shows_overflow_line_when_capped():
    postings = [make_posting("Acme Inc", "Data Engineer")]
    body = build_text_body(postings, overflow=23)

    assert "23 more new postings" in body
    assert str(MAX_POSTINGS_PER_DIGEST) in body


def test_text_body_singular_overflow_wording():
    postings = [make_posting("Acme Inc", "Data Engineer")]
    body = build_text_body(postings, overflow=1)

    assert "1 more new posting " in body or "1 more new posting\n" in body
    assert "postings not shown" not in body


def test_html_body_escapes_untrusted_content():
    postings = [
        make_posting(
            "Acme <script>alert(1)</script>",
            "Title & Stuff",
            location='"><img src=x>',
        )
    ]
    html = build_html_body(postings, overflow=0)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "Title &amp; Stuff" in html
    assert "<img src=x>" not in html


def test_html_body_includes_overflow_note_only_when_present():
    postings = [make_posting("Acme Inc", "Data Engineer")]

    with_overflow = build_html_body(postings, overflow=5)
    without_overflow = build_html_body(postings, overflow=0)

    assert "more new posting" in with_overflow
    assert "more new posting" not in without_overflow


# --- apply_filters ---------------------------------------------------------

FILTERS = Filters(
    title_include_keywords=("data analyst", "data engineer"),
    title_exclude_keywords=("senior", "manager"),
    location_include_keywords=("united states", "NY", "CA"),
)


def test_apply_filters_keeps_only_title_and_location_matches():
    us = "Remote, United States"
    postings = [
        make_posting("Acme", "Data Analyst", location=us, external_id="1"),
        make_posting("Acme", "Senior Data Analyst", location=us, external_id="2"),
        make_posting("Acme", "Data Analyst", location="Remote, Germany", external_id="3"),
        make_posting("Acme", "Account Executive", location=us, external_id="4"),
    ]
    matched, location_excluded = apply_filters(postings, FILTERS)

    assert [p.external_id for p in matched] == ["1"]
    # Only postings 2 title-fails (excluded), 3 title-passes but location-fails,
    # 4 title-fails -- so location_excluded should reflect only posting 3.
    assert location_excluded == [("Remote, Germany", 1)]


def test_apply_filters_groups_location_excluded_by_raw_location_string():
    postings = [
        make_posting("A", "Data Engineer", location="Remote, Germany", external_id="1"),
        make_posting("B", "Data Engineer", location="Remote, Germany", external_id="2"),
        make_posting("C", "Data Engineer", location="Remote, France", external_id="3"),
    ]
    _, location_excluded = apply_filters(postings, FILTERS)

    assert dict(location_excluded) == {"Remote, Germany": 2, "Remote, France": 1}
    # most-excluded first
    assert location_excluded[0] == ("Remote, Germany", 2)


def test_apply_filters_missing_location_is_labeled_and_counted():
    postings = [make_posting("A", "Data Engineer", location=None, external_id="1")]
    _, location_excluded = apply_filters(postings, FILTERS)

    assert location_excluded == [("(no location given)", 1)]


# --- round robin -------------------------------------------------------------


def test_round_robin_interleaves_across_companies():
    postings = [
        make_posting("Acme", "Acme 1", company_slug="acme", external_id="1"),
        make_posting("Acme", "Acme 2", company_slug="acme", external_id="2"),
        make_posting("Acme", "Acme 3", company_slug="acme", external_id="3"),
        make_posting("Beta", "Beta 1", company_slug="beta", external_id="1"),
        make_posting("Beta", "Beta 2", company_slug="beta", external_id="2"),
    ]
    result = _round_robin_by_company(postings)

    assert [p.title for p in result] == ["Acme 1", "Beta 1", "Acme 2", "Beta 2", "Acme 3"]


def test_round_robin_one_prolific_company_cannot_monopolize_a_capped_selection():
    # 40 from one company, 2 each from four others -- without round-robin, a
    # cap of 10 would be entirely (or almost entirely) the prolific company.
    postings = [
        make_posting("Big", f"Big {i}", company_slug="big", external_id=str(i))
        for i in range(40)
    ]
    for slug in ["b1", "b2", "b3", "b4"]:
        postings += [
            make_posting(slug, f"{slug} {i}", company_slug=slug, external_id=str(i))
            for i in range(2)
        ]

    capped = _round_robin_by_company(postings)[:10]
    companies_in_top_10 = {p.company_slug for p in capped}

    assert companies_in_top_10 == {"b1", "b2", "b3", "b4", "big"}


def test_round_robin_preserves_within_company_order():
    postings = [
        make_posting("Acme", "First", company_slug="acme", external_id="1"),
        make_posting("Acme", "Second", company_slug="acme", external_id="2"),
        make_posting("Zeta", "Only", company_slug="zeta", external_id="1"),
    ]
    result = [p for p in _round_robin_by_company(postings) if p.company_slug == "acme"]

    assert [p.title for p in result] == ["First", "Second"]
