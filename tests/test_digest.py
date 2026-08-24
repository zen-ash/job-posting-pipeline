"""Tests for digest content generation — pure functions, no DB or network."""

from __future__ import annotations

from datetime import UTC, datetime

from job_ingest.digest import (
    MAX_POSTINGS_PER_DIGEST,
    PendingPosting,
    TieredSelection,
    _group_by_company,
    _round_robin_by_company,
    apply_filters,
    build_html_body,
    build_subject,
    build_text_body,
    select_tiered,
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


def _sel(tier1=(), tier2=(), tier2_omitted=0, tier1_over_cap=0):
    return TieredSelection(list(tier1), list(tier2), tier2_omitted, tier1_over_cap)


def test_text_body_includes_all_postings_and_their_links():
    postings = [make_posting("Acme Inc", "Data Engineer", location="NYC")]
    body = build_text_body(_sel(tier2=postings))

    assert "Acme Inc" in body
    assert "Data Engineer" in body
    assert "NYC" in body
    assert "https://example.com/Data Engineer" in body
    assert "more new posting" not in body


def test_text_body_shows_overflow_line_when_capped():
    postings = [make_posting("Acme Inc", "Data Engineer")]
    body = build_text_body(_sel(tier2=postings, tier2_omitted=23))

    assert "23 more new postings" in body
    assert str(MAX_POSTINGS_PER_DIGEST) in body


def test_text_body_singular_overflow_wording():
    postings = [make_posting("Acme Inc", "Data Engineer")]
    body = build_text_body(_sel(tier2=postings, tier2_omitted=1))

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
    html = build_html_body(_sel(tier2=postings))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "Title &amp; Stuff" in html
    assert "<img src=x>" not in html


def test_html_body_includes_overflow_note_only_when_present():
    postings = [make_posting("Acme Inc", "Data Engineer")]

    with_overflow = build_html_body(_sel(tier2=postings, tier2_omitted=5))
    without_overflow = build_html_body(_sel(tier2=postings))

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
    matched, location_excluded, title_excluded, _inc = apply_filters(postings, FILTERS)

    assert [p.external_id for p in matched] == ["1"]
    # posting 2 matched an include but was killed by "senior" -- title_excluded.
    # posting 3 passed title but failed location -- location_excluded.
    # posting 4 never matched any include keyword -- not a signal for either.
    assert title_excluded == [("senior", 1)]
    assert location_excluded == [("Remote, Germany", 1)]


def test_apply_filters_groups_location_excluded_by_raw_location_string():
    postings = [
        make_posting("A", "Data Engineer", location="Remote, Germany", external_id="1"),
        make_posting("B", "Data Engineer", location="Remote, Germany", external_id="2"),
        make_posting("C", "Data Engineer", location="Remote, France", external_id="3"),
    ]
    _, location_excluded, _, _inc = apply_filters(postings, FILTERS)

    assert dict(location_excluded) == {"Remote, Germany": 2, "Remote, France": 1}
    # most-excluded first
    assert location_excluded[0] == ("Remote, Germany", 2)


def test_apply_filters_missing_location_is_labeled_and_counted():
    postings = [make_posting("A", "Data Engineer", location=None, external_id="1")]
    _, location_excluded, _, _inc = apply_filters(postings, FILTERS)

    assert location_excluded == [("(no location given)", 1)]


def test_apply_filters_groups_title_excluded_by_which_exclude_keyword_fired():
    us = "Remote, United States"
    postings = [
        make_posting("A", "Senior Data Analyst", location=us, external_id="1"),
        make_posting("B", "Senior Data Engineer", location=us, external_id="2"),
        make_posting("C", "Data Analyst Manager", location=us, external_id="3"),
        make_posting("D", "Data Analyst", location=us, external_id="4"),
    ]
    matched, _, title_excluded, _inc = apply_filters(postings, FILTERS)

    assert [p.external_id for p in matched] == ["4"]
    assert dict(title_excluded) == {"senior": 2, "manager": 1}
    # most-excluded first
    assert title_excluded[0] == ("senior", 2)


def test_apply_filters_title_excluded_ignores_postings_that_never_matched_an_include():
    # A title with no include keyword at all isn't a tuning signal for either
    # report, even if it happens to also contain an exclude keyword.
    postings = [
        make_posting("A", "Senior Account Executive", location="Remote, US", external_id="1")
    ]
    matched, location_excluded, title_excluded, _inc = apply_filters(postings, FILTERS)

    assert matched == []
    assert location_excluded == []
    assert title_excluded == []


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


# --- priority tiering --------------------------------------------------------

TIER_FILTERS = Filters(
    title_include_keywords=("analyst",),
    title_exclude_keywords=(),
    location_include_keywords=("united states",),
    priority_keywords=("intern", "co-op", "2027", "analyst program"),
)


def _p(company, title, eid):
    return make_posting(company, title, company_slug=company.lower(), external_id=eid)


def test_tier_split_puts_priority_titles_first():
    matched = [
        _p("Acme", "Data Analyst", "1"),
        _p("Acme", "Summer 2027 Analyst Intern", "2"),
        _p("Acme", "Risk Analyst", "3"),
        _p("Acme", "Co-Op, Audit Analyst", "4"),
    ]
    sel = select_tiered(matched, TIER_FILTERS)

    assert sorted(p.external_id for p in sel.tier1) == ["2", "4"]
    assert sorted(p.external_id for p in sel.tier2) == ["1", "3"]
    # tier 1 is listed before tier 2 in what actually gets sent
    assert sel.all_postings[: len(sel.tier1)] == sel.tier1


def test_tiering_never_drops_a_posting_it_only_reorders():
    matched = [_p("Acme", t, str(i)) for i, t in enumerate(
        ["Data Analyst", "Analyst Program 2027", "Risk Analyst"])]
    sel = select_tiered(matched, TIER_FILTERS, cap=50)
    assert len(sel.all_postings) == len(matched)


def test_round_robin_applies_within_each_tier():
    matched = [
        _p("Acme", "Intern Analyst A", "1"), _p("Acme", "Intern Analyst B", "2"),
        _p("Beta", "Intern Analyst C", "3"),
        _p("Acme", "Data Analyst A", "4"), _p("Acme", "Data Analyst B", "5"),
        _p("Beta", "Data Analyst C", "6"),
    ]
    sel = select_tiered(matched, TIER_FILTERS)
    assert [p.company_slug for p in sel.tier1] == ["acme", "beta", "acme"]
    assert [p.company_slug for p in sel.tier2] == ["acme", "beta", "acme"]


def test_cap_is_filled_from_tier_one_first():
    matched = ([_p("Acme", f"Intern Analyst {i}", f"a{i}") for i in range(4)]
               + [_p("Beta", f"Data Analyst {i}", f"b{i}") for i in range(10)])
    sel = select_tiered(matched, TIER_FILTERS, cap=6)

    assert len(sel.tier1) == 4          # all priority postings kept
    assert len(sel.tier2) == 2          # only the remaining cap room
    assert len(sel.all_postings) == 6
    assert sel.tier2_omitted == 8
    assert sel.tier1_over_cap == 0


def test_tier_one_overflow_exceeds_the_cap_rather_than_truncating():
    # The rule that matters most: an internship must never be dropped for
    # being posting number 51.
    matched = [_p("Acme", f"Intern Analyst {i}", f"a{i}") for i in range(9)]
    sel = select_tiered(matched, TIER_FILTERS, cap=5)

    assert len(sel.tier1) == 9                 # nothing truncated
    assert len(sel.all_postings) == 9          # cap deliberately exceeded
    assert sel.tier1_over_cap == 4
    assert sel.tier2 == []
    assert sel.tier2_omitted == 0


def test_tier_one_overflow_leaves_no_room_for_tier_two():
    matched = ([_p("Acme", f"Intern Analyst {i}", f"a{i}") for i in range(7)]
               + [_p("Beta", f"Data Analyst {i}", f"b{i}") for i in range(3)])
    sel = select_tiered(matched, TIER_FILTERS, cap=5)

    assert len(sel.tier1) == 7
    assert sel.tier2 == []
    assert sel.tier2_omitted == 3
    assert sel.tier1_over_cap == 2


def test_body_labels_both_tiers_and_warns_on_tier_one_overflow():
    sel = TieredSelection(
        tier1=[make_posting("Acme", "Intern Analyst")],
        tier2=[make_posting("Beta", "Data Analyst")],
        tier2_omitted=7,
        tier1_over_cap=3,
    )
    for body in (build_text_body(sel), build_html_body(sel)):
        assert "TIER 1" in body and "TIER 2" in body
        assert "exceed" in body                      # the overflow warning
        assert "7 from Tier 2" in body               # tier-split breakdown
        assert "0 from Tier 1" in body


def test_overflow_line_breaks_down_by_tier():
    sel = TieredSelection([], [make_posting("A", "Data Analyst")], tier2_omitted=12,
                          tier1_over_cap=0)
    body = build_text_body(sel)
    assert "12 more new postings" in body
    assert "0 from Tier 1" in body
    assert "12 from Tier 2" in body
    assert "exceed" not in body       # no warning when tier 1 fits


def test_body_omits_an_empty_tier_heading():
    sel = TieredSelection([], [make_posting("A", "Data Analyst")], 0, 0)
    assert "TIER 1" not in build_text_body(sel)
    assert "TIER 2" in build_text_body(sel)
