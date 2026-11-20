/* MapKit – keyless OpenStreetMap maps via a caching tile proxy (default), optional Google Maps (MAP_PROVIDER=google). */
window.MapKit = (function () {
  let cfg = null, gPromise = null;
  const routeCache = {};

  async function config() {
    if (!cfg) cfg = await fetch('/api/config').then(r => r.json()).catch(() => ({ google_maps_key: '', center: [18.612, 73.808] }));
    return cfg;
  }

  function loadGoogle(key) {
    if (window.google && google.maps) return Promise.resolve();
    if (!gPromise) gPromise = new Promise((res, rej) => {
      window.__gmReady = () => res();
      const s = document.createElement('script');
      s.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(key) + '&callback=__gmReady&v=weekly';
      s.async = true; s.onerror = () => rej(new Error('Google Maps failed to load'));
      document.head.appendChild(s);
      setTimeout(() => rej(new Error('Google Maps timeout')), 9000);
    });
    return gPromise;
  }
  // Google calls this when the key is invalid / API not enabled / billing missing.
  window.gm_authFailure = function () {
    try { sessionStorage.setItem('gm_fail', '1'); } catch (e) {}
    if (window.toast) toast('Google Maps key was rejected – switching to OpenStreetMap…', 'warn');
    setTimeout(() => location.reload(), 1800);
  };

  // ---------------------------------------------------------------- icons (shared SVG)
  function icon(kind, color, label, pulse) {
    color = color || '#4c8dff';
    let w = 38, h = 38, ax = 19, ay = 19, svg = '';
    if (kind === 'hospital') {
      svg = `<svg xmlns="http://www.w3.org/2000/svg" width="38" height="38" viewBox="0 0 38 38"><rect x="3" y="3" width="32" height="32" rx="10" fill="${color}" stroke="#fff" stroke-width="2"/><path d="M16 9h6v7h7v6h-7v7h-6v-7H9v-6h7z" fill="#fff"/></svg>`;
    } else if (kind === 'ambulance') {
      svg = `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40"><circle cx="20" cy="20" r="17" fill="#0b1220" stroke="${color}" stroke-width="3"/><text x="20" y="27" font-size="19" text-anchor="middle">🚑</text></svg>`;
      w = h = 40; ax = ay = 20;
    } else if (kind === 'incident') {
      const an = pulse ? `<circle cx="26" cy="26" r="12" fill="none" stroke="${color}" stroke-width="3"><animate attributeName="r" values="12;25" dur="1.4s" repeatCount="indefinite"/><animate attributeName="opacity" values=".9;0" dur="1.4s" repeatCount="indefinite"/></circle>` : '';
      svg = `<svg xmlns="http://www.w3.org/2000/svg" width="52" height="52" viewBox="0 0 52 52">${an}<circle cx="26" cy="26" r="14" fill="${color}" stroke="#fff" stroke-width="2.5"/><text x="26" y="31" font-size="13" font-weight="700" fill="#fff" text-anchor="middle" font-family="Arial">${label || '!'}</text></svg>`;
      w = h = 52; ax = ay = 26;
    } else { // pin
      svg = `<svg xmlns="http://www.w3.org/2000/svg" width="34" height="46" viewBox="0 0 34 46"><path d="M17 1C8 1 1 8 1 17c0 12 16 28 16 28s16-16 16-28C33 8 26 1 17 1z" fill="${color}" stroke="#fff" stroke-width="2"/><circle cx="17" cy="17" r="6" fill="#fff"/></svg>`;
      w = 34; h = 46; ax = 17; ay = 44;
    }
    return { svg, w, h, ax, ay };
  }

  const DARK_STYLE = [
    { elementType: 'geometry', stylers: [{ color: '#0f1729' }] }, { elementType: 'labels.text.fill', stylers: [{ color: '#8b98b8' }] },
    { elementType: 'labels.text.stroke', stylers: [{ color: '#0b1220' }] }, { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#1c2744' }] },
    { featureType: 'road.highway', elementType: 'geometry', stylers: [{ color: '#2a3a68' }] }, { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#06101f' }] },
    { featureType: 'poi', stylers: [{ visibility: 'off' }] }, { featureType: 'transit', stylers: [{ visibility: 'off' }] }];

  // ---------------------------------------------------------------- Leaflet provider (no API key, cached tiles)
  const STYLES = { dark: 'Dark', streets: 'Streets', sat: 'Satellite' };
  const ATTR = { dark: '&copy; OpenStreetMap contributors', streets: '&copy; OpenStreetMap contributors', sat: 'Imagery &copy; Esri, Maxar, Earthstar Geographics' };

  // Always-on schematic grid: the map is never blank, even with no internet and no cached tiles.
  const Graticule = L.GridLayer.extend({
    createTile(c) {
      const t = document.createElement('canvas'); t.width = t.height = 256; const x = t.getContext('2d');
      const light = this.options.light;
      x.fillStyle = light ? '#e8edf3' : '#0c1424'; x.fillRect(0, 0, 256, 256);
      x.strokeStyle = light ? 'rgba(60,80,110,.18)' : 'rgba(120,150,210,.12)'; x.lineWidth = 1;
      for (let i = 0; i <= 256; i += 64) { x.beginPath(); x.moveTo(i + .5, 0); x.lineTo(i + .5, 256); x.moveTo(0, i + .5); x.lineTo(256, i + .5); x.stroke(); }
      x.strokeStyle = light ? 'rgba(60,80,110,.38)' : 'rgba(120,150,210,.28)'; x.strokeRect(.5, .5, 255, 255);
      if (c.z >= 11) {
        const nw = this._map.unproject([c.x * 256, c.y * 256], c.z);
        x.fillStyle = light ? 'rgba(40,60,90,.55)' : 'rgba(150,175,230,.45)'; x.font = '10px monospace';
        x.fillText(nw.lat.toFixed(3) + '°N  ' + nw.lng.toFixed(3) + '°E', 6, 14);
      }
      return t;
    }
  });

  class LeafletView {
    constructor(el, o) {
      this.provider = 'leaflet'; this.m = {}; this.l = {}; this.loads = 0; this.errors = 0; this.offline = false; this.cfg = o.cfg || {};
      this.map = L.map(el, { zoomControl: false, attributionControl: true, minZoom: 9, maxZoom: 18, zoomSnap: .5 }).setView(o.center, o.zoom || 13);
      L.control.zoom({ position: 'topright' }).addTo(this.map);
      L.control.scale({ imperial: false, position: 'bottomright' }).addTo(this.map);
      this.base = new Graticule({ light: false, maxZoom: 19, zIndex: 1 }).addTo(this.map);
      let saved = null; try { saved = localStorage.getItem('sos_map_style'); } catch (e) {}
      this.style = STYLES[saved] ? saved : (o.dark === false ? 'streets' : 'dark');
      this.layers = {};
      Object.keys(STYLES).forEach(k => {
        const tl = L.tileLayer(`/tiles/${k}/{z}/{x}/{y}.png`, { maxZoom: 19, attribution: ATTR[k], className: k === 'dark' ? 'tiles-dark' : '', zIndex: 2, keepBuffer: 3 });
        tl.on('tileload', () => { this.loads++; if (this.offline) { this.offline = false; this._status(); } });
        tl.on('tileerror', () => { this.errors++; clearTimeout(this._ot); this._ot = setTimeout(() => { if (this.loads === 0 && this.errors >= 3) { this.offline = true; this._status(); } }, 2500); });
        this.layers[k] = tl;
      });
      this.layers[this.style].addTo(this.map);
      this._styleControl();
      this._areaLabels();
      if (o.onClick) this.map.on('click', e => o.onClick(e.latlng.lat, e.latlng.lng));
      setTimeout(() => this.map.invalidateSize(), 80);
    }
    _styleControl() {
      const self = this, C = L.Control.extend({
        options: { position: 'topright' },
        onAdd() {
          const d = L.DomUtil.create('div', 'mapstyle'); L.DomEvent.disableClickPropagation(d);
          Object.entries(STYLES).forEach(([k, lab]) => {
            const b = L.DomUtil.create('button', k === self.style ? 'on' : '', d); b.type = 'button'; b.textContent = lab;
            b.onclick = () => { self.setStyle(k); d.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); };
          });
          return d;
        }
      });
      new C().addTo(this.map);
    }
    setStyle(k) {
      if (!this.layers[k] || k === this.style) return;
      this.map.removeLayer(this.layers[this.style]); this.style = k; this.layers[k].addTo(this.map);
      try { localStorage.setItem('sos_map_style', k); } catch (e) {}
    }
    _areaLabels() {
      this.areaLayer = L.layerGroup();
      (this.cfg.areas || []).forEach(a => L.marker([a.lat, a.lng], { icon: L.divIcon({ className: 'arealabel', html: '<span>' + a.name + '</span>', iconSize: [0, 0] }), interactive: false, keyboard: false, zIndexOffset: -1000 }).addTo(this.areaLayer));
      const upd = () => { const on = this.map.getZoom() >= 12.5; if (on && !this.map.hasLayer(this.areaLayer)) this.areaLayer.addTo(this.map); if (!on && this.map.hasLayer(this.areaLayer)) this.map.removeLayer(this.areaLayer); };
      this.map.on('zoomend', upd); upd();
    }
    _status() { if (this.badge) this.badge.textContent = this.offline ? '🗺 Offline grid' : '🗺 OpenStreetMap · cached on server'; this.badge.title = this.offline ? 'Map tiles unavailable. Connect to the internet once (or run tools/prefetch_tiles.py) to cache the map.' : ''; }
    marker(id, o) {
      const ic = icon(o.kind, o.color, o.label, o.pulse);
      let mk = this.m[id];
      const sig = [o.kind, o.color, o.label, !!o.pulse].join('|');
      if (!mk) {
        mk = this.m[id] = L.marker([o.lat, o.lng], { icon: L.divIcon({ html: ic.svg, className: '', iconSize: [ic.w, ic.h], iconAnchor: [ic.ax, ic.ay] }), zIndexOffset: o.z || 0 }).addTo(this.map);
        if (o.onClick) mk.on('click', o.onClick);
        mk._sig = sig;
      } else {
        mk.setLatLng([o.lat, o.lng]);
        if (sig !== mk._sig) { mk.setIcon(L.divIcon({ html: ic.svg, className: '', iconSize: [ic.w, ic.h], iconAnchor: [ic.ax, ic.ay] })); mk._sig = sig; }
      }
      if (o.title) { if (mk.getTooltip()) mk.setTooltipContent(o.title); else mk.bindTooltip(o.title, { direction: 'top', offset: [0, -ic.ay + 6] }); }
    }
    removeMarker(id) { if (this.m[id]) { this.map.removeLayer(this.m[id]); delete this.m[id]; } }
    markerIds() { return Object.keys(this.m); }
    line(id, pts, o) {
      o = o || {};
      if (!pts || pts.length < 2) return this.removeLine(id);
      const st = { color: o.color || '#19e3c1', weight: o.weight || 4, opacity: o.opacity || .85, dashArray: o.dashed ? '8 8' : null };
      if (this.l[id]) { this.l[id].setLatLngs(pts); this.l[id].setStyle(st); } else this.l[id] = L.polyline(pts, st).addTo(this.map);
    }
    removeLine(id) { if (this.l[id]) { this.map.removeLayer(this.l[id]); delete this.l[id]; } }
    lineIds() { return Object.keys(this.l); }
    fit(pts, pad) { if (pts.length) this.map.fitBounds(L.latLngBounds(pts), { padding: [pad || 60, pad || 60], maxZoom: 16 }); }
    panTo(lat, lng, z) { this.map.setView([lat, lng], z || this.map.getZoom()); }
    resize() { this.map.invalidateSize(); }
    async route(a, b) { return routeVia(a, b, serverRoute); }
  }

  // ---------------------------------------------------------------- Google provider
  class GoogleView {
    constructor(el, o) {
      this.provider = 'google'; this.m = {}; this.l = {};
      this.map = new google.maps.Map(el, {
        center: { lat: o.center[0], lng: o.center[1] }, zoom: o.zoom || 13, disableDefaultUI: true, zoomControl: true, fullscreenControl: true,
        clickableIcons: false, gestureHandling: 'greedy', styles: o.dark !== false ? DARK_STYLE : null, backgroundColor: '#0b1220'
      });
      if (o.onClick) this.map.addListener('click', e => o.onClick(e.latLng.lat(), e.latLng.lng()));
    }
    _icon(o) {
      const ic = icon(o.kind, o.color, o.label, o.pulse);
      return { url: 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(ic.svg), scaledSize: new google.maps.Size(ic.w, ic.h), anchor: new google.maps.Point(ic.ax, ic.ay) };
    }
    marker(id, o) {
      const sig = [o.kind, o.color, o.label, !!o.pulse].join('|');
      let mk = this.m[id];
      if (!mk) {
        mk = this.m[id] = new google.maps.Marker({ map: this.map, position: { lat: o.lat, lng: o.lng }, icon: this._icon(o), title: o.title || '', zIndex: o.z || 1, optimized: false });
        if (o.onClick) mk.addListener('click', o.onClick);
        mk._sig = sig;
      } else {
        mk.setPosition({ lat: o.lat, lng: o.lng });
        if (sig !== mk._sig) { mk.setIcon(this._icon(o)); mk._sig = sig; }
        if (o.title) mk.setTitle(o.title);
      }
    }
    removeMarker(id) { if (this.m[id]) { this.m[id].setMap(null); delete this.m[id]; } }
    markerIds() { return Object.keys(this.m); }
    line(id, pts, o) {
      o = o || {};
      if (!pts || pts.length < 2) return this.removeLine(id);
      const path = pts.map(p => ({ lat: p[0], lng: p[1] }));
      const opt = { path, strokeColor: o.color || '#19e3c1', strokeOpacity: o.dashed ? 0 : (o.opacity || .85), strokeWeight: o.weight || 4, map: this.map,
        icons: o.dashed ? [{ icon: { path: 'M 0,-1 0,1', strokeOpacity: .85, scale: 3 }, offset: '0', repeat: '14px' }] : null };
      if (this.l[id]) this.l[id].setOptions(opt); else this.l[id] = new google.maps.Polyline(opt);
    }
    removeLine(id) { if (this.l[id]) { this.l[id].setMap(null); delete this.l[id]; } }
    lineIds() { return Object.keys(this.l); }
    fit(pts, pad) {
      if (!pts.length) return;
      const b = new google.maps.LatLngBounds(); pts.forEach(p => b.extend({ lat: p[0], lng: p[1] }));
      this.map.fitBounds(b, pad || 60);
      google.maps.event.addListenerOnce(this.map, 'idle', () => { if (this.map.getZoom() > 16) this.map.setZoom(16); });
    }
    panTo(lat, lng, z) { this.map.panTo({ lat, lng }); if (z) this.map.setZoom(z); }
    resize() { google.maps.event.trigger(this.map, 'resize'); }
    async route(a, b) { return routeVia(a, b, gdirections); }
  }

  // ---------------------------------------------------------------- routing (road geometry)
  async function serverRoute(a, b) {          // road geometry through our own server (OSRM, cached, straight-line fallback)
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 9000);
    try {
      const r = await fetch(`/api/route?from=${a[0]},${a[1]}&to=${b[0]},${b[1]}`, { signal: ctl.signal });
      const j = await r.json(); return j.points;
    } finally { clearTimeout(t); }
  }
  function gdirections(a, b) {
    return new Promise((res, rej) => {
      new google.maps.DirectionsService().route({ origin: { lat: a[0], lng: a[1] }, destination: { lat: b[0], lng: b[1] }, travelMode: 'DRIVING' }, (r, st) => {
        if (st === 'OK') res(r.routes[0].overview_path.map(p => [p.lat(), p.lng()])); else rej(new Error(st));
      });
    });
  }
  async function routeVia(a, b, fn) {
    const k = [a[0], a[1], b[0], b[1]].map(x => x.toFixed(4)).join(',');
    if (routeCache[k]) return routeCache[k];
    let pts;
    try { pts = await fn(a, b); } catch (e) { pts = [a, b]; }
    return (routeCache[k] = pts && pts.length > 1 ? pts : [a, b]);
  }

  // ---------------------------------------------------------------- polyline math
  function pointAlong(path, t) {
    if (!path || path.length < 2) return null;
    t = Math.max(0, Math.min(1, t));
    const seg = [0]; let tot = 0;
    for (let i = 1; i < path.length; i++) { const d = Math.hypot(path[i][0] - path[i - 1][0], (path[i][1] - path[i - 1][1]) * Math.cos(path[i][0] * Math.PI / 180)); tot += d; seg.push(tot); }
    const target = t * tot; let i = 1; while (i < seg.length - 1 && seg[i] < target) i++;
    const f = seg[i] - seg[i - 1] ? (target - seg[i - 1]) / (seg[i] - seg[i - 1]) : 0;
    return [path[i - 1][0] + (path[i][0] - path[i - 1][0]) * f, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * f];
  }
  function remainingPath(path, t) {
    if (!path || path.length < 2) return path;
    const p = pointAlong(path, t); if (!p) return path;
    const seg = [0]; let tot = 0;
    for (let i = 1; i < path.length; i++) { tot += Math.hypot(path[i][0] - path[i - 1][0], (path[i][1] - path[i - 1][1]) * Math.cos(path[i][0] * Math.PI / 180)); seg.push(tot); }
    const target = t * tot; const rest = [p];
    for (let i = 1; i < path.length; i++) if (seg[i] > target) rest.push(path[i]);
    return rest;
  }

  async function create(el, opts) {
    const c = await config();
    opts = Object.assign({ center: c.center, zoom: 13, dark: true, cfg: c }, opts || {});
    let view = null, note = '🗺 OpenStreetMap · cached on server';
    let failed = false; try { failed = sessionStorage.getItem('gm_fail') === '1'; } catch (e) {}
    if (c.map_provider === 'google' && c.google_maps_key && !failed) {
      try { await loadGoogle(c.google_maps_key); view = new GoogleView(el, opts); note = '🗺 Google Maps'; } catch (e) { console.warn(e); }
    }
    if (!view) view = new LeafletView(el, opts);
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    const b = document.createElement('div'); b.className = 'mapbadge'; b.textContent = note; el.appendChild(b); view.badge = b;
    return view;
  }
  async function search(q) { const r = await fetch('/api/geocode?q=' + encodeURIComponent(q)); return r.ok ? (await r.json()).results : []; }

  function navUrl(origin, dest) {   // works without any API key – opens Google Maps turn-by-turn
    const o = origin ? `&origin=${origin[0]},${origin[1]}` : '';
    return `https://www.google.com/maps/dir/?api=1${o}&destination=${dest[0]},${dest[1]}&travelmode=driving`;
  }
  return { create, config, search, pointAlong, remainingPath, navUrl, icon };
})();
