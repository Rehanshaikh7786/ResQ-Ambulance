import io
import json
import os
import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOS_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("SOS_UPLOADS", str(tmp_path / "up"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("STAFF_PIN", raising=False)
    from app import create_app
    app = create_app(start_engine=False, reset=True)
    app.config["TESTING"] = True
    from sos import engine
    with app.test_client() as c:
        c.engine = engine
        engine.set_hospital_mode("manual")
        yield c


def img_bytes(kind="blood"):
    im = Image.new("RGB", (320, 240), (150, 160, 150))
    d = ImageDraw.Draw(im)
    for i in range(0, 320, 16):                          # texture so it isn't "blurry"
        d.line([(i, 0), (320 - i, 240)], fill=(110, 120, 110), width=2)
    if kind == "blood":
        d.ellipse([60, 40, 260, 200], fill=(150, 10, 10))
    elif kind == "dark":
        im = Image.new("RGB", (320, 240), (5, 5, 5))
    b = io.BytesIO()
    im.save(b, "JPEG")
    b.seek(0)
    return b


LOC = {"lat": "18.628", "lng": "73.801"}


def report(client, incident="road", photo="blood", **kw):
    data = {**LOC, "incident": incident, "conscious": "Alert", "breathing": "Normal", "bleeding": "Some",
            "age_group": "adult", "name": "Test", "phone": "+91 99999 99999", **kw}
    if photo:
        data["photo"] = (img_bytes(photo), "p.jpg")
    return client.post("/api/report", data=data, content_type="multipart/form-data")


def tick(client, minutes, drift=False):
    client.engine.tick(extra=minutes * 60, drift=drift)


# ---------------------------------------------------------------- triage / vision
def test_bystander_triage_escalates():
    from sos.triage import normalise, triage
    t = triage(normalise({"type": "Trauma", "bystander": True, "bleeding": "Severe"}))
    assert t["priority"] == "P1"
    t = triage(normalise({"type": "Medical", "bystander": True}))
    assert t["priority"] == "P4"
    t = triage(normalise({"type": "Medical", "bystander": True, "breathing": "None", "avpu": "Unresponsive"}))
    assert t["priority"] == "P1" and t["needs"]["ventilator"]


def test_vision_heuristic_detects_blood_and_rejects_bad_files():
    from sos import vision
    r = vision.analyze(vision.validate_image(img_bytes("blood").read()))
    assert r["provider"] == "heuristic" and r["injury_detected"] and r["bleeding"] in ("Moderate", "Severe")
    clean = vision.analyze(vision.validate_image(img_bytes("plain").read()))
    assert not clean["injury_detected"]
    dark = vision.analyze(vision.validate_image(img_bytes("dark").read()))
    assert not dark["quality"]["ok"]
    with pytest.raises(ValueError):
        vision.validate_image(b"not an image")


def test_parse_model_json():
    from sos import vision
    r = vision.parse_model_json('Here: {"injury_detected": true, "severity": 3, "bleeding": "Severe", "confidence": 0.9, '
                                '"summary": "deep cut", "injuries": ["arm laceration"], "mechanism": "bogus"} thanks')
    assert r["severity"] == 3 and r["bleeding"] == "Severe" and r["mechanism"] == "None" and r["provider"] == "claude"


def test_claude_failure_falls_back(monkeypatch):
    from sos import vision
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(vision, "_claude", lambda im: (_ for _ in ()).throw(OSError("offline")))
    r = vision.analyze(vision.validate_image(img_bytes("blood").read()))
    assert r["provider"] == "heuristic" and "AI unavailable" in r["quality"]["note"]


# ---------------------------------------------------------------- full flow
def test_full_flow_photo_to_hospital(client):
    r = report(client)
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    code = out["code"]
    assert out["vision"]["injury_detected"] and out["ambulance"]
    v = client.get(f"/api/track/{code}").get_json()
    assert v["status"] == "awaiting_hospital"            # ambulance picked, hospital alerted, NOT yet dispatched
    assert v["ambulance"]["status"] == "reserved"
    assert v["alert"]["status"] == "pending"
    hid = v["hospital"]["id"]
    assert v["steps"][2]["done"] and v["steps"][3]["done"] and not v["steps"][5]["done"]
    # hospital sees alert with photo, then approves
    hv = client.get(f"/api/hospital/{hid}/view").get_json()
    assert len(hv["pending"]) == 1 and hv["pending"][0]["photo"].startswith("/uploads/")
    assert client.get(hv["pending"][0]["photo"]).status_code == 200
    d = client.post(f"/api/alerts/{hv['pending'][0]['alert_id']}/decide", json={"decision": "approve"})
    assert d.status_code == 200
    v = client.get(f"/api/track/{code}").get_json()
    assert v["status"] == "dispatched" and v["ambulance"]["status"] == "dispatched" and v["eta_remaining_min"] is not None
    tick(client, 90)
    v = client.get(f"/api/track/{code}").get_json()
    assert v["status"] == "completed"
    assert all(s["done"] for s in v["steps"])
    st = client.get("/api/state").get_json()
    assert next(a for a in st["ambulances"] if a["id"] == out["ambulance"])["status"] in ("returning", "available")
    tick(client, 60)
    st = client.get("/api/state").get_json()
    assert all(a["status"] == "available" for a in st["ambulances"])


def test_ambulance_not_dispatched_until_approval(client):
    code = report(client).get_json()["code"]
    tick(client, 30)
    v = client.get(f"/api/track/{code}").get_json()
    assert v["status"] in ("awaiting_hospital",) or v["alert"]["status"] != "pending"   # timeout may have escalated, never dispatched
    assert v["status"] != "dispatched"


def test_decline_escalates_then_no_hospital(client):
    code = report(client).get_json()["code"]
    first = client.get(f"/api/track/{code}").get_json()["hospital"]["id"]
    seen = {first}
    for _ in range(12):
        v = client.get(f"/api/track/{code}").get_json()
        if v["status"] == "no_hospital":
            break
        hid = v["hospital"]["id"]
        seen.add(hid)
        al = client.get(f"/api/hospital/{hid}/view").get_json()["pending"][0]
        client.post(f"/api/alerts/{al['alert_id']}/decide", json={"decision": "decline", "reason": "Full"})
    v = client.get(f"/api/track/{code}").get_json()
    assert v["status"] == "no_hospital" and len(seen) >= 2
    # control room force-assigns
    r = client.post(f"/api/cases/{code}/assign", json={"hospital_id": 1})
    assert r.status_code == 200
    assert client.get(f"/api/track/{code}").get_json()["status"] == "dispatched"


def test_timeout_escalates(client, monkeypatch):
    monkeypatch.setenv("SOS_ALERT_TIMEOUT", "0")
    code = report(client).get_json()["code"]
    first = client.get(f"/api/track/{code}").get_json()["hospital"]["id"]
    client.engine.tick(drift=False)
    v = client.get(f"/api/track/{code}").get_json()
    assert v["hospital"] is None or v["hospital"]["id"] != first or v["status"] == "no_hospital"


def test_auto_mode_completes_without_human(client, monkeypatch):
    monkeypatch.setenv("SOS_AUTO_DELAY", "0")
    client.engine.set_hospital_mode("auto")
    code = report(client).get_json()["code"]
    client.engine.tick(drift=False)
    assert client.get(f"/api/track/{code}").get_json()["status"] == "dispatched"


def test_constraints_reject_icu_full_hospital(client):
    code = report(client, incident="road", bleeding="Heavy").get_json()["code"]
    c = client.get(f"/api/cases/{code}").get_json()
    assert c["priority"] == "P1"
    metro = next(e for e in c["route"]["rejected"] if e["id"] == 2)        # MetroCare has 0 ICU / 0 vent
    assert any(v["code"] in ("bed", "vent") for v in metro["violations"])
    assert c["hospital"]["id"] != 2


def test_diversion_reroutes_inflight(client):
    code = report(client).get_json()["code"]
    hid = client.get(f"/api/track/{code}").get_json()["hospital"]["id"]
    al = client.get(f"/api/hospital/{hid}/view").get_json()["pending"][0]
    client.post(f"/api/alerts/{al['alert_id']}/decide", json={"decision": "approve"})
    r = client.patch(f"/api/hospitals/{hid}", json={"accepting": False})
    assert r.status_code == 200
    v = client.get(f"/api/track/{code}").get_json()
    assert v["hospital"]["id"] != hid and v["reroutes"] >= 1


def test_cancel_frees_reserved_ambulance(client):
    out = report(client).get_json()
    client.post(f"/api/track/{out['code']}/cancel")
    st = client.get("/api/state").get_json()
    assert next(a for a in st["ambulances"] if a["id"] == out["ambulance"])["status"] == "available"


def test_pediatric_requires_pediatric_hospital(client):
    code = report(client, incident="fall", age_group="child", bleeding="Heavy").get_json()["code"]
    c = client.get(f"/api/cases/{code}").get_json()
    assert c["triage"]["needs"]["pediatric"]
    assert c["hospital"]["id"] in (1, 2, 4, 6, 8)      # only hospitals with paediatric capability


def test_no_photo_report_works(client):
    r = report(client, photo=None, incident="heart", bleeding="None")
    assert r.status_code == 201 and r.get_json()["vision"]["provider"] == "none"


def test_validation(client):
    assert report(client, lat="12.9", lng="77.5").status_code == 422          # outside service area
    r = client.post("/api/report", data={"incident": "road"}, content_type="multipart/form-data")
    assert r.status_code == 400
    bad = client.post("/api/report", data={**LOC, "incident": "road", "photo": (io.BytesIO(b"junk"), "x.jpg")},
                      content_type="multipart/form-data")
    assert bad.status_code == 400
    assert client.post("/api/report", data={**LOC, "incident": "road", "website": "spam"}, content_type="multipart/form-data").status_code == 400
    assert client.get("/api/track/NOPE").status_code == 404
    assert client.get("/uploads/../app.py").status_code == 404


def test_surge_analytics_csv(client):
    client.engine.set_hospital_mode("auto")
    n = client.post("/api/sim/surge", json={"n": 5}).get_json()["created"]
    assert len(n) == 5
    a = client.get("/api/analytics").get_json()
    assert a["totals"]["cases"] >= 60 and a["times"]["golden_compliance"] is not None
    assert client.get("/api/export.csv").data.startswith(b"case_id")


def test_staff_pin_protects_staff_not_public(client, monkeypatch):
    monkeypatch.setenv("STAFF_PIN", "1234")
    assert client.get("/api/state").status_code == 401
    assert client.get("/control", follow_redirects=False).status_code == 302
    assert client.get("/report").status_code == 200 and client.get("/api/config").status_code == 200
    client.post("/login", data={"pin": "1234"})
    assert client.get("/api/state").status_code == 200
