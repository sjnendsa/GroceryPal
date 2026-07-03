"""Sync every store that already has a database, then exit.
Used by the scheduled GitHub Action that keeps the Pages site fresh,
but works the same locally:  python sync_all.py
"""
import glob
import os
import re

import db
import scraper
from app import _scrape_loblaw

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def main():
    failed = []
    for path in sorted(glob.glob(os.path.join(_DATA, "grocery_pal_*.db"))):
        m = re.match(r"grocery_pal_(saveon|superstore|nofrills)_(\d+)\.db$",
                     os.path.basename(path))
        if not m:
            continue
        retailer, sid = m.groups()
        key = f"{retailer}_{sid}"
        print(f"=== {key} ===", flush=True)
        try:
            if retailer == "saveon":
                n = scraper.scrape_api(sid, db_key=key)
            else:
                n = _scrape_loblaw(retailer, sid, key)
            # A store that scrapes zero products means the API is broken or
            # blocking us — its prices would silently go stale. Fail the run
            # so it shows up red instead of "success" with frozen data.
            if not n:
                print("  FAILED: scraped 0 products", flush=True)
                failed.append(key)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            failed.append(key)
    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
