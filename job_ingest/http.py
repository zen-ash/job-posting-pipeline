"""Shared HTTP conventions for talking to public ATS APIs: identify ourselves,
use a sane timeout, don't hammer anyone.
"""

from __future__ import annotations

import requests

USER_AGENT = (
    "job-ingest-pipeline/0.1 (personal job-search project; contact: kumaraayush2804@gmail.com)"
)
DEFAULT_TIMEOUT = 15  # seconds
# Small pause between polling different company boards, kept here rather than in
# each fetcher since it's about being a polite caller across a run, not about any
# one request. Applied by the orchestration loop in main.py (step 3), not here.
BETWEEN_BOARDS_DELAY_SECONDS = 1.0


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session
