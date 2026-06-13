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
                scraper.scrape_api(sid, db_key=key)
            else:
                _scrape_loblaw(retailer, sid, key)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            failed.append(key)
    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
