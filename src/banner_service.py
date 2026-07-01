"""Banner 9 (AUC Ellucian Self-Service) seat checker — plain HTTP, no browser.

VERIFIED LIVE against https://reg-prod.ec.aucegypt.edu on 2026-06-30:
  landing GET  -> sets JSESSIONID + AWSALB cookies
  term POST    -> primes the session with the selected term
  results GET  -> returns clean JSON with seatsAvailable
No login, no Chromium.

CRITICAL — fresh session per search:
Banner caches your LAST search in the server-side session, so searching again
in the same session can return STALE results. We therefore open a brand-new
httpx session (new cookie jar) for every CRN check — that is the HTTP
equivalent of "reload the page before each search", which AUC's Banner requires.
"""

import random
import string
import time

import httpx

from .config import settings


def _unique_session_id() -> str:
    """5 random lowercase letters + epoch milliseconds — Banner's cache-buster,
    kept consistent across the term-POST and results-GET of one handshake."""
    letters = "".join(random.choices(string.ascii_lowercase, k=5))
    return f"{letters}{int(time.time() * 1000)}"


class BannerClient:
    def __init__(self):
        self.base = settings.banner_base_url.rstrip("/")
        self.prefix = settings.banner_path_prefix
        self.headers = {
            "User-Agent": settings.banner_user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _url(self, path: str) -> str:
        return f"{self.base}{self.prefix}{path}"

    def get_seats(self, crn: str, term: str) -> dict | None:
        """Return {'seats','max','enrolled','title'} for one CRN, or None if it
        couldn't be read (bad host, blocked, not JSON, CRN not offered). Never
        raises — the poller must survive a flaky endpoint.

        Each call is a fully fresh session == a 'page reload', so results are
        always accurate for the CRN asked."""
        sid = _unique_session_id()
        try:
            with httpx.Client(headers=self.headers, timeout=20, follow_redirects=True) as client:
                # 1. Landing -> JSESSIONID
                client.get(self._url("/classSearch/classSearch"))
                # 2. Prime the term in the session
                client.post(
                    self._url("/term/search"),
                    params={"mode": "search"},
                    data={
                        "term": term,
                        "studyPath": "",
                        "studyPathText": "",
                        "startDatepicker": "",
                        "endDatepicker": "",
                        "uniqueSessionId": sid,
                    },
                )
                # 3. Search by CRN (keyword search matches the CRN exactly)
                params = {
                    "txt_keywordlike": crn,
                    "txt_term": term,
                    "startDatepicker": "",
                    "endDatepicker": "",
                    "uniqueSessionId": sid,
                    "pageOffset": 0,
                    "pageMaxSize": 10,
                    "sortColumn": "subjectDescription",
                    "sortDirection": "asc",
                }
                r = client.get(self._url("/searchResults/searchResults"), params=params)
                payload = r.json()
        except (httpx.HTTPError, ValueError):
            return None

        for item in payload.get("data") or []:
            if str(item.get("courseReferenceNumber")) == str(crn):
                title = item.get("courseTitle") or ""
                subj = item.get("subject") or ""
                num = item.get("courseNumber") or ""
                label = f"{subj} {num} — {title}".strip(" —")
                return {
                    "seats": int(item.get("seatsAvailable") or 0),
                    "max": int(item.get("maximumEnrollment") or 0),
                    "enrolled": int(item.get("enrollment") or 0),
                    "title": label,
                    # Waitlist: capacity = total size, count = people on it.
                    # Open spots = capacity - count.
                    "wait_capacity": int(item.get("waitCapacity") or 0),
                    "wait_count": int(item.get("waitCount") or 0),
                }
        return None  # CRN not found in this term

    def list_terms(self) -> list[dict]:
        """Discover term codes (e.g. 202710 = 'Fall 2026'). Handy for admin."""
        try:
            with httpx.Client(headers=self.headers, timeout=20, follow_redirects=True) as client:
                client.get(self._url("/classSearch/classSearch"))
                r = client.get(
                    self._url("/classSearch/getTerms"),
                    params={"searchTerm": "", "offset": 1, "max": 100, "_": int(time.time() * 1000)},
                )
                return r.json()
        except (httpx.HTTPError, ValueError):
            return []
