"""Bake the dashboard into a static docs/ bundle (Sailwind-Map style).

Produces docs/index.html + docs/data/*.js from the per-store SQLite DBs, so
the dashboard opens as a plain file on any machine — no Python, no Flask.
Data ships as .js files (not .json) so file:// works without a web server,
and the same folder can later be published with GitHub Pages as-is.

Run:  python export_static.py
"""
import glob
import json
import os
import re
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DOCS = os.path.join(BASE, "docs")

P_COLS = ["product_id", "name", "brand", "category", "subcategory", "image_url",
          "unit", "size", "latest_price", "regular_price", "was_price", "on_sale",
          "sale_label", "min_qty", "in_stock", "latest_at", "prev_price", "prev_at",
          "created_at", "member_price", "url"]
H_COLS = ["product_id", "price", "regular_price", "on_sale", "sale_label",
          "in_stock", "scraped_at", "member_price"]


def _write_js(path, assign, payload):
    """`assign` = JSON payload, with the payload kept verbatim-parseable."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(assign + "=")
        json.dump(payload, f, separators=(",", ":"))
        f.write(";\n")


def export_store(db_path):
    key = re.match(r"grocery_pal_(.+)\.db$", os.path.basename(db_path)).group(1)
    import db
    db.set_store(key)   # migrate older DBs (adds + backfills denormalized columns)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prows = [[r[c] for c in P_COLS]
             for r in conn.execute(f"SELECT {','.join(P_COLS)} FROM products")]
    # keep the last reading per product per day — charts collapse to daily anyway
    daily = {}
    for r in conn.execute(
            f"SELECT {','.join(H_COLS)} FROM price_history ORDER BY scraped_at"):
        daily[(r["product_id"], r["scraped_at"][:10])] = [r[c] for c in H_COLS]
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 20")]
    conn.close()
    _write_js(os.path.join(DOCS, "data", f"{key}.js"),
              f"window.GP_DATA=window.GP_DATA||{{}};window.GP_DATA[{json.dumps(key)}]",
              {"products": {"cols": P_COLS, "rows": prows},
               "history": {"cols": H_COLS, "rows": list(daily.values())},
               "runs": runs})
    return key, len(prows)


def export_stores_list(keys):
    with open(os.path.join(DATA, "stores.json"), encoding="utf-8") as f:
        stores = json.load(f)
    try:  # same coordinate corrections the app applies (Save-On only)
        with open(os.path.join(DATA, "store_coords_overrides.json"), encoding="utf-8") as f:
            ov = json.load(f)
        for s in stores:
            fix = s.get("retailer") == "saveon" and ov.get(s["store_id"])
            if fix:
                s["lat"], s["lng"] = fix["lat"], fix["lng"]
    except OSError:
        pass
    try:
        with open(os.path.join(DATA, "settings.json"), encoding="utf-8-sig") as f:
            st = json.load(f)
    except Exception:
        st = {"store_id": "1982", "store_name": "Save-on-Foods · Langley Twp",
              "retailer": "saveon"}
    if f"{st.get('retailer', 'saveon')}_{st['store_id']}" not in keys and keys:
        retailer, sid = keys[0].rsplit("_", 1)
        st = {"store_id": sid, "store_name": f"Store #{sid}", "retailer": retailer}
    _write_js(os.path.join(DOCS, "data", "stores.js"), "window.GP_STORES", stores)
    with open(os.path.join(DOCS, "data", "stores.js"), "a", encoding="utf-8") as f:
        f.write(f"window.GP_STORE_KEYS={json.dumps(keys)};\n")
        f.write(f"window.GP_DEFAULT_STORE={json.dumps(st)};\n")


def export_index():
    with open(os.path.join(BASE, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    inject = ('<script src="data/stores.js"></script>\n'
              '<script src="static_api.js"></script>\n</body>')
    html = html.replace("</body>", inject, 1)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    os.makedirs(os.path.join(DOCS, "data"), exist_ok=True)
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    keys = []
    for db_path in sorted(glob.glob(os.path.join(DATA, "grocery_pal_*.db"))):
        key, n = export_store(db_path)
        keys.append(key)
        print(f"  {key}: {n} products")
    export_stores_list(keys)
    export_index()
    print(f"Static bundle written to docs/ ({len(keys)} stores)")


if __name__ == "__main__":
    main()
