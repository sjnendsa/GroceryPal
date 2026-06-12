"""
Loblaw / PC Express scraper — Grocery Pal
------------------------------------------
Covers the Loblaw banners that share the api.pcexpress.ca backend:
  - No Frills              (banner "nofrills")
  - Real Canadian Superstore (banner "superstore")

Full catalog is enumerated by walking the banner's leaf (L3) categories,
which are harvested from the banner's SEO sitemap.xml (the storefront site
itself bot-blocks scripted browsing, but the sitemap and the API do not).

Per category:
  POST /pcx-bff/api/v2/products/search
  body filters: {"category": ["<leafCategoryId>"]}, pagination {"from": N}
Products are TILES nested in response.layout (any dict with a productId).

Returns products in the SAME normalised (product, price) dict shape the
Save-On scraper uses, so db.save_batch and the dashboard work unchanged.
"""

import json
import logging
import os
import re
import time
import uuid

import requests

_BASE = (os.path.dirname(__import__("sys").executable) if getattr(__import__("sys"), "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE, "data")

log = logging.getLogger("scraper.loblaw")

API = "https://api.pcexpress.ca/pcx-bff"
APIKEY = "C1xujSegT5j3ap3yexJjqhOfELwGKYvz"   # public web key, all banners

# banner -> storefront host (used for Origin/Referer and sitemap)
BANNER_HOST = {
    "superstore": "www.realcanadiansuperstore.ca",
    "nofrills": "www.nofrills.ca",
}
BANNER_LABEL = {"superstore": "Real Canadian Superstore", "nofrills": "No Frills"}

PAGE_SIZE = 48          # tiles per response
PAGE_CAP = 960          # API rejects pagination.from > ~960
RATE_LIMIT_DELAY = 0.05


def _headers(banner):
    host = BANNER_HOST.get(banner, "www.realcanadiansuperstore.ca")
    return {
        "X-Apikey": APIKEY,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en",
        "X-Application-Type": "Web",
        "X-Channel": "web",
        "Business-User-Agent": "PCXWEB",
        "X-Loblaw-Tenant-Id": "ONLINE_GROCERIES",
        "Origin": f"https://{host}",
        "Referer": f"https://{host}/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    }


# ── Category harvest (from sitemap) ───────────────────────────────────────────

def fetch_leaf_categories(banner):
    """Returns [{'id','slug'}] of L3 leaf categories from the banner sitemap.
    Caches to data/loblaw_cats_{banner}.json so a transient sitemap blip never
    yields an empty scrape — on failure we fall back to the last good cache."""
    cache = os.path.join(_CACHE_DIR, f"loblaw_cats_{banner}.json")
    cats = {}
    host = BANNER_HOST[banner]

    def pretty(slug):
        return slug.strip("-").replace("-", " ").title().replace(" And ", " & ")

    # Harvest L2 departments, each carrying its L3 children as a fallback.
    # We sweep L2 (14 of them) instead of L3 (146) — far fewer requests — and
    # only drop to a department's children if it exceeds the 960 pagination cap.
    try:
        xml = requests.get(f"https://{host}/sitemap.xml",
                           headers={"User-Agent": _headers(banner)["User-Agent"]},
                           timeout=25).text
        children = {}   # l2_id -> [child cats]
        for m in re.finditer(r"/en/food/([a-z0-9\-/]+)/c/(\d+)\?navid=flyout-(L\d)-", xml):
            path, cid, level = m.groups()
            parts = path.split("/")
            if level == "L2":
                cats[cid] = {"id": cid, "category": pretty(parts[-1]),
                             "subcategory": None, "children": []}
            elif level == "L3" and len(parts) > 1:
                children.setdefault(parts[-2], []).append(
                    {"id": cid, "subcategory": pretty(parts[-1])})
        # attach children to their L2 parent by slug
        slug_to_id = {}
        for m in re.finditer(r"/en/food/([a-z0-9\-]+)/c/(\d+)\?navid=flyout-L2-", xml):
            slug_to_id[m.group(1)] = m.group(2)
        for l2slug, kids in children.items():
            l2id = slug_to_id.get(l2slug)
            if l2id and l2id in cats:
                for k in kids:
                    cats[l2id]["children"].append(
                        {"id": k["id"], "category": cats[l2id]["category"],
                         "subcategory": k["subcategory"]})
    except requests.RequestException as e:
        log.warning(f"sitemap fetch failed: {e}")

    result = list(cats.values())
    if len(result) >= 8:                        # good harvest (≈14 L2s) → refresh cache
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(result, f)
        except OSError:
            pass
        return result
    # harvest failed/too small → reuse last known-good cache
    try:
        with open(cache, encoding="utf-8") as f:
            cached = json.load(f)
        log.warning(f"using cached {len(cached)} categories for {banner}")
        return cached
    except (OSError, ValueError):
        return result


def fetch_stores(banner):
    """Returns normalised store dicts for a banner (same shape as Save-On)."""
    try:
        r = requests.get(f"{API}/api/v1/pickup-locations?bannerIds={banner}",
                         headers=_headers(banner), timeout=25)
        if r.status_code != 200:
            return []
        rows = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for s in rows if isinstance(rows, list) else []:
        gp = s.get("geoPoint") or {}
        addr = s.get("address") or {}
        if not s.get("visible", True) or not gp.get("latitude"):
            continue
        out.append({
            "store_id": str(s.get("storeId")),
            "retailer": banner,
            "chain": BANNER_LABEL.get(banner, banner),
            "name": s.get("name"),
            "address": addr.get("line1"),
            "city": addr.get("town"),
            "province": addr.get("region"),
            "postcode": addr.get("postalCode"),
            "phone": s.get("orderContactNumber"),
            "hours": None,
            "lat": gp["latitude"],
            "lng": gp["longitude"],
        })
    return out


# ── Product tile extraction ───────────────────────────────────────────────────

def _iter_tiles(node):
    if isinstance(node, dict):
        if node.get("productId"):
            yield node
        else:
            for v in node.values():
                yield from _iter_tiles(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_tiles(v)


def _to_float(s):
    if s is None:
        return None
    try:
        return float(re.sub(r"[^0-9.]", "", str(s)))
    except (ValueError, TypeError):
        return None


def normalise(tile, category=None, subcategory=None):
    """tile (PC Express product) -> (product_dict, price_dict) | (None, None)."""
    pid = tile.get("productId")
    name = tile.get("title")
    if not pid or not name:
        return None, None

    pricing = tile.get("pricing") or {}
    price = _to_float(pricing.get("price"))
    member = _to_float(pricing.get("memberOnlyPrice"))
    was = _to_float(pricing.get("wasPrice"))
    if price is None and member is None:
        return None, None

    # member price is what the shopper pays when lower; regular = shelf
    current = member if (member is not None and (price is None or member < price)) else price
    regular = price if (member is not None and member < (price or 1e9)) else None

    promos = tile.get("promotions") or []
    deal = tile.get("deal") or {}
    on_sale = bool(was) or bool(deal) or bool(promos)
    label = None
    if promos and isinstance(promos[0], dict):
        label = promos[0].get("text") or promos[0].get("title")
    if not label and isinstance(deal, dict):
        label = deal.get("text")

    imgs = tile.get("productImage") or []
    img = None
    if imgs and isinstance(imgs[0], dict):
        img = imgs[0].get("imageUrl") or imgs[0].get("mediumUrl") or imgs[0].get("largeUrl")

    product = {
        "product_id": str(pid),
        "name": name,
        "brand": tile.get("brand"),
        "category": category,
        "subcategory": subcategory,
        "image_url": img,
        "unit": (pricing.get("pricingUnits") or {}).get("type") if isinstance(pricing.get("pricingUnits"), dict) else None,
        "size": tile.get("packageSizing"),
        "url": (tile.get("link") or None),
    }
    price_info = {
        "price": current,
        "regular_price": regular if regular is not None else was,
        "was_price": was,
        "on_sale": on_sale,
        "sale_label": label,
        "min_qty": 1,
        "in_stock": (tile.get("inventoryIndicator") not in ("OUT_OF_STOCK", "NOT_AVAILABLE")),
    }
    return product, price_info


# ── Catalog enumeration ───────────────────────────────────────────────────────

def _search_page(session, banner, store_id, category_id, frm):
    body = {
        "cart": {"cartId": str(uuid.uuid4())},
        "fulfillmentInfo": {"storeId": store_id, "pickupType": "STORE",
                            "offerType": "OG", "date": "", "timeSlot": None},
        "listingInfo": {"filters": {"category": [category_id]}, "sort": {},
                        "pagination": {"from": frm}, "includeFiltersInResponse": False},
        "banner": banner,
        "userData": {"domainUserId": str(uuid.uuid4()), "sessionId": str(uuid.uuid4())},
        "device": {"screenSize": 1358},
        "searchRelatedInfo": {"term": "", "options": [{"name": "rmp.unifiedSearchVariant", "value": "Y"}]},
    }
    try:
        r = session.post(f"{API}/api/v2/products/search", headers=_headers(banner),
                         json=body, timeout=25)
        if r.status_code != 200:
            return None, 0
        d = r.json()
        return d, int(d.get("searchResultsCount") or 0)
    except (requests.RequestException, ValueError):
        return None, 0


def _fetch_category(banner, store_id, cat):
    """Fetches ALL pages of one category. pagination.from is a 1-based PAGE
    NUMBER (not an offset). If the category exceeds the pagination cap, returns
    its L3 children so the caller can sweep them instead.
    Returns (cat, tiles, overflow_children)."""
    session = requests.Session()
    tiles, page = [], 1
    while True:
        data, total = _search_page(session, banner, store_id, cat["id"], page)
        if not data:
            break
        # department too big to page fully → defer to its children
        if total > PAGE_CAP and cat.get("children"):
            return cat, [], cat["children"]
        batch = list(_iter_tiles(data.get("layout")))
        if not batch:
            break
        tiles.extend(batch)
        if len(tiles) >= total or page * PAGE_SIZE >= total:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)
    return cat, tiles, None


def iter_catalog(banner, store_id, categories, on_page=None, workers=16):
    """Yields normalised (product, price) for the whole catalog, deduped.
    Categories are fetched in parallel; on_page(done, total, count) reports progress."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    seen = set()
    done = 0
    total_cats = len(categories)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = {ex.submit(_fetch_category, banner, store_id, c) for c in categories}
        while pending:
            fut = next(as_completed(pending))
            pending.discard(fut)
            cat, tiles, overflow = fut.result()
            if overflow:                      # department too big → sweep its children
                total_cats += len(overflow) - 1
                for child in overflow:
                    pending.add(ex.submit(_fetch_category, banner, store_id, child))
                continue
            done += 1
            for tile in tiles:
                product, price = normalise(tile, category=cat.get("category"),
                                           subcategory=cat.get("subcategory"))
                if not product or product["product_id"] in seen:
                    continue
                seen.add(product["product_id"])
                yield product, price
            if on_page:
                on_page(done, total_cats, len(seen))
