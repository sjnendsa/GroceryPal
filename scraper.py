"""
Save On Foods scraper — Grocery Pal
------------------------------------
Scrapes the FULL catalog by walking the store's category tree via the
storefrontgateway category-browse endpoint:

    /api/stores/{store}/categories/{categoryId}/search?skip=N&take=100

This enumerates every product in every department — unlike text search
(q=...), which only returns products matching the query words.

Category IDs are cached in data/categories.json. If the cache is missing,
they are re-harvested from the live site with Playwright (the site's HTML
nav contains links like /categories/bakery-id-30846).

Usage:
  python scraper.py                      # full catalog scrape (store 1982)
  python scraper.py --refresh-categories # re-harvest category ids first
  python scraper.py --dry-run            # show per-category totals, no DB writes
  python scraper.py --store 963          # different store ID
  python scraper.py --schedule           # run every 24 hours on a schedule
"""

import argparse
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://storefrontgateway.saveonfoods.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CA,en;q=0.9",
    "Origin": "https://www.saveonfoods.com",
    "Referer": "https://www.saveonfoods.com/",
    "X-Shopping-Mode": "11111111-1111-1111-1111-111111111111",
    "X-Site-Host": "www.saveonfoods.com",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

PAGE_SIZE = 100               # categories/{id}/search returns up to 100 per page
DEFAULT_WORKERS = 8           # concurrent page fetches (whole sync is only ~240 requests)
FLUSH_EVERY = 2000            # buffered products per DB transaction

_tls = threading.local()

def _session():
    """One requests.Session per worker thread (Session is not thread-safe)."""
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
    return _tls.s

import sys as _sys
_BASE = (os.path.dirname(_sys.executable) if getattr(_sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_CACHE = os.path.join(_BASE, "data", "categories.json")

# Snapshot of the top-level category tree (June 2026) — used if the cache file
# is missing and Playwright harvesting is unavailable.
DEFAULT_CATEGORIES = [
    {"id": "270574", "slug": "baby-care", "name": "Baby Care"},
    {"id": "30846", "slug": "bakery", "name": "Bakery"},
    {"id": "31470", "slug": "cleaning-paper-home", "name": "Cleaning Paper Home"},
    {"id": "30906", "slug": "dairy-eggs", "name": "Dairy Eggs"},
    {"id": "30726", "slug": "deli-ready-made-meals", "name": "Deli Ready Made Meals"},
    {"id": "31329", "slug": "floral-and-garden", "name": "Floral And Garden"},
    {"id": "30949", "slug": "frozen", "name": "Frozen"},
    {"id": "30681", "slug": "fruits-vegetables", "name": "Fruits Vegetables"},
    {"id": "31076", "slug": "health-beauty", "name": "Health Beauty"},
    {"id": "31405", "slug": "international-foods", "name": "International Foods"},
    {"id": "30791", "slug": "meat-seafood", "name": "Meat Seafood"},
    {"id": "31475", "slug": "pantry", "name": "Pantry"},
    {"id": "31016", "slug": "pet-care", "name": "Pet Care"},
    {"id": "32100", "slug": "plant-based-non-dairy", "name": "Plant Based Non Dairy"},
]


# ── API helpers ───────────────────────────────────────────────────────────────

def _get(path, params=None, session=None):
    headers = {**HEADERS, "X-Correlation-Id": str(uuid.uuid4())}
    url = BASE_URL + path
    try:
        r = (session or requests).get(url, headers=headers, params=params, timeout=20)
        return r
    except requests.RequestException as e:
        log.warning(f"Request failed: {e}")
        return None


def _get_json(path, params=None, session=None, retries=2):
    for attempt in range(retries + 1):
        r = _get(path, params=params, session=session)
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                pass
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


# ── Category discovery ────────────────────────────────────────────────────────

def harvest_categories():
    """
    Loads the real website in Chromium and extracts top-level category ids
    from nav links like /categories/bakery-id-30846. Requires Playwright.
    Returns a list of {"id", "slug", "name"} dicts, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — cannot harvest categories.")
        return None

    log.info("Harvesting category tree from saveonfoods.com (browser)...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(locale="en-CA", viewport={"width": 1400, "height": 900})
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            page.goto("https://www.saveonfoods.com/sm/pickup/rsid/1982/home",
                      wait_until="domcontentloaded", timeout=60_000)
            for _ in range(12):                     # wait out bot challenge
                page.wait_for_timeout(5000)
                if "moment" not in page.title().lower():
                    break
            page.wait_for_timeout(5000)
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
            browser.close()
    except Exception as e:
        log.warning(f"Category harvest failed: {e}")
        return None

    pat = re.compile(r"/categories/([a-z0-9\-]+)-id-(\d+)(?:[/?#]|$)")
    top = {}
    for h in set(hrefs or []):
        if not h:
            continue
        m = pat.search(h)
        if m and "/" not in h.split("/categories/", 1)[1].rsplit("-id-", 1)[0]:
            slug, cid = m.groups()
            top[cid] = {"id": cid, "slug": slug, "name": slug.replace("-", " ").title()}

    cats = sorted(top.values(), key=lambda c: c["name"])
    if cats:
        os.makedirs(os.path.dirname(CATEGORIES_CACHE), exist_ok=True)
        with open(CATEGORIES_CACHE, "w", encoding="utf-8") as f:
            json.dump(cats, f, indent=2)
        log.info(f"Harvested {len(cats)} top-level categories → {CATEGORIES_CACHE}")
    return cats or None


def load_categories(refresh=False):
    if refresh:
        cats = harvest_categories()
        if cats:
            return cats
        log.warning("Harvest failed — falling back to cached/default categories.")
    if os.path.exists(CATEGORIES_CACHE):
        try:
            with open(CATEGORIES_CACHE, encoding="utf-8") as f:
                cats = json.load(f)
            if cats:
                return cats
        except Exception:
            pass
    cats = harvest_categories()
    return cats or DEFAULT_CATEGORIES


# ── Data normalisation ────────────────────────────────────────────────────────

def _normalise_product(raw, category=None):
    """
    Normaliser for storefrontgateway product items.
    Field reference (confirmed from live API):
      productId, name, brand, priceNumeric, wholePrice,
      tprPrice[].{active, wholePrice, markdown, label},
      promotions[].{name, description},
      image.cell, defaultCategory[].categoryBreadcrumb,
      pricePerUnit, unitOfSize.{size, abbreviation}, available
    Returns (product_dict, price_info) or (None, None) if unusable.
    """
    pid  = raw.get("productId") or raw.get("sku") or raw.get("id")
    name = raw.get("name") or raw.get("productName")
    if not pid or not name:
        return None, None

    # priceNumeric = More Rewards / promotional price (what the user pays)
    # wholePrice   = regular single-unit price (non-member or buy-1 price)
    # wasPriceNumeric = "was" price before the current promotion period
    current_price = raw.get("priceNumeric") or raw.get("wholePrice")
    if current_price is None:
        return None, None
    current_price = float(current_price)

    whole = raw.get("wholePrice")
    regular_price = float(whole) if whole else None

    was_val = raw.get("wasPriceNumeric")
    was_price = float(was_val) if was_val else None

    promos = raw.get("promotions") or []
    active_promo = promos[0] if promos else None

    tpr_list = raw.get("tprPrice") or []
    active_tpr = next((t for t in tpr_list if t.get("active")), None)

    on_sale = bool(active_promo or active_tpr)
    sale_label = active_promo.get("name") if active_promo else (active_tpr.get("label") if active_tpr else None)
    # minimumQuantity > 1 = conditional deal (buy 2+); 1 = unconditional
    min_qty = int(active_promo.get("minimumQuantity") or 1) if active_promo else 1

    img_obj = raw.get("image") or {}
    img = img_obj.get("cell") or img_obj.get("default") if isinstance(img_obj, dict) else None
    if not img:
        img = raw.get("imageUrl") or raw.get("thumbnailUrl")

    # Breadcrumb e.g. "Grocery/Dairy & Eggs/Milk & Creams/2% Milk"
    default_cats = raw.get("defaultCategory") or []
    breadcrumb = default_cats[0].get("categoryBreadcrumb", "") if default_cats else ""
    crumbs = [c.strip() for c in breadcrumb.split("/") if c.strip()]
    cat = (crumbs[1] if len(crumbs) > 1 else crumbs[0] if crumbs else None) or category
    subcat = crumbs[2] if len(crumbs) > 2 else None

    uos = raw.get("unitOfSize") or {}
    size = f"{uos.get('size')} {uos.get('abbreviation')}" if uos.get("size") else None

    product = {
        "product_id": str(pid),
        "name": name,
        "brand": raw.get("brand") or raw.get("brandName"),
        "category": cat,
        "subcategory": subcat,
        "image_url": img,
        "unit": raw.get("pricePerUnit"),
        "size": size or raw.get("size"),
        "url": raw.get("url") or raw.get("productUrl"),
    }

    price = {
        "price": current_price,
        "regular_price": regular_price,
        "was_price": was_price,
        "on_sale": on_sale,
        "sale_label": sale_label,
        "min_qty": min_qty,
        "in_stock": bool(raw.get("available", True)),
    }

    return product, price


# ── Catalog scraper ───────────────────────────────────────────────────────────

def scrape_api(store_id, refresh_categories=False, dry_run=False, workers=DEFAULT_WORKERS, db_key=None):
    """
    Full-catalog scrape: paginate every top-level category via
    /api/stores/{store}/categories/{id}/search.

    Pages are fetched CONCURRENTLY (workers threads — the whole catalog is only
    ~240 requests) and products are written to SQLite in bulk transactions.
    Products are deduped across categories by product_id.
    """
    categories = load_categories(refresh=refresh_categories)
    log.info(f"Scraping store {store_id} — {len(categories)} categories, {workers} workers")
    t0 = time.time()

    def fetch(cid, skip):
        return _get_json(f"/api/stores/{store_id}/categories/{cid}/search",
                         params={"skip": skip, "take": PAGE_SIZE}, session=_session())

    # ── Pass 1: first page of every category in parallel (totals + items) ────
    with ThreadPoolExecutor(max_workers=workers) as ex:
        firsts = list(ex.map(lambda c: (c, fetch(c["id"], 0)), categories))

    first_pages = []     # (category_name, items) from pass 1
    page_tasks = []      # (category_id, category_name, skip) still to fetch
    failed_cats = []
    catalog_total = 0
    for cat, data in firsts:
        if data is None:
            log.warning(f"  [{cat['name']}] endpoint failed — skipping")
            failed_cats.append(cat["name"])
            continue
        cat_total = int(data.get("total") or 0)
        api_name = data.get("categoryName") or cat["name"]
        catalog_total += cat_total
        if dry_run:
            log.info(f"  [{api_name}] {cat_total} products")
            continue
        first_pages.append((api_name, data.get("items") or []))
        for skip in range(PAGE_SIZE, cat_total, PAGE_SIZE):
            page_tasks.append((cat["id"], api_name, skip))

    if dry_run:
        log.info(f"Dry run — {catalog_total} products across categories (pre-dedupe)")
        return catalog_total

    # ── Ingest machinery: normalise, dedupe, buffer, bulk-write ──────────────
    db.set_store(db_key or f"saveon_{store_id}")   # per-store database file
    run_id = db.create_run()
    existing_ids = db.get_all_product_ids()
    seen_ids: set = set()
    pending = []         # (product, price) buffered for the next bulk write
    total_products = 0
    new_products = 0

    def flush():
        nonlocal pending
        db.save_batch(pending)
        pending = []

    def ingest(cat_name, items):
        nonlocal total_products, new_products
        for raw in items:
            product, price = _normalise_product(raw, category=cat_name)
            if not product or not price:
                continue
            pid = product["product_id"]
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            pending.append((product, price))
            total_products += 1
            if pid not in existing_ids:
                new_products += 1

    status, notes = "completed", None
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        for name, items in first_pages:
            ingest(name, items)

        # ── Pass 2: all remaining pages in parallel ───────────────────────────
        futures = {ex.submit(fetch, cid, skip): cname
                   for cid, cname, skip in page_tasks}
        done = failed_pages = 0
        for fut in as_completed(futures):
            data = fut.result()
            done += 1
            if data is None:
                failed_pages += 1
                log.warning(f"  [{futures[fut]}] page fetch failed")
            else:
                ingest(futures[fut], data.get("items") or [])
            if len(pending) >= FLUSH_EVERY:
                flush()
            if done % 50 == 0 or done == len(page_tasks):
                log.info(f"  {done}/{len(page_tasks)} pages — {total_products} products")
        flush()

        elapsed = time.time() - t0
        if total_products == 0:
            status, notes = "failed", "Category browse returned 0 products."
            log.warning("No products scraped — API may have changed. "
                        "Try: python scraper.py --refresh-categories")
        else:
            notes = (f"{len(categories) - len(failed_cats)}/{len(categories)} categories "
                     f"in {elapsed/60:.1f} min")
            if failed_cats:
                notes += f" (failed: {', '.join(failed_cats)})"
            if failed_pages:
                notes += f", {failed_pages} pages failed"
            log.info(f"Done — {total_products} unique products "
                     f"({new_products} new) in {elapsed/60:.1f} min")

    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        flush()
        status, notes = "interrupted", None
        log.info("Interrupted — partial data saved.")
    except Exception as e:
        ex.shutdown(wait=False, cancel_futures=True)
        flush()
        status, notes = "error", str(e)
        log.exception(f"Scrape error: {e}")
    finally:
        ex.shutdown(wait=True)
        db.complete_run(run_id, total_products, new_products, status=status, notes=notes)

    triggered = db.check_alerts()
    for a in triggered:
        log.info(f"  PRICE ALERT: {a['name']} -> ${a['current_price']:.2f} (target ${a['target_price']:.2f})")

    return total_products


# ── Scheduled scraping ────────────────────────────────────────────────────────

def run_schedule(store_id, interval_hours=24):
    import schedule

    def job():
        log.info(f"Scheduled scrape starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        scrape_api(store_id)

    schedule.every(interval_hours).hours.do(job)
    log.info(f"Scheduled: scraping every {interval_hours}h. Press Ctrl+C to stop.")
    job()  # run immediately on start
    while True:
        schedule.run_pending()
        time.sleep(60)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Grocery Pal — Save On Foods scraper")
    parser.add_argument("--store", default="1982", help="Store ID (default: 1982)")
    parser.add_argument("--refresh-categories", action="store_true",
                        help="Re-harvest category ids from the live site first")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show per-category product totals without writing to DB")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent page fetches (default: {DEFAULT_WORKERS})")
    parser.add_argument("--schedule", action="store_true", help="Run on a 24h schedule")
    parser.add_argument("--interval", type=int, default=24, help="Schedule interval in hours")
    args = parser.parse_args()

    if args.schedule:
        run_schedule(args.store, args.interval)
    else:
        scrape_api(args.store, refresh_categories=args.refresh_categories,
                   dry_run=args.dry_run, workers=args.workers)


if __name__ == "__main__":
    main()
