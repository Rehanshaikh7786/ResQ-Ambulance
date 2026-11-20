"""SQLite persistence layer."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = threading.RLock()


def db_path():
    return os.environ.get("SOS_DB") or os.path.join(BASE_DIR, "data", "sos.sqlite")


def upload_dir():
    d = os.environ.get("SOS_UPLOADS") or os.path.join(BASE_DIR, "data", "uploads")
    os.makedirs(d, exist_ok=True)
    return d


@contextmanager
def conn():
    with LOCK:
        path = db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS hospitals (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT, phone TEXT,
  lat REAL NOT NULL, lng REAL NOT NULL, level INTEGER NOT NULL DEFAULT 2,
  caps TEXT NOT NULL, oncall TEXT NOT NULL,
  icu_total INTEGER, icu_free INTEGER, er_total INTEGER, er_free INTEGER,
  vent_total INTEGER, vent_free INTEGER, or_free INTEGER, or_total INTEGER,
  accepting INTEGER NOT NULL DEFAULT 1, updated_ts REAL
);
CREATE TABLE IF NOT EXISTS ambulances (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, crew TEXT, phone TEXT, base_name TEXT,
  base_lat REAL, base_lng REAL, lat REAL, lng REAL,
  status TEXT NOT NULL DEFAULT 'available', case_id TEXT,
  leg TEXT, leg_from_lat REAL, leg_from_lng REAL, leg_to_lat REAL, leg_to_lng REAL,
  leg_start REAL, leg_dur REAL, phase_until REAL, trips INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, seq INTEGER, created_ts REAL,
  reporter_name TEXT, reporter_phone TEXT,
  patient TEXT, triage TEXT, vision TEXT, report TEXT, photo TEXT,
  lat REAL, lng REAL, place TEXT,
  priority TEXT, risk INTEGER, etype TEXT, status TEXT,
  hospital_id INTEGER, ambulance_id TEXT, bed_type TEXT, vent_reserved INTEGER DEFAULT 0,
  t_alerted REAL, t_approved REAL, t_dispatched REAL, t_on_scene REAL, t_departed REAL, t_arrived REAL, t_closed REAL,
  eta_scene REAL, eta_hospital REAL, km_hospital REAL, golden_target REAL,
  tier INTEGER DEFAULT 0, relaxed INTEGER DEFAULT 0, override INTEGER DEFAULT 0,
  reroutes INTEGER DEFAULT 0, route_json TEXT, tried TEXT DEFAULT '[]', seeded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, hospital_id INTEGER,
  status TEXT, kind TEXT DEFAULT 'approval',
  created_ts REAL, created_wall REAL, decided_ts REAL, decided_wall REAL, reason TEXT, decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, case_id TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id);
"""

CAPS = ["trauma", "cardiac", "stroke", "burn", "pediatric", "obstetric", "ct", "cath_lab", "nicu"]

# name, area, phone, lat, lng, level, caps, on-call, icu(total,free), er, vent, or(total,free)
HOSPITALS = [
    ("Apex Trauma Institute", "Chinchwad", "+91 20 4000 1001", 18.630, 73.800, 1,
     ["trauma", "cardiac", "stroke", "pediatric", "ct", "cath_lab"], ["trauma", "cardiac", "stroke", "pediatric"],
     (20, 7), (30, 11), (12, 5), (6, 3)),
    ("MetroCare Super Speciality", "Pimpri", "+91 20 4000 1002", 18.620, 73.815, 1,
     ["trauma", "cardiac", "pediatric", "obstetric", "ct", "cath_lab", "nicu"], ["trauma", "cardiac", "pediatric", "obstetric"],
     (24, 0), (36, 4), (14, 0), (7, 2)),
    ("Sanjeevani Emergency Centre", "Akurdi", "+91 20 4000 1003", 18.645, 73.825, 2,
     ["trauma", "stroke", "burn", "ct"], ["trauma", "stroke", "burn"], (12, 4), (24, 9), (6, 2), (4, 2)),
    ("CityLife Multispeciality", "Nigdi", "+91 20 4000 1004", 18.610, 73.795, 2,
     ["cardiac", "stroke", "pediatric", "obstetric", "ct", "cath_lab"], ["cardiac", "stroke", "pediatric", "obstetric"],
     (14, 3), (28, 8), (6, 2), (3, 1)),
    ("Lifeline Cardiac & Stroke Centre", "Wakad", "+91 20 4000 1005", 18.585, 73.835, 2,
     ["cardiac", "stroke", "ct", "cath_lab"], ["cardiac", "stroke"], (16, 6), (18, 7), (8, 4), (3, 2)),
    ("Sunrise Children's Hospital", "Pimple Saudagar", "+91 20 4000 1006", 18.600, 73.770, 2,
     ["pediatric", "obstetric", "ct", "nicu", "trauma"], ["pediatric", "obstetric"], (10, 4), (16, 6), (6, 3), (2, 1)),
    ("Orchid Burns & Plastic Centre", "Bhosari", "+91 20 4000 1007", 18.660, 73.790, 2,
     ["burn", "trauma", "ct"], ["burn"], (10, 5), (14, 6), (5, 2), (3, 2)),
    ("Government District Hospital", "Aundh", "+91 20 4000 1008", 18.575, 73.805, 3,
     ["trauma", "obstetric", "pediatric", "ct"], ["obstetric"], (8, 1), (50, 12), (3, 1), (2, 1)),
]

AMBULANCES = [
    ("AMB-01", "ALS", "Dr. Kulkarni + 2 paramedics", "Chinchwad Station", 18.628, 73.797),
    ("AMB-02", "BLS", "2 EMTs", "Pimpri Station", 18.622, 73.812),
    ("AMB-03", "MICU", "Dr. Deshmukh + 2 paramedics", "Nigdi Station", 18.612, 73.792),
    ("AMB-04", "ALS", "Dr. Iyer + paramedic", "Akurdi Station", 18.642, 73.822),
    ("AMB-05", "BLS", "2 EMTs", "Wakad Station", 18.590, 73.830),
    ("AMB-06", "ALS", "Dr. Shaikh + 2 paramedics", "Bhosari Station", 18.658, 73.795),
    ("AMB-07", "BLS", "2 EMTs", "Aundh Station", 18.578, 73.808),
    ("AMB-08", "MICU", "Dr. Patil + 2 paramedics", "Pimple Saudagar Station", 18.602, 73.775),
    ("AMB-09", "ALS", "Dr. Joshi + paramedic", "Hinjewadi Station", 18.595, 73.815),
    ("AMB-10", "BLS", "2 EMTs", "Kalewadi Station", 18.615, 73.828),
]


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)


def get_meta(c, key, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_meta(c, key, value):
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value)))


def seed_network(c, now):
    c.execute("DELETE FROM hospitals")
    c.execute("DELETE FROM ambulances")
    for i, (name, area, phone, lat, lng, level, caps, oncall, icu, er, vent, orr) in enumerate(HOSPITALS, 1):
        c.execute(
            "INSERT INTO hospitals(id,name,area,phone,lat,lng,level,caps,oncall,icu_total,icu_free,er_total,er_free,"
            "vent_total,vent_free,or_total,or_free,accepting,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, name, area, phone, lat, lng, level, json.dumps({k: (k in caps) for k in CAPS}),
             json.dumps({k: (k in oncall) for k in CAPS}),
             icu[0], icu[1], er[0], er[1], vent[0], vent[1], orr[0], orr[1], 1, now))
    for n, (aid, typ, crew, base, lat, lng) in enumerate(AMBULANCES):
        c.execute("INSERT INTO ambulances(id,type,crew,phone,base_name,base_lat,base_lng,lat,lng,status) VALUES(?,?,?,?,?,?,?,?,?,'available')",
                  (aid, typ, crew, f"+91 98000 000{n:02d}", base, lat, lng, lat, lng))


def row_to_hospital(r):
    d = dict(r)
    d["caps"] = json.loads(d["caps"])
    d["oncall"] = json.loads(d["oncall"])
    d["accepting"] = bool(d["accepting"])
    er_occ = 1 - (d["er_free"] / d["er_total"]) if d["er_total"] else 1
    icu_occ = 1 - (d["icu_free"] / d["icu_total"]) if d["icu_total"] else 1
    d["load_pct"] = round(100 * (0.5 * er_occ + 0.5 * icu_occ))
    return d


def list_hospitals(c):
    return [row_to_hospital(r) for r in c.execute("SELECT * FROM hospitals ORDER BY id")]


def log_event(c, ts, level, message, case_id=None):
    c.execute("INSERT INTO events(ts,level,case_id,message) VALUES(?,?,?,?)", (ts, level, case_id, message))
