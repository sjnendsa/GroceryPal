"""Smoke test for the Loblaw (No Frills / Superstore) scraper.
Run: python test_loblaw.py"""
import loblaw

banner, store = "superstore", "1521"

cats = loblaw.fetch_leaf_categories(banner)
print(f"L2 departments: {len(cats)} (each with {sum(len(c.get('children',[])) for c in cats)} child cats total)")
assert len(cats) >= 8 and any(c.get("children") for c in cats)

stores = loblaw.fetch_stores(banner)
print(f"{banner} stores: {len(stores)}")
assert stores and stores[0]["lat"] and stores[0]["retailer"] == banner

# enumerate first 4 categories only (quick sanity, not a full run)
n, sample = 0, None
for product, price in loblaw.iter_catalog(banner, store, cats[:4]):
    n += 1
    sample = sample or (product, price)
print(f"products from 4 categories: {n}")
p, pr = sample
print(f"sample: {p['name'][:45]!r}  ${pr['price']}  cat={p['category']} / {p['subcategory']}")
assert n > 30 and p["product_id"] and pr["price"] and p["category"]
print("OK")
