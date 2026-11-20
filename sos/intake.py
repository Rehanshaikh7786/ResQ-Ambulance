"""Turns a bystander report (questionnaire + photo analysis) into a triage-ready patient dict."""
from __future__ import annotations

from .triage import ValidationError

INCIDENTS = {
    "road":      {"label": "Road accident",        "type": "Trauma",      "mechanism": "RTA low speed"},
    "fall":      {"label": "Fall / injury",        "type": "Trauma",      "mechanism": "Fall <3m"},
    "assault":   {"label": "Stabbing / assault",   "type": "Trauma",      "mechanism": "Penetrating limb"},
    "fire":      {"label": "Fire / burn",          "type": "Burn",        "mechanism": "None"},
    "heart":     {"label": "Chest pain / heart",   "type": "Cardiac",     "mechanism": "None"},
    "stroke":    {"label": "Stroke signs",         "type": "Stroke",      "mechanism": "None"},
    "pregnancy": {"label": "Pregnancy / labour",   "type": "Obstetric",   "mechanism": "None"},
    "breathing": {"label": "Breathing trouble",    "type": "Respiratory", "mechanism": "None"},
    "other":     {"label": "Other medical",        "type": "Medical",     "mechanism": "None"},
}
AGE = {"child": 8, "adult": 35, "elderly": 72}
BLEED_REPORT = {"None": "None", "Some": "Mild", "Heavy": "Severe"}
BLEED_RANK = ["None", "Mild", "Moderate", "Severe"]
MECH_RANK = ["None", "Fall <3m", "RTA low speed", "Penetrating limb", "Fall >3m", "RTA high speed", "Penetrating torso/head"]


def _b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def build_patient(form: dict, vision: dict) -> dict:
    inc = form.get("incident", "other")
    if inc not in INCIDENTS:
        raise ValidationError("Please choose what happened")
    meta = INCIDENTS[inc]
    conscious = form.get("conscious", "Alert")
    if conscious not in ("Alert", "Confused", "Unresponsive"):
        conscious = "Alert"
    breathing = form.get("breathing", "Normal")
    if breathing not in ("Normal", "Difficult", "None"):
        breathing = "Normal"
    age_group = form.get("age_group", "adult")
    if age_group not in AGE:
        age_group = "adult"
    bleed_self = BLEED_REPORT.get(form.get("bleeding", "None"), "None")
    bleeding = max(bleed_self, vision.get("bleeding", "None"), key=BLEED_RANK.index)
    mech = meta["mechanism"]
    vm = vision.get("mechanism", "None")
    if meta["type"] == "Trauma" and vm in MECH_RANK and MECH_RANK.index(vm) > MECH_RANK.index(mech):
        mech = vm
    if vision.get("unresponsive_visible") and conscious == "Alert":
        conscious = "Unresponsive"
    tbsa = float(vision.get("burn_tbsa", 0) or 0)
    typ = meta["type"]
    if typ != "Burn" and vision.get("burn") and tbsa >= 5:
        typ = "Burn"                                  # photo shows a major burn even if caller chose another option
    if typ == "Burn" and tbsa == 0:
        tbsa = 5.0                                    # caller says burn but photo didn't size it – assume minor-moderate

    p = {
        "type": typ, "bystander": True, "age": AGE[age_group], "sex": "U",
        "name": form.get("name", ""), "notes": form.get("notes", ""),
        "avpu": conscious, "breathing": breathing, "bleeding": bleeding, "mechanism": mech,
        "tbsa": tbsa, "inhalation": _b(form.get("inhalation")) or bool(vision.get("inhalation_risk")),
        "airway_compromised": bool(vision.get("airway_concern")),
        "image_severity": int(vision.get("severity", 0)) if vision.get("provider") != "none" else 0,
        "image_note": (vision.get("injuries") or [vision.get("summary", "")])[0][:120] if vision.get("injury_detected") else "",
        "chest_pain": inc == "heart", "st_elevation": False,
        "fast_face": _b(form.get("fast_face")), "fast_arm": _b(form.get("fast_arm")), "fast_speech": _b(form.get("fast_speech")),
        "onset_min": int(form.get("onset_min") or 30),
        "imminent_delivery": _b(form.get("imminent_delivery")),
        "heavy_vaginal_bleeding": inc == "pregnancy" and form.get("bleeding") == "Heavy",
        "pregnant": inc == "pregnancy",
        "cardiac_arrest": conscious == "Unresponsive" and breathing == "None",
    }
    if inc == "pregnancy":
        p["sex"] = "F"
        p["age"] = 28 if age_group == "adult" else AGE[age_group]
    return p
