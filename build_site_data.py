#!/usr/bin/env python3
"""Assemble site_data.json for the CB8 landmark-valuation map.

Sources (NYC Open Data / Socrata):
  buis-pvji  Individual Landmark Sites (geometry, designation date)
  yjxr-fw8i  Property Valuation & Assessment Data, FY2010/11-2018/19 (fullval)
  8y4t-faws  Property Valuation & Assessment Data, 2023-2027 (curmkttot)

Output: per-landmark yearly market-value series (2011-2027, gap 2020-2022)
plus a citywide average index for comparison.
"""
import csv
import json
import time
import urllib.parse
import urllib.request

BASE = "https://data.cityofnewyork.us/resource"


def soda(dataset, params, retries=5):
    url = f"{BASE}/{dataset}.json?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, "code", None)
            if attempt < retries - 1 and code in (None, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise


# Map a roll-year label to a single calendar year integer.
def yr(label):
    label = str(label)
    return int(label[:4]) + 1 if "/" in label else int(label)  # 2010/11 -> 2011


def centroid(geom):
    if not geom or geom.get("type") != "MultiPolygon":
        return None, None
    xs, ys, n = 0.0, 0.0, 0
    for poly in geom["coordinates"]:
        for ring in poly:
            for x, y in ring:
                xs += x; ys += y; n += 1
    return (round(ys / n, 6), round(xs / n, 6)) if n else (None, None)


def point_in_multipolygon(lon, lat, geom):
    """Ray-casting test against a MultiPolygon's outer rings."""
    inside = False
    for poly in geom["coordinates"]:
        ring = poly[0]  # outer ring
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]; xj, yj = ring[j]
            if (yi > lat) != (yj > lat) and \
               lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def get_cb8_districts():
    """Manhattan CB8 historic districts: centroid falls inside CB8 boundary."""
    cb8 = soda("5crt-au7u", {"$where": "boro_cd='108'", "$limit": 1})[0]["the_geom"]
    mn = soda("skyk-mpzq", {"$where": "borough='MN'",
                            "$select": "area_name,lp_number,desdate,the_geom",
                            "$limit": 300})
    out = []
    for r in mn:
        g = r.get("the_geom")
        if not g:
            continue
        lat, lon = centroid(g)
        if not point_in_multipolygon(lon, lat, cb8):
            continue
        # round coords to 5 decimals to shrink payload
        rings = [[[round(x, 5), round(y, 5)] for x, y in poly[0]]
                 for poly in g["coordinates"]]
        out.append({"name": r["area_name"], "lp": r.get("lp_number"),
                    "desyear": int((r.get("desdate") or "0000")[:4]) or None,
                    "rings": rings})
    return out


def batched(seq, size):
    seq = sorted(set(seq))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# PLUTO land-use codes -> readable labels
LANDUSE = {"1":"1-2 Family","2":"Walk-up Apts","3":"Elevator Apts",
           "4":"Mixed Res/Com","5":"Commercial/Office","6":"Industrial",
           "7":"Transportation","8":"Public/Institutional","9":"Open Space",
           "10":"Parking","11":"Vacant"}


def get_buildings(district_names):
    """Every PLUTO tax lot inside the given historic districts (by name)."""
    fields = ("bbl,histdist,address,latitude,longitude,yearbuilt,numfloors,"
              "bldgclass,landuse,unitsres,unitstotal,assesstot,ownername")
    names = ",".join("'" + n.replace("'", "''") + "'" for n in district_names)
    rows = soda("64uk-42ks", {
        "$select": fields, "$where": f"histdist in ({names})", "$limit": 50000})
    out = []
    for r in rows:
        if not (r.get("latitude") and r.get("longitude")):
            continue
        out.append({
            "hd": r["histdist"],
            "parid": str(r["bbl"]).split(".")[0].zfill(10),
            "lat": round(float(r["latitude"]), 6),
            "lon": round(float(r["longitude"]), 6),
            "addr": r.get("address"),
            "yb": int(r["yearbuilt"]) if r.get("yearbuilt") and r["yearbuilt"] != "0" else None,
            "fl": float(r["numfloors"]) if r.get("numfloors") else None,
            "bc": r.get("bldgclass"),
            "lu": LANDUSE.get((r.get("landuse") or "").lstrip("0"), r.get("landuse")),
            "ur": int(r["unitsres"]) if r.get("unitsres") else 0,
            "at": int(float(r["assesstot"])) if r.get("assesstot") else None,
        })
    return out


def fetch_market_series(parids):
    """Combined market-value series per parcel: fullval (2011-2019) +
    curmkttot (2023-2027). Returns {parid: {year:int -> value}}."""
    series = {}
    for chunk in batched([f"'{p}'" for p in parids], 80):
        rows = soda("yjxr-fw8i", {"$select": "bble,year,fullval",
                    "$where": f"bble in ({','.join(chunk)})", "$limit": 50000})
        for r in rows:
            p = r["bble"][:10]
            if r.get("fullval"):
                series.setdefault(p, {})[yr(r["year"])] = int(float(r["fullval"]))
    for chunk in batched([f"'{p}'" for p in parids], 80):
        rows = soda("8y4t-faws", {"$select": "parid,year,curmkttot",
                    "$where": f"parid in ({','.join(chunk)})", "$limit": 50000})
        for r in rows:
            if r.get("curmkttot"):
                series.setdefault(r["parid"], {})[yr(r["year"])] = int(float(r["curmkttot"]))
    return series


def main():
    # 1. CB8 landmarks (parid + meta) from the file we already built.
    meta = {}
    with open("landmarks_cb8_manhattan.csv") as f:
        for r in csv.DictReader(f):
            meta[r["parid"]] = {
                "parid": r["parid"], "name": r["lpc_name"],
                "address": r["address"], "type": r["landmarkty"],
                "desyear": int((r["desdate"] or "0000")[:4]) or None,
                "series": {},
            }
    parids = list(meta)
    print(f"{len(parids)} CB8 landmarks")

    # 2. Geometry -> centroid (join buis-pvji by bbl).
    bbls = [f"'{p}.0'" for p in parids]
    for chunk in batched(bbls, 50):
        rows = soda("buis-pvji", {
            "$select": "bbl,the_geom",
            "$where": f"bbl in ({','.join(chunk)})", "$limit": 5000})
        for r in rows:
            p = r["bbl"].split(".")[0].zfill(10)
            if p in meta and "lat" not in meta[p]:
                lat, lon = centroid(r.get("the_geom"))
                meta[p]["lat"], meta[p]["lon"] = lat, lon
    print("  geometry attached")

    # 3. Historical series (yjxr-fw8i, fullval). bble first 10 chars = BBL.
    for chunk in batched([f"'{p}'" for p in parids], 80):
        rows = soda("yjxr-fw8i", {
            "$select": "bble,year,fullval",
            "$where": f"bble in ({','.join(chunk)})", "$limit": 50000})
        for r in rows:
            p = r["bble"][:10]
            if p in meta and r.get("fullval"):
                meta[p]["series"][yr(r["year"])] = int(float(r["fullval"]))
    print("  historical (2011-2019) attached")

    # 4. Current series (8y4t-faws, curmkttot), all years.
    for chunk in batched([f"'{p}'" for p in parids], 80):
        rows = soda("8y4t-faws", {
            "$select": "parid,year,curmkttot",
            "$where": f"parid in ({','.join(chunk)})", "$limit": 50000})
        for r in rows:
            p = r["parid"]
            if p in meta and r.get("curmkttot"):
                meta[p]["series"][yr(r["year"])] = int(float(r["curmkttot"]))
    print("  current (2023-2027) attached")

    # 5. Citywide average index per year (server-side aggregates).
    city = {}
    for r in soda("yjxr-fw8i", {"$select": "year,avg(fullval)",
                                "$group": "year", "$order": "year"}):
        if r.get("avg_fullval"):
            city[yr(r["year"])] = round(float(r["avg_fullval"]))
    for r in soda("8y4t-faws", {"$select": "year,avg(curmkttot)",
                                "$group": "year", "$order": "year"}):
        if r.get("avg_curmkttot"):
            city[yr(r["year"])] = round(float(r["avg_curmkttot"]))
    print(f"  citywide index: {sorted(city)}")

    districts = get_cb8_districts()
    print(f"  {len(districts)} CB8 historic districts: "
          f"{[d['name'][:24] for d in districts]}")

    # Buildings inside those districts, via PLUTO histdist (names per PLUTO).
    pluto_names = ["Upper East Side Historic District",
                   "Upper East Side Historic District Extension",
                   "Carnegie Hill Historic District",
                   "Expanded Carnegie Hill Historic District",
                   "Metropolitan Museum Historic District",
                   "Treadwell Farm Historic District",
                   "Park Avenue Historic District",
                   "Henderson Place Historic District",
                   "Hardenbergh / Rhinelander Historic District"]
    buildings = get_buildings(pluto_names)
    print(f"  {len(buildings)} buildings pulled from PLUTO")

    # Market-value time series for every building parcel.
    print("  fetching market-value series for buildings...")
    bseries = fetch_market_series([b["parid"] for b in buildings])
    for b in buildings:
        s = bseries.get(b["parid"], {})
        b["mv"] = s[max(s)] if s else None  # latest market value

    # Per-district aggregate stats, attached to each district by fuzzy name match.
    def norm(s): return s.lower().replace(" ", "").replace("/", "")
    from statistics import median
    for d in districts:
        bs = [b for b in buildings if norm(b["hd"]) == norm(d["name"])]
        yrs = [b["yb"] for b in bs if b["yb"]]
        # district market-value trend over a FIXED basket: parcels with a
        # positive value at both endpoints (2011 and 2027).
        basket = [b for b in bs if bseries.get(b["parid"], {}).get(2011)
                                and bseries.get(b["parid"], {}).get(2027)]
        dseries = {}
        for y in sorted(city):
            vals = [bseries[b["parid"]].get(y) for b in basket]
            vals = [v for v in vals if v]
            if vals:
                dseries[y] = sum(vals)
        chg = ((dseries[2027] - dseries[2011]) / dseries[2011] * 100
               if 2011 in dseries and 2027 in dseries else None)
        d["stats"] = {
            "n": len(bs),
            "assessTot": sum(b["at"] or 0 for b in bs),
            "marketTot": sum(b["mv"] or 0 for b in bs),
            "resUnits": sum(b["ur"] for b in bs),
            "medYear": int(median(yrs)) if yrs else None,
            "yearRange": [min(yrs), max(yrs)] if yrs else None,
            "basketN": len(basket),
            "chg": round(chg, 1) if chg is not None else None,
            "series": dseries,
        }

    for b in buildings:
        b.pop("parid", None)  # not needed client-side; shrink payload

    landmarks = [m for m in meta.values()
                 if m["series"] and m.get("lat")]
    out = {"citywide": city, "districts": districts, "buildings": buildings,
           "landmarks": landmarks,
           "note": "Market value (fullval 2011-2019 / curmkttot 2023-2027). "
                   "2020-2022 not available. Citywide = mean of all parcels."}
    with open("site_data.json", "w") as f:
        json.dump(out, f)
    print(f"\nWrote site_data.json: {len(landmarks)} landmarks with series")


if __name__ == "__main__":
    main()
