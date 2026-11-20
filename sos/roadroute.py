"""Road geometry for ambulance routes (free OSRM public server, proxied + cached; straight-line fallback)."""
from __future__ import annotations
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import geo
from .tiles import UA

OSRM = os.environ.get("SOS_OSRM_URL", "https://router.project-osrm.org").rstrip("/")
_cache = {}
_down_until = 0.0
_lock = threading.Lock()


def _thin(pts, limit=220):
    if len(pts) <= limit:
        return pts
    step = len(pts) / limit
    out = [pts[int(i * step)] for i in range(limit)]
    out.append(pts[-1])
    return out


def route(a, b):
    """a, b = (lat, lng).  Returns {points:[[lat,lng]...], km, minutes, source}."""
    global _down_until
    key = tuple(round(v, 4) for v in (*a, *b))
    with _lock:
        if key in _cache:
            return _cache[key]
    straight = {"points": [list(a), list(b)], "km": round(geo.road_km(*a, *b), 2),
                "minutes": round(geo.eta_minutes(*a, *b, time.time())[0], 1), "source": "straight"}
    if _down_until > time.time():
        return straight
    url = f"{OSRM}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("SOS_ROUTE_TIMEOUT", "6"))) as r:
            j = json.loads(r.read().decode())
        rt = j["routes"][0]
        pts = _thin([[c[1], c[0]] for c in rt["geometry"]["coordinates"]])
        res = {"points": pts, "km": round(rt["distance"] / 1000, 2), "minutes": round(rt["duration"] / 60, 1), "source": "osrm"}
        with _lock:
            if len(_cache) > 2000:
                _cache.clear()
            _cache[key] = res
        return res
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TimeoutError):
        _down_until = time.time() + 20
        return straight
