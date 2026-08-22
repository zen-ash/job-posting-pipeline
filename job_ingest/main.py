"""Step 2 proof-of-life: fetch and normalize one company's board and print the
result. No database involved yet — that lands in step 3, where this becomes the
entrypoint for a full run across every company in companies.yml.
"""

from __future__ import annotations

import argparse
import sys

from job_ingest.ats import FETCHERS
from job_ingest.config import Company, load_companies


def fetch_company(company: Company) -> list:
    fetcher = FETCHERS[company.ats]
    raw = fetcher.fetch(company.board_token)
    return fetcher.normalize(raw, company)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and print postings for one company from companies.yml. No DB writes."
    )
    parser.add_argument("--company", required=True, help="a `slug` from companies.yml")
    parser.add_argument("--companies-file", default="companies.yml")
    args = parser.parse_args(argv)

    companies = {c.slug: c for c in load_companies(args.companies_file)}
    if args.company not in companies:
        print(
            f"Unknown company slug '{args.company}'. Known slugs: {sorted(companies)}",
            file=sys.stderr,
        )
        return 1

    company = companies[args.company]
    postings = fetch_company(company)

    header = f"{company.name} ({company.ats}, board_token={company.board_token})"
    print(f"{header}: {len(postings)} postings\n")
    for p in postings[:10]:
        print(f"  [{p.external_id}] {p.title}")
        print(f"      location: {p.location or 'n/a'}   department: {p.department or 'n/a'}")
        print(f"      {p.url}")
    if len(postings) > 10:
        print(f"\n  ... and {len(postings) - 10} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
