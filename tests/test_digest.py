"""Tests for digest content generation — pure functions, no DB or network."""

from __future__ import annotations

from datetime import UTC, datetime

from job_ingest.digest import (
    MAX_POSTINGS_PER_DIGEST,
    PendingPosting,
    _group_by_company,
    build_html_body,
    build_subject,
    build_text_body,
)


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
