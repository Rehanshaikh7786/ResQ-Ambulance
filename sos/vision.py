"""
Injury detection from an incident photo.

Provider order
  1. Claude vision (set ANTHROPIC_API_KEY)  – real AI analysis of the photo.
  2. Local heuristic (always available)     – a *basic* colour/texture check using Pillow only.
                                              It is NOT medical AI; it is clearly labelled as such.

Output is always the same normalised dict so the rest of the system doesn't care which ran.
The result only ever raises or informs priority – a human responder always verifies on scene.
"""
from __future__ import annotations
import base64
import colorsys
import io
import json
import os
import urllib.error
import urllib.request

from PIL import Image, ImageFilter, ImageOps

MAX_BYTES = 8 * 1024 * 1024
ALLOWED = {"JPEG", "PNG", "WEBP", "MPO", "GIF", "BMP"}
BLEEDING_LEVELS = ["None", "Mild", "Moderate", "Severe"]
MECHANISMS = ["None", "Fall <3m", "Fall >3m", "RTA low speed", "RTA high speed", "Penetrating limb", "Penetrating torso/head"]

PROMPT = """You are an emergency-dispatch image triage assistant. A bystander sent this photo from an emergency scene.
Assess ONLY what is visible. Do not guess identity. Respond with ONE JSON object and nothing else:
{
 "injury_detected": bool,
 "severity": 0-4 (0 none visible, 1 minor, 2 moderate, 3 serious, 4 life-threatening),
 "confidence": 0.0-1.0,
 "bleeding": "None"|"Mild"|"Moderate"|"Severe",
 "burn": bool,
 "burn_tbsa_percent": number (estimated % body surface burned, 0 if none),
 "inhalation_risk": bool (soot on face / enclosed fire),
 "unresponsive_visible": bool (person lying motionless / unconscious),
 "airway_concern": bool,
 "person_count": integer (people visibly needing help),
 "mechanism": "None"|"Fall <3m"|"Fall >3m"|"RTA low speed"|"RTA high speed"|"Penetrating limb"|"Penetrating torso/head",
 "injuries": [short strings, e.g. "deep laceration left forearm"],
 "hazards": [short strings, e.g. "fire", "traffic", "fuel leak", "live wires"],
 "summary": "one factual sentence for the ambulance crew and receiving hospital"
}
If the image is unclear, not an emergency, or shows no person, set injury_detected false, severity 0, low confidence and say so in summary."""


# ----------------------------------------------------------------------------- helpers
def validate_image(data: bytes):
    """Return a PIL image (EXIF-rotated, RGB) or raise ValueError."""
    if not data:
        raise ValueError("Empty image")
    if len(data) > MAX_BYTES:
        raise ValueError("Image is larger than 8 MB")
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        im.load()
    except Exception:
        raise ValueError("File is not a valid image")
    if fmt not in ALLOWED:
        raise ValueError("Unsupported image format")
    im = ImageOps.exif_transpose(im).convert("RGB")
    return im


def save_normalised(im: Image.Image, path: str, max_side=1280):
    im = im.copy()
    im.thumbnail((max_side, max_side))
    im.save(path, "JPEG", quality=85, optimize=True)
    return path


def _empty(provider, summary, quality_ok=True, note=""):
    return {
        "provider": provider, "injury_detected": False, "severity": 0, "confidence": 0.0, "bleeding": "None",
        "burn": False, "burn_tbsa": 0.0, "inhalation_risk": False, "unresponsive_visible": False,
        "airway_concern": False, "person_count": 1, "mechanism": "None", "injuries": [], "hazards": [],
        "summary": summary, "quality": {"ok": quality_ok, "note": note},
    }


# ----------------------------------------------------------------------------- Claude
def _claude(im: Image.Image):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    buf = io.BytesIO()
    small = im.copy()
    small.thumbnail((1280, 1280))
    small.save(buf, "JPEG", quality=85)
    payload = {
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 700,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": PROMPT}]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(os.environ.get("VISION_TIMEOUT", "25"))) as r:
        body = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    return parse_model_json(text)


def parse_model_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("No JSON in model reply")
    d = json.loads(text[a:b + 1])
    out = _empty("claude", str(d.get("summary", ""))[:300])
    out["severity"] = int(max(0, min(4, int(d.get("severity", 0) or 0))))
    out["injury_detected"] = bool(d.get("injury_detected", out["severity"] > 0)) or out["severity"] > 0
    out["confidence"] = round(float(max(0.0, min(1.0, float(d.get("confidence", 0.5) or 0)))), 2)
    b_ = d.get("bleeding", "None")
    out["bleeding"] = b_ if b_ in BLEEDING_LEVELS else "None"
    out["burn"] = bool(d.get("burn", False))
    out["burn_tbsa"] = float(max(0, min(100, float(d.get("burn_tbsa_percent", 0) or 0)))) if out["burn"] else 0.0
    out["inhalation_risk"] = bool(d.get("inhalation_risk", False))
    out["unresponsive_visible"] = bool(d.get("unresponsive_visible", False))
    out["airway_concern"] = bool(d.get("airway_concern", False))
    out["person_count"] = int(max(1, min(50, int(d.get("person_count", 1) or 1))))
    m = d.get("mechanism", "None")
    out["mechanism"] = m if m in MECHANISMS else "None"
    out["injuries"] = [str(x)[:80] for x in (d.get("injuries") or [])][:8]
    out["hazards"] = [str(x)[:60] for x in (d.get("hazards") or [])][:6]
    return out


# ----------------------------------------------------------------------------- heuristic
def _heuristic(im: Image.Image):
    """Colour + texture check: blood-red area, char/dark area, blur and exposure."""
    s = im.copy()
    s.thumbnail((220, 220))
    w, h = s.size
    total = max(1, w * h)
    hsv = s.convert("HSV")
    blood = char = bright = dark = 0
    raw = hsv.tobytes()
    for hh, ss, vv in zip(raw[0::3], raw[1::3], raw[2::3]):   # PIL HSV: each channel 0-255
        if (hh <= 10 or hh >= 245) and ss >= 150 and 35 <= vv <= 210:
            blood += 1                          # saturated dark-ish red ≈ blood
        if vv < 40:
            char += 1
        if vv > 240:
            bright += 1
        if vv < 25:
            dark += 1
    blood_r, char_r = blood / total, char / total
    edges = s.convert("L").filter(ImageFilter.FIND_EDGES)
    sharp = sum(edges.tobytes()) / total
    quality_ok, note = True, ""
    if dark / total > 0.7:
        quality_ok, note = False, "Photo is very dark"
    elif bright / total > 0.6:
        quality_ok, note = False, "Photo is over-exposed"
    elif sharp < 2.5:
        quality_ok, note = False, "Photo looks blurry or empty"

    out = _empty("heuristic", "", quality_ok, note)
    out["bleeding"] = "Severe" if blood_r > 0.07 else "Moderate" if blood_r > 0.025 else "Mild" if blood_r > 0.006 else "None"
    out["burn"] = char_r > 0.35 and blood_r < 0.02
    out["burn_tbsa"] = 10.0 if out["burn"] else 0.0
    sev = {"None": 0, "Mild": 1, "Moderate": 2, "Severe": 3}[out["bleeding"]]
    if out["burn"]:
        sev = max(sev, 2)
    out["severity"] = sev
    out["injury_detected"] = sev > 0
    out["confidence"] = round(min(0.55, 0.25 + 4 * blood_r + 0.3 * char_r), 2) if sev else 0.2
    if out["bleeding"] != "None":
        out["injuries"] = [f"possible visible blood ({blood_r * 100:.1f}% of image)"]
    if out["burn"]:
        out["injuries"].append("large dark/charred area – possible burn")
    out["summary"] = ("Basic colour analysis suggests " + (", ".join(out["injuries"]) if out["injuries"] else "no obvious blood or burn")
                      + ". Not a medical diagnosis – crew will verify.")
    if not quality_ok:
        out["summary"] += f" ({note}.)"
    return out


# ----------------------------------------------------------------------------- public
def analyze(im: Image.Image) -> dict:
    """Analyse a PIL image. Never raises: falls back to the heuristic if the AI call fails."""
    err = None
    try:
        res = _claude(im)
        if res is not None:
            res["quality"] = {"ok": True, "note": ""}
            return res
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as ex:
        err = f"AI unavailable ({type(ex).__name__}); used basic check"
    out = _heuristic(im)
    if err:
        out["quality"]["note"] = (out["quality"]["note"] + " " + err).strip()
    return out


def no_photo():
    out = _empty("none", "No photo supplied – assessment based on caller answers only.")
    out["quality"] = {"ok": True, "note": "no photo"}
    return out


def provider_name():
    return "Claude vision" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "Basic local check"
