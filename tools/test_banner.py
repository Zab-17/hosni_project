"""Banner reality-check. Run this ONCE you've put the real AUC host in .env.

    .venv/bin/python -m tools.test_banner 12345

It performs the full session handshake and prints either the parsed seat data
or a diagnosis (bad host, blocked, not JSON, CRN not found) so we know exactly
what to fix — no guessing.
"""

import sys

from src.banner_service import BannerClient
from src.config import settings


def main():
    if settings.banner_base_url.startswith("https://CHANGE-ME"):
        print("❌ BANNER_BASE_URL is still the placeholder. Put the real AUC host in .env first.")
        return 1

    crn = sys.argv[1] if len(sys.argv) > 1 else None
    term = sys.argv[2] if len(sys.argv) > 2 else settings.banner_term
    if not crn:
        print("Usage: python -m tools.test_banner <CRN> [TERM]")
        return 1

    print(f"Host : {settings.banner_base_url}{settings.banner_path_prefix}")
    print(f"Term : {term}\nCRN  : {crn}\n")

    client = BannerClient()
    info = client.get_seats(crn, term)

    if info is None:
        print("⚠️  No data returned. Likely causes:")
        print("   • host/path wrong  • Banner needs an extra step  • response wasn't JSON")
        print("   • term-priming POST rejected  • CRN not in this term")
        print("\nRe-check the endpoint in DevTools (F12 → Network → search a CRN) and")
        print("compare the request URL/params against src/banner_service.py.")
        return 1

    print("✅ SUCCESS — Banner is reachable over plain HTTP. No Chromium needed.")
    print(f"   {info['title']}")
    print(f"   Seats available: {info['seats']} / {info['max']}  (enrolled {info['enrolled']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
