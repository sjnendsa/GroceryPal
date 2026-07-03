"""One-time compaction of price_history.

Older DBs recorded one price_history row per product on every daily sync, even
when nothing changed — which bloated the committed .db files past GitHub's
100 MB push limit. save_batch now only records genuine price changes, so this
script collapses the existing backlog to the same shape: for each product it
keeps the first row and every subsequent *change point* (a row whose price /
regular_price / was_price / on_sale / in_stock differs from the previous one),
dropping the redundant duplicates in between. Then VACUUM reclaims the space.

Charts read history as a step function, so dropped duplicates carry no
information — the current "last seen" timestamp lives in products.latest_at.

Usage:  python compact_history.py            # compact every data/*.db
        python compact_history.py <file.db>  # compact one DB
"""
import glob
import os
import sqlite3
import sys

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# A row is redundant when it matches the chronologically previous row for the
# same product on every meaningful field (sale_label / min_qty are cosmetic).
_DELETE_REDUNDANT = """
DELETE FROM price_history
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               price, regular_price, was_price, on_sale, in_stock,
               LAG(price)         OVER w AS p_price,
               LAG(regular_price) OVER w AS p_reg,
               LAG(was_price)     OVER w AS p_was,
               LAG(on_sale)       OVER w AS p_sale,
               LAG(in_stock)      OVER w AS p_stock
        FROM price_history
        WINDOW w AS (PARTITION BY product_id ORDER BY scraped_at, id)
    )
    WHERE p_price IS NOT NULL
      AND price         IS p_price
      AND regular_price IS p_reg
      AND was_price     IS p_was
      AND on_sale       IS p_sale
      AND in_stock      IS p_stock
);
"""


def compact(path):
    before_bytes = os.path.getsize(path)
    conn = sqlite3.connect(path)
    try:
        n0 = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        conn.execute(_DELETE_REDUNDANT)
        conn.commit()
        n1 = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    after_bytes = os.path.getsize(path)
    print(f"{os.path.basename(path):40s} "
          f"rows {n0:>8,} -> {n1:>8,}   "
          f"{before_bytes/1048576:6.1f} MB -> {after_bytes/1048576:6.1f} MB",
          flush=True)


def main():
    targets = sys.argv[1:] or sorted(glob.glob(os.path.join(_DATA, "grocery_pal_*.db")))
    for path in targets:
        try:
            compact(path)
        except sqlite3.Error as e:
            print(f"{os.path.basename(path):40s} SKIPPED: {e}", flush=True)


if __name__ == "__main__":
    main()
