import requests

import db
import scraper

db.init_db()
session = requests.Session()

# Test 1: category list loads
cats = scraper.load_categories()
print(f"Categories loaded: {len(cats)}")
for c in cats:
    print(f"  {c['id']:>8}  {c['name']}")
assert len(cats) >= 10, "expected at least 10 top-level categories"
print()

# Test 2: category browse endpoint returns products and they normalise
cid, cname = cats[0]["id"], cats[0]["name"]
data = scraper._get_json(
    f"/api/stores/1982/categories/{cid}/search",
    params={"skip": 0, "take": 5},
    session=session,
)
assert data is not None, "category browse request failed"
items = data.get("items") or []
print(f"[{data.get('categoryName') or cname}] total={data.get('total')}, items={len(items)}")

ok = 0
for raw in items:
    p, pr = scraper._normalise_product(raw, category=cname)
    if p and pr:
        ok += 1
        if pr["on_sale"] and pr["regular_price"]:
            sale_str = f'SALE ${pr["regular_price"]:.2f} -> ${pr["price"]:.2f}'
        else:
            sale_str = f'${pr["price"]:.2f}'
        print(f'  {p["name"][:55]:55s}  {sale_str:25s}  cat={p["category"]}  img={bool(p["image_url"])}')
    else:
        print(f'  SKIP {raw.get("name", "?")[:40]}')

print(f"{ok}/{len(items)} normalised OK")
assert ok == len(items) > 0
