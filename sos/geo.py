"""Geography + travel-time model (no external services required)."""
from __future__ import annotations
import math
import time

ROAD_FACTOR = 1.38         # crow-flies -> road distance
AMBULANCE_KMH = 52.0       # free-flow emergency speed with lights/sirens

# Demo service area (Pimpri-Chinchwad / Pune)
CENTER = (18.612, 73.808)
BOUNDS = {"lat": (18.560, 18.670), "lng": (73.760, 73.850)}


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def road_km(lat1, lng1, lat2, lng2):
    return haversine_km(lat1, lng1, lat2, lng2) * ROAD_FACTOR


def traffic_factor(sim_ts: float) -> float:
    """Speed multiplier by local hour (rush hours are slower)."""
    h = time.localtime(sim_ts).tm_hour + time.localtime(sim_ts).tm_min / 60
    if 8 <= h < 11 or 17 <= h < 21:
        return 0.62
    if 11 <= h < 17:
        return 0.80
    if 21 <= h or h < 5:
        return 1.0
    return 0.90


def eta_minutes(lat1, lng1, lat2, lng2, sim_ts: float):
    """Return (minutes, road_km) for an ambulance trip."""
    km = road_km(lat1, lng1, lat2, lng2)
    speed = AMBULANCE_KMH * traffic_factor(sim_ts)
    return km / speed * 60 + 0.8, km


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t
