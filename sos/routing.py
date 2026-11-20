"""
Constraint-based hospital routing + ambulance selection.

Step 1  HARD CONSTRAINTS – every hospital is checked against *all* constraints (not first-fail),
        so the dispatcher can see every reason a hospital was rejected.
Step 2  RELAXATION LADDER – if no hospital satisfies everything, constraints are relaxed in a
        fixed, safety-ordered way and the result is flagged ("stabilise at nearest ED" etc.).
Step 3  WEIGHTED SCORE – feasible hospitals are ranked by an explainable weighted score
        (time, capacity, load, specialty fit, on-call depth) with weights depending on priority.
"""
from __future__ import annotations
from . import geo

# Violation codes, grouped by which relaxation tier still enforces them.
TIER_BLOCKING = {
    0: {"accepting", "capability", "oncall", "pediatric", "extra", "bed", "vent", "budget"},
    1: {"accepting", "capability", "bed", "vent"},
    2: {"accepting", "bed_any"},
    3: set(),
}
TIER_NOTE = {
    0: "All constraints satisfied.",
    1: "No hospital met every constraint – relaxed time-budget / on-call / extras. Verify by radio.",
    2: "No capable hospital available – stabilise at nearest accepting ED and plan secondary transfer.",
    3: "NO hospital is accepting – escalate to control room / mutual aid immediately.",
}
WEIGHTS = {
    1: {"time": 50, "capacity": 15, "load": 10, "fit": 15, "oncall": 10},
    2: {"time": 40, "capacity": 20, "load": 15, "fit": 15, "oncall": 10},
    3: {"time": 25, "capacity": 25, "load": 25, "fit": 15, "oncall": 10},
    4: {"time": 25, "capacity": 25, "load": 25, "fit": 15, "oncall": 10},
}
VIOLATION_LABEL = {
    "accepting": "On diversion / not accepting",
    "capability": "Missing required specialty",
    "oncall": "Specialist not on call",
    "pediatric": "No paediatric capability",
    "extra": "Missing required facility",
    "bed": "No suitable bed",
    "bed_any": "No bed at all",
    "vent": "No free ventilator",
    "budget": "Outside golden-hour time budget",
}


def evaluate_hospital(h, prio: int, needs: dict, loc, sim_ts, reserved_here: bool = False):
    """Evaluate one hospital. ``reserved_here`` ignores bed counts (patient already holds a bed)."""
    eta, km = geo.eta_minutes(loc[0], loc[1], h["lat"], h["lng"], sim_ts)
    v = []

    def bad(code, text):
        v.append({"code": code, "label": VIOLATION_LABEL[code], "text": text})

    cap = needs.get("capability")
    if not h["accepting"]:
        bad("accepting", f"{h['name']} is on diversion")
    if cap:
        if not h["caps"].get(cap):
            bad("capability", f"No {cap} service")
        elif prio <= 2 and not h["oncall"].get(cap):
            bad("oncall", f"No {cap} specialist on call")
    if needs.get("pediatric") and not h["caps"].get("pediatric"):
        bad("pediatric", "No paediatric team")
    for ex in needs.get("extra", []):
        if ex == "operating_theatre":
            if h["or_free"] < 1 and not reserved_here:
                bad("extra", "No free operating theatre")
        elif ex == "ventilator_icu":
            if h["vent_free"] < 1 and not reserved_here:
                bad("extra", "No ventilator-capable ICU bed")
        elif not h["caps"].get(ex):
            bad("extra", f"No {ex.replace('_', ' ')}")
    if not reserved_here:
        lvl = needs.get("care_level", "ER")
        if lvl == "ICU" and h["icu_free"] < 1:
            bad("bed", "No free ICU bed")
        elif lvl == "ER" and h["er_free"] < 1:
            bad("bed", "No free ER bed")
        if h["icu_free"] + h["er_free"] < 1:
            bad("bed_any", "Hospital full")
        if needs.get("ventilator") and h["vent_free"] < 1:
            bad("vent", "No free ventilator")
    if needs.get("hard_budget") and eta > needs["budget_min"]:
        bad("budget", f"ETA {eta:.0f} min exceeds {needs['budget_min']} min budget")

    # ---- weighted score (always computed, used when feasible)
    w = WEIGHTS[prio]
    ref = max(10.0, needs.get("budget_min", 40) * 1.5)
    s_time = max(0.0, 1 - eta / ref)
    if needs.get("care_level") == "ICU":
        s_cap = min(1.0, h["icu_free"] / max(1.0, h["icu_total"] * 0.4))
    else:
        s_cap = min(1.0, h["er_free"] / max(1.0, h["er_total"] * 0.4))
    s_load = 1 - h["load_pct"] / 100
    lvl_score = {1: 1.0, 2: 0.7, 3: 0.4}.get(h["level"], 0.5)
    extras = [e for e in needs.get("extra", []) if e not in ("operating_theatre", "ventilator_icu")]
    extra_share = (sum(1 for e in extras if h["caps"].get(e)) / len(extras)) if extras else 1.0
    s_fit = 0.6 * lvl_score + 0.4 * extra_share
    s_oncall = 1.0 if (not cap or h["oncall"].get(cap)) else 0.0
    parts = {"time": s_time, "capacity": s_cap, "load": s_load, "fit": s_fit, "oncall": s_oncall}
    breakdown = {k: round(parts[k] * w[k], 1) for k in parts}
    score = round(sum(breakdown.values()), 1)
    return {
        "id": h["id"], "name": h["name"], "area": h["area"], "lat": h["lat"], "lng": h["lng"],
        "level": h["level"], "phone": h["phone"],
        "eta_min": round(eta, 1), "km": round(km, 1),
        "load_pct": h["load_pct"], "icu_free": h["icu_free"], "er_free": h["er_free"],
        "vent_free": h["vent_free"], "accepting": h["accepting"],
        "violations": v, "score": score, "breakdown": breakdown, "weights": w,
        "eligible_strict": not v,
    }


def rank_hospitals(hospitals, prio_str: str, needs: dict, loc, sim_ts, reserved_hospital_id=None, exclude_ids=()):
    prio = int(prio_str[1])
    evals = []
    for h in hospitals:
        if h["id"] in exclude_ids:
            continue
        evals.append(evaluate_hospital(h, prio, needs, loc, sim_ts, reserved_here=(h["id"] == reserved_hospital_id)))
    tier, feasible = 3, []
    for t in (0, 1, 2, 3):
        block = TIER_BLOCKING[t]
        feasible = [e for e in evals if not any(x["code"] in block for x in e["violations"])]
        if feasible:
            tier = t
            break
    if tier == 2:       # stabilise at nearest ED
        feasible.sort(key=lambda e: (e["eta_min"], -e["score"]))
    elif tier == 3:
        feasible = sorted(evals, key=lambda e: e["eta_min"])
    else:
        feasible.sort(key=lambda e: (-e["score"], e["eta_min"]))
    ids = {e["id"] for e in feasible}
    rejected = sorted([e for e in evals if e["id"] not in ids], key=lambda e: e["eta_min"])
    for e in evals:
        e["selected_tier_ok"] = e["id"] in ids
    best = feasible[0] if feasible else None
    return {
        "tier": tier, "relaxed": tier > 0, "note": TIER_NOTE[tier],
        "best": best, "ranked": feasible, "rejected": rejected, "all": evals,
    }


def pick_ambulance(ambulances, loc, needs, sim_ts):
    """Nearest suitable available ambulance. ALS/MICU for P1/P2; BLS preferred for low priority."""
    want_als = needs.get("ambulance") == "ALS"
    cands = []
    for a in ambulances:
        if a["status"] != "available":
            continue
        eta, km = geo.eta_minutes(a["lat"], a["lng"], loc[0], loc[1], sim_ts)
        is_adv = a["type"] in ("ALS", "MICU")
        penalty = 0.0
        if want_als and not is_adv:
            penalty += 40          # strongly avoid BLS for critical cases (still possible as fallback)
        if not want_als and is_adv:
            penalty += 4           # keep ALS capacity in reserve
        if needs.get("ventilator") and a["type"] != "MICU":
            penalty += 2
        cands.append((eta + penalty, eta, km, a, want_als and not is_adv))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    _, eta, km, a, downgraded = cands[0]
    return {"ambulance": a, "eta_min": round(eta, 1), "km": round(km, 1), "downgraded": downgraded}
