#!/usr/bin/env python3
"""Cross-reference NYC individual landmarks with property valuation data.

Datasets (NYC Open Data / Socrata SODA API):
  - buis-pvji : Individual Landmark Sites (has BBL)
  - 8y4t-faws : Property Valuation & Assessment Data (parid = BBL, time series)

Strategy: pull the ~1,542 landmark BBLs, then query valuation filtered to
just those parcels in batches, keeping the most recent year per parcel.
"""
import csv
import json
import urllib.parse
import urllib.request

BASE = "https://data.cityofnewyork.us/resource"
LANDMARKS = "buis-pvji"
VALUATION = "8y4t-faws"


def soda(dataset, params, retries=4):
    import time
    url = f"{BASE}/{dataset}.json?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def get_landmarks():
    rows = soda(LANDMARKS, {
        "$select": "bbl,lpc_name,borough,address,landmarkty,desdate,lpc_lpnumb",
        "$limit": 5000,
    })
    for r in rows:
        bbl = r.get("bbl", "")
        r["parid"] = bbl.split(".")[0].zfill(10) if bbl else ""
    return [r for r in rows if r["parid"]]


def get_valuations(parids):
    """Fetch valuation rows for the given parids; keep latest year per parcel."""
    latest = {}
    fields = ("parid,year,owner,bldg_class,zoning,curmkttot,curmktland,"
              "curtaxclass,cpb_boro,cpb_dist,zip_code")
    BATCH = 100
    uniq = sorted(set(parids))
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        in_list = ",".join(f"'{p}'" for p in chunk)
        rows = soda(VALUATION, {
            "$select": fields,
            "$where": f"parid in ({in_list})",
            "$limit": 50000,
        })
        for r in rows:
            p, y = r.get("parid"), r.get("year", "0")
            if p not in latest or y > latest[p].get("year", "0"):
                latest[p] = r
        print(f"  valuation batch {i//BATCH + 1}: {len(chunk)} parcels queried, "
              f"{len(latest)} matched so far")
    return latest


def main():
    print("Pulling individual landmarks...")
    landmarks = get_landmarks()
    print(f"  {len(landmarks)} landmarks with a valid BBL")

    print("Pulling valuations for landmark parcels (batched)...")
    vals = get_valuations([l["parid"] for l in landmarks])

    cols = ["parid", "lpc_name", "borough", "address", "landmarkty", "desdate",
            "lpc_lpnumb", "owner", "bldg_class", "zoning", "taxclass",
            "cpb_boro", "cpb_dist", "zip_code", "curmktland", "curmkttot",
            "val_year"]

    rows = []
    for l in landmarks:
        v = vals.get(l["parid"], {})
        rows.append({
            "parid": l["parid"], "lpc_name": l.get("lpc_name"),
            "borough": l.get("borough"), "address": l.get("address"),
            "landmarkty": l.get("landmarkty"), "desdate": l.get("desdate"),
            "lpc_lpnumb": l.get("lpc_lpnumb"), "owner": v.get("owner"),
            "bldg_class": v.get("bldg_class"), "zoning": v.get("zoning"),
            "taxclass": v.get("curtaxclass"), "cpb_boro": v.get("cpb_boro"),
            "cpb_dist": v.get("cpb_dist"), "zip_code": v.get("zip_code"),
            "curmktland": v.get("curmktland"), "curmkttot": v.get("curmkttot"),
            "val_year": v.get("year"),
        })

    def write(path, data):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    write("landmarks_with_valuation.csv", rows)
    matched = sum(1 for r in rows if r["curmkttot"])
    print(f"\nWrote landmarks_with_valuation.csv: {len(rows)} landmarks, "
          f"{matched} matched ({100*matched//max(len(rows),1)}%)")

    # CB8 Manhattan = community planning board boro 1, district 8
    cb8 = [r for r in rows if r["cpb_boro"] == "1" and r["cpb_dist"] == "8"]
    write("landmarks_cb8_manhattan.csv", cb8)
    total = sum(int(r["curmkttot"]) for r in cb8 if r["curmkttot"])
    print(f"\n--- Manhattan CB8 (Upper East Side) ---")
    print(f"{len(cb8)} individual landmarks; total market value ${total:,}")
    for r in sorted(cb8, key=lambda r: int(r["curmkttot"] or 0), reverse=True)[:10]:
        print(f"  ${int(r['curmkttot'] or 0):>13,}  {r['lpc_name']}  ({r['address']})")


if __name__ == "__main__":
    main()
