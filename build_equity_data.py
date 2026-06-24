#!/usr/bin/env python3
"""Assemble equity_data.json for the Upper Manhattan pedestrian safety dashboard.

Sources:
  Census Bureau ACS 5-year estimates (2023, no API key required for dev use)
    – Tract-level: median income, % residents of color, senior share, no-car %
  Census TIGERweb REST API — tract boundary GeoJSON for Manhattan
  NYC Open Data (Socrata):
    h9gi-nx95  Crashes (for reference, not re-pulled here)
    s3k6-pzi2  DOE School Points (Active Schools)
    g3vh-kbnw  NYCHA Developments
    drh3-e2fd  Subway Station Entrances
    dn2g-nvm2  Facilities Database (senior centers, libraries, hospitals, etc.)
    8vv7-7wx3  Vision Zero SIP (Street Improvement Projects) — interventions

Output: equity_data.json with keys:
  tracts      GeoJSON FeatureCollection — Manhattan census tracts with equity attrs
  anchors     [{type, name, lat, lon}] — schools, NYCHA, senior centers, subway
  interventions [{type, name, lat, lon, status}] — LPIs, SIPs, 20-mph zones

Notes:
  - Run this separately from build_crash_data.py (slower due to tract geometry)
  - Upper Manhattan bounding box: lat 40.796–40.885, lon -74.02 to -73.905
  - Requires Python 3.8+ and standard library only
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# Upper Manhattan bounding box (CB9–CB12: Harlem, Washington Heights, Inwood)
UMN_LAT_LO = 40.796
UMN_LAT_HI = 40.885
UMN_LON_LO = -74.02
UMN_LON_HI = -73.905

# All-Manhattan bbox (for tract geometry fetch)
MN_LAT_LO = 40.700
MN_LAT_HI = 40.885

NYDATA_BASE = "https://data.cityofnewyork.us/resource"
CENSUS_BASE = "https://api.census.gov/data/2023/acs/acs5"
TIGER_BASE  = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
               "TIGERweb/Tracts_Blocks/MapServer/0/query")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_json(url, params=None, retries=4, timeout=120):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, "code", None)
            if attempt < retries - 1 and code in (None, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            print(f"  WARNING: HTTP error {code} for {url[:80]}…")
            return None
    return None


def soda(dataset, params, retries=4):
    url = f"{NYDATA_BASE}/{dataset}.json"
    params = dict(params)
    for attempt in range(retries):
        full_url = url + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(full_url, timeout=120) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, "code", None)
            if attempt < retries - 1 and code in (None, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            print(f"  WARNING: {dataset} HTTP {code}")
            return []
    return []


def soda_paginate(dataset, where, select, page=5000):
    out, offset = [], 0
    while True:
        rows = soda(dataset, {"$where": where, "$select": select,
                               "$limit": page, "$offset": offset,
                               "$order": ":id"})
        out.extend(rows or [])
        if not rows or len(rows) < page:
            return out
        offset += page


# ---------------------------------------------------------------------------
# 1. ACS Census data — tract-level equity metrics
# ---------------------------------------------------------------------------

ACS_VARS = {
    "B19013_001E": "income",           # median household income
    "B03002_001E": "pop_total",        # total population (for race denom)
    "B03002_003E": "pop_white_nh",     # non-Hispanic white alone
    "B01001_001E": "pop_age_total",    # total population (for age denom)
    # Male 65+ age groups
    "B01001_020E": "m65_66",
    "B01001_021E": "m67_69",
    "B01001_022E": "m70_74",
    "B01001_023E": "m75_79",
    "B01001_024E": "m80_84",
    "B01001_025E": "m85p",
    # Female 65+ age groups
    "B01001_044E": "f65_66",
    "B01001_045E": "f67_69",
    "B01001_046E": "f70_74",
    "B01001_047E": "f75_79",
    "B01001_048E": "f80_84",
    "B01001_049E": "f85p",
    "B08201_001E": "hh_total",         # total households (for vehicle denom)
    "B08201_002E": "hh_no_car",        # zero-vehicle households
}

SENIOR_VARS = ["m65_66","m67_69","m70_74","m75_79","m80_84","m85p",
               "f65_66","f67_69","f70_74","f75_79","f80_84","f85p"]


def fetch_acs_tracts():
    """Fetch ACS equity metrics for all Manhattan (county 061) census tracts."""
    var_list = ",".join(ACS_VARS.keys())
    url = (f"{CENSUS_BASE}?get={var_list},NAME"
           f"&for=tract:*&in=state:36%20county:061")
    print(f"  Fetching ACS data from Census Bureau …")
    data = get_json(url, timeout=60)
    if not data or len(data) < 2:
        print("  WARNING: ACS fetch returned no data")
        return {}

    headers = data[0]
    tracts = {}
    for row in data[1:]:
        d = dict(zip(headers, row))
        # GEOID = state(2) + county(3) + tract(6)
        geoid = f"1400000US{d.get('state','36')}{d.get('county','061')}{d.get('tract','')}"
        tract_id = d.get("tract", "")

        def safe_float(key, default=None):
            try:
                v = float(d.get(key, -999) or -999)
                return None if v < 0 else v
            except (TypeError, ValueError):
                return default

        income      = safe_float("B19013_001E")
        pop_total   = safe_float("B03002_001E") or 0
        pop_white   = safe_float("B03002_003E") or 0
        age_total   = safe_float("B01001_001E") or 0
        hh_total    = safe_float("B08201_001E") or 0
        hh_no_car   = safe_float("B08201_002E") or 0

        seniors = sum(safe_float(f"B01001_{k}", 0) or 0 for k in
                      ["020E","021E","022E","023E","024E","025E",
                       "044E","045E","046E","047E","048E","049E"])

        pct_color   = round(100 * (1 - pop_white / pop_total), 1) if pop_total else None
        senior_pct  = round(100 * seniors / age_total, 1) if age_total else None
        no_car_pct  = round(100 * hh_no_car / hh_total, 1) if hh_total else None

        tracts[tract_id] = {
            "geoid":      geoid,
            "name":       d.get("NAME", ""),
            "income":     income,
            "pct_color":  pct_color,
            "senior_pct": senior_pct,
            "no_car_pct": no_car_pct,
        }
    print(f"  Got ACS data for {len(tracts)} Manhattan tracts")
    return tracts


# ---------------------------------------------------------------------------
# 2. Census TIGERweb — tract boundary GeoJSON
# ---------------------------------------------------------------------------

def fetch_tract_geometry():
    """Fetch Manhattan census tract polygons from Census TIGERweb REST API."""
    print("  Fetching tract geometries from TIGERweb …")
    params = {
        "where": "STATE='36' AND COUNTY='061'",
        "outFields": "GEOID,TRACTCE,NAME",
        "f": "geojson",
        "outSR": "4326",
        "returnGeometry": "true",
    }
    data = get_json(TIGER_BASE, params, timeout=180)
    if not data or "features" not in data:
        print("  WARNING: TIGERweb returned no geometry")
        return {}
    tract_geoms = {}
    for feat in data["features"]:
        props = feat.get("properties", {})
        tract_id = props.get("TRACTCE", "")
        if tract_id:
            tract_geoms[tract_id] = feat["geometry"]
    print(f"  Got geometry for {len(tract_geoms)} Manhattan tracts")
    return tract_geoms


def build_tracts_geojson(acs_data, tract_geoms):
    """Join ACS attributes to tract geometries → GeoJSON FeatureCollection."""
    features = []
    for tract_id, attrs in acs_data.items():
        geom = tract_geoms.get(tract_id)
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "tract": tract_id,
                "geoid": attrs["geoid"],
                "income":     attrs["income"],
                "pct_color":  attrs["pct_color"],
                "senior_pct": attrs["senior_pct"],
                "no_car_pct": attrs["no_car_pct"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# 3. Vulnerable anchors — NYC Open Data point datasets
# ---------------------------------------------------------------------------

UMN_BBOX_WHERE = (f"latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI} "
                  f"AND longitude > {UMN_LON_LO} AND longitude < {UMN_LON_HI}")


def fetch_schools():
    """DOE Active Schools — dataset s3k6-pzi2."""
    print("  Fetching schools …")
    rows = soda_paginate(
        "s3k6-pzi2",
        where=f"latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI}",
        select="school_name,latitude,longitude,grades_final_text",
    )
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (UMN_LON_LO < lon < UMN_LON_HI):
            continue
        out.append({
            "type": "school",
            "name": r.get("school_name", "School"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "detail": r.get("grades_final_text", ""),
        })
    print(f"  {len(out)} schools")
    return out


def fetch_nycha():
    """NYCHA Developments — dataset g3vh-kbnw (centroids from address fields)."""
    print("  Fetching NYCHA developments …")
    rows = soda(
        "g3vh-kbnw",
        {"$select": "development_name,latitude,longitude,address",
         "$where": f"latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI}",
         "$limit": 500},
    )
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (UMN_LON_LO < lon < UMN_LON_HI):
            continue
        out.append({
            "type": "nycha",
            "name": r.get("development_name", "NYCHA"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
        })
    print(f"  {len(out)} NYCHA developments")
    return out


def fetch_senior_centers():
    """NYC Facilities Database — senior centers in Upper Manhattan.
    Dataset: dn2g-nvm2 (NYC Facilities Database, filter factype)."""
    print("  Fetching senior centers …")
    rows = soda(
        "dn2g-nvm2",
        {"$select": "facname,latitude,longitude,factype",
         "$where": (f"(factype='Senior Center' OR factype='Naturally Occurring "
                    f"Retirement Community (NORC)') "
                    f"AND latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI}"),
         "$limit": 500},
    )
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (UMN_LON_LO < lon < UMN_LON_HI):
            continue
        out.append({
            "type": "senior",
            "name": r.get("facname", "Senior Center"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
        })
    print(f"  {len(out)} senior centers")
    return out


def fetch_subway_entrances():
    """MTA Subway Station Entrances — dataset drh3-e2fd."""
    print("  Fetching subway entrances …")
    rows = soda(
        "drh3-e2fd",
        {"$select": "station_name,entrance_latitude,entrance_longitude",
         "$where": (f"entrance_latitude > {UMN_LAT_LO} "
                    f"AND entrance_latitude < {UMN_LAT_HI} "
                    f"AND entrance_longitude > {UMN_LON_LO} "
                    f"AND entrance_longitude < {UMN_LON_HI}"),
         "$limit": 500},
    )
    out = []
    for r in rows:
        try:
            lat = float(r.get("entrance_latitude") or 0)
            lon = float(r.get("entrance_longitude") or 0)
        except (TypeError, ValueError):
            continue
        if not lat or not lon:
            continue
        out.append({
            "type": "subway",
            "name": r.get("station_name", "Subway"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
        })
    print(f"  {len(out)} subway entrances")
    return out


# ---------------------------------------------------------------------------
# 4. Design interventions — DOT open data
# ---------------------------------------------------------------------------

def fetch_sip():
    """DOT Vision Zero Street Improvement Projects — dataset 8vv7-7wx3.
    Returns completed and planned SIPs in Upper Manhattan."""
    print("  Fetching SIP (Street Improvement Projects) …")
    rows = soda(
        "8vv7-7wx3",
        {"$select": "project_name,project_status,latitude,longitude,primary_street",
         "$where": (f"latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI}"),
         "$limit": 500},
    )
    out = []
    for r in rows:
        try:
            lat = float(r.get("latitude") or 0)
            lon = float(r.get("longitude") or 0)
        except (TypeError, ValueError):
            continue
        if not lat or not lon or not (UMN_LON_LO < lon < UMN_LON_HI):
            continue
        status = (r.get("project_status") or "").lower()
        out.append({
            "type": "sip",
            "name": r.get("project_name") or r.get("primary_street") or "SIP",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "status": "completed" if "complet" in status else "planned",
        })
    print(f"  {len(out)} SIPs")
    return out


def fetch_speed_cameras():
    """NYC Speed Camera locations — dataset p98w-26ue (if available).
    Falls back to empty list on API error."""
    print("  Fetching speed camera locations …")
    rows = soda(
        "p98w-26ue",
        {"$select": "location,latitude,longitude",
         "$where": (f"latitude > {UMN_LAT_LO} AND latitude < {UMN_LAT_HI} "
                    f"AND longitude > {UMN_LON_LO} AND longitude < {UMN_LON_HI}"),
         "$limit": 500},
    )
    out = []
    for r in rows:
        try:
            lat = float(r.get("latitude") or 0)
            lon = float(r.get("longitude") or 0)
        except (TypeError, ValueError):
            continue
        if not lat or not lon:
            continue
        out.append({
            "type": "speed_camera",
            "name": r.get("location", "Speed Camera"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "status": "active",
        })
    print(f"  {len(out)} speed cameras")
    return out


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def main():
    print("=== Building equity_data.json ===\n")

    # --- ACS + tract geometry ---
    print("Step 1: Census ACS tract data")
    acs_data = fetch_acs_tracts()

    print("\nStep 2: Tract geometry (TIGERweb)")
    tract_geoms = fetch_tract_geometry()

    tracts_geojson = build_tracts_geojson(acs_data, tract_geoms)
    print(f"  Built FeatureCollection: {len(tracts_geojson['features'])} tracts")

    # Compute Upper Manhattan summary stats for the dashboard callout
    umn_tracts = []
    for feat in tracts_geojson["features"]:
        props = feat["properties"]
        # Filter to tracts whose centroid latitude is roughly in Upper Manhattan.
        # We use the bounding box of the geometry as a proxy since we don't have
        # centroids — any tract that overlaps the UMN lat range is included.
        geom = feat.get("geometry", {})
        polys = (geom.get("coordinates", []) if geom.get("type") == "MultiPolygon"
                 else [geom.get("coordinates", [])])
        lats = []
        for poly in polys:
            for ring in poly:
                for coord in ring:
                    if len(coord) >= 2:
                        lats.append(coord[1])
        if lats and (max(lats) > UMN_LAT_LO and min(lats) < UMN_LAT_HI):
            umn_tracts.append(props)

    no_car_vals = [t["no_car_pct"] for t in umn_tracts if t.get("no_car_pct") is not None]
    income_vals = [t["income"] for t in umn_tracts if t.get("income") is not None]
    pct_color_vals = [t["pct_color"] for t in umn_tracts if t.get("pct_color") is not None]
    umn_summary = {
        "n_tracts":         len(umn_tracts),
        "median_no_car":    round(sorted(no_car_vals)[len(no_car_vals)//2], 1) if no_car_vals else None,
        "median_income":    int(sorted(income_vals)[len(income_vals)//2]) if income_vals else None,
        "median_pct_color": round(sorted(pct_color_vals)[len(pct_color_vals)//2], 1) if pct_color_vals else None,
    }
    print(f"\n  Upper Manhattan summary (from ACS):")
    print(f"    Tracts in UMN bbox:     {umn_summary['n_tracts']}")
    print(f"    Median no-car %:        {umn_summary['median_no_car']}%")
    print(f"    Median household income: ${umn_summary['median_income']:,}" if umn_summary['median_income'] else "    Median income: N/A")
    print(f"    Median % residents of color: {umn_summary['median_pct_color']}%")

    # --- Anchors ---
    print("\nStep 3: Vulnerable anchors (NYC Open Data)")
    anchors = []
    anchors += fetch_schools()
    anchors += fetch_nycha()
    anchors += fetch_senior_centers()
    anchors += fetch_subway_entrances()
    print(f"  Total anchors: {len(anchors)}")

    # --- Interventions ---
    print("\nStep 4: Design interventions (DOT open data)")
    interventions = []
    interventions += fetch_sip()
    interventions += fetch_speed_cameras()
    # LPI and daylighting locations are not yet available as a clean open dataset.
    # Add them manually below or update when DOT publishes the data.
    # interventions += fetch_lpis()  # TODO: wire up when DOT dataset is confirmed
    print(f"  Total interventions: {len(interventions)}")
    if not interventions:
        print("  NOTE: SIP/speed camera data may require dataset ID verification.")
        print("  Dashboard will render the layer as empty until data is confirmed.")

    # --- Write output ---
    out = {
        "generated":   __import__("time").strftime("%Y-%m-%d"),
        "umn_bbox":    {"lat_lo": UMN_LAT_LO, "lat_hi": UMN_LAT_HI,
                        "lon_lo": UMN_LON_LO, "lon_hi": UMN_LON_HI},
        "umn_summary": umn_summary,
        "tracts":      tracts_geojson,
        "anchors":     anchors,
        "interventions": interventions,
        "anchor_types": {
            "school":       {"label": "School",          "color": "#f59e0b"},
            "nycha":        {"label": "NYCHA Development","color": "#a855f7"},
            "senior":       {"label": "Senior Center",   "color": "#06b6d4"},
            "subway":       {"label": "Subway Entrance", "color": "#22c55e"},
        },
        "intervention_types": {
            "sip":          {"label": "Street Improvement Project", "color": "#3b82f6"},
            "speed_camera": {"label": "Speed Camera",               "color": "#f97316"},
        },
    }
    with open("equity_data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))

    print(f"\nWrote equity_data.json")
    print(f"  {len(tracts_geojson['features'])} tract features")
    print(f"  {len(anchors)} anchor points")
    print(f"  {len(interventions)} intervention points")


if __name__ == "__main__":
    main()
