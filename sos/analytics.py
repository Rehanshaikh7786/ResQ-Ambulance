"""Aggregate analytics computed live from the cases table."""
from __future__ import annotations
import csv
import io
import json
import time
from collections import Counter, defaultdict

from . import db, engine


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def compute():
    with db.conn() as c:
        now = engine.current_ts(c)
        rows = [dict(r) for r in c.execute("SELECT * FROM cases")]
        hospitals = {h["id"]: h for h in db.list_hospitals(c)}
    live = [r for r in rows if r["status"] != "cancelled"]
    by_prio = Counter(r["priority"] for r in live)
    by_type = Counter(r["etype"] for r in live)
    by_hosp = Counter(r["hospital_id"] for r in live if r["hospital_id"])
    resp = [(r["t_on_scene"] - r["created_ts"]) / 60 for r in live if r["t_on_scene"]]
    transport = [(r["t_arrived"] - r["t_departed"]) / 60 for r in live if r["t_arrived"] and r["t_departed"]]
    door = [(r["t_arrived"] - r["created_ts"]) / 60 for r in live if r["t_arrived"]]
    met = [((r["t_arrived"] - r["created_ts"]) / 60) <= r["golden_target"] for r in live if r["t_arrived"]]
    handover = [(r["t_closed"] - r["t_arrived"]) / 60 for r in live if r["t_arrived"] and r["t_closed"]]
    approval = [(r["t_approved"] - r["t_alerted"]) / 60 for r in live if r["t_approved"] and r["t_alerted"] and r["t_approved"] >= r["t_alerted"]]
    photos = sum(1 for r in rows if r["photo"])
    p1_door = [(r["t_arrived"] - r["created_ts"]) / 60 for r in live if r["t_arrived"] and r["priority"] == "P1"]

    rej = Counter()
    from .routing import VIOLATION_LABEL
    for r in rows:
        if not r["route_json"]:
            continue
        try:
            rj = json.loads(r["route_json"])
        except ValueError:
            continue
        for e in rj.get("rejected", []):
            for code in {v["code"] for v in e["violations"]}:
                rej[VIOLATION_LABEL.get(code, code)] += 1

    # cases per day (last 7 sim days) and per hour-of-day
    day = defaultdict(int)
    hours = [0] * 24
    for r in rows:
        lt = time.localtime(r["created_ts"])
        day[time.strftime("%a %d", lt)] += 1
        hours[lt.tm_hour] += 1
    days = []
    for i in range(6, -1, -1):
        k = time.strftime("%a %d", time.localtime(now - i * 86400))
        days.append({"label": k, "count": day.get(k, 0)})

    # priority x type matrix
    matrix = {t: {p: 0 for p in ("P1", "P2", "P3", "P4")} for t in by_type}
    for r in live:
        matrix[r["etype"]][r["priority"]] += 1

    total = len(rows)
    return {
        "totals": {
            "cases": total, "with_photo": photos, "completed": sum(1 for r in rows if r["status"] == "completed"),
            "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
            "active": sum(1 for r in rows if r["status"] in engine.ACTIVE),
            "p1_share": round(100 * by_prio.get("P1", 0) / max(1, len(live))),
            "reroutes": sum(r["reroutes"] for r in rows),
            "overrides": sum(r["override"] for r in rows),
            "relaxed": sum(r["relaxed"] for r in rows),
        },
        "times": {
            "response": _avg(resp), "transport": _avg(transport), "call_to_door": _avg(door),
            "handover": _avg(handover), "approval": _avg(approval), "p1_call_to_door": _avg(p1_door),
            "golden_compliance": round(100 * sum(met) / len(met)) if met else None,
            "golden_n": len(met),
        },
        "by_priority": {p: by_prio.get(p, 0) for p in ("P1", "P2", "P3", "P4")},
        "by_type": dict(by_type),
        "by_hospital": [{"id": hid, "name": hospitals[hid]["name"], "count": n, "load": hospitals[hid]["load_pct"]}
                        for hid, n in by_hosp.most_common() if hid in hospitals],
        "rejections": [{"reason": k, "count": v} for k, v in rej.most_common()],
        "per_day": days,
        "per_hour": hours,
        "matrix": matrix,
    }


def export_csv():
    with db.conn() as c:
        rows = c.execute("SELECT c.*, h.name hname FROM cases c LEFT JOIN hospitals h ON h.id=c.hospital_id ORDER BY created_ts DESC").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["case_id", "created", "type", "priority", "risk", "status", "hospital", "ambulance", "response_min",
                "call_to_door_min", "golden_target_min", "golden_met", "reroutes", "override", "relaxed", "age", "place"])
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
    for r in rows:
        p = json.loads(r["patient"])
        resp = round((r["t_on_scene"] - r["created_ts"]) / 60, 1) if r["t_on_scene"] else ""
        door = round((r["t_arrived"] - r["created_ts"]) / 60, 1) if r["t_arrived"] else ""
        w.writerow([r["id"], fmt(r["created_ts"]), r["etype"], r["priority"], r["risk"], r["status"], r["hname"] or "",
                    r["ambulance_id"] or "", resp, door, r["golden_target"],
                    "" if door == "" else int(door <= r["golden_target"]), r["reroutes"], r["override"], r["relaxed"], p["age"], r["place"]])
    return out.getvalue()
