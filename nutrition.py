"""Nutrition-facts cache — Grocery Pal

Browsers can't call the retailer APIs from the static site (no CORS), so
nutrition is fetched server-side into a per-retailer cache DB
(data/nutrition_<retailer>.db) and baked into docs/ by export_static.py.

Facts basically never change, so each product is fetched once; the daily
sync tops up whatever is new with a small request budget.

CLI:  python backfill_nutrition.py [budget-per-retailer]
"""
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import loblaw

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_BASE, "data")
log = logging.getLogger("scraper.nutrition")

# Mirrors app.py's Save-On live-detail parsing (kept in sync manually; the
# Flask app still fetches live per-request, this module only feeds the cache).
_SOF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.saveonfoods.com",
    "Referer": "https://www.saveonfoods.com/",
    "X-Shopping-Mode": "11111111-1111-1111-1111-111111111111",
    "X-Site-Host": "www.saveonfoods.com",
}

NUTRIENT_LAYOUT = [
    ("Calories",           0, ["calories", "energy"]),
    ("Total Fat",          0, ["total fat", "fat"]),
    ("Saturated",          1, ["saturated", "saturated fat"]),
    ("Trans",              1, ["trans", "trans fat"]),
    ("Polyunsaturated",    1, ["polyunsaturated", "polyunsaturated fat"]),
    ("Monounsaturated",    1, ["monounsaturated", "monounsaturated fat"]),
    ("Omega-3",            1, ["omega-3", "omega 3"]),
    ("Omega-6",            1, ["omega-6", "omega 6"]),
    ("Cholesterol",        0, ["cholesterol"]),
    ("Sodium",             0, ["sodium"]),
    ("Total Carbohydrate", 0, ["total carbohydrate", "carbohydrate", "total carbohydrates"]),
    ("Dietary Fibre",      1, ["dietary fibre", "dietary fiber", "fibre", "fiber"]),
    ("Sugars",             1, ["sugars", "sugar"]),
    ("Added Sugars",       1, ["added sugars"]),
    ("Sugar Alcohol",      1, ["sugar alcohol", "polyols"]),
    ("Starch",             1, ["starch"]),
    ("Protein",            0, ["protein"]),
]


def order_nutrition(nutrition):
    """{name: {size, unit, pct}} -> ordered label rows (same as app.py)."""
    remaining = dict(nutrition)
    lower_map = {k.lower().strip(): k for k in remaining}
    rows = []
    for canonical, indent, aliases in NUTRIENT_LAYOUT:
        for alias in aliases:
            key = lower_map.get(alias)
            if key and key in remaining:
                v = remaining.pop(key)
                rows.append({"name": canonical, "indent": indent, "group": "main", **v})
                break
    for key in sorted(remaining):
        rows.append({"name": key, "indent": 0, "group": "micro", **remaining[key]})
    return rows


# ── Save-On detail ────────────────────────────────────────────────────────────

def _saveon_payload(d):
    nutrition = {}
    profiles = d.get("nutritionProfiles") or {}
    if isinstance(profiles, dict):
        raw = profiles.get("nutrition") or {}
        if isinstance(raw, dict):
            for name, vals in raw.items():
                if isinstance(vals, dict):
                    nutrition[name] = {"size": vals.get("size"),
                                       "unit": vals.get("abbreviation") or vals.get("unit"),
                                       "pct": vals.get("percentDailyValue")}
        if not nutrition:
            for n in (profiles.get("nutrients") or []):
                if isinstance(n, dict) and n.get("name"):
                    nutrition[n["name"]] = {"size": n.get("amount") or n.get("size"),
                                            "unit": n.get("unit") or n.get("abbreviation"),
                                            "pct": n.get("dailyValue") or n.get("percentDailyValue")}
    if not nutrition and isinstance(d.get("nutrition"), dict):
        for name, vals in d["nutrition"].items():
            if isinstance(vals, dict):
                nutrition[name] = {"size": vals.get("size"),
                                   "unit": vals.get("abbreviation") or vals.get("unit"),
                                   "pct": vals.get("percentDailyValue")}
    prof = profiles if isinstance(profiles, dict) else {}
    sv = d.get("servingSize") or prof.get("servingSize")
    if isinstance(sv, dict):
        serving = sv.get("description") or (f"{sv['size']} {sv.get('unit','')}" if sv.get("size") else None)
    else:
        serving = sv or prof.get("servingSizeDescription")
    return {
        "nutrition": order_nutrition(nutrition),
        "serving_size": serving.strip() if isinstance(serving, str) else serving,
        "num_servings": d.get("numberOfServings") or prof.get("servingsPerContainer") or None,
        "ingredients": (d.get("ingredients") or "").strip(),
        "description": (d.get("description") or "").strip(),
    }


def _fetch_saveon(store_id, pid):
    r = requests.get(
        f"https://storefrontgateway.saveonfoods.com/api/stores/{store_id}/products/{pid}",
        headers={**_SOF_HEADERS, "X-Correlation-Id": str(uuid.uuid4())}, timeout=15)
    if r.status_code != 200:
        return None
    return _saveon_payload(r.json())


def _fetch_loblaw(banner, store_id, pid):
    d = loblaw.fetch_product_detail(banner, store_id, pid)
    if d is None:
        return None
    nutrition, serving = loblaw.parse_nutrition(d)
    return {
        "nutrition": order_nutrition(nutrition),
        "serving_size": serving,
        "num_servings": None,
        "ingredients": re.sub(r"&nbsp;?", " ", d.get("ingredients") or "").strip(),
        "description": re.sub(r"<[^>]+>", " ", d.get("description") or "").strip(),
    }


# ── Cache DB ──────────────────────────────────────────────────────────────────

def cache_path(retailer):
    return os.path.join(_DATA, f"nutrition_{retailer}.db")


def _cache_conn(retailer):
    conn = sqlite3.connect(cache_path(retailer))
    conn.execute("""CREATE TABLE IF NOT EXISTS facts (
        product_id TEXT PRIMARY KEY,
        payload    TEXT,              -- JSON; NULL = fetched but nothing published
        fetched_at TEXT DEFAULT (datetime('now')))""")
    return conn


def backfill(retailer, store_id, product_ids, budget=3000, workers=12, delay=0.0):
    """Fetches facts for up to `budget` uncached products. Returns fetch count.
    `delay` adds a per-worker pause between requests — the Loblaw detail
    endpoint bans IPs that fetch too aggressively."""
    conn = _cache_conn(retailer)
    have = {r[0] for r in conn.execute("SELECT product_id FROM facts")}
    todo = [p for p in product_ids if p not in have][:budget]
    if not todo:
        conn.close()
        return 0

    base = (lambda pid: _fetch_saveon(store_id, pid)) if retailer == "saveon" \
        else (lambda pid: _fetch_loblaw(retailer, store_id, pid))
    fetch = (lambda pid: (time.sleep(delay), base(pid))[1]) if delay else base

    done = errors = 0
    t0 = time.time()
    tty = sys.stdout.isatty()

    def progress():
        pct = done / len(todo)
        bar = "█" * int(28 * pct) + "░" * (28 - int(28 * pct))
        rate = done / max(time.time() - t0, 1)
        eta = (len(todo) - done) / max(rate, 0.1) / 60
        sys.stdout.write(f"\r  {retailer:10s} [{bar}] {done:>6,}/{len(todo):,}"
                         f"  {rate:4.0f}/s  ETA {eta:3.0f}m  errors {errors:,} ")
        sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, pid): pid for pid in todo}
        batch = []
        for fut in as_completed(futs):
            pid = futs[fut]
            try:
                payload = fut.result()
            except Exception:
                payload = None
            done += 1
            if tty and done % 20 == 0:
                progress()
            if payload is None:
                # fetch FAILED (HTTP error / rate limit) — don't cache, so it
                # retries next run. Only a successful "nothing published"
                # response is cached as NULL.
                errors += 1
                continue
            has_facts = bool(payload["nutrition"] or payload["ingredients"])
            batch.append((pid, json.dumps(payload, separators=(",", ":")) if has_facts else None))
            if len(batch) >= 500:
                conn.executemany("INSERT OR REPLACE INTO facts (product_id, payload) VALUES (?,?)", batch)
                conn.commit()
                batch = []
                if not tty:
                    log.info(f"  nutrition {retailer}: {done}/{len(todo)}")
    if tty:
        progress()
        print()
    conn.executemany("INSERT OR REPLACE INTO facts (product_id, payload) VALUES (?,?)", batch)
    conn.commit()
    conn.close()
    log.info(f"  nutrition {retailer}: fetched {done} (cache had {len(have)})")
    return done


def union_product_ids(retailer):
    """All product ids across every tracked store DB of a retailer."""
    import glob
    ids = set()
    for path in glob.glob(os.path.join(_DATA, f"grocery_pal_{retailer}_*.db")):
        conn = sqlite3.connect(path)
        try:
            ids.update(r[0] for r in conn.execute("SELECT product_id FROM products"))
        finally:
            conn.close()
    return ids


def a_store_of(retailer):
    """Any tracked store id of the retailer (details aren't store-sensitive)."""
    import glob
    for path in sorted(glob.glob(os.path.join(_DATA, f"grocery_pal_{retailer}_*.db"))):
        m = re.search(rf"grocery_pal_{retailer}_(\d+)\.db$", path)
        if m:
            return m.group(1)
    return None
