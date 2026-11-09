#!/usr/bin/env python3
"""Download the dashboard's data into data/ so the page doesn't have to query the sources on every visit.

Run by .github/workflows/update-data.yml (monthly, on demand, and when this script changes).
Uses only the Python standard library. Each source is independent: if one fails, its previous
file is kept and the others still update.

Sources
  paavo.geojson  Statistics Finland, Paavo postal-code statistics for Helsinki (CC BY 4.0)
  prices.json    Statistics Finland, prices of old dwellings by postal code, table 13mu (CC BY 4.0)
  rents.json     Statistics Finland, rents by postal code, archived table 13eb (CC BY 4.0)
  osm.json       OpenStreetMap via Overpass: schools, daycare, parks, health care, stations (ODbL)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
UA = {"User-Agent": "helsinki-dashboard data refresh (github.com/esamuikkunen/helsinki-dashboard)"}
PX = "https://pxdata.stat.fi/PxWeb/api/v1/en/"
HEL_BBOX = "60.09,24.78,60.30,25.27"


def get(url, data=None, headers=None, timeout=180):
    h = dict(UA, **(headers or {}))
    req = urllib.request.Request(url, data=data, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - retry any network error
            if attempt == 2:
                raise
            print(f"  retry after error: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))


def get_json(url, body=None, **kw):
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(get(url, data=data, **kw))


def save(name, obj):
    path = os.path.join(DATA, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"  wrote {name} ({os.path.getsize(path) // 1024} kB)")


def round_coords(c):
    if isinstance(c, (int, float)):
        return round(c, 5)
    return [round_coords(x) for x in c]


# ---------- json-stat2 helper ----------
def jstat_getter(js):
    ids, size = js["id"], js["size"]
    index = {d: js["dimension"][d]["category"]["index"] for d in ids}

    def at(**coords):
        k = 0
        for d, n in zip(ids, size):
            k = k * n + index[d][coords[d]]
        return js["value"][k]

    return at


def var(meta, test):
    return next(v for v in meta["variables"] if test(v))


# ---------- Paavo ----------
def fetch_paavo():
    for layer in ("pno_tilasto_2026", "pno_tilasto_2025", "pno_tilasto"):
        q = urllib.parse.urlencode({
            "service": "WFS", "version": "1.0.0", "request": "GetFeature",
            "typeName": f"postialue:{layer}", "outputFormat": "application/json",
            "srsName": "EPSG:4326", "CQL_FILTER": "kunta='091'",
        })
        try:
            gj = get_json("https://geo.stat.fi/geoserver/postialue/wfs?" + q)
        except Exception as e:  # noqa: BLE001
            print(f"  {layer}: {e}", file=sys.stderr)
            continue
        feats = gj.get("features") or []
        if not feats:
            continue
        for f in feats:
            f["geometry"]["coordinates"] = round_coords(f["geometry"]["coordinates"])
            f.pop("id", None)
            f.pop("geometry_name", None)
        save("paavo.geojson", {"type": "FeatureCollection", "layer": layer, "features": feats})
        return layer
    raise RuntimeError("no Paavo layer returned data")


# ---------- dwelling prices (13mu) ----------
def fetch_prices():
    tables = get_json(PX + "StatFin/ashi")
    tbl = next(t for t in tables if t.get("type") == "t" and re.search("postal code", t["text"], re.I)
               and re.search("yearly", t["text"], re.I) and re.search("square", t["text"], re.I))
    url = PX + "StatFin/ashi/" + tbl["id"]
    m = get_json(url)
    vy = next((v for v in m["variables"] if v.get("time")), None) or var(m, lambda v: re.search("year", v["text"], re.I))
    vp = var(m, lambda v: re.search("postal", v["text"], re.I))
    vb = var(m, lambda v: re.search("building", v["text"], re.I))
    vc = var(m, lambda v: re.search("contents|information", v["code"] + v["text"], re.I))
    years = vy["values"][-2:]
    pcs = [v for v, t in zip(vp["values"], vp["valueTexts"]) if re.match(r"00\d{3}", v) or re.match(r"00\d{3}", t)]
    price = vc["values"][next(i for i, t in enumerate(vc["valueTexts"]) if re.search("price", t, re.I))]
    sales = vc["values"][next(i for i, t in enumerate(vc["valueTexts"]) if re.search("number|sales", t, re.I))]
    q = {"query": [
        {"code": vy["code"], "selection": {"filter": "item", "values": years}},
        {"code": vp["code"], "selection": {"filter": "item", "values": pcs}},
        {"code": vb["code"], "selection": {"filter": "item", "values": vb["values"]}},
        {"code": vc["code"], "selection": {"filter": "item", "values": [price, sales]}},
    ], "response": {"format": "json-stat2"}}
    at = jstat_getter(get_json(url, q))
    out = {}
    for pc in pcs:
        for y in reversed(years):  # latest year, else the year before
            s = w = 0
            types = {}
            for b, bt in zip(vb["values"], vb["valueTexts"]):
                pr = at(**{vy["code"]: y, vp["code"]: pc, vb["code"]: b, vc["code"]: price})
                sl = at(**{vy["code"]: y, vp["code"]: pc, vb["code"]: b, vc["code"]: sales})
                if pr and sl:
                    s += pr * sl
                    w += sl
                    types[bt] = pr
            if w:
                out[pc[:5]] = {"price": round(s / w, 1), "types": types, "sales": w, "year": y}
                break
    detached = None
    try:
        detached = fetch_detached_average()
    except Exception as e:  # noqa: BLE001 - optional reference figure
        print(f"  detached-house average failed: {e}", file=sys.stderr)
    save("prices.json", {"table": tbl["id"], "year": years[-1], "areas": out, "detached": detached})
    return f"{len(out)} areas, {years[-1]}" + (f", detached avg {detached['price']}" if detached else "")


def fetch_detached_average():
    """Average price per m² of old one-dwelling (detached) houses on their own plot in Greater Helsinki,
    last four quarters weighted by number of sales. Statistics Finland publishes no finer area for these."""
    tables = get_json(PX + "StatFin/kihi")
    tbl = next(t for t in tables if t.get("type") == "t" and re.search("one-dwelling houses", t["text"], re.I)
               and re.search("square", t["text"], re.I) and re.search("quarterly", t["text"], re.I))
    url = PX + "StatFin/kihi/" + tbl["id"]
    m = get_json(url)
    vq = next((v for v in m["variables"] if v.get("time")), None) or var(m, lambda v: re.search("quarter", v["text"], re.I))
    vr = var(m, lambda v: re.search("region", v["text"], re.I))
    vc = var(m, lambda v: re.search("contents|information", v["code"] + v["text"], re.I))
    region = vr["values"][next(i for i, t in enumerate(vr["valueTexts"]) if t.strip().lower() == "greater helsinki")]
    price = vc["values"][next(i for i, t in enumerate(vc["valueTexts"]) if re.match("average price per square", t, re.I))]
    count = vc["values"][next(i for i, t in enumerate(vc["valueTexts"]) if re.match("number of transactions", t, re.I))]
    quarters = vq["values"][-4:]
    q = {"query": [
        {"code": vq["code"], "selection": {"filter": "item", "values": quarters}},
        {"code": vr["code"], "selection": {"filter": "item", "values": [region]}},
        {"code": vc["code"], "selection": {"filter": "item", "values": [price, count]}},
    ], "response": {"format": "json-stat2"}}
    at = jstat_getter(get_json(url, q))
    s = w = 0
    for qv in quarters:
        pr = at(**{vq["code"]: qv, vr["code"]: region, vc["code"]: price})
        n = at(**{vq["code"]: qv, vr["code"]: region, vc["code"]: count})
        if pr and n:
            s += pr * n
            w += n
    if not w:
        raise RuntimeError("no detached-house data")
    return {"price": round(s / w), "sales": w, "period": f"{quarters[0]}–{quarters[-1]}", "region": "Greater Helsinki"}


# ---------- rents (archived 13eb) ----------
def fetch_rents():
    base = PX + "StatFin_Passiivi/asvu/"
    tables = get_json(base)
    tbl = sorted((t for t in tables if t.get("type") == "t" and re.search("postal code", t["text"], re.I)
                  and re.search("rent", t["text"], re.I)), key=lambda t: t["id"], reverse=True)[0]
    url = base + tbl["id"]
    m = get_json(url)
    vq = next((v for v in m["variables"] if v.get("time")), None) or var(m, lambda v: re.search("quarter", v["text"], re.I))
    vp = var(m, lambda v: re.search("postal|postinumero", v["code"] + v["text"], re.I))
    vr = var(m, lambda v: re.search("room|huoneluku", v["code"] + v["text"], re.I))
    vt = var(m, lambda v: re.search("tiedot|information", v["code"] + v["text"], re.I))
    quarters = vq["values"][-4:]
    pcs = [v for v in vp["values"] if re.match(r"00\d{3}", v)]
    cnt = vt["values"][next(i for i, t in enumerate(vt["valueTexts"]) if re.search("number|lukum", t, re.I))]
    rent = vt["values"][next(i for i, t in enumerate(vt["valueTexts"]) if re.search("rent|vuokra", t, re.I))]
    q = {"query": [
        {"code": vq["code"], "selection": {"filter": "item", "values": quarters}},
        {"code": vp["code"], "selection": {"filter": "item", "values": pcs}},
        {"code": vr["code"], "selection": {"filter": "item", "values": vr["values"]}},
        {"code": vt["code"], "selection": {"filter": "item", "values": [cnt, rent]}},
    ], "response": {"format": "json-stat2"}}
    at = jstat_getter(get_json(url, q))
    out = {}
    for pc in pcs:
        s = w = 0
        rooms = {}
        for rv, rt in zip(vr["values"], vr["valueTexts"]):
            rs = rw = 0
            for qv in quarters:
                c = at(**{vq["code"]: qv, vp["code"]: pc, vr["code"]: rv, vt["code"]: cnt})
                v = at(**{vq["code"]: qv, vp["code"]: pc, vr["code"]: rv, vt["code"]: rent})
                if c and v:
                    rs += c * v
                    rw += c
            if rw:
                rooms[rt] = round(rs / rw, 2)
                s += rs
                w += rw
        if w:
            out[pc[:5]] = {"rentM2": round(s / w, 2), "rooms": rooms, "n": w}
    period = f"{quarters[0]}–{quarters[-1]}"
    save("rents.json", {"table": tbl["id"], "period": period, "areas": out})
    return f"{len(out)} areas, {period}"


# ---------- OpenStreetMap ----------
KEEP_TAGS = ("name", "name:en", "name:sv", "amenity", "leisure", "healthcare", "social_facility",
             "railway", "station", "isced:level")


def fetch_osm():
    b = HEL_BBOX
    q = (f'[out:json][timeout:180];(nwr["amenity"="school"]({b});nwr["amenity"="kindergarten"]({b});'
         f'nwr["leisure"="playground"]({b});way["leisure"="park"]({b});'
         f'nwr["amenity"~"^(hospital|clinic)$"]({b});nwr["healthcare"="centre"]({b});'
         f'nwr["amenity"="nursing_home"]({b});nwr["social_facility"="nursing_home"]({b});'
         f'node["railway"="station"]({b});node["station"="subway"]({b});node["railway"="tram_stop"]({b}););'
         'out center tags;')
    body = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for mirror in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            js = json.loads(get(mirror, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=300))
            break
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  {mirror}: {e}", file=sys.stderr)
    else:
        raise RuntimeError(f"Overpass failed: {last}")
    els = []
    for e in js.get("elements", []):
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None:
            continue
        tags = {k: v for k, v in (e.get("tags") or {}).items() if k in KEEP_TAGS}
        els.append({"lat": round(lat, 6), "lon": round(lon, 6), "tags": tags})
    if len(els) < 500:  # a partial answer from a busy server; keep the previous file
        raise RuntimeError(f"only {len(els)} elements returned")
    save("osm.json", {"elements": els})
    return f"{len(els)} places"


def main():
    os.makedirs(DATA, exist_ok=True)
    meta_path = os.path.join(DATA, "meta.json")
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    sources = meta.get("sources", {})
    ok = 0
    for key, fn in (("paavo", fetch_paavo), ("prices", fetch_prices), ("rents", fetch_rents), ("osm", fetch_osm)):
        print(f"{key}:")
        try:
            info = fn()
            sources[key] = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "info": info}
            ok += 1
        except Exception as e:  # noqa: BLE001 - keep the previous file for this source
            print(f"  FAILED: {e}", file=sys.stderr)
    if ok:
        meta = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "sources": sources}
        save("meta.json", meta)
    print(f"{ok}/4 sources updated")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
