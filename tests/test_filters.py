"""Tests for job_ingest.filters — pure functions, no DB or network."""

from __future__ import annotations

from pathlib import Path

from job_ingest.filters import Filters, load_filters, matches, matches_location, matches_title

REPO_ROOT = Path(__file__).parent.parent

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
    real_filters = load_filters(REPO_ROOT / "filters.yml")

    # Same include signals ("associate", "risk"), no exclude keyword present --
    # proves the include side genuinely matches, so the next assertion isn't
    # vacuously true.
    assert matches_title("Associate, Risk Management", real_filters) is True

    # Adding "Director" must flip this to excluded even though the title still
    # matches "associate" and "risk" in title_include_keywords.
    assert matches_title("Associate Director, Risk Management", real_filters) is False


def test_legal_roles_excluded_despite_broad_compliance_and_risk_includes():
    # "compliance" and "risk" are broad includes in the real filters.yml and
    # will fire on legal-adjacent titles that aren't actually data/analyst
    # roles -- counsel/attorney/paralegal excludes exist specifically to catch
    # those.
    real_filters = load_filters(REPO_ROOT / "filters.yml")

    assert matches_title("Compliance Counsel", real_filters) is False
    assert matches_title("Attorney, Regulatory Compliance", real_filters) is False
    assert matches_title("Paralegal, Risk & Compliance", real_filters) is False
    assert matches_title("Vice President, Compliance", real_filters) is False
