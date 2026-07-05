"""Sync every store that already has a database, then exit.
Used by the scheduled GitHub Action that keeps the Pages site fresh,
but works the same locally:  python sync_all.py
"""
import glob
import os
import re
import time

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

    # Loblaw failures are almost always Akamai blocking the runner IP for a
    # while, not a broken store — give the block time to lift, then retry
    # those stores once before declaring the run red.
    blocked = [k for k in failed if not k.startswith("saveon_")]
    if blocked:
        import loblaw
        print(f"retrying {len(blocked)} blocked store(s) after cool-down...", flush=True)
        time.sleep(300)
        loblaw.reset_block()
        for key in blocked:
            retailer, sid = key.rsplit("_", 1)
            print(f"=== {key} (retry) ===", flush=True)
            try:
                if _scrape_loblaw(retailer, sid, key):
                    failed.remove(key)
                else:
                    print("  FAILED: scraped 0 products", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)

    # Top up the nutrition caches for products that appeared since the last
    # run (facts are fetched once per product, then baked into docs/).
    import nutrition
    for retailer in ("saveon", "nofrills", "superstore"):
        store = nutrition.a_store_of(retailer)
        if store:
            try:
                nutrition.backfill(retailer, store,
                                   sorted(nutrition.union_product_ids(retailer)),
                                   budget=8000)
            except Exception as e:
                print(f"  nutrition top-up {retailer} failed: {e}", flush=True)

    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
