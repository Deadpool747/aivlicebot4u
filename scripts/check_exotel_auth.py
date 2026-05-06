#!/usr/bin/env python3
"""Check Exotel authentication and endpoint wiring without placing a live call."""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.config import load_settings  # noqa: E402


def main() -> None:
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("EXOTEL_ACCOUNT_SID", settings.exotel_account_sid),
            ("EXOTEL_API_KEY", settings.exotel_api_key),
            ("EXOTEL_API_TOKEN", settings.exotel_api_token),
            ("EXOTEL_SUBDOMAIN", settings.exotel_subdomain),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing Exotel settings: {', '.join(missing)}")

    url = (
        f"https://{settings.exotel_subdomain}"
        f"/v1/Accounts/{settings.exotel_account_sid}/Calls.json?Page=1&PageSize=1"
    )
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(
                f"{settings.exotel_api_key}:{settings.exotel_api_token}".encode("utf-8")
            ).decode("ascii"),
            "Accept": "application/json, application/xml;q=0.9, */*;q=0.8",
        },
    )
    ssl_context = __import__("ssl").create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
        print("AUTH_OK")
        print(url)
        print(body[:1000])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print("AUTH_FAILED")
        print(url)
        print(f"status={exc.code}")
        print(body[:2000])
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print("AUTH_ERROR")
        print(url)
        print(str(exc.reason))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
