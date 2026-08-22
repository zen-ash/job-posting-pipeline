# job-ingest

Daily ingestion pipeline for public ATS job-board postings (Greenhouse, Lever, Ashby).
Polls a hand-maintained list of companies, stores every posting revision in Postgres,
diffs against the previous run to find what's genuinely new, and emails a digest.

Built as a personal job-search tool and a portfolio project — CS senior at Georgia State,
targeting entry-level data engineering / data analyst roles.

**Status:** in progress. This is the repo skeleton — no ingestion logic yet.

## Build plan

- [x] 1. Repo skeleton, venv, requirements, `.gitignore`, `.env.example`
- [x] 2. ATS fetchers + normalization to a `Posting` dataclass (fixture-tested, no DB)
- [ ] 3. Postgres schema + upsert logic + new/closed detection
- [ ] 4. Digest email
- [ ] 5. GitHub Actions daily cron
- [ ] 6. Full README: architecture, schema diagram, what's next

## Prerequisites

- **Python 3.12** — `brew install python@3.12` (this repo pins to 3.12 explicitly; Homebrew
  also ships a 3.9 you should *not* use for this project).
- **Docker Desktop** — runs a local Postgres for development via `docker-compose.yml`.
- **A free [Neon](https://neon.tech) Postgres project** — used as the database GitHub
  Actions writes to on its daily run, since Actions runners can't reach a Postgres running
  on your Mac. Added in step 5.
- **A free [Resend](https://resend.com) account** — sends the digest email. Added in step 4.

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env   # then fill in real values — .env is gitignored, never commit it
```

More setup steps (starting Postgres, running the pipeline, running tests) land as each
build step above is implemented.
