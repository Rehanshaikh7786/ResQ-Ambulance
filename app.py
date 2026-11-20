"""GoldenHour SOS – photo-based emergency reporting, AI injury detection, nearest-ambulance
reservation, hospital approval workflow and live Google-Maps dispatch.      Run:  python app.py"""
import os
import secrets
import time
from collections import defaultdict, deque
from functools import wraps


def load_env(path):
    """Tiny .env loader (no extra dependency)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


BASE = os.path.dirname(os.path.abspath(__file__))
load_env(os.path.join(BASE, ".env"))

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)

from sos import analytics, db, engine, geo, intake, places, roadroute, tiles, vision
from sos.triage import ValidationError

SERVICE_RADIUS_KM = float(os.environ.get("SOS_SERVICE_RADIUS_KM", 60))


def create_app(start_engine=True, reset=False):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    engine.init(reset=reset)
    if start_engine:
        engine.start_background()

    # ------------------------------------------------------------------ helpers
    buckets = defaultdict(deque)

    def limited(name, limit, per=60):
        if app.config.get("TESTING"):
            return False
        key = (name, request.remote_addr)
        q = buckets[key]
        now = time.time()
        while q and now - q[0] > per:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False

    def pin():
        return os.environ.get("STAFF_PIN", "").strip()

    def staff(f):
        @wraps(f)
        def w(*a, **k):
            if pin() and not session.get("staff"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Staff login required"}), 401
                return redirect(url_for("login", next=request.path))
            return f(*a, **k)
        return w

    def body():
        return request.get_json(silent=True) or {}

    def parse_loc(d):
        try:
            lat, lng = float(d.get("lat")), float(d.get("lng"))
        except (TypeError, ValueError):
            raise ValidationError("Location is required – allow GPS or tap the map")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValidationError("Invalid coordinates")
        if geo.haversine_km(lat, lng, *geo.CENTER) > SERVICE_RADIUS_KM:
            err = ValidationError("This location is outside the demo service area (Pune / Pimpri-Chinchwad). "
                                  "In a real emergency call 112 / 108.")
            err.code = "outside_area"
            raise err
        return lat, lng

    def read_photo():
        f = request.files.get("photo")
        if not f or not f.filename:
            return None
        try:
            return vision.validate_image(f.read())
        except ValueError as e:
            raise ValidationError(str(e))

    @app.errorhandler(ValidationError)
    def bad(e):
        return jsonify({"error": str(e), "code": getattr(e, "code", "invalid")}), 422 if getattr(e, "code", "") == "outside_area" else 400

    @app.errorhandler(413)
    def too_big(e):
        return jsonify({"error": "Upload too large (max 8 MB)"}), 413

    # ------------------------------------------------------------------ pages
    @app.route("/")
    def home():
        return render_template("home.html")

    @app.route("/report")
    def report_page():
        return render_template("report.html", incidents=intake.INCIDENTS)

    @app.route("/track/<code>")
    def track_page(code):
        return render_template("track.html", code=code)

    @app.route("/control")
    @staff
    def control_page():
        return render_template("control.html")

    @app.route("/hospital")
    @staff
    def hospital_page():
        return render_template("hospital.html")

    @app.route("/driver")
    @staff
    def driver_page():
        return render_template("driver.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = ""
        if request.method == "POST":
            if secrets.compare_digest(request.form.get("pin", ""), pin() or "\0"):
                session["staff"] = True
                return redirect(request.args.get("next") or url_for("control_page"))
            err = "Wrong PIN"
        return render_template("login.html", err=err)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/uploads/<path:name>")
    def uploads(name):
        if not name.endswith(".jpg") or "/" in name:
            abort(404)
        return send_from_directory(db.upload_dir(), name, max_age=3600)

    # ------------------------------------------------------------------ public API
    @app.get("/api/config")
    def api_config():
        use_google = os.environ.get("MAP_PROVIDER", "osm").strip().lower() == "google"
        return jsonify({"google_maps_key": os.environ.get("GOOGLE_MAPS_API_KEY", "").strip() if use_google else "",
                        "map_provider": "google" if use_google and os.environ.get("GOOGLE_MAPS_API_KEY") else "osm",
                        "areas": places.areas(), "bounds": geo.BOUNDS,
                        "vision": vision.provider_name(), "center": geo.CENTER,
                        "staff_protected": bool(pin()), "service_radius_km": SERVICE_RADIUS_KM})

    @app.get("/tiles/<style>/<int:z>/<int:x>/<int:y>.png")
    def tile(style, z, x, y):
        try:
            data, mime = tiles.get_tile(style, z, x, y)
        except tiles.TileUnavailable:
            return Response(status=404)
        return Response(data, mimetype=mime, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/route")
    def api_route():
        if limited("route", 90):
            return jsonify({"error": "Too many requests"}), 429
        try:
            a = [float(v) for v in request.args["from"].split(",")]
            b = [float(v) for v in request.args["to"].split(",")]
            assert len(a) == 2 and len(b) == 2
        except (KeyError, ValueError, AssertionError):
            raise ValidationError("from and to must be 'lat,lng'")
        for lat, lng in (a, b):
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValidationError("Invalid coordinates")
        return jsonify(roadroute.route(tuple(a), tuple(b)))

    @app.get("/api/geocode")
    def api_geocode():
        if limited("geocode", 40):
            return jsonify({"error": "Too many searches – slow down"}), 429
        return jsonify(places.search(request.args.get("q", "")))

    @app.route("/sw.js")
    def service_worker():
        r = send_from_directory(os.path.join(BASE, "static"), "sw.js", mimetype="application/javascript")
        r.headers["Service-Worker-Allowed"] = "/"
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.post("/api/analyze")
    def api_analyze():
        if limited("analyze", 20):
            return jsonify({"error": "Too many requests – slow down"}), 429
        im = read_photo()
        if im is None:
            raise ValidationError("No photo attached")
        return jsonify(vision.analyze(im))

    @app.post("/api/report")
    def api_report():
        f = request.form
        if f.get("website"):                        # honeypot for bots
            return jsonify({"error": "rejected"}), 400
        if limited("report", 6):
            return jsonify({"error": "Too many reports from this device – call 112 if urgent"}), 429
        lat, lng = parse_loc(f)
        phone = (f.get("phone") or "").strip()
        if phone and not all(ch.isdigit() or ch in "+- ()" for ch in phone):
            raise ValidationError("Phone number looks invalid")
        im = read_photo()
        photo_name, vis = None, vision.no_photo()
        if im is not None:
            photo_name = secrets.token_hex(10) + ".jpg"
            vision.save_normalised(im, os.path.join(db.upload_dir(), photo_name))
            vis = vision.analyze(im)
        patient = intake.build_patient(f.to_dict(), vis)
        cid = engine.create_case(patient, lat, lng, vision=vis, report=f.to_dict(), photo=photo_name,
                                 name=f.get("name", ""), phone=phone, place=f.get("place", ""))
        pub = engine.public_case(cid)
        return jsonify({"code": cid, "track_url": url_for("track_page", code=cid), "priority": pub["priority"],
                        "label": pub["label"], "vision": vis, "ambulance": (pub["ambulance"] or {}).get("id")}), 201

    @app.get("/api/track/<code>")
    def api_track(code):
        c = engine.public_case(code)
        return (jsonify(c), 200) if c else (jsonify({"error": "Report not found"}), 404)

    @app.post("/api/track/<code>/cancel")
    def api_track_cancel(code):
        if limited("cancel", 6):
            return jsonify({"error": "Too many requests"}), 429
        return jsonify(engine.cancel_case(code, "Cancelled by reporter"))

    # ------------------------------------------------------------------ staff API
    @app.get("/api/state")
    @staff
    def api_state():
        return jsonify(engine.state())

    @app.get("/api/cases")
    @staff
    def api_cases():
        return jsonify(engine.list_cases(request.args.get("status", "all"), request.args.get("limit", 200)))

    @app.get("/api/cases/<cid>")
    @staff
    def api_case(cid):
        c = engine.get_case(cid)
        return (jsonify(c), 200) if c else (jsonify({"error": "Case not found"}), 404)

    @app.post("/api/cases/<cid>/cancel")
    @staff
    def api_cancel(cid):
        return jsonify(engine.cancel_case(cid, body().get("reason") or "Cancelled by dispatcher"))

    @app.post("/api/cases/<cid>/assign")
    @staff
    def api_assign(cid):
        try:
            hid = int(body().get("hospital_id"))
        except (TypeError, ValueError):
            raise ValidationError("hospital_id required")
        return jsonify(engine.assign_hospital(cid, hid))

    @app.patch("/api/hospitals/<int:hid>")
    @staff
    def api_hospital_patch(hid):
        return jsonify(engine.update_hospital(hid, body()))

    @app.get("/api/hospital/<int:hid>/view")
    @staff
    def api_hospital_view(hid):
        v = engine.hospital_view(hid)
        return (jsonify(v), 200) if v else (jsonify({"error": "Hospital not found"}), 404)

    @app.post("/api/alerts/<int:aid>/decide")
    @staff
    def api_decide(aid):
        d = body()
        return jsonify(engine.decide_alert(aid, d.get("decision"), d.get("reason", ""), d.get("by") or "Hospital desk"))

    @app.get("/api/driver/<aid>")
    @staff
    def api_driver(aid):
        v = engine.driver_view(aid)
        return (jsonify(v), 200) if v else (jsonify({"error": "Ambulance not found"}), 404)

    @app.post("/api/sim/speed")
    @staff
    def api_speed():
        return jsonify({"speed": engine.set_speed(body().get("speed", 10))})

    @app.post("/api/sim/pause")
    @staff
    def api_pause():
        return jsonify({"paused": engine.set_paused(bool(body().get("paused", True)))})

    @app.post("/api/sim/hospital-mode")
    @staff
    def api_mode():
        return jsonify({"mode": engine.set_hospital_mode(body().get("mode"))})

    @app.post("/api/sim/surge")
    @staff
    def api_surge():
        return jsonify({"created": engine.surge(int(body().get("n", 6)))})

    @app.post("/api/sim/reset")
    @staff
    def api_reset():
        engine.reset()
        return jsonify({"ok": True})

    @app.get("/api/analytics")
    @staff
    def api_analytics():
        return jsonify(analytics.compute())

    @app.get("/api/export.csv")
    @staff
    def api_export():
        return Response(analytics.export_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=goldenhour_cases.csv"})

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  GoldenHour SOS running →  http://127.0.0.1:{port}")
    g = os.environ.get("MAP_PROVIDER", "osm").lower() == "google" and os.environ.get("GOOGLE_MAPS_API_KEY")
    print(f"  Maps:        {'Google Maps (optional)' if g else 'OpenStreetMap via local cache – no key needed'}")
    print(f"  Photo AI:    {vision.provider_name()}\n")
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False, threaded=True)
