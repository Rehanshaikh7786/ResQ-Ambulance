"""Place search: built-in gazetteer of the demo network (works offline) + OpenStreetMap Nominatim (free, no key)."""
from __future__ import annotations
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db, geo
from .tiles import UA

NOMINATIM = os.environ.get("SOS_NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")


def gazetteer():
    """Areas, hospitals and ambulance stations of the demo network."""
    seen, out = set(), []
    for h in db.HOSPITALS:
        out.append({"name": h[1], "label": f"{h[1]} (area)", "lat": h[3], "lng": h[4], "kind": "area"})
        out.append({"name": h[0], "label": f"{h[0]} (hospital)", "lat": h[3], "lng": h[4], "kind": "hospital"})
    for a in db.AMBULANCES:
        out.append({"name": a[3].replace(" Station", ""), "label": f"{a[3]} (ambulance base)", "lat": a[4], "lng": a[5], "kind": "station"})
    res = []
    for p in out:
        k = (p["label"])
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


def areas():
    """Distinct area labels for drawing on the map (name -> lat/lng)."""
    best = {}
    for h in db.HOSPITALS:
        best.setdefault(h[1], (h[3], h[4]))
    for a in db.AMBULANCES:
        best.setdefault(a[3].replace(" Station", ""), (a[4], a[5]))
    return [{"name": n, "lat": v[0], "lng": v[1]} for n, v in best.items()]


def search_local(q, limit=6):
    q = q.strip().lower()
    if not q:
        return []
    hits = [p for p in gazetteer() if q in p["name"].lower() or q in p["label"].lower()]
    hits.sort(key=lambda p: (not p["name"].lower().startswith(q), p["kind"] != "area", len(p["name"])))
    return hits[:limit]


_cache, _lock, _last, _down = {}, threading.Lock(), [0.0], [0.0]


def search_online(q, limit=5):
    """Nominatim, throttled to 1 request/second (usage policy), cached, biased to the service area."""
    key = q.strip().lower()
    with _lock:
        if key in _cache:
            return _cache[key]
        if _down[0] > time.time():
            return []
        wait = 1.05 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    b = geo.BOUNDS
    params = {"q": q, "format": "jsonv2", "limit": limit, "countrycodes": "in",
              "viewbox": f"{b['lng'][0] - .3},{b['lat'][1] + .3},{b['lng'][1] + .3},{b['lat'][0] - .3}"}
    try:
        req = urllib.request.Request(NOMINATIM + "/search?" + urllib.parse.urlencode(params), headers={"User-Agent": UA, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("SOS_GEOCODE_TIMEOUT", "6"))) as r:
            rows = json.loads(r.read().decode())
        res = [{"name": r["display_name"].split(",")[0], "label": r["display_name"][:110], "lat": float(r["lat"]),
                "lng": float(r["lon"]), "kind": "osm"} for r in rows]
        with _lock:
            _cache[key] = res
        return res
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError):
        _down[0] = time.time() + 20
        return []


def search(q):
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"results": [], "online": False}
    local = search_local(q)
    online = search_online(q) if len(local) < 4 else []
    return {"results": local + online, "online": bool(online)}
