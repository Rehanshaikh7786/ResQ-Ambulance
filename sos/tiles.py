"""
Caching map-tile proxy – makes maps work with NO API key, NO billing and survive blocked CDNs.

The browser only ever talks to this server (/tiles/<style>/<z>/<x>/<y>.png).  The server fetches the tile
from a free public source once (OpenStreetMap, Esri imagery as a satellite option), stores it under
data/tiles/ and serves it from disk afterwards – so a map you have viewed once keeps working OFFLINE.

Polite-use notes (OSM tile policy): identifying User-Agent, on-disk caching, max 2 concurrent upstream
connections, failure back-off, and no bulk scraping (the optional prefetch tool covers just the demo area).
"""
from __future__ import annotations
import math
import os
import threading
import time
import urllib.error
import urllib.request

from . import db

UA = "GoldenHourSOS/2.0 (educational emergency-routing prototype; self-hosted)"
TIMEOUT = float(os.environ.get("SOS_TILE_TIMEOUT", "6"))

# cache key -> ordered list of upstream URL templates (first that answers wins)
UPSTREAMS = {
    "osm": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"],
    "sat": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
}
if os.environ.get("SOS_TILE_URL"):          # use your own / a different provider, e.g. https://tiles.example.com/{z}/{x}/{y}.png
    UPSTREAMS["osm"] = [os.environ["SOS_TILE_URL"]]
# public style name -> cache key.  "dark" is OSM tiles darkened by the browser (CSS filter).
STYLES = {"streets": "osm", "dark": "osm", "sat": "sat"}

_down_until = {}                     # cache key -> unix time until which we skip upstream (back-off)
_gate = threading.Semaphore(2)       # at most 2 concurrent upstream downloads


class TileUnavailable(Exception):
    pass


def tile_dir():
    d = os.environ.get("SOS_TILES") or os.path.join(db.BASE_DIR, "data", "tiles")
    os.makedirs(d, exist_ok=True)
    return d


def _valid(style, z, x, y):
    if style not in STYLES or not (0 <= z <= 19):
        return False
    n = 1 << z
    return 0 <= x < n and 0 <= y < n


def _sniff(b: bytes):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def cached_path(style, z, x, y):
    return os.path.join(tile_dir(), STYLES[style], str(z), str(x), f"{y}.tile")


def get_tile(style, z, x, y, allow_fetch=True):
    """Return (bytes, mimetype) or raise TileUnavailable."""
    if not _valid(style, z, x, y):
        raise TileUnavailable("bad tile address")
    path = cached_path(style, z, x, y)
    if os.path.exists(path):
        data = open(path, "rb").read()
        mime = _sniff(data)
        if mime:
            return data, mime
    if not allow_fetch:
        raise TileUnavailable("not cached")
    key = STYLES[style]
    if _down_until.get(key, 0) > time.time():
        raise TileUnavailable("upstream down (back-off)")
    with _gate:
        if os.path.exists(path):                       # another thread may have just fetched it
            data = open(path, "rb").read()
            if _sniff(data):
                return data, _sniff(data)
        last = None
        for tpl in UPSTREAMS[key]:
            url = tpl.format(z=z, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*"})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read(2_000_000)
                mime = _sniff(data)
                if not mime:
                    raise ValueError("not an image")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + f".{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                return data, mime
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as ex:
                last = ex
        _down_until[key] = time.time() + 20             # don't hammer an unreachable upstream
        raise TileUnavailable(f"upstream unreachable: {last}")


# ------------------------------------------------------------------ prefetch (optional CLI)
def lonlat_to_tile(lat, lng, z):
    n = 1 << z
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tiles_for(bounds, zooms):
    (lat0, lat1), (lng0, lng1) = bounds["lat"], bounds["lng"]
    for z in zooms:
        x0, y1 = lonlat_to_tile(lat0, lng0, z)
        x1, y0 = lonlat_to_tile(lat1, lng1, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def prefetch(bounds, zooms=(11, 12, 13, 14, 15), styles=("streets",), delay=0.3, log=print):
    """Download the demo area once so the map works offline afterwards (~110 tiles per style)."""
    ok = skip = fail = 0
    for style in styles:
        for z, x, y in tiles_for(bounds, zooms):
            if os.path.exists(cached_path(style, z, x, y)):
                skip += 1
                continue
            try:
                get_tile(style, z, x, y)
                ok += 1
                time.sleep(delay)
            except TileUnavailable as ex:
                fail += 1
                if fail >= 5 and ok == 0:
                    log(f"Upstream unreachable – stopping ({ex}). Check your internet connection.")
                    return {"downloaded": ok, "cached": skip, "failed": fail}
    return {"downloaded": ok, "cached": skip, "failed": fail}
