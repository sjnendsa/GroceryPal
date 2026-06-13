"""Scrape one store on demand (creates or refreshes its DB).
Used by the "Track a store" workflow; works the same locally.
Usage:  python track_store.py <saveon|superstore|nofrills> <store_id>
"""
import json
import os
import sys

import scraper
from app import _scrape_loblaw

_BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    retailer, sid = sys.argv[1].strip(), sys.argv[2].strip()
    with open(os.path.join(_BASE, "data", "stores.json"), encoding="utf-8") as f:
        stores = json.load(f)
    match = next((s for s in stores
                  if s["retailer"] == retailer and str(s["store_id"]) == sid), None)
    if not match:
        raise SystemExit(f"unknown store: {retailer} {sid}")
    print(f"Tracking {match['name']} — {match['city']}, {match['province']}")
    if retailer == "saveon":
        scraper.scrape_api(sid, db_key=f"saveon_{sid}")
    else:
        _scrape_loblaw(retailer, sid, f"{retailer}_{sid}")


if __name__ == "__main__":
    main()
