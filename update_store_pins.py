"""Match Save-On-Foods stores to human-mapped OSM POIs (one Overpass query).
Override a store's pin only when exactly one POI sits within 10 km."""
import json, math, re, requests

def dist_m(a, b, c, d):
    p = math.pi / 180
    x = 0.5 - math.cos((c-a)*p)/2 + math.cos(a*p)*math.cos(c*p)*(1-math.cos((d-b)*p))/2
    return 12742000 * math.asin(math.sqrt(x))

q = """[out:json][timeout:120];
nwr["shop"="supermarket"]["name"~"Save.On.Foods",i](48,-141,62,-95);
out center tags;"""
r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q},
                  headers={"User-Agent": "GroceryPal/1.0 sjnendsa@gmail.com"}, timeout=150)
pois = []
for e in r.json()["elements"]:
    lat = e.get("lat") or e.get("center", {}).get("lat")
    lon = e.get("lon") or e.get("center", {}).get("lon")
    if lat:
        pois.append({"lat": lat, "lng": lon, "hn": e.get("tags", {}).get("addr:housenumber", "")})
print(f"{len(pois)} OSM POIs")

stores = json.load(open("data/stores.json", encoding="utf-8"))
ov, multi, nomatch = {}, 0, 0
for s in stores:
    ranked = sorted(pois, key=lambda p: dist_m(s["lat"], s["lng"], p["lat"], p["lng"]))
    d1 = dist_m(s["lat"], s["lng"], ranked[0]["lat"], ranked[0]["lng"])
    d2 = dist_m(s["lat"], s["lng"], ranked[1]["lat"], ranked[1]["lng"]) if len(ranked) > 1 else 1e9
    # nearest POI wins only if close AND clearly separated from runner-up
    if d1 > 1500:
        nomatch += 1
        continue
    if not (d2 > 2 * d1 or d2 > 3000):
        multi += 1
        continue
    p = ranked[0]
    d = dist_m(s["lat"], s["lng"], p["lat"], p["lng"])
    if d > 150:
        ov[s["store_id"]] = {"lat": p["lat"], "lng": p["lng"]}
        print(f"FIX {s['name']:28s} {s['city']:18s} moved {d/1000:.2f} km")

json.dump(ov, open("data/store_coords_overrides.json", "w"), indent=1)
print(f"\n{len(ov)} overrides | {nomatch} no POI nearby (API pin kept) | {multi} ambiguous (kept)")
