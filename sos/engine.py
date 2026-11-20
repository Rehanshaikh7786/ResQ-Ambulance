"""
Operations engine – the heart of GoldenHour SOS.

Life of an emergency
  1. REPORT      bystander sends photo + location + answers  → vision analysis → triage
  2. AMBULANCE   nearest suitable ambulance is picked and RESERVED first (held at base)
  3. ALERT       best hospital (hard constraints + weighted score) gets an approval request
  4. DECISION    hospital APPROVES  → ambulance is dispatched URGENTLY (lights & sirens)
                 hospital DECLINES / no reply → next-best hospital is alerted automatically
  5. RESPONSE    on scene → transport → pre-alert → hospital handover → ambulance returns

All timestamps are simulation seconds (speed adjustable; 1x == real time).
"""
from __future__ import annotations
import json
import os
import random
import string
import threading
import time

from . import db, geo, routing
from .triage import normalise, triage, ValidationError

ACTIVE = ("awaiting_hospital", "no_hospital", "pending", "dispatched", "on_scene", "transporting", "at_hospital")
IN_FIELD = ("awaiting_hospital", "no_hospital", "pending", "dispatched", "on_scene", "transporting")
ACTIVE_SQL = "('awaiting_hospital','no_hospital','pending','dispatched','on_scene','transporting','at_hospital')"
SCENE_MIN = {1: 5, 2: 7, 3: 8, 4: 9}
HANDOVER_MIN = {1: 5, 2: 6, 3: 7, 4: 8}
MAX_SPEED = 120.0


def alert_timeout():
    return float(os.environ.get("SOS_ALERT_TIMEOUT", 60))


def auto_delay():
    return float(os.environ.get("SOS_AUTO_DELAY", 8))


_rng = random.Random()
_bg = {"thread": None, "stop": threading.Event()}


# ============================================================================= clock
def current_ts(c):
    sim = db.get_meta(c, "sim_ts")
    if db.get_meta(c, "paused", False):
        return sim
    last = db.get_meta(c, "last_wall")
    return sim + min(5.0, max(0.0, time.time() - last)) * db.get_meta(c, "speed", 10.0)


def advance_clock(c, extra=0.0):
    ts = current_ts(c) + extra
    db.set_meta(c, "sim_ts", ts)
    db.set_meta(c, "last_wall", time.time())
    return ts


def set_speed(speed):
    speed = max(1.0, min(MAX_SPEED, float(speed)))
    with db.conn() as c:
        advance_clock(c)
        db.set_meta(c, "speed", speed)
    return speed


def set_paused(p):
    with db.conn() as c:
        advance_clock(c)
        db.set_meta(c, "paused", bool(p))
        db.set_meta(c, "last_wall", time.time())
    return bool(p)


def set_hospital_mode(mode):
    if mode not in ("auto", "manual"):
        raise ValidationError("mode must be 'auto' or 'manual'")
    with db.conn() as c:
        db.set_meta(c, "hospital_mode", mode)
        db.log_event(c, current_ts(c), "info",
                     "Hospital desks set to " + ("DEMO AUTO-REPLY (simulated hospital staff)" if mode == "auto" else "MANUAL approval"))
    return mode


def init(reset=False, seed_history=60):
    from . import seed
    db.init_schema()
    with db.conn() as c:
        have = c.execute("SELECT COUNT(*) n FROM hospitals").fetchone()["n"]
        if reset or not have:
            for t in ("cases", "events", "meta", "alerts"):
                c.execute(f"DELETE FROM {t}")
            now = time.time()
            db.set_meta(c, "sim_ts", now)
            db.set_meta(c, "last_wall", now)
            db.set_meta(c, "speed", 10.0)
            db.set_meta(c, "paused", False)
            db.set_meta(c, "seq", 0)
            db.set_meta(c, "hospital_mode", "auto")
            db.seed_network(c, now)
            if seed_history:
                seed.seed_history(c, now, seed_history)
            db.log_event(c, now, "info", "GoldenHour SOS online – 8 hospitals, 10 ambulances ready")


# ============================================================================= helpers
def _ambulances(c):
    return [dict(r) for r in c.execute("SELECT * FROM ambulances ORDER BY id")]


def _amb_pos(a, now):
    if a["leg"] and a["status"] in ("dispatched", "transporting", "returning") and a["leg_dur"]:
        t = (now - a["leg_start"]) / a["leg_dur"]
        return (geo.lerp(a["leg_from_lat"], a["leg_to_lat"], t), geo.lerp(a["leg_from_lng"], a["leg_to_lng"], t),
                max(0.0, min(1.0, t)))
    return a["lat"], a["lng"], None


def _start_leg(c, aid, leg, frm, to, start, minutes):
    c.execute("UPDATE ambulances SET leg=?, leg_from_lat=?, leg_from_lng=?, leg_to_lat=?, leg_to_lng=?, leg_start=?, leg_dur=? WHERE id=?",
              (leg, frm[0], frm[1], to[0], to[1], start, max(1.0, minutes * 60.0), aid))


def _nearest_area(c, lat, lng):
    best = None
    for h in c.execute("SELECT area,lat,lng FROM hospitals"):
        d = geo.haversine_km(lat, lng, h["lat"], h["lng"])
        if best is None or d < best[0]:
            best = (d, h["area"])
    return f"Near {best[1]}" if best else "Incident location"


def _reserve_bed(c, hid, needs):
    h = c.execute("SELECT icu_free, er_free, vent_free FROM hospitals WHERE id=?", (hid,)).fetchone()
    bed = None
    if needs["care_level"] == "ICU" and h["icu_free"] > 0:
        bed = "ICU"
    elif h["er_free"] > 0:
        bed = "ER"
    elif h["icu_free"] > 0:
        bed = "ICU"
    if bed == "ICU":
        c.execute("UPDATE hospitals SET icu_free=icu_free-1 WHERE id=?", (hid,))
    elif bed == "ER":
        c.execute("UPDATE hospitals SET er_free=er_free-1 WHERE id=?", (hid,))
    vent = 0
    if needs.get("ventilator") and h["vent_free"] > 0:
        c.execute("UPDATE hospitals SET vent_free=vent_free-1 WHERE id=?", (hid,))
        vent = 1
    return bed, vent


def _release_bed(c, case):
    hid = case["hospital_id"]
    if not hid:
        return
    if case["bed_type"] == "ICU":
        c.execute("UPDATE hospitals SET icu_free=MIN(icu_total, icu_free+1) WHERE id=?", (hid,))
    elif case["bed_type"] == "ER":
        c.execute("UPDATE hospitals SET er_free=MIN(er_total, er_free+1) WHERE id=?", (hid,))
    if case["vent_reserved"]:
        c.execute("UPDATE hospitals SET vent_free=MIN(vent_total, vent_free+1) WHERE id=?", (hid,))
    c.execute("UPDATE cases SET bed_type=NULL, vent_reserved=0 WHERE id=?", (case["id"],))


def _compact_route(r, chosen_id=None):
    def slim(e):
        return {"id": e["id"], "name": e["name"], "eta_min": e["eta_min"], "km": e["km"], "score": e["score"],
                "breakdown": e["breakdown"], "violations": [{"code": x["code"], "text": x["text"]} for x in e["violations"]]}
    return {"tier": r["tier"], "note": r["note"], "chosen_id": chosen_id,
            "ranked": [slim(e) for e in r["ranked"]], "rejected": [slim(e) for e in r["rejected"]]}


def _case(c, cid):
    return c.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()


def _new_code(c):
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    while True:
        code = "SOS-" + "".join(_rng.choices(alphabet, k=5))
        if not _case(c, code):
            return code


def _hospital_name(c, hid):
    r = c.execute("SELECT name FROM hospitals WHERE id=?", (hid,)).fetchone()
    return r["name"] if r else "?"


# ============================================================================= preview / create
def preview_routing(patient_raw, lat, lng):
    patient = normalise(patient_raw)
    tri = triage(patient)
    with db.conn() as c:
        now = current_ts(c)
        r = routing.rank_hospitals(db.list_hospitals(c), tri["priority"], tri["needs"], (lat, lng), now)
        amb = routing.pick_ambulance(_ambulances(c), (lat, lng), tri["needs"], now)
    return patient, tri, r, amb


def create_case(patient_raw, lat, lng, vision=None, report=None, photo=None, name="", phone="", place="", seeded=False):
    patient = normalise(patient_raw)
    tri = triage(patient)
    vision = vision or {}
    with db.conn() as c:
        now = current_ts(c)
        seq = db.get_meta(c, "seq", 0) + 1
        db.set_meta(c, "seq", seq)
        cid = _new_code(c)
        place = (place or "").strip()[:90] or _nearest_area(c, lat, lng)
        c.execute(
            "INSERT INTO cases(id,seq,created_ts,reporter_name,reporter_phone,patient,triage,vision,report,photo,lat,lng,place,"
            "priority,risk,etype,status,golden_target,tried,seeded) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, seq, now, (name or "")[:60], (phone or "")[:20], json.dumps(patient), json.dumps(tri), json.dumps(vision),
             json.dumps(report or {}), photo, lat, lng, place, tri["priority"], tri["risk"], patient["type"],
             "awaiting_hospital", tri["needs"]["golden_target_min"], "[]", int(seeded)))
        lvl = "crit" if tri["priority"] == "P1" else "warn" if tri["priority"] == "P2" else "info"
        db.log_event(c, now, lvl, f"🆘 New report at {place}: {tri['label']} {patient['type']} (risk {tri['risk']}/100)", cid)
        if vision.get("provider") and vision["provider"] != "none":
            who = "Claude vision" if vision["provider"] == "claude" else "basic local check"
            db.log_event(c, now, "info", f"📷 Photo analysed ({who}): {vision.get('summary', '')[:160]}", cid)
        # step 2 – nearest ambulance FIRST
        _reserve_ambulance(c, cid, now)
        # step 3 – alert best hospital
        _alert_best(c, cid, now, exclude=())
        _assign_ambulances(c, now)
        return cid


def _reserve_ambulance(c, cid, now):
    case = _case(c, cid)
    if case["ambulance_id"]:
        return True
    tri = json.loads(case["triage"])
    pick = routing.pick_ambulance(_ambulances(c), (case["lat"], case["lng"]), tri["needs"], now)
    if not pick:
        return False
    a = pick["ambulance"]
    c.execute("UPDATE ambulances SET status='reserved', case_id=? WHERE id=?", (cid, a["id"]))
    c.execute("UPDATE cases SET ambulance_id=?, eta_scene=? WHERE id=?", (a["id"], pick["eta_min"], cid))
    db.log_event(c, now, "info", f"🚑 Nearest ambulance {a['id']} ({a['type']}) reserved – ETA to patient {pick['eta_min']:.0f} min"
                 + (" – BLS only, no ALS free" if pick["downgraded"] else "") + " – holding for hospital approval", cid)
    return True


def _assign_ambulances(c, now):
    rows = c.execute(f"SELECT id,status FROM cases WHERE ambulance_id IS NULL AND status IN ('awaiting_hospital','no_hospital','pending') "
                     "ORDER BY priority, created_ts").fetchall()
    for r in rows:
        if not any(a["status"] == "available" for a in _ambulances(c)):
            break
        if _reserve_ambulance(c, r["id"], now) and r["status"] == "pending":
            _dispatch(c, r["id"], now)


def _alert_best(c, cid, now, exclude=()):
    case = _case(c, cid)
    tri = json.loads(case["triage"])
    r = routing.rank_hospitals(db.list_hospitals(c), case["priority"], tri["needs"], (case["lat"], case["lng"]), now,
                               exclude_ids=tuple(exclude))
    best = r["best"]
    if not best or (r["tier"] == 3 and exclude):
        c.execute("UPDATE cases SET status='no_hospital', route_json=? WHERE id=?", (json.dumps(_compact_route(r, None)), cid))
        db.log_event(c, now, "crit", "⛔ No hospital available – control room must assign a destination", cid)
        return None
    c.execute("UPDATE cases SET hospital_id=?, eta_hospital=?, km_hospital=?, tier=?, relaxed=?, route_json=?, status='awaiting_hospital', t_alerted=? WHERE id=?",
              (best["id"], best["eta_min"], best["km"], r["tier"], int(r["relaxed"]), json.dumps(_compact_route(r, best["id"])), now, cid))
    c.execute("INSERT INTO alerts(case_id,hospital_id,status,kind,created_ts,created_wall) VALUES(?,?,?,?,?,?)",
              (cid, best["id"], "pending", "approval", now, time.time()))
    db.log_event(c, now, "warn" if case["priority"] in ("P1", "P2") else "info",
                 f"🏥 Alert sent to {best['name']} – awaiting approval (ETA {best['eta_min']:.0f} min, score {best['score']})"
                 + (f" – tier {r['tier']} fallback" if r["relaxed"] else ""), cid)
    return best


# ============================================================================= hospital decision
def decide_alert(alert_id, decision, reason="", by="Hospital desk"):
    if decision not in ("approve", "decline"):
        raise ValidationError("decision must be 'approve' or 'decline'")
    with db.conn() as c:
        now = current_ts(c)
        al = c.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not al:
            raise ValidationError("Alert not found")
        if al["status"] != "pending":
            raise ValidationError(f"Alert already {al['status']}")
        if decision == "approve":
            _approve(c, al, by, now)
        else:
            _decline(c, al, reason or "Cannot accept", by, now)
        return {"ok": True, "case": al["case_id"], "decision": decision}


def _mark(c, al, status, by, reason, now):
    c.execute("UPDATE alerts SET status=?, decided_ts=?, decided_wall=?, reason=?, decided_by=? WHERE id=?",
              (status, now, time.time(), reason, by, al["id"]))


def _approve(c, al, by, now):
    case = _case(c, al["case_id"])
    if case["status"] not in ("awaiting_hospital", "no_hospital"):
        _mark(c, al, "cancelled", by, "case no longer waiting", now)
        raise ValidationError("Case is no longer waiting for approval")
    tri = json.loads(case["triage"])
    hname = _hospital_name(c, al["hospital_id"])
    _mark(c, al, "approved", by, "", now)
    c.execute("UPDATE alerts SET status='cancelled' WHERE case_id=? AND status='pending' AND id<>?", (case["id"], al["id"]))
    if case["hospital_id"] != al["hospital_id"]:
        c.execute("UPDATE cases SET hospital_id=? WHERE id=?", (al["hospital_id"], case["id"]))
    bed, vent = _reserve_bed(c, al["hospital_id"], tri["needs"])
    c.execute("UPDATE cases SET t_approved=?, bed_type=?, vent_reserved=? WHERE id=?", (now, bed, vent, case["id"]))
    db.log_event(c, now, "info", f"✅ {hname} APPROVED by {by} – {bed or 'bed'} reserved"
                 + (" + ventilator" if vent else "") + "; sending ambulance urgently", case["id"])
    db.log_event(c, now, "info", f"Pre-alert to {hname}: " + "; ".join(tri["prealert"]), case["id"])
    if _case(c, case["id"])["ambulance_id"] or _reserve_ambulance(c, case["id"], now):
        _dispatch(c, case["id"], now)
    else:
        c.execute("UPDATE cases SET status='pending' WHERE id=?", (case["id"],))
        db.log_event(c, now, "crit", "⏳ Approved but ALL ambulances busy – case queued first in line", case["id"])


def _decline(c, al, reason, by, now):
    case = _case(c, al["case_id"])
    hname = _hospital_name(c, al["hospital_id"])
    _mark(c, al, "declined", by, reason, now)
    db.log_event(c, now, "warn", f"❌ {hname} DECLINED ({reason}) – escalating to next-best hospital", case["id"])
    _escalate(c, case["id"], now)


def _tried(c, cid):
    return [r["hospital_id"] for r in c.execute("SELECT DISTINCT hospital_id FROM alerts WHERE case_id=? AND kind='approval'", (cid,))]


def _escalate(c, cid, now):
    case = _case(c, cid)
    if case["status"] not in ("awaiting_hospital", "no_hospital"):
        return
    c.execute("UPDATE cases SET reroutes=reroutes+1 WHERE id=?", (cid,))
    _alert_best(c, cid, now, exclude=_tried(c, cid))


def assign_hospital(case_id, hospital_id, by="Control room"):
    """Control-room override: force a destination and approve it on the hospital's behalf."""
    with db.conn() as c:
        now = current_ts(c)
        case = _case(c, case_id)
        if not case:
            raise ValidationError("Case not found")
        if case["status"] not in ("awaiting_hospital", "no_hospital"):
            raise ValidationError("Case is not waiting for a hospital")
        h = c.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
        if not h:
            raise ValidationError("Unknown hospital")
        c.execute("UPDATE alerts SET status='cancelled' WHERE case_id=? AND status='pending'", (case_id,))
        eta, km = geo.eta_minutes(case["lat"], case["lng"], h["lat"], h["lng"], now)
        c.execute("UPDATE cases SET hospital_id=?, eta_hospital=?, km_hospital=?, override=1 WHERE id=?", (hospital_id, round(eta, 1), round(km, 1), case_id))
        c.execute("INSERT INTO alerts(case_id,hospital_id,status,kind,created_ts,created_wall) VALUES(?,?,?,?,?,?)",
                  (case_id, hospital_id, "pending", "approval", now, time.time()))
        al = c.execute("SELECT * FROM alerts WHERE id=last_insert_rowid()").fetchone()
        db.log_event(c, now, "warn", f"Control room override: destination set to {h['name']}", case_id)
        _approve(c, al, by + " (override)", now)
        return {"ok": True}


# ============================================================================= dispatch
def _dispatch(c, cid, now):
    case = _case(c, cid)
    a = c.execute("SELECT * FROM ambulances WHERE id=?", (case["ambulance_id"],)).fetchone()
    eta, km = geo.eta_minutes(a["lat"], a["lng"], case["lat"], case["lng"], now)
    c.execute("UPDATE ambulances SET status='dispatched', case_id=? WHERE id=?", (cid, a["id"]))
    _start_leg(c, a["id"], "to_scene", (a["lat"], a["lng"]), (case["lat"], case["lng"]), now, eta)
    c.execute("UPDATE cases SET status='dispatched', t_dispatched=?, eta_scene=? WHERE id=?", (now, round(eta, 1), cid))
    db.log_event(c, now, "crit" if case["priority"] == "P1" else "warn",
                 f"🚨 {a['id']} DISPATCHED URGENTLY (lights & sirens) – ETA {eta:.0f} min, {km:.1f} km", cid)


# ============================================================================= re-route in flight
def _reroute_inflight(c, case, now, reason):
    tri = json.loads(case["triage"])
    a = c.execute("SELECT * FROM ambulances WHERE id=?", (case["ambulance_id"],)).fetchone() if case["ambulance_id"] else None
    if case["status"] == "transporting" and a:
        lat, lng, _ = _amb_pos(dict(a), now)
    else:
        lat, lng = case["lat"], case["lng"]
    old = case["hospital_id"]
    _release_bed(c, case)
    r = routing.rank_hospitals(db.list_hospitals(c), case["priority"], tri["needs"], (lat, lng), now, exclude_ids=(old,))
    best = r["best"]
    if not best:
        db.log_event(c, now, "crit", f"REROUTE FAILED – no alternative hospital ({reason})", case["id"])
        return
    bed, vent = _reserve_bed(c, best["id"], tri["needs"])
    c.execute("UPDATE cases SET hospital_id=?, bed_type=?, vent_reserved=?, eta_hospital=?, km_hospital=?, tier=?, relaxed=?, reroutes=reroutes+1, route_json=? WHERE id=?",
              (best["id"], bed, vent, best["eta_min"], best["km"], r["tier"], int(r["relaxed"]), json.dumps(_compact_route(r, best["id"])), case["id"]))
    c.execute("INSERT INTO alerts(case_id,hospital_id,status,kind,created_ts,created_wall,decided_ts,decided_wall,decided_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (case["id"], best["id"], "approved", "reroute", now, time.time(), now, time.time(), "Auto-reroute", reason))
    if case["status"] == "transporting" and a:
        _start_leg(c, a["id"], "to_hospital", (lat, lng), (best["lat"], best["lng"]), now, best["eta_min"])
    db.log_event(c, now, "warn", f"🔀 REROUTED: {_hospital_name(c, old)} → {best['name']} (ETA {best['eta_min']:.0f} min) – {reason}", case["id"])
    db.log_event(c, now, "info", f"Pre-alert to {best['name']}: " + "; ".join(tri["prealert"]), case["id"])


def reevaluate_inflight(c, now, hospital_id=None, reason="Hospital status changed"):
    hosp = {h["id"]: h for h in db.list_hospitals(c)}
    n = 0
    for case in c.execute("SELECT * FROM cases WHERE status IN ('pending','dispatched','on_scene','transporting')").fetchall():
        if not case["hospital_id"] or (hospital_id and case["hospital_id"] != hospital_id):
            continue
        tri = json.loads(case["triage"])
        e = routing.evaluate_hospital(hosp[case["hospital_id"]], int(case["priority"][1]), tri["needs"],
                                      (case["lat"], case["lng"]), now, reserved_here=True)
        if any(x["code"] in ("accepting", "capability", "oncall", "pediatric") for x in e["violations"]):
            _reroute_inflight(c, case, now, reason)
            n += 1
    # cases still waiting on this hospital's approval get re-alerted elsewhere
    for al in c.execute("SELECT * FROM alerts WHERE status='pending' AND hospital_id=?", (hospital_id,)).fetchall() if hospital_id else []:
        h = hosp[al["hospital_id"]]
        if not h["accepting"]:
            _mark(c, al, "expired", "System", "hospital went on diversion", now)
            _escalate(c, al["case_id"], now)
    return n


def update_hospital(hid, data):
    with db.conn() as c:
        now = current_ts(c)
        h = c.execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone()
        if not h:
            raise ValidationError("Hospital not found")
        sets, vals = [], []
        totals = {"icu_free": h["icu_total"], "er_free": h["er_total"], "vent_free": h["vent_total"], "or_free": h["or_total"]}
        for f, tot in totals.items():
            if f in data:
                try:
                    v = int(data[f])
                except (TypeError, ValueError):
                    raise ValidationError(f"{f} must be an integer")
                if not 0 <= v <= tot:
                    raise ValidationError(f"{f} must be between 0 and {tot}")
                sets.append(f"{f}=?")
                vals.append(v)
        if "accepting" in data:
            sets.append("accepting=?")
            vals.append(1 if data["accepting"] else 0)
            db.set_meta(c, f"autodiv:{hid}", False)
        for key in ("caps", "oncall"):
            if isinstance(data.get(key), dict):
                cur = json.loads(h[key])
                for k, v in data[key].items():
                    if k in cur:
                        cur[k] = bool(v)
                sets.append(f"{key}=?")
                vals.append(json.dumps(cur))
        if sets:
            sets += ["updated_ts=?"]
            vals += [now]
            c.execute(f"UPDATE hospitals SET {', '.join(sets)} WHERE id=?", (*vals, hid))
            db.log_event(c, now, "warn" if data.get("accepting") is False else "info", f"{h['name']} updated: " + ", ".join(data.keys()))
            reevaluate_inflight(c, now, hid, f"{h['name']} status changed")
        return db.row_to_hospital(c.execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone())


# ============================================================================= cancel
def cancel_case(cid, reason="Cancelled"):
    with db.conn() as c:
        now = current_ts(c)
        case = _case(c, cid)
        if not case:
            raise ValidationError("Case not found")
        if case["status"] not in IN_FIELD:
            raise ValidationError("Case can no longer be cancelled")
        _release_bed(c, case)
        c.execute("UPDATE alerts SET status='cancelled' WHERE case_id=? AND status='pending'", (cid,))
        c.execute("UPDATE cases SET status='cancelled', t_closed=? WHERE id=?", (now, cid))
        if case["ambulance_id"]:
            a = dict(c.execute("SELECT * FROM ambulances WHERE id=?", (case["ambulance_id"],)).fetchone())
            if a["status"] == "reserved":
                c.execute("UPDATE ambulances SET status='available', case_id=NULL WHERE id=?", (a["id"],))
            else:
                lat, lng, _ = _amb_pos(a, now)
                eta, _k = geo.eta_minutes(lat, lng, a["base_lat"], a["base_lng"], now)
                c.execute("UPDATE ambulances SET status='returning', case_id=NULL, lat=?, lng=? WHERE id=?", (lat, lng, a["id"]))
                _start_leg(c, a["id"], "returning", (lat, lng), (a["base_lat"], a["base_lng"]), now, eta)
        db.log_event(c, now, "warn", f"{cid} cancelled – {reason}", cid)
    return get_case(cid)


# ============================================================================= tick
def tick(extra=0.0, drift=True):
    with db.conn() as c:
        if db.get_meta(c, "paused", False) and not extra:
            db.set_meta(c, "last_wall", time.time())
            return
        prev = db.get_meta(c, "sim_ts")
        now = advance_clock(c, extra)
        _process_alerts(c, now)
        for _ in range(8):
            changed = False
            for a in _ambulances(c):
                changed |= _step_ambulance(c, a, now)
            if not changed:
                break
        _assign_ambulances(c, now)
        _retry_no_hospital(c, now)
        if drift:
            _drift(c, now, now - prev)


def _process_alerts(c, now, wall=None):
    wall = wall or time.time()
    mode = db.get_meta(c, "hospital_mode", "auto")
    for al in c.execute("SELECT * FROM alerts WHERE status='pending' AND kind='approval' ORDER BY id").fetchall():
        age = wall - al["created_wall"]
        case = _case(c, al["case_id"])
        if mode == "auto" and age >= auto_delay():
            h = db.row_to_hospital(c.execute("SELECT * FROM hospitals WHERE id=?", (al["hospital_id"],)).fetchone())
            tri = json.loads(case["triage"])
            e = routing.evaluate_hospital(h, int(case["priority"][1]), tri["needs"], (case["lat"], case["lng"]), now)
            block = routing.TIER_BLOCKING[case["tier"]]
            bad = [x for x in e["violations"] if x["code"] in block]
            if bad:
                _decline(c, al, bad[0]["text"], "Hospital desk (simulated)", now)
            else:
                _approve(c, al, "Hospital desk (simulated)", now)
        elif age >= alert_timeout():
            _mark(c, al, "expired", "System", f"no reply in {alert_timeout():.0f}s", now)
            db.log_event(c, now, "warn", f"⏱ {_hospital_name(c, al['hospital_id'])} did not reply in {alert_timeout():.0f}s – escalating", al["case_id"])
            _escalate(c, al["case_id"], now)


def _retry_no_hospital(c, now):
    for case in c.execute("SELECT id FROM cases WHERE status='no_hospital'").fetchall():
        last = db.get_meta(c, f"nh:{case['id']}", 0)
        if time.time() - last >= 20:
            db.set_meta(c, f"nh:{case['id']}", time.time())
            r = _alert_best(c, case["id"], now, exclude=())
            if r:
                db.log_event(c, now, "info", "Capacity found – retrying hospital alert", case["id"])


def _step_ambulance(c, a, now):
    st, cid = a["status"], a["case_id"]
    case = _case(c, cid) if cid else None
    if st == "dispatched" and case and now >= a["leg_start"] + a["leg_dur"]:
        arr = a["leg_start"] + a["leg_dur"]
        dwell = (SCENE_MIN[int(case["priority"][1])] + case["seq"] % 3) * 60
        c.execute("UPDATE ambulances SET status='on_scene', lat=?, lng=?, leg=NULL, phase_until=? WHERE id=?",
                  (a["leg_to_lat"], a["leg_to_lng"], arr + dwell, a["id"]))
        c.execute("UPDATE cases SET status='on_scene', t_on_scene=? WHERE id=?", (arr, cid))
        db.log_event(c, arr, "info", f"🚑 {a['id']} ON SCENE – response {(arr - case['created_ts']) / 60:.1f} min from report; treating patient", cid)
        return True
    if st == "on_scene" and case and now >= a["phase_until"]:
        dep = a["phase_until"]
        h = c.execute("SELECT * FROM hospitals WHERE id=?", (case["hospital_id"],)).fetchone()
        if not h:
            return False
        eta, km = geo.eta_minutes(case["lat"], case["lng"], h["lat"], h["lng"], dep)
        _start_leg(c, a["id"], "to_hospital", (case["lat"], case["lng"]), (h["lat"], h["lng"]), dep, eta)
        c.execute("UPDATE ambulances SET status='transporting' WHERE id=?", (a["id"],))
        c.execute("UPDATE cases SET status='transporting', t_departed=?, eta_hospital=?, km_hospital=? WHERE id=?", (dep, round(eta, 1), round(km, 1), cid))
        db.log_event(c, dep, "info", f"➡ {a['id']} transporting to {h['name']} – ETA {eta:.0f} min", cid)
        return True
    if st == "transporting" and case and now >= a["leg_start"] + a["leg_dur"]:
        arr = a["leg_start"] + a["leg_dur"]
        c.execute("UPDATE ambulances SET status='at_hospital', lat=?, lng=?, leg=NULL, phase_until=? WHERE id=?",
                  (a["leg_to_lat"], a["leg_to_lng"], arr + HANDOVER_MIN[int(case["priority"][1])] * 60, a["id"]))
        c.execute("UPDATE cases SET status='at_hospital', t_arrived=? WHERE id=?", (arr, cid))
        total = (arr - case["created_ts"]) / 60
        ok = total <= case["golden_target"]
        db.log_event(c, arr, "info" if ok else "warn",
                     f"🏥 Arrived at {_hospital_name(c, case['hospital_id'])} – report-to-door {total:.0f} min (target {case['golden_target']:.0f}) "
                     f"{'✓ within golden hour' if ok else '✕ target missed'}", cid)
        return True
    if st == "at_hospital" and case and now >= a["phase_until"]:
        done = a["phase_until"]
        eta, _ = geo.eta_minutes(a["lat"], a["lng"], a["base_lat"], a["base_lng"], done)
        c.execute("UPDATE cases SET status='completed', t_closed=? WHERE id=?", (done, cid))
        c.execute("UPDATE ambulances SET status='returning', case_id=NULL, trips=trips+1 WHERE id=?", (a["id"],))
        _start_leg(c, a["id"], "returning", (a["lat"], a["lng"]), (a["base_lat"], a["base_lng"]), done, eta)
        db.log_event(c, done, "info", f"✔ Handover complete; {a['id']} returning to base", cid)
        return True
    if st == "returning" and now >= a["leg_start"] + a["leg_dur"]:
        c.execute("UPDATE ambulances SET status='available', lat=base_lat, lng=base_lng, leg=NULL WHERE id=?", (a["id"],))
        return True
    if st in ("dispatched", "transporting", "on_scene", "at_hospital", "reserved") and not case:
        c.execute("UPDATE ambulances SET status='returning', leg=NULL WHERE id=?", (a["id"],))
        return True
    return False


def _drift(c, now, dt):
    """Ambient hospital demand + automatic diversion when completely full."""
    if dt <= 0:
        return
    for h in c.execute("SELECT * FROM hospitals").fetchall():
        if _rng.random() < min(0.9, dt / 900.0):
            field, total = (("icu_free", h["icu_total"]) if _rng.random() < 0.35 else ("er_free", h["er_total"]))
            free = h[field]
            occ = 1 - free / total if total else 1
            if _rng.random() < (0.7 if occ > 0.7 else 0.5):
                if free < total:
                    c.execute(f"UPDATE hospitals SET {field}={field}+1 WHERE id=?", (h["id"],))
            elif free > 1:
                c.execute(f"UPDATE hospitals SET {field}={field}-1 WHERE id=?", (h["id"],))
        h = c.execute("SELECT * FROM hospitals WHERE id=?", (h["id"],)).fetchone()
        auto = db.get_meta(c, f"autodiv:{h['id']}", False)
        if h["accepting"] and h["icu_free"] + h["er_free"] == 0:
            c.execute("UPDATE hospitals SET accepting=0 WHERE id=?", (h["id"],))
            db.set_meta(c, f"autodiv:{h['id']}", True)
            db.log_event(c, now, "crit", f"{h['name']} FULL – automatic diversion")
            reevaluate_inflight(c, now, h["id"], f"{h['name']} went on automatic diversion")
        elif auto and not h["accepting"] and h["icu_free"] + h["er_free"] >= 3:
            c.execute("UPDATE hospitals SET accepting=1 WHERE id=?", (h["id"],))
            db.set_meta(c, f"autodiv:{h['id']}", False)
            db.log_event(c, now, "info", f"{h['name']} capacity recovered – diversion lifted")


# ============================================================================= scenarios
def surge(n=6):
    from . import seed
    n = max(1, min(20, int(n)))
    with db.conn() as c:
        db.log_event(c, current_ts(c), "crit", f"⚠ SURGE SCENARIO – {n} simultaneous incidents")
    cx, cy = _rng.uniform(*geo.BOUNDS["lat"]), _rng.uniform(*geo.BOUNDS["lng"])
    out = []
    for _ in range(n):
        p = seed.random_patient(_rng, bias_severe=True)
        lat = min(geo.BOUNDS["lat"][1], max(geo.BOUNDS["lat"][0], cx + _rng.uniform(-0.012, 0.012)))
        lng = min(geo.BOUNDS["lng"][1], max(geo.BOUNDS["lng"][0], cy + _rng.uniform(-0.012, 0.012)))
        out.append(create_case(p, lat, lng, vision={"provider": "none"}, name="Simulated caller"))
    return out


def reset(history=60):
    init(reset=True, seed_history=history)


# ============================================================================= views
def _eta_remaining(c, case, now):
    if case["status"] in ("dispatched", "transporting") and case["ambulance_id"]:
        a = c.execute("SELECT leg_start,leg_dur,leg FROM ambulances WHERE id=?", (case["ambulance_id"],)).fetchone()
        if a and a["leg"]:
            return max(0.0, round((a["leg_start"] + a["leg_dur"] - now) / 60, 1))
    return None


def _case_out(c, r, now, detail=False):
    patient, tri, vision = json.loads(r["patient"]), json.loads(r["triage"]), json.loads(r["vision"] or "{}")
    h = c.execute("SELECT id,name,area,lat,lng,phone FROM hospitals WHERE id=?", (r["hospital_id"],)).fetchone() if r["hospital_id"] else None
    active = r["status"] in ACTIVE
    deadline = r["created_ts"] + r["golden_target"] * 60
    al = c.execute("SELECT id,status,created_wall FROM alerts WHERE case_id=? AND kind='approval' ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
    out = {
        "id": r["id"], "priority": r["priority"], "risk": r["risk"], "type": r["etype"], "status": r["status"], "label": tri["label"],
        "created_ts": r["created_ts"], "lat": r["lat"], "lng": r["lng"], "place": r["place"], "summary": tri["summary"],
        "patient": {k: patient[k] for k in ("name", "age", "sex", "type")},
        "photo": f"/uploads/{r['photo']}" if r["photo"] else None,
        "vision_summary": vision.get("summary"), "vision_provider": vision.get("provider"), "vision_severity": vision.get("severity", 0),
        "hospital": dict(h) if h else None, "ambulance_id": r["ambulance_id"], "bed_type": r["bed_type"],
        "eta_scene": r["eta_scene"], "eta_hospital": r["eta_hospital"], "km_hospital": r["km_hospital"],
        "eta_remaining_min": _eta_remaining(c, r, now),
        "t_alerted": r["t_alerted"], "t_approved": r["t_approved"], "t_dispatched": r["t_dispatched"], "t_on_scene": r["t_on_scene"],
        "t_departed": r["t_departed"], "t_arrived": r["t_arrived"], "t_closed": r["t_closed"],
        "golden_target": r["golden_target"], "golden_deadline": deadline,
        "golden_remaining_min": round((deadline - now) / 60, 1) if active and not r["t_arrived"] else None,
        "tier": r["tier"], "relaxed": bool(r["relaxed"]), "override": bool(r["override"]), "reroutes": r["reroutes"],
        "elapsed_min": round(((r["t_closed"] or now) - r["created_ts"]) / 60, 1), "seeded": bool(r["seeded"]),
        "alert": ({"id": al["id"], "status": al["status"], "wait_s": round(time.time() - al["created_wall"])} if al else None),
    }
    if r["t_arrived"]:
        out["call_to_door_min"] = round((r["t_arrived"] - r["created_ts"]) / 60, 1)
        out["golden_met"] = out["call_to_door_min"] <= r["golden_target"]
    if detail:
        out.update({
            "patient": patient, "triage": tri, "vision": vision, "report": json.loads(r["report"] or "{}"),
            "route": json.loads(r["route_json"]) if r["route_json"] else None,
            "reporter": {"name": r["reporter_name"], "phone": r["reporter_phone"]},
            "timeline": [dict(e) for e in c.execute("SELECT ts,level,message FROM events WHERE case_id=? ORDER BY id", (r["id"],))],
            "alerts": [dict(a) for a in c.execute("SELECT id,hospital_id,status,kind,reason,decided_by,created_ts,decided_ts FROM alerts WHERE case_id=? ORDER BY id", (r["id"],))],
        })
    return out


def get_case(cid):
    with db.conn() as c:
        now = current_ts(c)
        r = _case(c, cid)
        return _case_out(c, r, now, detail=True) if r else None


def list_cases(status="all", limit=200):
    with db.conn() as c:
        now = current_ts(c)
        q = "SELECT * FROM cases"
        if status == "active":
            q += f" WHERE status IN {ACTIVE_SQL}"
        elif status == "closed":
            q += " WHERE status IN ('completed','cancelled')"
        return [_case_out(c, r, now) for r in c.execute(q + " ORDER BY created_ts DESC LIMIT ?", (int(limit),))]


STEPS = [("reported", "Report received"), ("analysed", "Photo analysed"), ("reserved", "Nearest ambulance picked"),
         ("alerted", "Hospital alerted"), ("approved", "Hospital approved"), ("enroute", "Ambulance on its way"),
         ("onscene", "Ambulance arrived"), ("transport", "Heading to hospital"), ("arrived", "Reached hospital")]


def public_case(cid):
    """Citizen-facing view (no other patients' data)."""
    with db.conn() as c:
        now = current_ts(c)
        r = _case(c, cid)
        if not r:
            return None
        o = _case_out(c, r, now, detail=True)
        a = c.execute("SELECT * FROM ambulances WHERE id=?", (r["ambulance_id"],)).fetchone() if r["ambulance_id"] else None
        amb = None
        if a and a["status"] in ("reserved", "dispatched", "on_scene", "transporting", "at_hospital", "returning"):
            lat, lng, prog = _amb_pos(dict(a), now)
            amb = {"id": a["id"], "type": a["type"], "crew": a["crew"], "phone": a["phone"], "status": a["status"], "lat": lat, "lng": lng,
                   "leg": a["leg"], "leg_from": [a["leg_from_lat"], a["leg_from_lng"]] if a["leg"] else None,
                   "leg_to": [a["leg_to_lat"], a["leg_to_lng"]] if a["leg"] else None,
                   "leg_start": a["leg_start"], "leg_dur": a["leg_dur"]}
        done = {
            "reported": True, "analysed": True, "reserved": bool(r["ambulance_id"]), "alerted": bool(r["t_alerted"]),
            "approved": bool(r["t_approved"]), "enroute": bool(r["t_dispatched"]), "onscene": bool(r["t_on_scene"]),
            "transport": bool(r["t_departed"]), "arrived": bool(r["t_arrived"]),
        }
        idx = max([i for i, (k, _) in enumerate(STEPS) if done[k]] or [0])
        speed = db.get_meta(c, "speed", 10.0)
        paused = db.get_meta(c, "paused", False)
    return {
        "id": o["id"], "status": o["status"], "priority": o["priority"], "label": o["label"], "type": o["type"], "place": o["place"],
        "lat": o["lat"], "lng": o["lng"], "photo": o["photo"], "vision": {k: o["vision"].get(k) for k in ("summary", "severity", "provider", "injuries", "hazards", "confidence")},
        "hospital": o["hospital"], "ambulance": amb, "eta_remaining_min": o["eta_remaining_min"], "eta_scene": o["eta_scene"],
        "steps": [{"key": k, "label": lab, "done": done[k], "active": i == idx and o["status"] not in ("completed", "cancelled")} for i, (k, lab) in enumerate(STEPS)],
        "alert": o["alert"], "timeline": o["timeline"], "reroutes": o["reroutes"], "prealert": o["triage"]["prealert"],
        "first_aid_type": o["type"], "bleeding": o["patient"].get("bleeding"), "burn": o["patient"].get("tbsa", 0) > 0,
        "sim": {"ts": now, "speed": speed, "paused": paused, "wall": time.time()}, "elapsed_min": o["elapsed_min"],
        "golden_target": o["golden_target"], "golden_remaining_min": o["golden_remaining_min"],
    }


def hospital_view(hid):
    with db.conn() as c:
        now = current_ts(c)
        h = c.execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone()
        if not h:
            return None
        mode = db.get_meta(c, "hospital_mode", "auto")
        pend = []
        for al in c.execute("SELECT * FROM alerts WHERE hospital_id=? AND status='pending' AND kind='approval' ORDER BY id", (hid,)):
            case = _case(c, al["case_id"])
            o = _case_out(c, case, now, detail=True)
            age = time.time() - al["created_wall"]
            o["alert_id"] = al["id"]
            o["seconds_left"] = max(0, round(alert_timeout() - age))
            o["auto_in"] = max(0, round(auto_delay() - age)) if mode == "auto" else None
            o["violations"] = next((e["violations"] for e in (o["route"] or {}).get("ranked", []) + (o["route"] or {}).get("rejected", []) if e["id"] == hid), [])
            pend.append(o)
        pend.sort(key=lambda x: (x["priority"], x["created_ts"]))
        inbound = [_case_out(c, r, now) for r in c.execute(
            "SELECT * FROM cases WHERE hospital_id=? AND status IN ('dispatched','on_scene','transporting','at_hospital') ORDER BY priority, created_ts", (hid,))]
        recent = [dict(x) for x in c.execute("SELECT id,ts,level,case_id,message FROM events WHERE case_id IN (SELECT case_id FROM alerts WHERE hospital_id=?) ORDER BY id DESC LIMIT 25", (hid,))]
        hosp = db.row_to_hospital(h)
    return {"hospital": hosp, "pending": pend, "inbound": inbound, "events": recent, "mode": mode, "now": now, "timeout": alert_timeout()}


def driver_view(aid):
    with db.conn() as c:
        now = current_ts(c)
        a = c.execute("SELECT * FROM ambulances WHERE id=?", (aid,)).fetchone()
        if not a:
            return None
        case = _case(c, a["case_id"]) if a["case_id"] else None
        lat, lng, prog = _amb_pos(dict(a), now)
        return {"ambulance": {k: a[k] for k in ("id", "type", "crew", "phone", "status", "base_name", "trips")} | {"lat": lat, "lng": lng},
                "case": _case_out(c, case, now, detail=True) if case else None, "now": now}


def state(event_limit=50):
    with db.conn() as c:
        now = current_ts(c)
        hospitals = db.list_hospitals(c)
        ambs = []
        for a in _ambulances(c):
            lat, lng, prog = _amb_pos(a, now)
            ambs.append({"id": a["id"], "type": a["type"], "crew": a["crew"], "base": a["base_name"], "status": a["status"], "case_id": a["case_id"],
                         "lat": lat, "lng": lng, "progress": prog, "leg": a["leg"], "trips": a["trips"],
                         "leg_from": [a["leg_from_lat"], a["leg_from_lng"]] if a["leg"] else None,
                         "leg_to": [a["leg_to_lat"], a["leg_to_lng"]] if a["leg"] else None,
                         "leg_start": a["leg_start"], "leg_dur": a["leg_dur"]})
        active = [_case_out(c, r, now) for r in c.execute(f"SELECT * FROM cases WHERE status IN {ACTIVE_SQL} ORDER BY priority, created_ts")]
        events = [dict(e) for e in c.execute("SELECT id,ts,level,case_id,message FROM events ORDER BY id DESC LIMIT ?", (event_limit,))]
        speed, paused = db.get_meta(c, "speed", 10.0), db.get_meta(c, "paused", False)
        mode = db.get_meta(c, "hospital_mode", "auto")
        pending_alerts = c.execute("SELECT COUNT(*) n FROM alerts WHERE status='pending' AND kind='approval'").fetchone()["n"]
    return {
        "sim": {"ts": now, "speed": speed, "paused": paused, "wall": time.time()},
        "hospital_mode": mode, "hospitals": hospitals, "ambulances": ambs, "cases": active, "events": events,
        "kpi": {"active": len(active), "p1_active": sum(1 for x in active if x["priority"] == "P1"),
                "ambulances_total": len(ambs), "ambulances_available": sum(1 for a in ambs if a["status"] == "available"),
                "network_load": round(sum(h["load_pct"] for h in hospitals) / max(1, len(hospitals))),
                "hospitals_online": sum(1 for h in hospitals if h["accepting"]), "hospitals_total": len(hospitals),
                "awaiting": sum(1 for x in active if x["status"] in ("awaiting_hospital", "no_hospital")),
                "pending_alerts": pending_alerts, "no_hospital": sum(1 for x in active if x["status"] == "no_hospital")},
    }


# ============================================================================= background loop
def start_background(interval=1.0):
    if _bg["thread"] and _bg["thread"].is_alive():
        return

    def loop():
        while not _bg["stop"].wait(interval):
            try:
                tick()
            except Exception as ex:
                print("[engine] tick error:", repr(ex))

    t = threading.Thread(target=loop, name="sos-engine", daemon=True)
    _bg["thread"] = t
    _bg["stop"].clear()
    t.start()
