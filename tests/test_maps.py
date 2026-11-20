import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PNG = io.BytesIO()
Image.new("RGB", (256, 256), (40, 60, 90)).save(PNG, "PNG")
PNG = PNG.getvalue()


class Mock(BaseHTTPRequestHandler):
    hits = {"tile": 0, "route": 0, "search": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/t/"):
            Mock.hits["tile"] += 1
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers(); self.wfile.write(PNG)
        elif self.path.startswith("/route/v1/driving/"):
            Mock.hits["route"] += 1
            body = json.dumps({"routes": [{"distance": 4200, "duration": 420, "geometry": {"coordinates": [[73.80, 18.63], [73.805, 18.628], [73.81, 18.625], [73.815, 18.62]]}}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/search"):
            Mock.hits["search"] += 1
            body = json.dumps([{"display_name": "Mock Chowk, Pune, Maharashtra, India", "lat": "18.61", "lon": "73.80"}]).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    for k in Mock.hits:
        Mock.hits[k] = 0
    monkeypatch.setenv("SOS_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("SOS_UPLOADS", str(tmp_path / "up"))
    monkeypatch.setenv("SOS_TILES", str(tmp_path / "tiles"))
    monkeypatch.delenv("STAFF_PIN", raising=False)
    from sos import places, roadroute, tiles
    monkeypatch.setattr(tiles, "UPSTREAMS", {"osm": [base + "/t/{z}/{x}/{y}.png"], "sat": [base + "/t/{z}/{x}/{y}.png"]})
    monkeypatch.setattr(roadroute, "OSRM", base)
    monkeypatch.setattr(places, "NOMINATIM", base)
    tiles._down_until.clear(); roadroute._cache.clear(); roadroute._down_until = 0.0; places._cache.clear(); places._down[0] = 0.0
    from app import create_app
    app = create_app(start_engine=False, reset=True)
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.base, c.srv = base, srv
        yield c
    srv.shutdown()


def test_tile_proxy_fetches_once_then_serves_from_disk_offline(env):
    r = env.get("/tiles/streets/13/5811/3556.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and Mock.hits["tile"] == 1
    assert env.get("/tiles/dark/13/5811/3556.png").status_code == 200       # dark shares OSM cache
    assert Mock.hits["tile"] == 1
    env.srv.shutdown(); env.srv.server_close()                                # "internet" goes away
    r = env.get("/tiles/streets/13/5811/3556.png")
    assert r.status_code == 200 and r.data == PNG                             # still works offline


def test_tile_proxy_rejects_bad_requests(env):
    assert env.get("/tiles/evil/13/1/1.png").status_code == 404
    assert env.get("/tiles/streets/25/1/1.png").status_code == 404
    assert env.get("/tiles/streets/3/99/1.png").status_code == 404
    assert env.get("/tiles/streets/3/-1/1.png").status_code == 404


def test_tile_upstream_down_returns_404_and_backs_off(env, monkeypatch):
    from sos import tiles
    monkeypatch.setattr(tiles, "UPSTREAMS", {"osm": ["http://127.0.0.1:1/t/{z}/{x}/{y}.png"], "sat": []})
    t0 = time.time()
    assert env.get("/tiles/streets/12/2900/1800.png").status_code == 404
    assert env.get("/tiles/streets/12/2901/1800.png").status_code == 404      # back-off: instant
    assert time.time() - t0 < 5


def test_non_image_upstream_is_not_cached(env, monkeypatch):
    from sos import tiles
    monkeypatch.setattr(tiles, "UPSTREAMS", {"osm": [env.base + "/route/v1/driving/x"], "sat": []})
    assert env.get("/tiles/streets/12/2900/1800.png").status_code == 404


def test_prefetch_caches_demo_area(env):
    from sos import geo, tiles
    r = tiles.prefetch(geo.BOUNDS, zooms=(11, 12), delay=0)
    assert r["downloaded"] >= 2 and r["failed"] == 0
    again = tiles.prefetch(geo.BOUNDS, zooms=(11, 12), delay=0)
    assert again["downloaded"] == 0 and again["cached"] == r["downloaded"]


def test_route_proxy_osrm_then_straight_fallback(env, monkeypatch):
    q = "/api/route?from=18.630,73.800&to=18.620,73.815"
    r = env.get(q).get_json()
    assert r["source"] == "osrm" and len(r["points"]) == 4 and r["km"] == 4.2
    env.get(q); assert Mock.hits["route"] == 1                                # cached
    from sos import roadroute
    roadroute._cache.clear(); monkeypatch.setattr(roadroute, "OSRM", "http://127.0.0.1:1")
    r = env.get("/api/route?from=18.6,73.8&to=18.7,73.85").get_json()
    assert r["source"] == "straight" and len(r["points"]) == 2 and r["km"] > 0
    assert env.get("/api/route?from=abc&to=1,2").status_code == 400
    assert env.get("/api/route").status_code == 400


def test_geocode_gazetteer_works_offline(env, monkeypatch):
    from sos import places
    monkeypatch.setattr(places, "NOMINATIM", "http://127.0.0.1:1")
    r = env.get("/api/geocode?q=chinch").get_json()
    assert any("Chinchwad" in x["name"] for x in r["results"]) and not r["online"]
    assert env.get("/api/geocode?q=a").get_json()["results"] == []


def test_geocode_adds_online_results(env):
    r = env.get("/api/geocode?q=Mock Chowk").get_json()
    assert r["online"] and r["results"][0]["kind"] == "osm" and r["results"][0]["lat"] == 18.61


def test_config_is_keyless_by_default(env, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza-test")
    c = env.get("/api/config").get_json()
    assert c["map_provider"] == "osm" and c["google_maps_key"] == "" and len(c["areas"]) >= 8
    monkeypatch.setenv("MAP_PROVIDER", "google")
    c = env.get("/api/config").get_json()
    assert c["map_provider"] == "google" and c["google_maps_key"] == "AIza-test"
