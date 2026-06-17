#!/usr/bin/env python3
"""Assemble crash_data.json: car collisions above 60th St (Manhattan) before &
after congestion pricing (live 2025-01-05), with a focus on the Upper East Side
and on whether the effect concentrates near the 60th St line.

Sources (NYC Open Data / Socrata):
  h9gi-nx95  Motor Vehicle Collisions - Crashes (date, lat/lon, injuries, deaths,
             pedestrian breakdown, collision_id)
  bm4k-52h4  Motor Vehicle Collisions - Vehicle (state_registration; join by
             collision_id) -> NY vs out-of-state plates
  gthc-hcne  Borough Boundaries (Manhattan polygon for the 60th St cut)
  y76i-bdw7  Police Precincts (precinct 19 = Upper East Side)

Geography: the Congestion Relief Zone is Manhattan *below* 60th St; "above 60th"
is just outside the tolled zone -- the place to look for displaced traffic.
Regions: above-60th (all of Manhattan north of the line), Upper East Side
(precinct 19), and rest of NYC (citywide minus above-60th).

Output: monthly series (raw + indexed to 2024 baseline) for collisions /
pedestrian-involved collisions / pedestrians injured & killed; before/after by
distance band from 60th St; NY vs out-of-state plate share; geometry + sampled
points for the map.
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


# ---------------------------------------------------------------------------
def precinct_geom(where):
    """Fetch one or more precincts and merge them into a single MultiPolygon."""
    coords = []
    for r in soda("y76i-bdw7", {"$where": where, "$limit": 50}):
        g = r["the_geom"]
        coords.extend(g["coordinates"] if g["type"] == "MultiPolygon"
                      else [g["coordinates"]])
    return {"type": "MultiPolygon", "coordinates": coords}


print("Fetching boundaries ...")
MN = soda("gthc-hcne", {"$where": "borocode='1'", "$limit": 5})[0]["the_geom"]
UES = precinct_geom("precinct=19")              # Upper East Side
UWS = precinct_geom("precinct in (20,24)")      # Upper West Side (20 + 24)

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
corridor_geom = {"ues": UES, "uws": UWS}     # neighborhood corridors
cmonths = {"ues": {}, "uws": {}}    # monthly buckets per corridor
SCOPES = ("above", "ues", "uws")
dist = {s: {p: [new_bucket() for _ in BAND_LABELS] for p in PERIODS}
        for s in SCOPES}            # distance-band totals (base=2023)
# collision_ids for the plate join (2024 pre / 2025 post only)
ids = {s: {"pre": set(), "post": set()} for s in SCOPES}
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
        if period:
            for f in METRIC_FIELDS:
                dist[cname][period][band][f] += vals[f]
        if period in ("pre", "post") and cid:
            ids[cname][period].add(cid)

    crash_points.append({
        "lat": round(lat, 5), "lon": round(lon, 5), "d": r["crash_date"][:10],
        "ped": 1 if (ped_i + ped_k) > 0 else 0, "kill": int(vals["deaths"]),
        "ues": 1 if tag["ues"] else 0, "uws": 1 if tag["uws"] else 0,
        "street": (r.get("on_street_name") or "").strip(),
    })
ues, uws = cmonths["ues"], cmonths["uws"]
print(f"  above 60th: {sum(b['crashes'] for b in above.values()):.0f}; "
      f"UES: {sum(b['crashes'] for b in ues.values()):.0f}; "
      f"UWS: {sum(b['crashes'] for b in uws.values()):.0f}")

# --- Citywide monthly totals -> rest of NYC = citywide - above.
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

# --- Plate origin: join collision_ids to the Vehicle dataset.
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

# --- Assemble ordered monthly series (drop partial trailing months).
LAG_MONTHS = 2
_t = time.localtime()
_cut = _t.tm_year * 12 + (_t.tm_mon - 1) - LAG_MONTHS
LAST_MONTH = f"{_cut // 12:04d}-{_cut % 12 + 1:02d}"
months = [m for m in sorted(set(above) | set(rest) | set(ues)) if m <= LAST_MONTH]
print(f"Series through {LAST_MONTH}")


def series(buckets, field):
    return [round(buckets.get(m, {}).get(field, 0), 1) for m in months]


def mean_2024(buckets, field):
    vals = [buckets.get(f"2024-{i:02d}", {}).get(field, 0) for i in range(1, 13)]
    return sum(vals) / 12 if vals else 0


def idx(raw, base):
    return [round(100 * v / base, 1) if base else None for v in raw]


regions = {}
for name, buckets in (("above", above), ("ues", ues), ("uws", uws),
                      ("rest", rest)):
    regions[name] = {}
    for f in METRIC_FIELDS:
        raw = series(buckets, f)
        regions[name][f] = raw
        regions[name][f + "_idx"] = idx(raw, mean_2024(buckets, f))


def yr_total(buckets, field, yr):
    return sum(buckets.get(f"{yr}-{i:02d}", {}).get(field, 0)
               for i in range(1, 13))


summary = {}
for name, buckets in (("above", above), ("ues", ues), ("uws", uws),
                      ("rest", rest)):
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


# --- Difference-in-differences: is the near-60th change real, net of trend?
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


def sample(pts, cap):
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    return [pts[int(i * step)] for i in range(cap)]


out = {
    "generated": time.strftime("%Y-%m-%d"),
    "pricing_date": PRICING,
    "months": months,
    "regions": regions,
    "summary": summary,
    "distance": distance,
    "did": did,
    "plates": plates,
    "line_60th": [[L_A[1], L_A[0]], [L_B[1], L_B[0]]],
    "manhattan": MN,
    "ues_poly": UES,
    "uws_poly": UWS,
    "crash_points": sample(crash_points, 4000),
}
with open("crash_data.json", "w") as f:
    json.dump(out, f, separators=(",", ":"))

print("\n=== collisions 2024 -> 2025 ===")
for name in ("above", "ues", "uws", "rest"):
    s = summary[name]
    print(f"  {name:6s} crashes {s['crashes']['pct']}%  "
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
        d = did["corridors"][corridor][comp]["crashes"]
        r, pl = d["real"], d["placebo"]
        print(f"    vs {did['corridors'][corridor][comp]['label']}: "
              f"near {r['t0']}→{r['t1']} ({r['t_pct']:+}%) vs "
              f"{r['c0']}→{r['c1']} ({r['c_pct']:+}%); RR {r['rr']} "
              f"(CI {r['lo']}–{r['hi']}, p={r['p']}); placebo RR {pl['rr']} p={pl['p']}")
print(f"\nWrote crash_data.json ({len(months)} months, "
      f"{len(out['crash_points'])} pts)")
