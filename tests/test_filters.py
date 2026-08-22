"""Tests for job_ingest.filters — pure functions, no DB or network."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_ingest.filters import (
    Filters,
    load_filters,
    matches,
    matches_location,
    matches_title,
    title_exclude_hit,
    title_include_match,
)

REPO_ROOT = Path(__file__).parent.parent
REAL_FILTERS = load_filters(REPO_ROOT / "filters.yml")

FILTERS = Filters(
    title_include_keywords=(
        "data analyst",
        "analytics",
        "business intelligence",
        "data engineer",
        "data scientist",
        "reporting",
        "insights",
        "sql",
    ),
    title_exclude_keywords=(
        "senior",
        "staff",
        "principal",
        "lead",
        "manager",
        "director",
        "vp",
        "head of",
    ),
    location_include_keywords=(
        "united states",
        "usa",
        "remote, us",
        "north america",
        "NY",
        "CA",
        "IL",
        "OR",
    ),
)


# --- title matching -----------------------------------------------------------


def test_title_matches_an_include_keyword():
    assert matches_title("Data Analyst", FILTERS) is True


def test_title_rejects_without_any_include_keyword():
    assert matches_title("Account Executive", FILTERS) is False


def test_title_rejects_when_exclude_keyword_present_even_with_include_match():
    assert matches_title("Senior Data Analyst", FILTERS) is False


def test_title_matching_is_case_insensitive():
    assert matches_title("DATA ENGINEER", FILTERS) is True
    assert matches_title("senior Analytics Lead", FILTERS) is False


def test_title_exclude_is_whole_word_not_substring():
    # "Leadership" contains "lead" as a substring but not as a whole word.
    assert matches_title("Data Analyst, Leadership Development", FILTERS) is True


def test_title_include_sql_does_not_match_inside_mysql():
    # "sql" must not match as a substring of "MySQL" -- whole-word only.
    assert matches_title("MySQL Administrator", FILTERS) is False


def test_title_include_multiword_phrase_matches():
    assert matches_title("Business Intelligence Analyst", FILTERS) is True


# --- location matching ----------------------------------------------------


def test_location_matches_country_phrase():
    assert matches_location("Remote, United States", FILTERS) is True


def test_location_matches_state_code_in_city_state_format():
    assert matches_location("Romeoville, IL", FILTERS) is True


def test_location_matches_one_segment_of_multi_value_string():
    assert matches_location("Remote, Canada; Remote, United States", FILTERS) is True


def test_location_none_or_empty_does_not_match():
    assert matches_location(None, FILTERS) is False
    assert matches_location("", FILTERS) is False


def test_location_state_code_does_not_false_positive_on_containing_word():
    # "CA" must not match inside "Canada" (no word boundary), and plain "US"-like
    # substrings must not match inside unrelated country names.
    assert matches_location("Remote, Canada", FILTERS) is False


def test_location_us_substring_does_not_false_positive_inside_other_countries():
    # "us" (implied via other keywords) should never match "Russia"/"Australia"-
    # style names just because the letters appear inside them.
    strict_filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=("us",),
    )
    assert matches_location("Remote, Russia", strict_filters) is False
    assert matches_location("Sydney, Australia", strict_filters) is False
    assert matches_location("Remote, US", strict_filters) is True


def test_location_bare_remote_with_no_country_does_not_match():
    # No country/state signal at all -- ambiguous, should be excluded by default.
    assert matches_location("Remote", FILTERS) is False


# --- combined ------------------------------------------------------------


def test_matches_requires_both_title_and_location():
    assert matches("Data Analyst", "Remote, United States", FILTERS) is True
    assert matches("Data Analyst", "Remote, Germany", FILTERS) is False
    assert matches("Account Executive", "Remote, United States", FILTERS) is False


# --- exclude beats include -------------------------------------------------


def test_exclude_beats_include_when_both_match():
    # "risk" is an include keyword and "director" is an exclude keyword --
    # both fire on this title, and exclude must win.
    filters = Filters(
        title_include_keywords=("associate", "risk"),
        title_exclude_keywords=("director",),
        location_include_keywords=(),
    )
    assert matches_title("Associate Director, Risk Management", filters) is False


def test_exclude_beats_include_against_the_real_filters_yml():
    # Regression test against the actual project config, not a synthetic
    # Filters object -- if filters.yml ever loses this exclude, this fails.

    # Same include signals ("associate", "risk"), no exclude keyword present --
    # proves the include side genuinely matches, so the next assertion isn't
    # vacuously true.
    assert matches_title("Associate, Risk Management", REAL_FILTERS) is True

    # Adding "Director" must flip this to excluded even though the title still
    # matches "associate" and "risk" in title_include_keywords.
    assert matches_title("Associate Director, Risk Management", REAL_FILTERS) is False


def test_legal_roles_excluded_despite_broad_compliance_and_risk_includes():
    # "compliance" and "risk" are broad includes in the real filters.yml and
    # will fire on legal-adjacent titles that aren't actually data/analyst
    # roles -- counsel/attorney/paralegal excludes exist specifically to catch
    # those.
    assert matches_title("Compliance Counsel", REAL_FILTERS) is False
    assert matches_title("Attorney, Regulatory Compliance", REAL_FILTERS) is False
    assert matches_title("Paralegal, Risk & Compliance", REAL_FILTERS) is False
    assert matches_title("Vice President, Compliance", REAL_FILTERS) is False


# --- dead-entry fix: keywords ending in punctuation ------------------------
#
# \b requires an actual word-char/non-word-char transition. A keyword ending
# in punctuation (a period, a closing paren) has no such transition left when
# the next real character is also non-word (a comma, a space, end of string)
# -- so the trailing \b silently never matches. Confirmed empirically against
# this project's real data before the fix: "u.s." and "remote (us)" were both
# completely dead in location_include_keywords, and would have been for any
# punctuation-ending entry in title_exclude_keywords too (e.g. "sr.").


def test_location_dead_entry_fix_trailing_period():
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=("u.s.",),
    )
    # Under plain \b...\b this never matched -- the period is non-word, and
    # end-of-string/comma/space after it is also non-word, so \b at that
    # position never fires.
    assert matches_location("Remote, u.s.", filters) is True
    assert matches_location("u.s.", filters) is True


def test_location_dead_entry_fix_trailing_parenthesis():
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=("remote (us)",),
    )
    assert matches_location("Remote (US)", filters) is True


def test_location_remote_us_parenthetical_matches_against_real_filters_yml():
    # The exact real posting (PostHog) that was silently missed before the fix.
    assert matches_location("Remote (US)", REAL_FILTERS) is True


def test_title_exclude_dead_entry_fix_trailing_period():
    filters = Filters(
        title_include_keywords=("analyst",),
        title_exclude_keywords=("sr.",),
        location_include_keywords=(),
    )
    assert matches_title("Sr. Data Analyst", filters) is False


def test_title_exclude_sr_dot_against_real_filters_yml():
    assert matches_title("Sr. Data Analyst", REAL_FILTERS) is False
    # "senior" spelled out is still caught too -- both forms covered.
    assert matches_title("Senior Data Analyst", REAL_FILTERS) is False


# --- state code strictness: comma-anchored, case-sensitive -----------------


def test_state_code_matches_only_with_comma_and_uppercase():
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=(),
        location_include_state_codes=("IL",),
    )
    assert matches_location("Chicago, IL", filters) is True
    assert matches_location("Chicago, il", filters) is False  # lowercase rejected
    assert matches_location("IL Remote", filters) is False  # no comma, rejected
    assert matches_location("Chicago,IL", filters) is True  # no space is fine


def test_state_code_does_not_false_positive_on_common_words():
    # The exact collision risk cited as the reason for comma-anchoring: "in"
    # and "or" are common English words that a bare word-boundary match on
    # the state code alone could catch.
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=(),
        location_include_state_codes=("IN", "OR"),
    )
    assert matches_location("Remote, based in Denver", filters) is False
    assert matches_location("Remote, US or Canada", filters) is False


def test_state_codes_still_match_real_city_state_postings():
    assert matches_location("Romeoville, IL", REAL_FILTERS) is True
    assert matches_location("Boynton Beach, FL", REAL_FILTERS) is True
    assert matches_location("New York, NY", REAL_FILTERS) is True


def test_bare_us_covers_state_code_without_comma_against_real_filters_yml():
    # Real data: state code present but NOT comma-anchored, so it doesn't
    # match via location_include_state_codes -- covered instead by the plain
    # "us" entry in location_include_keywords.
    assert matches_location("US IL - Remote", REAL_FILTERS) is True
    assert matches_location("US WA Seattle - Remote", REAL_FILTERS) is True


def test_spelled_out_state_name_matches_without_a_state_code():
    # Recall added back specifically to offset requiring state codes to be
    # comma-anchored + uppercase.
    assert matches_location("Austin, Texas", REAL_FILTERS) is True
    assert matches_location("Remote, Georgia", REAL_FILTERS) is True


# --- roman numeral seniority suffixes ---------------------------------------


def test_analyst_ii_is_not_excluded():
    # "ii" is deliberately not in title_exclude_keywords -- entry-level-
    # adjacent at the employers this is targeting.
    assert matches_title("Data Analyst II", REAL_FILTERS) is True


def test_analyst_iii_and_iv_are_excluded():
    assert matches_title("Data Analyst III", REAL_FILTERS) is False
    assert matches_title("Data Analyst IV", REAL_FILTERS) is False


# --- non-US locations stay excluded after the fix ---------------------------
#
# 11 real, distinct, single-country location strings from this project's own
# data (see the DB query used to build this list), covering Europe, Asia, the
# Middle East, and the Americas outside the US. None of these should ever
# start matching as a side effect of loosening the boundary regex or widening
# the include lists -- that's the whole risk this fix could introduce.

NON_US_LOCATIONS = [
    "Remote, Canada",
    "Remote, Mexico",
    "Remote, United Kingdom",
    "Remote Ireland",
    "Remote, Germany",
    "Remote, France",
    "Remote, Poland",
    "Remote, India",
    "Remote, Japan",
    "Remote, Singapore",
    "Remote, Australia",
]


@pytest.mark.parametrize("location", NON_US_LOCATIONS)
def test_non_us_location_stays_excluded(location):
    assert matches_location(location, REAL_FILTERS) is False


def test_non_us_locations_list_has_eleven_distinct_entries():
    # Guards the list above itself against accidental duplication/shrinkage.
    assert len(NON_US_LOCATIONS) == len(set(NON_US_LOCATIONS)) == 11


# --- period normalization ----------------------------------------------------
#
# Periods are stripped from both the searched text and every keyword before
# matching, so "U.S." and "US" compare equal, "Sr." and "Sr" compare equal,
# without needing a separate punctuated-form keyword for each. This replaced
# explicit "u.s." / "remote (us)" entries in filters.yml, which are now
# redundant (and were removed) rather than kept as unreachable duplicates.


def test_period_normalization_makes_punctuated_and_bare_forms_equivalent():
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=(),
        location_include_keywords=("us",),
    )
    assert matches_location("Remote, U.S.", filters) is True
    assert matches_location("Remote, US", filters) is True


def test_period_normalization_applies_to_title_excludes_too():
    filters = Filters(
        title_include_keywords=("analyst",),
        title_exclude_keywords=("sr",),
        location_include_keywords=(),
    )
    assert matches_title("Sr. Data Analyst", filters) is False
    assert matches_title("Sr Data Analyst", filters) is False


def test_u_dot_s_dot_and_remote_parenthetical_us_no_longer_need_explicit_entries():
    # Confirms the real filters.yml doesn't carry "u.s."/"remote (us)" as
    # separate entries anymore -- bare "us" + period-stripping covers both.
    assert "u.s." not in REAL_FILTERS.location_include_keywords
    assert "remote (us)" not in REAL_FILTERS.location_include_keywords
    assert matches_location("Remote, U.S.", REAL_FILTERS) is True
    assert matches_location("Remote (US)", REAL_FILTERS) is True


# --- title_include_match / title_exclude_hit --------------------------------


def test_title_include_match_true_only_with_an_include_keyword():
    filters = Filters(
        title_include_keywords=("analyst",),
        title_exclude_keywords=(),
        location_include_keywords=(),
    )
    assert title_include_match("Data Analyst", filters) is True
    assert title_include_match("Account Executive", filters) is False


def test_title_exclude_hit_returns_the_keyword_that_matched():
    filters = Filters(
        title_include_keywords=(),
        title_exclude_keywords=("senior", "manager"),
        location_include_keywords=(),
    )
    assert title_exclude_hit("Senior Data Analyst", filters) == "senior"
    assert title_exclude_hit("Data Analyst Manager", filters) == "manager"
    assert title_exclude_hit("Data Analyst", filters) is None


# --- new exclude keywords: qa/support/investment-banking, not marketing ----


def test_qa_and_support_roles_excluded_against_real_filters_yml():
    assert matches_title("QA Analyst", REAL_FILTERS) is False
    assert matches_title("Quality Assurance Analyst", REAL_FILTERS) is False
    assert matches_title("Help Desk Analyst", REAL_FILTERS) is False
    assert matches_title("Helpdesk Support Analyst", REAL_FILTERS) is False
    assert matches_title("Service Desk Analyst", REAL_FILTERS) is False
    assert matches_title("Desktop Support Analyst", REAL_FILTERS) is False
    assert matches_title("Technical Support Analyst", REAL_FILTERS) is False
    assert matches_title("IT Support Analyst", REAL_FILTERS) is False
    assert matches_title("Investment Banking Analyst", REAL_FILTERS) is False


def test_marketing_analytics_not_excluded_against_real_filters_yml():
    # Explicitly NOT excluded -- marketing analytics is a legitimate target,
    # unlike the QA/support/investment-banking roles above.
    assert matches_title("Marketing Analytics Analyst", REAL_FILTERS) is True


# --- "staff": narrowed, then reverted back to bare (see test_bare_staff_* below) ---
#
# An earlier version of this config narrowed "staff" to "staff engineer"/
# "staff software" on the theory that "Staff Analyst" is entry-level at
# universities/government agencies. That reasoning was sound in general but
# didn't apply to the companies actually in companies.yml (tech-adjacent
# fintechs, where Staff outranks Senior) and was costing real digest slots --
# see test_bare_staff_excludes_against_real_filters_yml further down, which
# is what replaced the two tests that used to be here.


# --- bare "associate" removed from includes ---------------------------------


def test_bare_associate_no_longer_matches_against_real_filters_yml():
    # Confirmed against real data before removing: 70/153 (46%) of all
    # matches were "associate" alone, almost entirely false positives.
    assert title_include_match("Business Development Associate", REAL_FILTERS) is False
    assert title_include_match("Fraud Operations Associate", REAL_FILTERS) is False
    assert title_include_match("Private Equity Partnerships Associate", REAL_FILTERS) is False


def test_domain_associate_titles_still_match_via_the_domain_word():
    # The whole point of removing bare "associate": a genuinely relevant title
    # already matches on its domain word without needing "associate" at all.
    assert matches_title("Risk Associate", REAL_FILTERS) is True
    assert matches_title("Compliance Associate", REAL_FILTERS) is True


# --- department-name collision excludes: designer/developer, NOT engineer/scientist --


def test_designer_and_developer_excluded_against_real_filters_yml():
    # No "senior" here on purpose -- title_exclude_hit returns the FIRST
    # matching keyword in list order, and "senior" comes before "designer".
    assert title_exclude_hit("Product Designer", REAL_FILTERS) == "designer"
    assert title_exclude_hit("Backend Developer", REAL_FILTERS) == "developer"


def test_engineer_and_scientist_deliberately_not_in_title_excludes():
    # Guards the design decision itself, not just its consequence below --
    # if someone adds "engineer" or "scientist" back to title_exclude_keywords
    # without reading the comment explaining why not, this fails loudly.
    assert "engineer" not in REAL_FILTERS.title_exclude_keywords
    assert "scientist" not in REAL_FILTERS.title_exclude_keywords


def test_data_engineer_and_data_scientist_would_break_if_engineer_and_scientist_were_excluded():
    # The actual collision, proven directly: exclude checking runs
    # independently of which include keyword fired, so a bare "engineer" or
    # "scientist" exclude would kill "Data Engineer"/"Data Scientist" even
    # though those exact phrases are in title_include_keywords. This is why
    # they're deliberately absent from the real config (previous test) --
    # this test proves what WOULD happen if they were added, so the reasoning
    # stays verifiable rather than just asserted in a comment.
    filters = Filters(
        title_include_keywords=("data engineer", "data scientist"),
        title_exclude_keywords=("engineer", "scientist"),
        location_include_keywords=(),
    )
    assert title_include_match("Data Engineer", filters) is True
    assert matches_title("Data Engineer", filters) is False  # would be a false exclude

    assert title_include_match("Data Scientist", filters) is True
    assert matches_title("Data Scientist", filters) is False  # would be a false exclude


def test_data_engineer_and_data_scientist_actually_still_match_real_filters_yml():
    # The real config (designer/developer only, not engineer/scientist)
    # doesn't have the problem the test above demonstrates.
    assert matches_title("Data Engineer", REAL_FILTERS) is True
    assert matches_title("Data Scientist", REAL_FILTERS) is True


# --- bare "staff" restored -----------------------------------------------


def test_bare_staff_excludes_against_real_filters_yml():
    # Re-added: at the tech-adjacent fintechs actually in companies.yml,
    # Staff ranks above Senior, and the narrowed "staff engineer"/"staff
    # software" version was letting "Staff Data Analyst"/"Staff Data
    # Scientist" through -- 11/50 real digest slots.
    assert title_exclude_hit("Staff Analyst", REAL_FILTERS) == "staff"
    assert matches_title("Staff Data Analyst", REAL_FILTERS) is False
    assert matches_title("Staff Data Scientist", REAL_FILTERS) is False


def test_associate_still_absent_from_includes_against_real_filters_yml():
    # Re-confirms a prior fix rather than re-doing it -- "associate" was
    # already removed from title_include_keywords, this just guards it.
    assert "associate" not in REAL_FILTERS.title_include_keywords
    assert title_include_match("Business Development Associate", REAL_FILTERS) is False


# --- engineering sub-role excludes: specific phrases, not bare "engineer" --


def test_engineering_subrole_phrases_excluded_against_real_filters_yml():
    # The exact two real postings that motivated this: leaked in via domain
    # words unrelated to the actual role.
    assert matches_title("Backend Engineer, Payments and Risk", REAL_FILTERS) is False
    assert matches_title(
        "Software Engineer, Reconciliation & Reporting", REAL_FILTERS
    ) is False
    assert title_exclude_hit("Frontend Engineer", REAL_FILTERS) == "frontend"
    assert title_exclude_hit("Full Stack Engineer", REAL_FILTERS) == "full stack"
    assert title_exclude_hit("Android Engineer", REAL_FILTERS) == "android"
    assert title_exclude_hit("iOS Engineer", REAL_FILTERS) == "ios"
    assert title_exclude_hit("Security Engineer, Compliance", REAL_FILTERS) == "security engineer"


def test_data_engineer_survives_the_new_engineering_subrole_excludes():
    # The specific thing to confirm before adding "software engineer" and
    # "backend engineer" to excludes: they're specific enough as PHRASES
    # that neither is a substring of "data engineer", unlike bare "engineer"
    # (see the collision tests above). Explicitly checked, not just assumed.
    assert matches_title("Data Engineer", REAL_FILTERS) is True
    assert matches_title("Backend Data Engineer", REAL_FILTERS) is True
    assert matches_title("Data Engineer, Backend Systems", REAL_FILTERS) is True
    assert title_exclude_hit("Data Engineer", REAL_FILTERS) is None
