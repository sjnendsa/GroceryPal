"""One-shot nutrition backfill across every tracked retailer.

Usage:  python backfill_nutrition.py [budget-per-retailer]   (default 50000)
"""
import sys

import scraper  # noqa: F401  (configures logging)
import nutrition


def main(budget=50000):
    total = 0
    for retailer in ("saveon", "nofrills", "superstore"):
        store = nutrition.a_store_of(retailer)
        if not store:
            continue
        ids = nutrition.union_product_ids(retailer)
        print(f"=== {retailer} (store {store}): {len(ids)} products ===", flush=True)
        total += nutrition.backfill(retailer, store, sorted(ids), budget=budget)
    print(f"backfill done — {total} fetched", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 50000)
