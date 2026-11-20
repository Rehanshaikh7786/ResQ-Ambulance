"""
Explainable triage engine.

Adult physiology uses the Royal College of Physicians NEWS2 scale (2017), extended with
shock index, age modifiers and condition-specific red flags (STEMI, FAST stroke, burns TBSA,
trauma mechanism, obstetric emergencies).  Children (<16) use age-banded reference ranges
because NEWS2 is not validated in paediatrics.

Every point awarded is returned in ``factors`` so a dispatcher can see *why* a priority
was assigned.  ACADEMIC DECISION-SUPPORT PROTOTYPE - NOT CLINICALLY VALIDATED.
"""
from __future__ import annotations

EMERGENCY_TYPES = ["Trauma", "Cardiac", "Stroke", "Burn", "Obstetric", "Respiratory", "Medical"]
AVPU = ["Alert", "Confused", "Voice", "Pain", "Unresponsive"]
MECHANISMS = ["None", "Fall <3m", "Fall >3m", "RTA low speed", "RTA high speed", "Penetrating limb", "Penetrating torso/head"]
BLEEDING = ["None", "Mild", "Moderate", "Severe"]

PRIORITY_LABEL = {
    "P1": "P1 CRITICAL",
    "P2": "P2 URGENT",
    "P3": "P3 MODERATE",
    "P4": "P4 LOW",
}

# Transport time budget (minutes) before definitive care is compromised.  Derived from
# commonly cited targets: trauma golden hour, door-to-balloon <=90, stroke thrombolysis window.
TRANSPORT_BUDGET = {
    "Trauma": 35, "Cardiac": 45, "Stroke": 40, "Burn": 60,
    "Obstetric": 45, "Respiratory": 45, "Medical": 60,
}
# Total clock target (minutes from emergency call to hospital door).
GOLDEN_TARGET = {
    "Trauma": 60, "Cardiac": 90, "Stroke": 60, "Burn": 90,
    "Obstetric": 75, "Respiratory": 75, "Medical": 90,
}

REQUIRED_CAPABILITY = {
    "Trauma": "trauma", "Cardiac": "cardiac", "Stroke": "stroke", "Burn": "burn",
    "Obstetric": "obstetric", "Respiratory": None, "Medical": None,
}


class ValidationError(ValueError):
    pass


# ----------------------------------------------------------------------------- input
def _num(d, key, default, lo, hi, cast=float, required=False):
    v = d.get(key, None)
    if v in (None, ""):
        if required:
            raise ValidationError(f"'{key}' is required")
        return default
    try:
        v = cast(v)
    except (TypeError, ValueError):
        raise ValidationError(f"'{key}' must be a number")
    if v < lo or v > hi:
        raise ValidationError(f"'{key}' must be between {lo} and {hi}")
    return v


def _flag(d, key):
    v = d.get(key, False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def normalise(d: dict) -> dict:
    """Validate and coerce raw input (JSON or form) into a clean patient dict."""
    d = d or {}
    etype = d.get("type", "Medical")
    if etype not in EMERGENCY_TYPES:
        raise ValidationError(f"'type' must be one of {EMERGENCY_TYPES}")
    avpu = d.get("avpu", d.get("consciousness", "Alert"))
    if avpu not in AVPU:
        raise ValidationError(f"'avpu' must be one of {AVPU}")
    bleeding = d.get("bleeding", "None")
    if bleeding not in BLEEDING:
        raise ValidationError(f"'bleeding' must be one of {BLEEDING}")
    mech = d.get("mechanism", "None")
    if mech not in MECHANISMS:
        raise ValidationError(f"'mechanism' must be one of {MECHANISMS}")
    sex = d.get("sex", "U")
    if sex not in ("M", "F", "U"):
        sex = "U"
    p = {
        "type": etype,
        "name": str(d.get("name", "") or "")[:60],
        "notes": str(d.get("notes", "") or "")[:300],
        "age": _num(d, "age", 35, 0, 120, int),
        "sex": sex,
        "hr": _num(d, "hr", 80, 0, 300, int),
        "rr": _num(d, "rr", 16, 0, 80, int),
        "spo2": _num(d, "spo2", 98, 40, 100, int),
        "sbp": _num(d, "sbp", _num(d, "sys", 120, 0, 300, int), 0, 300, int),
        "temp": _num(d, "temp", 36.8, 28, 44, float),
        "on_oxygen": _flag(d, "on_oxygen"),
        "avpu": avpu,
        "bleeding": bleeding,
        "mechanism": mech,
        "chest_pain": _flag(d, "chest_pain"),
        "st_elevation": _flag(d, "st_elevation"),
        "cardiac_arrest": _flag(d, "cardiac_arrest"),
        "airway_compromised": _flag(d, "airway_compromised"),
        "seizure": _flag(d, "seizure"),
        "anaphylaxis": _flag(d, "anaphylaxis"),
        "fast_face": _flag(d, "fast_face"),
        "fast_arm": _flag(d, "fast_arm"),
        "fast_speech": _flag(d, "fast_speech"),
        "onset_min": _num(d, "onset_min", 60, 0, 5000, int),
        "tbsa": _num(d, "tbsa", 0, 0, 100, float),
        "inhalation": _flag(d, "inhalation"),
        "pregnant": _flag(d, "pregnant") or etype == "Obstetric",
        "imminent_delivery": _flag(d, "imminent_delivery"),
        "heavy_vaginal_bleeding": _flag(d, "heavy_vaginal_bleeding"),
        "bystander": _flag(d, "bystander"),
        "breathing": d.get("breathing", "Normal") if d.get("breathing", "Normal") in ("Normal", "Difficult", "None") else "Normal",
        "image_severity": _num(d, "image_severity", 0, 0, 4, int),
        "image_note": str(d.get("image_note", "") or "")[:200],
    }
    # HR/RR/SBP of zero are only legitimate in arrest; otherwise treat as invalid input.
    if not p["cardiac_arrest"] and not p["bystander"]:
        for k, lo in (("hr", 10), ("rr", 1), ("sbp", 30)):
            if p[k] < lo:
                raise ValidationError(f"'{k}' looks invalid (<{lo}); tick 'cardiac arrest' if the patient has no pulse")
    return p


# ----------------------------------------------------------------------------- scoring
def _news2_adult(p):
    f = []

    def add(name, value, pts, note):
        f.append({"name": name, "value": value, "points": pts, "note": note, "group": "physiology"})

    rr, spo2, sbp, hr, t = p["rr"], p["spo2"], p["sbp"], p["hr"], p["temp"]
    pts = 3 if rr <= 8 else 1 if rr <= 11 else 0 if rr <= 20 else 2 if rr <= 24 else 3
    add("Respiratory rate", f"{rr}/min", pts, "NEWS2")
    pts = 3 if spo2 <= 91 else 2 if spo2 <= 93 else 1 if spo2 <= 95 else 0
    add("SpO₂", f"{spo2}%", pts, "NEWS2 scale 1")
    add("Supplemental oxygen", "yes" if p["on_oxygen"] else "no", 2 if p["on_oxygen"] else 0, "NEWS2")
    pts = 3 if sbp <= 90 else 2 if sbp <= 100 else 1 if sbp <= 110 else 0 if sbp <= 219 else 3
    add("Systolic BP", f"{sbp} mmHg", pts, "NEWS2")
    pts = 3 if hr <= 40 else 1 if hr <= 50 else 0 if hr <= 90 else 1 if hr <= 110 else 2 if hr <= 130 else 3
    add("Heart rate", f"{hr}/min", pts, "NEWS2")
    add("Consciousness", p["avpu"], 0 if p["avpu"] == "Alert" else 3, "NEWS2 (new confusion or CVPU = 3)")
    pts = 3 if t <= 35.0 else 1 if t <= 36.0 else 0 if t <= 38.0 else 1 if t <= 39.0 else 2
    add("Temperature", f"{t:.1f}°C", pts, "NEWS2")
    return f


def _peds_ranges(age):
    """Simplified age-banded reference ranges (HR, RR, min SBP) for children."""
    if age < 1:
        return (100, 160), (30, 60), 70
    if age < 5:
        return (80, 140), (20, 40), 70 + 2 * age
    if age < 12:
        return (70, 120), (16, 30), 80 + 2 * age
    return (60, 110), (12, 25), 90


def _score_peds(p):
    f = []
    (hlo, hhi), (rlo, rhi), smin = _peds_ranges(p["age"])

    def dev(v, lo, hi):
        if lo <= v <= hi:
            return 0
        span = hi - lo
        d = (lo - v) if v < lo else (v - hi)
        return 2 if d <= 0.35 * span else 3

    def add(name, value, pts, note):
        f.append({"name": name, "value": value, "points": pts, "note": note, "group": "physiology"})

    add("Heart rate", f"{p['hr']}/min", dev(p["hr"], hlo, hhi), f"age range {hlo}-{hhi}")
    add("Respiratory rate", f"{p['rr']}/min", dev(p["rr"], rlo, rhi), f"age range {rlo}-{rhi}")
    spo2 = p["spo2"]
    add("SpO₂", f"{spo2}%", 3 if spo2 <= 91 else 2 if spo2 <= 93 else 1 if spo2 <= 95 else 0, "paediatric SpO₂")
    sbp = p["sbp"]
    add("Systolic BP", f"{sbp} mmHg", 3 if sbp < smin - 10 else 2 if sbp < smin else 0, f"min for age ≈{smin}")
    add("Supplemental oxygen", "yes" if p["on_oxygen"] else "no", 2 if p["on_oxygen"] else 0, "")
    add("Consciousness", p["avpu"], 0 if p["avpu"] == "Alert" else 3, "AVPU")
    t = p["temp"]
    add("Temperature", f"{t:.1f}°C", 3 if t <= 35 else 1 if (t >= 38.5 or t <= 36) else 0, "")
    return f


def triage(p: dict) -> dict:
    """Return a full, explainable triage assessment for a normalised patient dict."""
    child = p["age"] < 16
    arrest = p["cardiac_arrest"]
    factors = _score_peds(p) if child else _news2_adult(p)
    if p["bystander"]:  # no measured vitals – rely on photo + reported signs
        factors = []
    if arrest:  # vitals meaningless in arrest
        factors = [dict(x, points=0, note="not scored (arrest)") for x in factors]

    overrides = []       # reasons that force a minimum priority
    min_prio = 4

    def force(level, why):
        nonlocal min_prio
        overrides.append({"priority": f"P{level}", "reason": why})
        min_prio = min(min_prio, level)

    def bump(name, value, pts, note, group="modifier"):
        if pts:
            factors.append({"name": name, "value": value, "points": pts, "note": note, "group": group})

    # --- shock index
    if not arrest and not p["bystander"] and p["sbp"] > 0:
        si = p["hr"] / p["sbp"]
        if si >= 1.4:
            bump("Shock index", f"{si:.2f}", 3, "HR/SBP ≥ 1.4 (severe shock)")
        elif si >= 1.0:
            bump("Shock index", f"{si:.2f}", 2, "HR/SBP ≥ 1.0 (compensated shock)")
        elif si >= 0.9:
            bump("Shock index", f"{si:.2f}", 1, "HR/SBP ≥ 0.9")
    # --- age
    if p["age"] >= 80:
        bump("Age", f"{p['age']}y", 2, "frail / high mortality risk")
    elif p["age"] >= 65:
        bump("Age", f"{p['age']}y", 1, "older adult")
    elif p["age"] < 1:
        bump("Age", f"{p['age']}y", 2, "infant")
    # --- bleeding
    if p["bleeding"] == "Severe":
        bump("Bleeding", "severe", 4, "uncontrolled haemorrhage", "condition")
        force(1, "Severe / uncontrolled bleeding")
    elif p["bleeding"] == "Moderate":
        bump("Bleeding", "moderate", 2, "significant blood loss", "condition")
    elif p["bleeding"] == "Mild":
        bump("Bleeding", "mild", 1, "minor blood loss", "condition")

    # --- universal red flags
    if arrest:
        bump("Cardiac arrest", "yes", 10, "no pulse / not breathing", "condition")
        force(1, "Cardiac arrest")
    if p["airway_compromised"]:
        bump("Airway", "compromised", 4, "airway at risk", "condition")
        force(1, "Compromised airway")
    if p["breathing"] == "None" and not arrest:
        bump("Breathing", "not breathing", 6, "reported by caller", "condition")
        force(1, "Not breathing (caller report)")
    elif p["breathing"] == "Difficult":
        bump("Breathing", "difficult", 3, "reported by caller", "condition")
        force(2, "Breathing difficulty (caller report)")
    sev = p["image_severity"]
    if sev:
        bump("Photo assessment", f"severity {sev}/4", {1: 1, 2: 2, 3: 4, 4: 6}[sev], p["image_note"] or "image analysis", "condition")
        if sev >= 4:
            force(1, "Life-threatening injury visible in photo")
        elif sev == 3:
            force(2, "Serious injury visible in photo")
    if p["anaphylaxis"]:
        bump("Anaphylaxis", "yes", 4, "severe allergic reaction", "condition")
        force(1, "Anaphylaxis")
    if p["seizure"]:
        bump("Active seizure", "yes", 3, "ongoing / recent seizure", "condition")
        force(2, "Active seizure")
    if p["bystander"] and p["avpu"] != "Alert":
        bump("Consciousness", p["avpu"], {"Confused": 2, "Voice": 3, "Pain": 5, "Unresponsive": 6}[p["avpu"]], "caller report", "condition")
    if p["avpu"] in ("Pain", "Unresponsive"):
        force(1, f"Responds only to {p['avpu'].lower()}")

    # --- condition specific
    t = p["type"]
    if t == "Trauma":
        m = p["mechanism"]
        pts = {"None": 0, "Fall <3m": 1, "RTA low speed": 1, "Penetrating limb": 2,
               "Fall >3m": 3, "RTA high speed": 3, "Penetrating torso/head": 5}[m]
        bump("Mechanism of injury", m, pts, "trauma triage criteria", "condition")
        if m == "Penetrating torso/head":
            force(1, "Penetrating torso/head injury")
        elif m in ("RTA high speed", "Fall >3m"):
            force(2, f"High-energy mechanism ({m})")
    elif t == "Cardiac":
        if p["chest_pain"]:
            bump("Chest pain", "yes", 2, "possible ACS", "condition")
            force(2, "Chest pain – possible ACS")
        if p["st_elevation"]:
            bump("ECG ST-elevation", "yes", 5, "STEMI – time critical", "condition")
            force(1, "STEMI (ST-elevation on ECG)")
    elif t == "Stroke":
        fast = sum([p["fast_face"], p["fast_arm"], p["fast_speech"]])
        in_window = p["onset_min"] <= 270
        if fast:
            bump("FAST positive", f"{fast}/3", fast * 2, "face / arm / speech", "condition")
            if fast >= 2 and in_window:
                force(1, f"Stroke (FAST {fast}/3) inside thrombolysis window")
            else:
                force(2, "Suspected stroke")
        if fast and not in_window:
            bump("Onset time", f"{p['onset_min']} min", 0, "outside 4.5 h thrombolysis window", "condition")
    elif t == "Burn":
        tb = p["tbsa"]
        pts = 0 if tb == 0 else 1 if tb < 10 else 3 if tb < 20 else 5
        bump("Burn surface area", f"{tb:g}%", pts, "TBSA", "condition")
        if p["inhalation"]:
            bump("Inhalation injury", "yes", 4, "airway burn risk", "condition")
            force(1, "Inhalation injury")
        if tb >= 20 or (child and tb >= 10):
            force(1, f"Major burn (TBSA {tb:g}%)")
        elif tb >= 10:
            force(2, f"Significant burn (TBSA {tb:g}%)")
    elif t == "Obstetric":
        if p["heavy_vaginal_bleeding"]:
            bump("Antepartum/postpartum haemorrhage", "yes", 4, "obstetric haemorrhage", "condition")
            force(1, "Obstetric haemorrhage")
        if p["imminent_delivery"]:
            bump("Imminent delivery", "yes", 2, "crowning / contractions <2 min", "condition")
            force(2, "Imminent delivery")
        if p["seizure"]:
            force(1, "Possible eclampsia")
    elif t == "Respiratory":
        if p["spo2"] < 90 and not arrest:
            force(2, "Hypoxia (SpO₂ <90%)")

    points = sum(x["points"] for x in factors)
    news_points = sum(x["points"] for x in factors if x["group"] == "physiology")
    single_red = any(x["group"] == "physiology" and x["points"] == 3 for x in factors)

    # --- priority from points
    t1, t2, t3 = (7, 4, 2) if p["bystander"] else (9, 6, 3)
    if points >= t1:
        prio = 1
    elif points >= t2 or (single_red and not arrest):
        prio = 2
    elif points >= t3:
        prio = 3
    else:
        prio = 4
    if single_red and prio > 2:
        prio = 2
    prio = min(prio, min_prio)
    risk = min(100, round(points * 8))
    lo, hi = {1: (72, 100), 2: (45, 69), 3: (25, 44), 4: (0, 24)}[prio]
    risk = max(lo, min(hi, risk))

    # --- resource requirements
    cap = REQUIRED_CAPABILITY[t]
    needs = {
        "capability": cap,
        "extra": [],
        "care_level": "ICU" if prio == 1 else "ER" if prio <= 3 else "ER",
        "ventilator": bool(
            arrest or p["airway_compromised"] or p["avpu"] in ("Pain", "Unresponsive") or p["breathing"] == "None"
            or (p["spo2"] < 85) or (prio == 1 and p["rr"] <= 8)
        ),
        "pediatric": child,
        "ambulance": "ALS" if prio <= 2 else "ANY",
        "budget_min": TRANSPORT_BUDGET[t] if prio <= 2 else TRANSPORT_BUDGET[t] + 25,
        "golden_target_min": GOLDEN_TARGET[t],
        "hard_budget": prio <= 2,
    }
    if t == "Cardiac" and (p["st_elevation"] or arrest):
        needs["extra"].append("cath_lab")
    if t == "Stroke":
        needs["extra"].append("ct")
    if t == "Trauma":
        needs["extra"].append("ct")
        if prio == 1:
            needs["extra"].append("operating_theatre")
    if t == "Obstetric" and prio <= 2:
        needs["extra"].append("nicu")
    if t == "Burn" and p["inhalation"]:
        needs["extra"].append("ventilator_icu")

    prealert = _prealert(p, prio, needs)
    summary = _summary(p, prio, overrides, factors)
    return {
        "priority": f"P{prio}",
        "label": PRIORITY_LABEL[f"P{prio}"],
        "risk": risk,
        "points": points,
        "news2": news_points,
        "scale": "Bystander report + photo" if p["bystander"] else ("Paediatric age-banded" if child else "NEWS2 + modifiers"),
        "factors": [x for x in factors if x["points"] or x["group"] == "physiology"],
        "overrides": overrides,
        "needs": needs,
        "prealert": prealert,
        "summary": summary,
    }


def _prealert(p, prio, needs):
    a = []
    t = p["type"]
    if p["cardiac_arrest"]:
        a.append("Resuscitation team on standby at door")
    if t == "Trauma" and prio <= 2:
        a.append("Activate trauma team; prepare blood products & CT")
    if t == "Cardiac" and p["st_elevation"]:
        a.append("Activate cath-lab (STEMI) – bypass ED where possible")
    if t == "Stroke":
        a.append("Stroke team alert; keep CT scanner free; note last-known-well time")
    if t == "Burn":
        a.append("Burn unit alert; prepare fluid resuscitation" + ("; airway/ICU bed for inhalation injury" if p["inhalation"] else ""))
    if t == "Obstetric":
        a.append("Obstetric team + NICU alert" + ("; massive haemorrhage protocol" if p["heavy_vaginal_bleeding"] else ""))
    if needs["pediatric"]:
        a.append("Paediatric team required")
    if needs["ventilator"]:
        a.append("Ventilator / airway equipment ready")
    if not a:
        a.append("Standard ED reception")
    return a


def _summary(p, prio, overrides, factors):
    top = sorted([x for x in factors if x["points"]], key=lambda x: -x["points"])[:3]
    parts = [f"{x['name']} {x['value']}" for x in top]
    base = f"{p['age']}y {p['type'].lower()} case"
    if overrides:
        base += f"; escalated by: {overrides[0]['reason']}"
    elif parts:
        base += "; driven by " + ", ".join(parts)
    return base
