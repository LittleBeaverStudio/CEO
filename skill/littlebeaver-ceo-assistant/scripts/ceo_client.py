#!/usr/bin/env python3
"""Read the local Little Beaver CEO assistant API."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


PATHS = {
    "health": "/api/skill/health",
    "info": "/api/skill/info",
    "companies": "/api/skill/companies",
    "brief": "/api/skill/ceo-brief",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=PATHS)
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--company-id", type=int, help="Company ID for the brief action")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.company_id is not None and (args.action != "brief" or args.company_id < 1):
        parser.error("--company-id requires the brief action and must be positive")

    url = f"http://{args.host}:{args.port}{PATHS[args.action]}"
    if args.company_id is not None:
        url += f"?company_id={args.company_id}"
    try:
        with urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            payload = json.load(exc)
        except (ValueError, OSError):
            payload = {"error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (URLError, TimeoutError, OSError) as exc:
        print(f"Cannot reach the local CEO app at {url}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
