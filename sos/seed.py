"""Synthetic patient/location generators and historical data seeding (for analytics + surge demos)."""
from __future__ import annotations
import json
import random

from . import db, geo, routing
from .triage import normalise, triage

TYPE_WEIGHTS = [("Trauma", 28), ("Cardiac", 20), ("Stroke", 10), ("Burn", 6),
                ("Obstetric", 8), ("Respiratory", 12), ("Medical", 16)]


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def random_location(rng):
    lat = _clip(rng.gauss(geo.CENTER[0], 0.022), *geo.BOUNDS["lat"])
    lng = _clip(rng.gauss(geo.CENTER[1], 0.022), *geo.BOUNDS["lng"])
    return round(lat, 5), round(lng, 5)


def random_patient(rng, bias_severe=False, etype=None):
    types, weights = zip(*TYPE_WEIGHTS)
    t = etype or rng.choices(types, weights)[0]
    s = rng.choices([0, 1, 2], [0.25, 0.30, 0.45] if bias_severe else [0.45, 0.33, 0.22])[0]
    child = rng.random() < (0.45 if t == "Burn" else 0.10) and t not in ("Obstetric",)
    if t == "Obstetric":
        age = rng.randint(18, 40)
    elif child:
        age = rng.randint(0, 14)
    elif t in ("Cardiac", "Stroke"):
        age = rng.randint(45, 88)
    else:
        age = rng.randint(16, 85)
    hr = int(rng.gauss([80, 106, 136][s], 9))
    rr = int(rng.gauss([16, 23, 30][s], 3))
    spo2 = int(_clip(rng.gauss([97, 93, 86][s], 2), 70, 100))
    sbp = int(rng.gauss([125, 106, 82][s], 8))
    if child:
        hr += 25 if age < 5 else 10
        rr += 8 if age < 5 else 3
        sbp -= 25 if age < 5 else 12
    sbp = max(55, sbp)
    avpu = rng.choices(["Alert", "Confused", "Voice", "Pain"],
                       [[1, 0, 0, 0], [0.8, 0.2, 0, 0], [0.3, 0.3, 0.2, 0.2]][s])[0]
    p = {
        "type": t, "age": age, "sex": rng.choice(["M", "F"]), "hr": hr, "rr": rr, "spo2": spo2, "sbp": sbp,
        "temp": round(rng.gauss(36.8 if t != "Medical" else [36.9, 38.2, 39.2][s], 0.4), 1),
        "avpu": avpu, "on_oxygen": s == 2 and rng.random() < 0.5,
        "bleeding": "None",
    }
    if t == "Trauma":
        p["mechanism"] = rng.choice([["None", "Fall <3m", "RTA low speed"],
                                     ["Fall <3m", "RTA low speed", "Fall >3m", "Penetrating limb"],
                                     ["RTA high speed", "Fall >3m", "Penetrating torso/head", "RTA high speed"]][s])
        p["bleeding"] = ["None", "Moderate", "Severe"][s] if rng.random() < 0.7 else "Mild"
    elif t == "Cardiac":
        p["chest_pain"] = True
        p["st_elevation"] = s >= 1 and rng.random() < (0.45 if s == 1 else 0.7)
        p["cardiac_arrest"] = s == 2 and rng.random() < 0.06
    elif t == "Stroke":
        p["fast_face"] = rng.random() < 0.4 + 0.2 * s
        p["fast_arm"] = rng.random() < 0.4 + 0.25 * s
        p["fast_speech"] = rng.random() < 0.3 + 0.3 * s
        p["onset_min"] = rng.randint(15, 300)
    elif t == "Burn":
        p["tbsa"] = [rng.randint(2, 8), rng.randint(8, 18), rng.randint(20, 45)][s]
        p["inhalation"] = s == 2 and rng.random() < 0.35
        p["bleeding"] = "None"
    elif t == "Obstetric":
        p["sex"] = "F"
        p["pregnant"] = True
        p["heavy_vaginal_bleeding"] = s == 2 and rng.random() < 0.6
        p["imminent_delivery"] = s == 1 and rng.random() < 0.4
        p["seizure"] = s == 2 and rng.random() < 0.15
    elif t == "Respiratory":
        p["airway_compromised"] = s == 2 and rng.random() < 0.15
    else:
        p["anaphylaxis"] = s == 2 and rng.random() < 0.08
        p["seizure"] = rng.random() < 0.05
    return p


def seed_history(c, now, n=90, days=7):
    """Create ``n`` completed historical cases spread across the past ``days`` days."""
    rng = random.Random(20260101)
    hospitals = db.list_hospitals(c)
    amb_ids = [r["id"] for r in c.execute("SELECT id FROM ambulances")]
    for i in range(1, n + 1):
        # busier in the daytime
        while True:
            created = now - rng.uniform(12 * 60, days * 86400)
            hour = (int(created // 3600) + 5) % 24    # approx IST hour for the demo
            if rng.random() < (0.35 if hour < 6 else 1.0):
                break
        patient = normalise(random_patient(rng))
        tri = triage(patient)
        lat, lng = random_location(rng)
        hs = []
        for h in hospitals:         # historical availability varied
            h2 = dict(h)
            h2["icu_free"] = rng.randint(0, h["icu_total"] // 2 + 1)
            h2["er_free"] = rng.randint(0, h["er_total"] // 2 + 1)
            h2["vent_free"] = rng.randint(0, h["vent_total"])
            h2["or_free"] = rng.randint(0, h["or_total"])
            h2["accepting"] = rng.random() > 0.06
            hs.append(h2)
        r = routing.rank_hospitals(hs, tri["priority"], tri["needs"], (lat, lng), created)
        best = r["best"]
        if not best:
            continue
        prio = int(tri["priority"][1])
        resp_eta = rng.uniform(3.5, 11)
        dispatch = created + rng.uniform(40, 130)
        on_scene = dispatch + resp_eta * 60
        dwell = rng.uniform(5, 11) * 60
        departed = on_scene + dwell
        transport = best["eta_min"] * rng.uniform(0.9, 1.3) * 60
        arrived = departed + transport
        closed = arrived + rng.uniform(4, 9) * 60
        cancelled = rng.random() < 0.04
        status = "cancelled" if cancelled else "completed"
        route = {"tier": r["tier"], "note": r["note"], "chosen_id": best["id"],
                 "ranked": [{"id": e["id"], "name": e["name"], "eta_min": e["eta_min"], "km": e["km"], "score": e["score"],
                             "breakdown": e["breakdown"], "violations": []} for e in r["ranked"]],
                 "rejected": [{"id": e["id"], "name": e["name"], "eta_min": e["eta_min"], "km": e["km"], "score": e["score"],
                               "breakdown": e["breakdown"],
                               "violations": [{"code": x["code"], "text": x["text"]} for x in e["violations"]]} for e in r["rejected"]]}
        code = f"SOS-H{i:03d}"
        c.execute(
            "INSERT INTO cases(id,seq,created_ts,patient,triage,vision,report,lat,lng,place,priority,risk,etype,status,hospital_id,ambulance_id,bed_type,"
            "t_alerted,t_approved,t_dispatched,t_on_scene,t_departed,t_arrived,t_closed,eta_scene,eta_hospital,km_hospital,golden_target,tier,relaxed,override,"
            "reroutes,route_json,tried,seeded) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (code, -i, created, json.dumps(patient), json.dumps(tri), "{}", "{}", lat, lng, "Historical record",
             tri["priority"], tri["risk"], patient["type"], status, best["id"], rng.choice(amb_ids),
             "ICU" if tri["needs"]["care_level"] == "ICU" else "ER",
             created + 5, created + rng.uniform(15, 70), dispatch, on_scene, None if cancelled else departed, None if cancelled else arrived, closed,
             round(resp_eta, 1), best["eta_min"], best["km"], tri["needs"]["golden_target_min"],
             r["tier"], int(r["relaxed"]), int(rng.random() < 0.05), int(rng.random() < 0.06), json.dumps(route), "[]"))
        if cancelled:
            c.execute("UPDATE cases SET t_arrived=NULL, t_departed=NULL WHERE id=?", (code,))
