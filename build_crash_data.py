#!/usr/bin/env python3
"""Assemble crash_data.json: car collisions above 60th St (Manhattan) before &
after congestion pricing (live 2025-01-05), with a focus on the Upper East Side,
Upper West Side, and Upper Manhattan (CB9–CB12: West Harlem, Central Harlem,
East Harlem, Washington Heights/Inwood).

Sources (NYC Open Data / Socrata):
  h9gi-nx95  Motor Vehicle Collisions - Crashes (date, lat/lon, injuries, deaths,
             pedestrian breakdown, collision_id)
  bm4k-52h4  Motor Vehicle Collisions - Vehicle (state_registration,
             vehicle_type_code_1; join by collision_id)
  gthc-hcne  Borough Boundaries (Manhattan polygon for the 60th St cut)
  y76i-bdw7  Police Precincts (precinct 19 = UES, 20+24 = UWS)
  jp9i-3b7y  Community Districts (CB9=109, CB10=110, CB11=111, CB12=112)

Geography: the Congestion Relief Zone is Manhattan *below* 60th St; "above 60th"
is just outside the tolled zone. Upper Manhattan (CB9–CB12) is the advocacy focus:
dense, transit-dependent, low-car-ownership neighborhoods on wide arterials.

Output: monthly series + 2024→2025 summaries for all regions; distance-band
analysis; difference-in-differences; plate origin; latitude-band gradient (9
bands from 60th to Inwood, for the boundary-vs-diversion test); vehicle type
breakdown for Upper Manhattan pedestrian crashes; geometry + sampled crash points
with vehicle type and UMN region flags.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://data.cityofnewyork.us/resource"
START = "2023-01-01"
PRICING = "2025-01-05"

# 60th St dividing line: Hudson end -> East River end (lon, lat).
L_A = (-73.9920, 40.7726)
L_B = (-73.9588, 40.7607)
LAT0 = 40.77  # reference latitude for the local metric projection

# Distance-from-60th bands (km north of the line). ~0.4 km ~= 5 blocks.
BAND_EDGES = [0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8]
BAND_LABELS = ["60–65 St", "65–70 St", "70–75 St", "75–80 St", "80–85 St",
               "85–90 St", "90–95 St", "95 St +"]

# US states + DC + common Canadian provinces => "known plate origin".
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
                "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
                "TN TX UT VT VA WA WV WI WY DC ON QC BC AB MB".split())

METRIC_FIELDS = ("crashes", "ped_crashes", "ped_injured", "ped_killed",
                 "injuries", "deaths")

# Difference-in-differences band groups (indices into BAND_LABELS).
NEAR_BANDS = (0, 1)        # 60–70 St, 0–0.8 km from the line (treatment)
FAR_BANDS = (4, 5, 6)      # 80–95 St, 1.6–2.8 km (within-UES control)
PERIODS = ("base", "pre", "post")  # 2023, 2024, 2025

# Upper Manhattan community districts (CB9–CB12)
UMN_CDS = {
    "harlem_west":    109,  # West Harlem / Hamilton Heights
    "harlem_central": 110,  # Central Harlem
    "harlem_east":    111,  # East Harlem
    "washington_hts": 112,  # Washington Heights + Inwood
}
UMN_NAMES = tuple(UMN_CDS.keys())

# Vehicle type categories for the striking-vehicle chart
VTYPE_LABELS = ["car", "suv", "truck", "moped", "ebike", "other"]


def soda(dataset, params, retries=5):
    url = f"{BASE}/{dataset}.json?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, "code", None)
            if attempt < retries - 1 and code in (None, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise


def paginate(dataset, where, select=None, page=50000):
    out, offset = [], 0
    while True:
        params = {"$where": where, "$limit": page, "$offset": offset,
                  "$order": ":id"}
        if select:
            params["$select"] = select
        rows = soda(dataset, params)
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def project(lon, lat):
    """Equirectangular meters relative to the 60th St line's west end."""
    x = (lon - L_A[0]) * 111320 * math.cos(math.radians(LAT0))
    y = (lat - L_A[1]) * 110540
    return x, y


# 60th St line in projected meters: direction and unit normal toward upper Manhattan.
_BX, _BY = project(*L_B)
_LEN = math.hypot(_BX, _BY)
_DIR = (_BX / _LEN, _BY / _LEN)
_NRM = (-_DIR[1], _DIR[0])  # left normal


def signed_dist_km(lon, lat):
    """Signed distance (km) from the 60th St line; positive = upper Manhattan."""
    x, y = project(lon, lat)
    d = (x * _NRM[0] + y * _NRM[1]) / 1000.0
    return d


# Orient the normal so upper Manhattan is positive.
if signed_dist_km(-73.97, 40.78) < 0:
    _NRM = (-_NRM[0], -_NRM[1])
assert signed_dist_km(-73.97, 40.78) > 0 > signed_dist_km(-73.985, 40.758), \
    "60th St orientation wrong"


def band_of(dist_km):
    for i, e in enumerate(BAND_EDGES):
        if dist_km < e:
            return i
    return len(BAND_EDGES)


def point_in_multipolygon(lon, lat, geom):
    inside = False
    polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
             else [geom["coordinates"]])
    for poly in polys:
        ring = poly[0]
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > lat) != (yj > lat) and \
               lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def month_key(iso):
    return iso[:7]


def did_stats(t0, t1, c0, c1):
    """Rate-ratio difference-in-differences with a 95% CI (log/Poisson delta
    method). RR = (t1/t0) / (c1/c0); RR>1 => treatment grew more than control."""
    if min(t0, t1, c0, c1) <= 0:
        return None
    rr = (t1 / t0) / (c1 / c0)
    se = math.sqrt(1 / t1 + 1 / t0 + 1 / c1 + 1 / c0)
    lrr = math.log(rr)
    z = lrr / se
    return {
        "rr": round(rr, 3),
        "lo": round(math.exp(lrr - 1.96 * se), 3),
        "hi": round(math.exp(lrr + 1.96 * se), 3),
        "p": round(math.erfc(abs(z) / math.sqrt(2)), 4),
        "t0": int(t0), "t1": int(t1), "c0": int(c0), "c1": int(c1),
        "t_pct": round(100 * (t1 - t0) / t0, 1),
        "c_pct": round(100 * (c1 - c0) / c0, 1),
        "t_idx": [100, round(100 * t1 / t0, 1)],
        "c_idx": [100, round(100 * c1 / c0, 1)],
    }


def new_bucket():
    return {f: 0.0 for f in METRIC_FIELDS}


def add(series, key, vals):
    b = series.setdefault(key, new_bucket())
    for f, v in vals.items():
        b[f] += v


def vehicle_type_bin(vtype_str):
    """Map a raw vehicle_type_code_1 string to a dashboard category."""
    v = (vtype_str or "").lower().strip()
    if any(x in v for x in ("e-bike", "ebike", "e bike", "electric bike",
                              "e-scooter", "escooter", "electric scooter")):
        return "ebike"
    if any(x in v for x in ("moped", "motorcycle", "motorbike", "motor bike")):
        return "moped"
    if any(x in v for x in ("sport utility", "suv", "station wagon")):
        return "suv"
    if any(x in v for x in ("pick-up", "pickup", "flatbed", "dump truck",
                              "tractor", "semi", "cement", "flat bed")):
        return "truck"
    if any(x in v for x in ("van", "bus", "truck", "garbage", "cargo")):
        return "truck"
    if any(x in v for x in ("sedan", "passenger vehicle", "taxi", "livery",
                              "limousine", "roadster", "coupe", "convertible")):
        return "car"
    if v in ("", "unknown", "other", "unspecified"):
        return "other"
    return "car"  # most unrecognized types are passenger vehicles


def vtype_counts(collision_ids):
    """Return {vtype: count} for a collection of collision IDs (one per crash)."""
    counts = {k: 0 for k in VTYPE_LABELS}
    cids = list(collision_ids)
    seen_cids = set()
    for i in range(0, len(cids), 300):
        batch = cids[i:i + 300]
        inlist = ",".join("'" + c + "'" for c in batch)
        rows = soda("bm4k-52h4", {
            "$select": "collision_id,vehicle_type_code_1",
            "$where": f"collision_id in ({inlist})",
            "$limit": len(batch) + 50,
        })
        for row_v in rows:
            cv = row_v.get("collision_id")
            if cv in seen_cids:
                continue
            seen_cids.add(cv)
            vt = vehicle_type_bin(row_v.get("vehicle_type_code_1"))
            counts[vt] = counts.get(vt, 0) + 1
    return counts


def build_latitude_bands():
    """9 latitude bands from 60th St to Inwood: crash counts + % change 2024→2025."""
    BAND_DEFS = [
        ("60–65 St",       40.7646, 40.7682),
        ("65–72 St",       40.7682, 40.7726),
        ("72–79 St",       40.7726, 40.7769),
        ("79–86 St",       40.7769, 40.7805),
        ("86–96 St",       40.7805, 40.7860),
        ("96–110 St",      40.7860, 40.7960),
        ("110–125 St",     40.7960, 40.8085),
        ("125–155 St",     40.8085, 40.8300),
        ("155 St–Inwood",  40.8300, 41.0000),
    ]
    out = []
    for label, lat_lo, lat_hi in BAND_DEFS:
        row = {"label": label}
        lat_where = (f"upper(borough)='MANHATTAN' AND latitude IS NOT NULL "
                     f"AND latitude >= {lat_lo} AND latitude < {lat_hi}")
        for yr, yr_s, yr_e in (("2024", "2024-01-01T00:00:00", "2025-01-01T00:00:00"),
                                ("2025", "2025-01-01T00:00:00", "2026-01-01T00:00:00")):
            date_clause = f"crash_date >= '{yr_s}' AND crash_date < '{yr_e}'"
            res = soda("h9gi-nx95", {
                "$select": ("count(*) AS n,"
                            "sum(number_of_pedestrians_injured) AS pi,"
                            "sum(number_of_pedestrians_killed) AS pk"),
                "$where": f"{lat_where} AND {date_clause}",
                "$limit": 1,
            })
            ped_res = soda("h9gi-nx95", {
                "$select": "count(*) AS n",
                "$where": (f"{lat_where} AND {date_clause} AND "
                           "(number_of_pedestrians_injured > 0 "
                           "OR number_of_pedestrians_killed > 0)"),
                "$limit": 1,
            })
            r0 = (res or [{}])[0]
            row[f"total_{yr}"]   = int(float(r0.get("n",  0)))
            row[f"ped_{yr}"]     = int(float((ped_res or [{}])[0].get("n", 0)))
            row[f"ped_inj_{yr}"] = int(float(r0.get("pi", 0)))
            row[f"ped_kil_{yr}"] = int(float(r0.get("pk", 0)))
        t24, t25 = row["total_2024"], row["total_2025"]
        p24, p25 = row["ped_2024"],   row["ped_2025"]
        row["pct_total"] = round(100 * (t25 - t24) / t24, 1) if t24 else None
        row["pct_ped"]   = round(100 * (p25 - p24) / p24, 1) if p24 else None
        out.append(row)
    return out


# ---------------------------------------------------------------------------
def precinct_geom(where):
    """Fetch one or more precincts and merge them into a single MultiPolygon."""
    coords = []
    for r in soda("y76i-bdw7", {"$where": where, "$limit": 50}):
        g = r["the_geom"]
        coords.extend(g["coordinates"] if g["type"] == "MultiPolygon"
                      else [g["coordinates"]])
    return {"type": "MultiPolygon", "coordinates": coords}


def cd_geom(boro_cd_val):
    """Fetch a Community District boundary from NYC Open Data (dataset jp9i-3b7y)."""
    rows = soda("jp9i-3b7y", {"$where": f"boro_cd={boro_cd_val}", "$limit": 5})
    if not rows:
        print(f"  WARNING: no geometry returned for boro_cd={boro_cd_val}")
        return None
    g = rows[0]["the_geom"]
    coords = (g["coordinates"] if g["type"] == "MultiPolygon"
              else [g["coordinates"]])
    return {"type": "MultiPolygon", "coordinates": coords}


print("Fetching boundaries ...")
MN = soda("gthc-hcne", {"$where": "borocode='1'", "$limit": 5})[0]["the_geom"]
UES = precinct_geom("precinct=19")              # Upper East Side
UWS = precinct_geom("precinct in (20,24)")      # Upper West Side (20 + 24)

print("Fetching Upper Manhattan community district boundaries ...")
umn_geoms = {}
for _name, _cd_id in UMN_CDS.items():
    umn_geoms[_name] = cd_geom(_cd_id)
    print(f"  {_name} (CD{_cd_id}): {'ok' if umn_geoms[_name] else 'MISSING'}")

BBOX = ("latitude > 40.758 AND latitude < 40.885 "
        "AND longitude > -74.02 AND longitude < -73.905")

print("Fetching upper-Manhattan crashes ...")
crash_where = (f"crash_date >= '{START}T00:00:00' "
               f"AND latitude IS NOT NULL AND {BBOX}")
crash_rows = paginate(
    "h9gi-nx95", crash_where,
    select=("collision_id,crash_date,latitude,longitude,"
            "number_of_persons_injured,number_of_persons_killed,"
            "number_of_pedestrians_injured,number_of_pedestrians_killed,"
            "on_street_name"))
print(f"  {len(crash_rows)} crashes in bbox")

above = {}                          # monthly buckets, all above-60th Manhattan
corridor_geom = {"ues": UES, "uws": UWS}
cmonths = {"ues": {}, "uws": {}}    # monthly buckets per named corridor

# Extend corridor map with Upper Manhattan community districts
for _n in UMN_NAMES:
    if umn_geoms.get(_n):
        corridor_geom[_n] = umn_geoms[_n]
        cmonths[_n] = {}

SCOPES = ("above", "ues", "uws")
dist = {s: {p: [new_bucket() for _ in BAND_LABELS] for p in PERIODS}
        for s in SCOPES}
# collision_ids for the plate join (2024 pre / 2025 post only)
ids = {s: {"pre": set(), "post": set()} for s in SCOPES}
# Pedestrian crash IDs per Upper Manhattan CD (used for vehicle type chart)
umn_ped_ids = {n: {"pre": set(), "post": set()} for n in UMN_NAMES}

crash_points = []

for r in crash_rows:
    lon, lat = fnum(r.get("longitude")), fnum(r.get("latitude"))
    if not (lon and lat):
        continue
    d = signed_dist_km(lon, lat)
    if d <= 0 or not point_in_multipolygon(lon, lat, MN):
        continue  # not above 60th & in Manhattan
    mk = month_key(r["crash_date"])
    ped_i = fnum(r.get("number_of_pedestrians_injured"))
    ped_k = fnum(r.get("number_of_pedestrians_killed"))
    vals = {
        "crashes": 1,
        "ped_crashes": 1 if (ped_i + ped_k) > 0 else 0,
        "ped_injured": ped_i, "ped_killed": ped_k,
        "injuries": fnum(r.get("number_of_persons_injured")),
        "deaths": fnum(r.get("number_of_persons_killed")),
    }
    band = band_of(d)
    year = r["crash_date"][:4]
    period = {"2023": "base", "2024": "pre", "2025": "post"}.get(year)
    cid = r.get("collision_id")

    add(above, mk, vals)
    if period:
        for f in METRIC_FIELDS:
            dist["above"][period][band][f] += vals[f]
    if period in ("pre", "post") and cid:
        ids["above"][period].add(cid)

    tag = {}
    for cname, geom in corridor_geom.items():
        inside = point_in_multipolygon(lon, lat, geom)
        tag[cname] = inside
        if not inside:
            continue
        add(cmonths[cname], mk, vals)
        if cname in SCOPES:
            if period:
                for f in METRIC_FIELDS:
                    dist[cname][period][band][f] += vals[f]
            if period in ("pre", "post") and cid:
                ids[cname][period].add(cid)
        elif cname in UMN_NAMES:
            # Track pedestrian crash IDs for vehicle-type chart (no dist needed)
            if period in ("pre", "post") and cid and (ped_i + ped_k) > 0:
                umn_ped_ids[cname][period].add(cid)

    crash_points.append({
        "lat": round(lat, 5), "lon": round(lon, 5), "d": r["crash_date"][:10],
        "ped": 1 if (ped_i + ped_k) > 0 else 0, "kill": int(vals["deaths"]),
        "ues": 1 if tag.get("ues") else 0,
        "uws": 1 if tag.get("uws") else 0,
        "hw":  1 if tag.get("harlem_west") else 0,
        "hc":  1 if tag.get("harlem_central") else 0,
        "he":  1 if tag.get("harlem_east") else 0,
        "wh":  1 if tag.get("washington_hts") else 0,
        "street": (r.get("on_street_name") or "").strip(),
        "_cid": cid,  # temporary; stripped after vehicle-type join below
    })

ues = cmonths["ues"]
uws = cmonths["uws"]
umn_month_data = {n: cmonths[n] for n in UMN_NAMES if n in cmonths}
print(f"  above 60th: {sum(b['crashes'] for b in above.values()):.0f}; "
      f"UES: {sum(b['crashes'] for b in ues.values()):.0f}; "
      f"UWS: {sum(b['crashes'] for b in uws.values()):.0f}")
for _n, _bkts in umn_month_data.items():
    print(f"  {_n}: {sum(b['crashes'] for b in _bkts.values()):.0f} crashes")

# --- Vehicle type for sampled crash points -----------------------------------
def sample(pts, cap):
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    return [pts[int(i * step)] for i in range(cap)]

sampled_pts = sample(crash_points, 4000)
print(f"Fetching vehicle types for {len(sampled_pts)} sampled crash points ...")
cid_to_vtype = {}
sample_cids = [p["_cid"] for p in sampled_pts if p.get("_cid")]
for i in range(0, len(sample_cids), 300):
    batch = sample_cids[i:i + 300]
    inlist = ",".join("'" + c + "'" for c in batch)
    rows_v = soda("bm4k-52h4", {
        "$select": "collision_id,vehicle_type_code_1",
        "$where": f"collision_id in ({inlist})",
        "$limit": len(batch) + 50,
    })
    for row_v in rows_v:
        cv = row_v.get("collision_id")
        if cv and cv not in cid_to_vtype:
            cid_to_vtype[cv] = vehicle_type_bin(row_v.get("vehicle_type_code_1"))

for p in sampled_pts:
    cv = p.pop("_cid", None)
    p["vtype"] = cid_to_vtype.get(cv, "other") if cv else "other"

# --- Aggregated vehicle type stats for Upper Manhattan ped crashes -----------
print("Computing Upper Manhattan vehicle type breakdown ...")
all_umn_ped_pre  = set().union(*[umn_ped_ids[n]["pre"]  for n in UMN_NAMES])
all_umn_ped_post = set().union(*[umn_ped_ids[n]["post"] for n in UMN_NAMES])
vehicle_ksi = {
    "pre":  vtype_counts(all_umn_ped_pre),
    "post": vtype_counts(all_umn_ped_post),
    "labels": VTYPE_LABELS,
    "note": ("Counts pedestrian-involved crashes in CB9–CB12 by first-listed "
             "vehicle type. Pre = full-year 2024; post = full-year 2025."),
}
print(f"  pre  (2024): {len(all_umn_ped_pre)} ped crashes → {vehicle_ksi['pre']}")
print(f"  post (2025): {len(all_umn_ped_post)} ped crashes → {vehicle_ksi['post']}")

# --- Latitude band gradient (boundary-vs-diversion test) --------------------
print("Building latitude band gradient (9 bands × 2 years × 2 queries = ~36 API calls) ...")
latitude_bands = build_latitude_bands()

# --- Citywide monthly totals -> rest of NYC = citywide - above ---------------
print("Aggregating citywide totals ...")
city = {}
agg = soda("h9gi-nx95", {
    "$select": ("date_extract_y(crash_date) AS y, date_extract_m(crash_date) "
                "AS m, count(1) AS crashes, "
                "sum(number_of_persons_injured) AS injuries, "
                "sum(number_of_persons_killed) AS deaths, "
                "sum(number_of_pedestrians_injured) AS ped_injured, "
                "sum(number_of_pedestrians_killed) AS ped_killed"),
    "$where": f"crash_date >= '{START}T00:00:00'",
    "$group": "y,m", "$limit": 5000})
for r in agg:
    mk = f"{int(r['y']):04d}-{int(r['m']):02d}"
    add(city, mk, {f: fnum(r.get(f)) for f in
                   ("crashes", "injuries", "deaths", "ped_injured", "ped_killed")})
ped_agg = soda("h9gi-nx95", {
    "$select": ("date_extract_y(crash_date) AS y, date_extract_m(crash_date) "
                "AS m, count(1) AS ped_crashes"),
    "$where": (f"crash_date >= '{START}T00:00:00' AND "
               "(number_of_pedestrians_injured > 0 OR "
               "number_of_pedestrians_killed > 0)"),
    "$group": "y,m", "$limit": 5000})
for r in ped_agg:
    mk = f"{int(r['y']):04d}-{int(r['m']):02d}"
    add(city, mk, {"ped_crashes": fnum(r.get("ped_crashes"))})

rest = {}
for mk, c in city.items():
    a = above.get(mk, new_bucket())
    rest[mk] = {f: max(0.0, c[f] - a[f]) for f in METRIC_FIELDS}

# --- Plate origin: join collision_ids to the Vehicle dataset -----------------
print("Fetching plate origins (Vehicle dataset) ...")


def plate_share(collision_ids):
    """Return (out_of_state_share, n_known) for a set of collision_ids."""
    ny = oos = 0
    cids = list(collision_ids)
    for i in range(0, len(cids), 300):
        batch = cids[i:i + 300]
        inlist = ",".join("'" + c + "'" for c in batch)
        rows = soda("bm4k-52h4", {
            "$select": "state_registration, count(1) AS n",
            "$where": f"collision_id in ({inlist})",
            "$group": "state_registration", "$limit": 100})
        for r in rows:
            st = (r.get("state_registration") or "").strip().upper()
            n = int(r["n"])
            if st == "NY":
                ny += n
            elif st in US_STATES:
                oos += n
    known = ny + oos
    return (round(100 * oos / known, 1) if known else None, known)


plates = {}
for region in SCOPES:
    plates[region] = {}
    for period in ("pre", "post"):
        share, n = plate_share(ids[region][period])
        plates[region][period] = {"oos_share": share, "n": n}
        print(f"  {region} {period}: out-of-state {share}% of {n} plates")

# citywide baseline straight from the Vehicle dataset (server-side group)
city_plate = soda("bm4k-52h4", {
    "$select": ("date_extract_y(crash_date) AS y, state_registration, "
                "count(1) AS n"),
    "$where": "crash_date between '2024-01-01' and '2025-12-31'",
    "$group": "y,state_registration", "$limit": 5000})
cp = {"2024": [0, 0], "2025": [0, 0]}  # [ny, oos]
for r in city_plate:
    if not r.get("y"):
        continue
    yr = str(int(r["y"]))
    if yr not in cp:
        continue
    st = (r.get("state_registration") or "").strip().upper()
    n = int(r["n"])
    if st == "NY":
        cp[yr][0] += n
    elif st in US_STATES:
        cp[yr][1] += n
plates["citywide"] = {}
for yr, p in (("2024", "pre"), ("2025", "post")):
    ny, oos = cp[yr]
    plates["citywide"][p] = {
        "oos_share": round(100 * oos / (ny + oos), 1) if (ny + oos) else None,
        "n": ny + oos}
print(f"  citywide pre/post: {plates['citywide']['pre']['oos_share']}% / "
      f"{plates['citywide']['post']['oos_share']}%")

# --- Assemble ordered monthly series (drop partial trailing months) ----------
LAG_MONTHS = 2
_t = time.localtime()
_cut = _t.tm_year * 12 + (_t.tm_mon - 1) - LAG_MONTHS
LAST_MONTH = f"{_cut // 12:04d}-{_cut % 12 + 1:02d}"
_all_month_sets = [set(above), set(rest), set(ues)]
for _bkts in umn_month_data.values():
    _all_month_sets.append(set(_bkts))
months = [m for m in sorted(set().union(*_all_month_sets)) if m <= LAST_MONTH]
print(f"Series through {LAST_MONTH}")


def series(buckets, field):
    return [round(buckets.get(m, {}).get(field, 0), 1) for m in months]


def mean_2024(buckets, field):
    vals = [buckets.get(f"2024-{i:02d}", {}).get(field, 0) for i in range(1, 13)]
    return sum(vals) / 12 if vals else 0


def idx(raw, base):
    return [round(100 * v / base, 1) if base else None for v in raw]


all_named_regions = [("above", above), ("ues", ues), ("uws", uws),
                     ("rest", rest)] + list(umn_month_data.items())

regions = {}
for name, buckets in all_named_regions:
    regions[name] = {}
    for f in METRIC_FIELDS:
        raw = series(buckets, f)
        regions[name][f] = raw
        regions[name][f + "_idx"] = idx(raw, mean_2024(buckets, f))


def yr_total(buckets, field, yr):
    return sum(buckets.get(f"{yr}-{i:02d}", {}).get(field, 0)
               for i in range(1, 13))


summary = {}
for name, buckets in all_named_regions:
    summary[name] = {}
    for f in METRIC_FIELDS:
        pre, post = yr_total(buckets, f, 2024), yr_total(buckets, f, 2025)
        summary[name][f] = {
            "y2024": round(pre, 1), "y2025": round(post, 1),
            "pct": round(100 * (post - pre) / pre, 1) if pre else None}

# distance bands: pct change per band, per scope.
distance = {"bands": BAND_LABELS, "above": {}, "ues": {}, "uws": {}}
for scope in SCOPES:
    for f in METRIC_FIELDS:
        pre = [b[f] for b in dist[scope]["pre"]]
        post = [b[f] for b in dist[scope]["post"]]
        distance[scope][f] = {
            "pre": [round(x) for x in pre],
            "post": [round(x) for x in post],
            "pct": [round(100 * (po - pr) / pr, 1) if pr else None
                    for pr, po in zip(pre, post)]}


# --- Difference-in-differences: is the near-60th change real, net of trend? --
def bandsum(scope, period, field, idxs):
    return sum(dist[scope][period][i][field] for i in idxs)


rest_tot = {y: {f: yr_total(rest, f, y) for f in METRIC_FIELDS}
            for y in (2023, 2024, 2025)}

CORR_NAME = {"ues": "Upper East Side", "uws": "Upper West Side"}
did = {"near_label": "60–70 St (0–0.8 km)", "far_label": "80–95 St (1.6–2.8 km)",
       "corridors": {}}
for corridor in ("ues", "uws"):
    cc = {"name": CORR_NAME[corridor],
          "within": {"label": "Upper " + corridor.upper() + " (80–95 St)"},
          "vs_city": {"label": "Rest of NYC"}}
    for f in ("crashes", "ped_crashes"):
        nb = {p: bandsum(corridor, p, f, NEAR_BANDS) for p in PERIODS}
        fb = {p: bandsum(corridor, p, f, FAR_BANDS) for p in PERIODS}
        rc = [rest_tot[y][f] for y in (2023, 2024, 2025)]
        near = [nb["base"], nb["pre"], nb["post"]]
        far = [fb["base"], fb["pre"], fb["post"]]
        cc["within"][f] = {
            "real": did_stats(nb["pre"], nb["post"], fb["pre"], fb["post"]),
            "placebo": did_stats(nb["base"], nb["pre"], fb["base"], fb["pre"]),
            "series": {"years": [2023, 2024, 2025], "t": near, "c": far}}
        cc["vs_city"][f] = {
            "real": did_stats(nb["pre"], nb["post"],
                              rest_tot[2024][f], rest_tot[2025][f]),
            "placebo": did_stats(nb["base"], nb["pre"],
                                 rest_tot[2023][f], rest_tot[2024][f]),
            "series": {"years": [2023, 2024, 2025], "t": near, "c": rc}}
    did["corridors"][corridor] = cc


out = {
    "generated": time.strftime("%Y-%m-%d"),
    "pricing_date": PRICING,
    "months": months,
    "regions": regions,
    "summary": summary,
    "distance": distance,
    "did": did,
    "plates": plates,
    "latitude_bands": latitude_bands,
    "vehicle_ksi": vehicle_ksi,
    "line_60th": [[L_A[1], L_A[0]], [L_B[1], L_B[0]]],
    "manhattan": MN,
    "ues_poly": UES,
    "uws_poly": UWS,
    "harlem_west_poly":    umn_geoms.get("harlem_west"),
    "harlem_central_poly": umn_geoms.get("harlem_central"),
    "harlem_east_poly":    umn_geoms.get("harlem_east"),
    "washington_hts_poly": umn_geoms.get("washington_hts"),
    "crash_points": sampled_pts,
}
with open("crash_data.json", "w") as f:
    json.dump(out, f, separators=(",", ":"))

print("\n=== collisions 2024 -> 2025 ===")
for name, _ in all_named_regions:
    s = summary[name]
    print(f"  {name:16s} crashes {s['crashes']['pct']}%  "
          f"ped-crashes {s['ped_crashes']['pct']}%  "
          f"ped-injured {s['ped_injured']['pct']}%  "
          f"ped-killed {s['ped_killed']['pct']}%")
print("\n=== UES collisions pct change by distance from 60th St ===")
for lab, pct in zip(BAND_LABELS, distance["ues"]["crashes"]["pct"]):
    print(f"  {lab:10s} {pct}%")
print("\n=== difference-in-differences (near-60th collisions) ===")
for corridor in ("ues", "uws"):
    print(f"  corridor: {did['corridors'][corridor]['name']}")
    for comp in ("within", "vs_city"):
        d_r = did["corridors"][corridor][comp]["crashes"]
        r_d, pl = d_r["real"], d_r["placebo"]
        print(f"    vs {did['corridors'][corridor][comp]['label']}: "
              f"near {r_d['t0']}→{r_d['t1']} ({r_d['t_pct']:+}%) vs "
              f"{r_d['c0']}→{r_d['c1']} ({r_d['c_pct']:+}%); RR {r_d['rr']} "
              f"(CI {r_d['lo']}–{r_d['hi']}, p={r_d['p']}); "
              f"placebo RR {pl['rr']} p={pl['p']}")
print("\n=== latitude band gradient (ped crashes 2024→2025) ===")
for lb in latitude_bands:
    sign = "+" if (lb["pct_ped"] or 0) >= 0 else ""
    print(f"  {lb['label']:18s} {lb['ped_2024']:4d}→{lb['ped_2025']:4d} "
          f"({sign}{lb['pct_ped']}%)")
print(f"\nWrote crash_data.json ({len(months)} months, "
      f"{len(sampled_pts)} pts)")
