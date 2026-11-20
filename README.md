# GoldenHour SOS 🚑  (v2)
Photo-based emergency reporting → AI injury detection → nearest ambulance reserved → hospital alerted →
**hospital approves → ambulance dispatched urgently** → live tracking on a map that **needs no API key**.

## Run (VS Code / Windows)
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:8000 (or double-click `run_windows.bat`; Mac/Linux `./run_mac_linux.sh`; Docker: `docker build -t sos . && docker run -p 8000:8000 sos`).

## Maps – no Google key, no billing (what changed in v2)
* The app now uses **OpenStreetMap through a caching tile proxy** built into the server (`/tiles/...`).
  Your browser only talks to *your* server, so ad-blockers / blocked CDNs / missing keys can't break the map.
* Every tile you view is **saved to `data/tiles/`** → that map keeps working **offline**.
  Optional one-off pre-download of the demo area (~110 tiles): `python tools/prefetch_tiles.py` (add `--sat` for satellite).
* **Never blank:** if no tiles are available at all, you still get a labelled coordinate grid with area names, and every feature keeps working.
* **Dark / Streets / Satellite** switcher (top-right of every map).
* **Road routes** come from the free OSRM server via `/api/route` (cached). Offline → straight lines.
* **Address search** on the report page: built-in demo-area gazetteer (works offline) + OpenStreetMap Nominatim.
* Optional: `MAP_PROVIDER=google` + `GOOGLE_MAPS_API_KEY=...` in `.env` re-enables Google Maps. `SOS_TILE_URL` lets you point at your own tile server.

## The flow
1. **/report** – choose what happened, take/upload a **photo**, 4 quick questions, GPS / map pin / address search → SOS.
2. **Injury detection** – Claude vision (`ANTHROPIC_API_KEY`) or a basic local colour check (no key) → triage P1–P4.
3. **Nearest ambulance is picked first** and held (reserved).
4. **Best hospital is alerted** (specialty, on-call specialist, ICU/ER bed, ventilator, paediatric, golden-hour ETA → weighted score) with photo + triage.
5. **Hospital approves** → ambulance **dispatched urgently**. Decline / no reply in 60 s → next-best hospital. None left → control room assigns.
6. Live tracking for everyone; hospital diversion re-routes ambulances already on the way.

## Pages
| URL | Who | What |
|---|---|---|
| `/report` → `/track/<code>` | Citizen | Report, live ETA, map, first aid, CPR metronome, browser notifications, English / हिन्दी / मराठी, installable (PWA) |
| `/control` | Dispatcher | Live map, cases, hospitals, fleet, analytics, surge simulator, force-assign, handover sheet |
| `/hospital` | Hospital staff | Approve/decline with photo + countdown, beds, diversion, on-call specialists |
| `/driver` | Ambulance crew | Assignment, photo, one-tap navigation link |

## Demo tips
* **Demo mode (default):** simulated hospital staff auto-reply after ~8 s so one person sees the full flow. For real approval click **"Hospital: auto-reply"** in the Control Room (→ manual), then approve in `/hospital` in a second tab.
* **Surge ×6** creates 6 simultaneous incidents. The speed selector changes simulation speed.
* Service area is Pune / Pimpri-Chinchwad; if your GPS is elsewhere the app offers a demo location.

## Optional settings (`.env`, copy from `.env.example`)
`ANTHROPIC_API_KEY` (real photo AI) · `STAFF_PIN` (protect staff pages) · `MAP_PROVIDER` / `GOOGLE_MAPS_API_KEY` · `SOS_TILE_URL` · `SOS_ALERT_TIMEOUT` · `SOS_AUTO_DELAY` · `PORT`

## Tests
`pip install pytest` then `python -m pytest tests` – 26 tests: triage, vision, constraint routing, full approve/decline/timeout/escalation flows, re-routing, validation, PIN, **map tile cache (incl. offline), routing & geocoding fallbacks**.

## Honest limitations
Academic prototype: not clinically validated; simulated hospitals/ambulances (no real vehicle GPS); no real SMS/phone integration. Without an API key the photo check is a colour heuristic, not medical AI. Translations cover the report form, status headlines and core first aid. The public OSM/OSRM/Nominatim servers are for light, demo-scale use – use your own tile/route servers for production. Never use for real emergencies – call 112 / 108.
