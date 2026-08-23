"""Loads and validates companies.yml — the hand-edited list of boards to poll."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

VALID_ATS = {"greenhouse", "lever", "ashby", "workday"}

# Fields every company entry needs regardless of ATS.
REQUIRED_FIELDS = {"slug", "name", "ats"}
# Per-ATS additional requirements. Workday is addressed by a tenant/host/site
# triple rather than a single board token, so it requires those instead of
# board_token -- existing greenhouse/lever/ashby rows are untouched and stay
# valid exactly as written.
REQUIRED_FIELDS_BY_ATS = {
    "greenhouse": {"board_token"},
    "lever": {"board_token"},
    "ashby": {"board_token"},
    "workday": {"tenant", "wd_host", "site"},
}


class ConfigError(ValueError):
    """companies.yml is missing, malformed, or contains an invalid entry."""


@dataclass(frozen=True, slots=True)
class Company:
    slug: str  # our internal identifier — do not rename once jobs exist for it
    name: str  # display name, used in the digest
    ats: str  # "greenhouse" | "lever" | "ashby" | "workday"
    # The token/slug the ATS's own API expects. Required for greenhouse/lever/
    # ashby; None for workday, which uses the three fields below instead.
    board_token: str | None = None
    # Workday only. A Workday board is addressed by three parts, e.g.
    # https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    tenant: str | None = None  # e.g. "homedepot"
    wd_host: str | None = None  # e.g. "wd5" -- differs per customer, not guessable
    site: str | None = None  # career-site name, e.g. "CareerDepot"


def load_companies(path: str | Path = "companies.yml") -> list[Company]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "companies" not in raw:
        raise ConfigError(f"{path} must have a top-level 'companies' list")

    entries = raw["companies"] or []
    companies: list[Company] = []
    seen_slugs: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"company entry must be a mapping, got: {entry!r}")

        missing = REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ConfigError(
                f"company entry {entry} is missing required field(s): {sorted(missing)}"
            )

        if entry["ats"] not in VALID_ATS:
            raise ConfigError(
                f"company '{entry['slug']}' has unknown ats '{entry['ats']}' "
                f"(must be one of {sorted(VALID_ATS)})"
            )

        ats_missing = REQUIRED_FIELDS_BY_ATS[entry["ats"]] - entry.keys()
        if ats_missing:
            raise ConfigError(
                f"company '{entry['slug']}' (ats={entry['ats']}) is missing required "
                f"field(s) for that ATS: {sorted(ats_missing)}"
            )

        if entry["slug"] in seen_slugs:
            raise ConfigError(f"duplicate company slug '{entry['slug']}' in {path}")
        seen_slugs.add(entry["slug"])

        companies.append(
            Company(
                slug=entry["slug"],
                name=entry["name"],
                ats=entry["ats"],
                board_token=entry.get("board_token"),
                tenant=entry.get("tenant"),
                wd_host=entry.get("wd_host"),
                site=entry.get("site"),
            )
        )

    return companies
