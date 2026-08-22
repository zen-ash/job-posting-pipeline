"""Loads and validates companies.yml — the hand-edited list of boards to poll."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

VALID_ATS = {"greenhouse", "lever", "ashby"}
REQUIRED_FIELDS = {"slug", "name", "ats", "board_token"}


class ConfigError(ValueError):
    """companies.yml is missing, malformed, or contains an invalid entry."""


@dataclass(frozen=True, slots=True)
class Company:
    slug: str  # our internal identifier — do not rename once jobs exist for it
    name: str  # display name, used in the digest
    ats: str  # "greenhouse" | "lever" | "ashby"
    board_token: str  # the token/slug the ATS's own API expects


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

        if entry["slug"] in seen_slugs:
            raise ConfigError(f"duplicate company slug '{entry['slug']}' in {path}")
        seen_slugs.add(entry["slug"])

        companies.append(
            Company(
                slug=entry["slug"],
                name=entry["name"],
                ats=entry["ats"],
                board_token=entry["board_token"],
            )
        )

    return companies
