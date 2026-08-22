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
- [x] 3. Postgres schema + upsert logic + new/closed detection
- [x] 4. Digest email
- [x] 5. GitHub Actions daily cron
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

## GitHub Actions (daily cron)

`.github/workflows/ingest.yml` runs the full pipeline once a day and can also be
triggered by hand from the Actions tab (`workflow_dispatch`) — useful for testing a
change without waiting for the next scheduled run.

### Why GitHub Actions instead of a local cron job

A `cron` entry (or `launchd` job) on this Mac would only run while the laptop is on,
awake, and not asleep from the lid being closed — exactly the conditions a daily
morning digest can't rely on. GitHub Actions runs on GitHub's infrastructure
regardless of whether this machine is even turned on, at effectively no cost for a
job this small and infrequent, and it's what step 3's Neon vs. local-Postgres split
was already built around: the workflow talks to Neon (reachable from anywhere),
while local Docker Postgres stays purely a dev convenience.

### Schedule and DST

The workflow runs at `11:00 UTC` daily. GitHub Actions cron schedules are always UTC
and have no concept of time zones or daylight saving — so this is a fixed point that
drifts relative to Atlanta time across the year:

| | UTC | Atlanta (America/New_York) |
|---|---|---|
| Winter (EST, UTC-5) | 11:00 | 6:00 AM |
| Summer (EDT, UTC-4) | 11:00 | 7:00 AM |

That one-hour, twice-a-year drift is an accepted tradeoff rather than something the
workflow corrects — the digest still lands before the workday either way, which is
all that actually matters here.

### Repo setup

1. Push this repo to GitHub (`git remote add origin <url>`, `git push -u origin main`).
2. Under **Settings → Secrets and variables → Actions**, add these repository secrets:
   - `DATABASE_URL` — your **Neon** connection string (not the local Docker one —
     Actions runners can't reach `localhost`). Get this from the Neon dashboard.
   - `RESEND_API_KEY`
   - `DIGEST_FROM_EMAIL`
   - `DIGEST_TO_EMAIL`
3. Once pushed, trigger a manual run from **Actions → Daily job ingest → Run workflow**
   to verify the secrets are correct before waiting for the schedule.

`.github/workflows/tests.yml` runs lint (`ruff`) and the full offline test suite on
every push and pull request — no secrets needed, since all 51 tests run against
fixtures/pure functions rather than the network or a live database.
