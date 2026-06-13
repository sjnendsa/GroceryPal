"""
Grocery Pal — Dashboard server
Run:  python app.py
Then open http://localhost:5000
"""

import json
import os
import sys
import threading
import time
import uuid
import requests as _requests
from flask import Flask, jsonify, render_template, request

import db

# When frozen into an .exe, data lives next to the exe and templates are
# unpacked into PyInstaller's temp dir (sys._MEIPASS).
_FROZEN = getattr(sys, "frozen", False)
_BASE = os.path.dirname(sys.executable) if _FROZEN else os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(getattr(sys, "_MEIPASS", _BASE), "templates"))

_DATA_DIR = os.path.join(_BASE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_SETTINGS_PATH = os.path.join(_DATA_DIR, "settings.json")
_STORES_CACHE = os.path.join(_DATA_DIR, "stores.json")


def _load_settings():
    try:
        # utf-8-sig: tolerate the BOM that PowerShell/Windows editors prepend
        with open(_SETTINGS_PATH, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"store_id": "1982", "store_name": "Save-on-Foods Langley", "retailer": "saveon"}


def _save_settings(s):
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def _db_key(settings):
    """Composite per-store DB key; retailer prefix avoids id collisions
    between chains (Save-On 1982 vs Superstore 1521 etc.)."""
    return f"{settings.get('retailer', 'saveon')}_{settings['store_id']}"


_settings = _load_settings()
_settings.setdefault("retailer", "saveon")
db.set_store(_db_key(_settings))

# ── HTML ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Stores ────────────────────────────────────────────────────────────────────

_COORDS_OVERRIDES = os.path.join(_DATA_DIR, "store_coords_overrides.json")


def _apply_overrides(stores):
    """API lat/lngs are sloppy geocodes; replace with corrected ones where known."""
    try:
        with open(_COORDS_OVERRIDES, encoding="utf-8") as f:
            ov = json.load(f)
        for s in stores:
            # overrides were geocoded from OSM for Save-On only
            if s.get("retailer", "saveon") != "saveon":
                continue
            fix = ov.get(s["store_id"])
            if fix:
                s["lat"], s["lng"] = fix["lat"], fix["lng"]
    except Exception:
        pass
    return stores


def _fetch_saveon_stores():
    headers = {**_SOF_HEADERS, "X-Correlation-Id": str(uuid.uuid4())}
    r = _requests.get("https://storefrontgateway.saveonfoods.com/api/stores",
                      headers=headers, timeout=20)
    if r.status_code != 200:
        return []
    out = []
    for s in r.json().get("items", []):
        loc = s.get("location") or {}
        if s.get("status") != "Active" or not s.get("retailerStoreId") or not loc.get("latitude"):
            continue
        out.append({
            "store_id": s["retailerStoreId"],
            "retailer": "saveon",
            "chain": "Save-On-Foods",
            "name": s.get("name"),
            "address": s.get("addressLine1"),
            "city": s.get("city"),
            "province": s.get("countyProvinceState"),
            "postcode": s.get("postCode"),
            "phone": s.get("phone"),
            "hours": s.get("openingHours"),
            "lat": loc["latitude"],
            "lng": loc["longitude"],
        })
    return out


def _stores_cache_usable(stores):
    """Caches written before multi-chain support lack the retailer tag, and a
    cache missing a chain means one fetch failed — both get refetched."""
    return (isinstance(stores, list) and stores
            and all(s.get("retailer") for s in stores)
            and {"saveon", "superstore", "nofrills"} <= {s["retailer"] for s in stores})


@app.route("/api/stores")
def stores_list():
    """All locations across supported chains (name, address, lat/lng). Cached 7 days."""
    try:
        if os.path.exists(_STORES_CACHE) and time.time() - os.path.getmtime(_STORES_CACHE) < 7 * 86400:
            with open(_STORES_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            if _stores_cache_usable(cached):
                return jsonify(_apply_overrides(cached))
    except Exception:
        pass
    import loblaw
    stores = _fetch_saveon_stores()
    for banner in ("superstore", "nofrills"):
        stores += loblaw.fetch_stores(banner)
    if not stores:
        return jsonify({"error": "store list unavailable"}), 502
    with open(_STORES_CACHE, "w", encoding="utf-8") as f:
        json.dump(stores, f)
    return jsonify(_apply_overrides(stores))


@app.route("/api/store", methods=["GET"])
def store_get():
    s = dict(_settings)
    s["has_data"] = os.path.exists(os.path.join(_DATA_DIR, f"grocery_pal_{_db_key(_settings)}.db"))
    return jsonify(s)


@app.route("/api/store", methods=["POST"])
def store_set():
    data = request.get_json() or {}
    sid = str(data.get("store_id", "")).strip()
    if not sid:
        return jsonify({"error": "store_id required"}), 400
    if _scrape_status["running"]:
        return jsonify({"error": "Cannot switch stores during a sync"}), 409
    _settings["store_id"] = sid
    _settings["retailer"] = data.get("retailer") or "saveon"
    _settings["store_name"] = data.get("store_name") or sid
    _save_settings(_settings)
    key = _db_key(_settings)
    had_data = os.path.exists(os.path.join(_DATA_DIR, f"grocery_pal_{key}.db"))
    db.set_store(key)
    return jsonify({"status": "ok", "store_id": sid, "has_data": had_data})


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def stats():
    return jsonify(db.get_stats())


# ── Products ──────────────────────────────────────────────────────────────────

@app.route("/api/products")
def products():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip() or None
    on_sale = request.args.get("on_sale", "false").lower() == "true"
    sort = request.args.get("sort", "name")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 48))

    items, total = db.get_products(
        search=search or None,
        category=category,
        on_sale=on_sale,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    return jsonify({"products": items, "total": total, "page": page, "per_page": per_page})


@app.route("/api/products/<product_id>")
def product_detail(product_id):
    p = db.get_product(product_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify(p)


_SOF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.saveonfoods.com",
    "Referer": "https://www.saveonfoods.com/",
    "X-Shopping-Mode": "11111111-1111-1111-1111-111111111111",
    "X-Site-Host": "www.saveonfoods.com",
}

# Canonical Nutrition Facts label layout: (canonical name, indent level, aliases)
# Anything not listed lands in the vitamins/minerals section at the bottom.
_NUTRIENT_LAYOUT = [
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


def _order_nutrition(nutrition: dict) -> list:
    """Turns a {name: {size, unit, pct}} dict into ordered label rows:
    macros in canonical order with indents, then vitamins/minerals."""
    remaining = dict(nutrition)
    lower_map = {k.lower().strip(): k for k in remaining}
    rows = []
    for canonical, indent, aliases in _NUTRIENT_LAYOUT:
        for alias in aliases:
            key = lower_map.get(alias)
            if key and key in remaining:
                v = remaining.pop(key)
                rows.append({"name": canonical, "indent": indent, "group": "main", **v})
                break
    # everything left = vitamins & minerals (Iron, Calcium, Potassium, Vitamin A...)
    for key in sorted(remaining):
        rows.append({"name": key, "indent": 0, "group": "micro", **remaining[key]})
    return rows


@app.route("/api/products/<product_id>/live")
def product_live_detail(product_id):
    """Fetches live description, nutrition, and current promotions from Save On Foods API."""
    # Live product detail (nutrition/ingredients) is a Save-On-only endpoint.
    if _settings.get("retailer", "saveon") != "saveon":
        return jsonify({"nutrition": [], "promotions": [], "description": "", "ingredients": ""})
    store_id = _settings["store_id"]
    headers = {**_SOF_HEADERS, "X-Correlation-Id": str(uuid.uuid4())}
    try:
        r = _requests.get(
            f"https://storefrontgateway.saveonfoods.com/api/stores/{store_id}/products/{product_id}",
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            return jsonify({"error": "Not found in live API"}), 404
        d = r.json()

        # Parse nutrition profile — confirmed live shape (June 2026):
        # nutritionProfiles.nutrition = {name: {size, unit, abbreviation,
        #                                       percentDailyValue, traceAmount}}
        nutrition = {}
        profiles = d.get("nutritionProfiles") or {}

        if isinstance(profiles, dict):
            raw_nutrients = profiles.get("nutrition") or {}
            if isinstance(raw_nutrients, dict):
                for name, vals in raw_nutrients.items():
                    if isinstance(vals, dict):
                        nutrition[name] = {
                            "size": vals.get("size"),
                            "unit": vals.get("abbreviation") or vals.get("unit"),
                            "pct": vals.get("percentDailyValue"),
                        }
            # Older shape: {"nutrients": [{"name": ..., "amount": N, ...}]}
            if not nutrition:
                for n in (profiles.get("nutrients") or []):
                    if isinstance(n, dict) and n.get("name"):
                        nutrition[n["name"]] = {
                            "size": n.get("amount") or n.get("size"),
                            "unit": n.get("unit") or n.get("abbreviation"),
                            "pct": n.get("dailyValue") or n.get("percentDailyValue"),
                        }
        elif isinstance(profiles, list):
            for n in profiles:
                if isinstance(n, dict) and n.get("name"):
                    nutrition[n["name"]] = {
                        "size": n.get("amount") or n.get("size"),
                        "unit": n.get("unit") or n.get("abbreviation"),
                        "pct": n.get("dailyValue") or n.get("percentDailyValue"),
                    }

        if not nutrition and isinstance(d.get("nutrition"), dict):
            for name, vals in d["nutrition"].items():
                if isinstance(vals, dict):
                    nutrition[name] = {
                        "size": vals.get("size"),
                        "unit": vals.get("abbreviation") or vals.get("unit"),
                        "pct": vals.get("percentDailyValue"),
                    }

        # Parse promotions
        promos = []
        for p in (d.get("promotions") or []):
            promos.append({
                "name": p.get("name"),
                "start": p.get("startDate"),
                "end": p.get("endDate"),
                "min_qty": p.get("minimumQuantity", 1),
                "loyalty": p.get("loyaltyBased", False),
                "limit": p.get("limit"),
            })

        # Parse pricing
        tpr = d.get("tprInfo") or {}

        _prof = profiles if isinstance(profiles, dict) else {}
        # servingSize can be a dict like {"size": N, "description": "per 1 cup"}
        _sv_raw = d.get("servingSize") or _prof.get("servingSize")
        if isinstance(_sv_raw, dict):
            serving_size = (
                _sv_raw.get("description") or
                (f"{_sv_raw['size']} {_sv_raw.get('unit','')}" if _sv_raw.get("size") else None)
            )
        else:
            serving_size = _sv_raw or _prof.get("servingSizeDescription")
        _ns_raw = d.get("numberOfServings") or _prof.get("servingsPerContainer") or _prof.get("numberOfServings")
        num_servings = _ns_raw if _ns_raw else None
        return jsonify({
            "description": d.get("description") or "",
            "ingredients": d.get("ingredients") or "",
            "serving_size": (serving_size.strip() if isinstance(serving_size, str) else serving_size),
            "num_servings": num_servings,
            "nutrition": _order_nutrition(nutrition),
            "promotions": promos,
            "price": d.get("price"),          # More Rewards / sale price
            "was_price": d.get("wasPrice"),   # regular price before promotion
            "tpr": tpr.get("markdown"),       # the sale price label
            "tpr_until": tpr.get("effectiveUntil"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/products/<product_id>/history")
def product_history(product_id):
    history = db.get_price_history(product_id)
    return jsonify(history)


# ── Categories ────────────────────────────────────────────────────────────────

@app.route("/api/categories")
def categories():
    return jsonify(db.get_categories())


# ── Sales & trends ────────────────────────────────────────────────────────────

@app.route("/api/sales")
def sales():
    return jsonify(db.get_sales())


@app.route("/api/price-drops")
def price_drops():
    limit = int(request.args.get("limit", 10))
    return jsonify(db.get_top_price_drops(limit))


# ── Scrape runs ───────────────────────────────────────────────────────────────

@app.route("/api/runs")
def runs():
    return jsonify(db.get_runs())


_scrape_thread = None
_scrape_status = {"running": False, "message": ""}


def _scrape_loblaw(banner, store_id, db_key):
    """Full-catalog sync for a Loblaw banner (No Frills / Superstore)."""
    import loblaw
    db.set_store(db_key)
    run_id = db.create_run()
    cats = loblaw.fetch_leaf_categories(banner)
    existing = db.get_all_product_ids()
    buf, seen, new = [], 0, 0

    def progress(done, total, count):
        _scrape_status["message"] = f"Syncing {loblaw.BANNER_LABEL[banner]}… {count} items ({done}/{total} categories)"

    try:
        for product, price in loblaw.iter_catalog(banner, store_id, cats, on_page=progress):
            buf.append((product, price))
            if product["product_id"] not in existing:
                new += 1
            seen += 1
            if len(buf) >= 2000:
                db.save_batch(buf); buf = []
        db.save_batch(buf)
        db.complete_run(run_id, seen, new,
                        notes=f"{len(cats)} categories ({loblaw.BANNER_LABEL[banner]})")
    except Exception as e:
        db.save_batch(buf)
        db.complete_run(run_id, seen, new, status="error", notes=str(e))
        raise
    db.check_alerts()
    return seen


@app.route("/api/scrape", methods=["POST"])
def trigger_scrape():
    global _scrape_thread, _scrape_status
    if _scrape_status["running"]:
        return jsonify({"error": "A scrape is already running"}), 409

    store = _settings["store_id"]
    retailer = _settings.get("retailer", "saveon")
    db_key = _db_key(_settings)

    def run():
        _scrape_status["running"] = True
        _scrape_status["message"] = "Scraping..."
        try:
            if retailer in ("superstore", "nofrills"):
                n = _scrape_loblaw(retailer, store, db_key)
            else:
                import scraper
                n = scraper.scrape_api(store, db_key=db_key)
            _scrape_status["message"] = f"Done — {n} products"
        except Exception as e:
            _scrape_status["message"] = f"Error: {e}"
        finally:
            _scrape_status["running"] = False
            db.set_store(db_key)  # scraper may have re-pointed the DB

    _scrape_thread = threading.Thread(target=run, daemon=True)
    _scrape_thread.start()
    return jsonify({"status": "started"})


@app.route("/api/scrape/status")
def scrape_status():
    return jsonify(_scrape_status)


# ── Price alerts ──────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def alerts_list():
    return jsonify(db.get_alerts())


@app.route("/api/alerts", methods=["POST"])
def alerts_create():
    data = request.get_json()
    if not data or "product_id" not in data or "target_price" not in data:
        return jsonify({"error": "product_id and target_price required"}), 400
    db.add_alert(data["product_id"], float(data["target_price"]))
    return jsonify({"status": "created"}), 201


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def alerts_delete(alert_id):
    db.delete_alert(alert_id)
    return jsonify({"status": "deleted"})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("PORT", 5000))
    print(f"Grocery Pal dashboard running at http://localhost:{port}")
    if _FROZEN or os.environ.get("GROCERY_PAL_OPEN"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(debug=not _FROZEN, port=port, use_reloader=False)
