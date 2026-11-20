(async function () {
  let S = null, view = null, anim = null, tab = 'cases', histMode = false, openId = null, lastEvent = 0, paneSig = '', charts = {}, anaT = 0, firstFit = false;
  const CAPS = { trauma: 'Trauma', cardiac: 'Cardiac', stroke: 'Stroke', burn: 'Burn', pediatric: 'Paeds', obstetric: 'Obstetric', ct: 'CT', cath_lab: 'Cath lab', nicu: 'NICU' };

  view = await MapKit.create($('#map'), { dark: true, zoom: 12 });
  anim = new LegAnimator(view);

  // ------------------------------------------------------------------ top controls
  $('#speed').onchange = e => api('/api/sim/speed', { json: { speed: +e.target.value } }).catch(x => toast(x.message, 'err'));
  $('#pause').onclick = () => api('/api/sim/pause', { json: { paused: !S.sim.paused } }).then(poll).catch(x => toast(x.message, 'err'));
  $('#mode').onclick = () => api('/api/sim/hospital-mode', { json: { mode: S.hospital_mode === 'auto' ? 'manual' : 'auto' } }).then(poll).catch(x => toast(x.message, 'err'));
  $('#surge').onclick = async () => { try { const r = await api('/api/sim/surge', { json: { n: 6 } }); toast(`⚠ Surge: ${r.created.length} incidents created`, 'warn'); poll(); } catch (e) { toast(e.message, 'err'); } };
  $('#reset').onclick = async () => { if (!confirm('Reset the whole simulation (cases, beds, ambulances)?')) return; await api('/api/sim/reset', { json: {} }); closeDrawer(); toast('Simulation reset'); poll(); };
  $$('#tabs .tab').forEach(t => t.onclick = () => { tab = t.dataset.t; $$('#tabs .tab').forEach(x => x.classList.toggle('on', x === t)); paneSig = ''; renderPane(); });
  $('#scrim').onclick = closeDrawer;
  setInterval(() => { if (S) $('#clock').textContent = '🕒 ' + fmtClock(SimClock.now()); }, 250);

  // ------------------------------------------------------------------ polling
  async function poll() {
    try { S = await api('/api/state'); } catch (e) { return; }
    SimClock.sync(S.sim);
    updateTop(); updateMap(); renderPane(); notify();
    if (openId && $('#drawer').classList.contains('open')) refreshDrawer();
  }
  (function loop() { poll().finally(() => setTimeout(loop, 1200)); })();

  function kpi(id, v, hot) { const k = $(id); k.firstElementChild.textContent = v; k.classList.toggle('hot', !!hot); }
  function updateTop() {
    const k = S.kpi;
    kpi('#k-active', k.active); kpi('#k-p1', k.p1_active, k.p1_active > 0); kpi('#k-await', k.awaiting, k.awaiting > 0);
    kpi('#k-amb', `${k.ambulances_available}/${k.ambulances_total}`, k.ambulances_available === 0);
    kpi('#k-hosp', `${k.hospitals_online}/${k.hospitals_total}`, k.hospitals_online < k.hospitals_total); kpi('#k-load', k.network_load + '%', k.network_load > 80);
    $('#pause').textContent = S.sim.paused ? '▶ Resume' : '⏸ Pause'; $('#speed').value = String(S.sim.speed);
    $('#mode').textContent = S.hospital_mode === 'auto' ? '🏥 Hospital: auto-reply (demo)' : '🏥 Hospital: manual approval';
    $('#mode').classList.toggle('teal', S.hospital_mode === 'auto');
    const n = $('#n-cases'); n.style.display = k.active ? '' : 'none'; n.textContent = k.active;
  }

  function notify() {
    const evs = S.events.slice().reverse();
    if (!lastEvent) { lastEvent = S.events[0] ? S.events[0].id : 0; return; }
    evs.filter(e => e.id > lastEvent).forEach(e => {
      if (e.level === 'crit') { toast(e.message, 'err', 5000); beep(2, 740); } else if (e.message.includes('APPROVED') || e.message.includes('DISPATCHED')) toast(e.message, 'ok', 3500);
      else if (e.message.includes('DECLINED') || e.message.includes('REROUTED')) toast(e.message, 'warn', 4500);
    });
    if (S.events[0]) lastEvent = Math.max(lastEvent, S.events[0].id);
  }

  // ------------------------------------------------------------------ map
  const hospColor = h => !h.accepting ? '#5d6a88' : h.load_pct > 85 ? '#ff3b52' : h.load_pct > 65 ? '#ffa31a' : '#2fe38a';
  function updateMap() {
    const keep = new Set();
    S.hospitals.forEach(h => {
      const id = 'h' + h.id; keep.add(id);
      view.marker(id, { lat: h.lat, lng: h.lng, kind: 'hospital', color: hospColor(h), z: 2, title: `${h.name}${h.accepting ? '' : ' (DIVERSION)'} · ICU ${h.icu_free}/${h.icu_total} · ER ${h.er_free}/${h.er_total}`, onClick: () => { tab = 'hospitals'; $$('#tabs .tab').forEach(x => x.classList.toggle('on', x.dataset.t === 'hospitals')); paneSig = ''; renderPane(); } });
    });
    S.cases.forEach(c => {
      const id = 'c' + c.id; keep.add(id);
      view.marker(id, { lat: c.lat, lng: c.lng, kind: 'incident', color: PCOL[c.priority], label: c.priority, pulse: ['awaiting_hospital', 'no_hospital', 'pending', 'dispatched'].includes(c.status), z: 6, title: `${c.id} · ${c.label} · ${c.place}`, onClick: () => openCase(c.id) });
      const lid = 'd' + c.id;
      if (c.hospital && ['awaiting_hospital', 'dispatched', 'on_scene', 'pending'].includes(c.status)) { keep.add(lid); view.line(lid, [[c.lat, c.lng], [c.hospital.lat, c.hospital.lng]], { color: c.status === 'awaiting_hospital' ? '#ffb020' : '#19e3c1', dashed: true, weight: 3, opacity: .7 }); } else view.removeLine(lid);
    });
    S.ambulances.forEach(a => {
      const id = 'a' + a.id; keep.add(id); anim.ensure(a);
    });
    view.markerIds().forEach(id => { if (!keep.has(id) && !id.startsWith('a')) view.removeMarker(id); });
    view.lineIds().forEach(id => { if (id.startsWith('d') && !keep.has(id)) view.removeLine(id); });
    if (!firstFit) { firstFit = true; view.fit(S.hospitals.map(h => [h.lat, h.lng]), 50); }
  }
  function frame() {
    if (S) S.ambulances.forEach(a => {
      const r = anim.at(a); if (!r.pos) return;
      view.marker('a' + a.id, { lat: r.pos[0], lng: r.pos[1], kind: 'ambulance', color: AMB_COL[a.status], z: 9, title: `${a.id} (${a.type}) – ${a.status.replace('_', ' ')}${a.case_id ? ' · ' + a.case_id : ''}` });
      if (r.pts && a.leg && a.leg !== 'returning') view.line('r' + a.id, MapKit.remainingPath(r.pts, r.t), { color: a.leg === 'to_scene' ? '#ff3b52' : '#ff7a45', weight: 4 }); else view.removeLine('r' + a.id);
    });
    setTimeout(frame, 130);
  }
  frame();

  // ------------------------------------------------------------------ side panes
  function renderPane() {
    if (!S) return;
    const p = $('#pane');
    if (tab === 'analytics') return renderAnalytics(p);
    let html = '';
    if (tab === 'cases') html = casesHtml(); else if (tab === 'hospitals') html = hospitalsHtml(); else if (tab === 'fleet') html = fleetHtml(); else html = logHtml();
    const sig = tab + html.replace(/\d+s\b|\d+:\d\d/g, '');           // ignore pure countdown noise
    if (sig === paneSig) return;
    paneSig = sig; const y = p.scrollTop; p.innerHTML = html; p.scrollTop = y;
    if (tab === 'cases' && histMode) loadHistory();
  }

  function casesHtml() {
    const head = `<div class="row between" style="margin-bottom:10px"><div class="pills"><div class="pill ${histMode ? '' : 'on'}" data-act="hist" data-v="0">Active (${S.cases.length})</div><div class="pill ${histMode ? 'on' : ''}" data-act="hist" data-v="1">History</div></div><a class="btn sm ghost" href="/api/export.csv">⬇ CSV</a></div>`;
    if (histMode) return head + '<div id="hist" class="empty">Loading…</div>';
    if (!S.cases.length) return head + `<div class="empty">No active emergencies.<br><small>Open <a href="/report" style="color:var(--teal)">/report</a> on your phone, or press <b>Surge</b>.</small></div>`;
    return head + S.cases.map(caseItem).join('');
  }
  function caseItem(c) {
    const need = ['awaiting_hospital', 'no_hospital', 'pending'].includes(c.status);
    let line = '';
    if (c.status === 'awaiting_hospital') line = `🏥 Waiting for <b>${esc(c.hospital ? c.hospital.name : '?')}</b> to approve${c.alert ? ` · ${c.alert.wait_s}s` : ''}`;
    else if (c.status === 'no_hospital') line = '⛔ <b>No hospital accepted</b> – assign manually';
    else if (c.status === 'pending') line = '⏳ Approved – no free ambulance';
    else line = `🚑 ${esc(c.ambulance_id || '')} → 🏥 ${esc(c.hospital ? c.hospital.name : '')}${c.eta_remaining_min != null ? ' · ETA ' + fmtMin(c.eta_remaining_min) : ''}`;
    const gold = c.golden_remaining_min != null ? `<span class="chip ${c.golden_remaining_min < 15 ? 'bad' : ''}">⏱ ${Math.round(c.golden_remaining_min)}m left</span>` : '';
    return `<div class="item ${c.priority.toLowerCase()} ${need ? 'need' : ''}" data-case="${c.id}">
      <div class="row between"><div class="row wrapx" style="gap:6px">${prioBadge(c.priority)} ${statusBadge(c.status)} <b class="mono">${esc(c.id)}</b></div><span>${TYPE_ICON[c.type] || ''}</span></div>
      <div style="margin:6px 0 3px;font-size:14px">${esc(c.summary)}</div><div class="mut" style="font-size:13px">${line}</div>
      <div style="margin-top:4px">${c.photo ? '<span class="chip ok">📸 photo</span>' : ''}${gold}${c.reroutes ? `<span class="chip bad">${c.reroutes} reroute/escalation</span>` : ''}${c.override ? '<span class="chip">override</span>' : ''}${c.relaxed ? '<span class="chip bad">relaxed tier ' + c.tier + '</span>' : ''}</div>
      ${need ? `<div class="row" style="margin-top:8px"><button class="btn sm green" data-act="force" data-id="${c.id}" data-h="${c.hospital ? c.hospital.id : ''}">✔ Approve for hospital</button><button class="btn sm" data-case="${c.id}">Details</button></div>` : ''}</div>`;
  }
  async function loadHistory() {
    const list = await api('/api/cases?status=closed&limit=40'); const el = $('#hist'); if (!el) return;
    el.className = ''; el.innerHTML = list.map(c => `<div class="item ${c.priority.toLowerCase()}" data-case="${c.id}"><div class="row between"><div class="row" style="gap:6px">${prioBadge(c.priority)} ${statusBadge(c.status)} <b class="mono">${esc(c.id)}</b></div><small>${fmtDT(c.created_ts)}</small></div>
      <div style="font-size:13.5px;margin-top:4px">${esc(c.summary)}</div><small>${c.hospital ? esc(c.hospital.name) : ''} ${c.call_to_door_min != null ? '· door ' + Math.round(c.call_to_door_min) + ' min ' + (c.golden_met ? '✓' : '✕') : ''}</small></div>`).join('') || '<div class="empty">No history</div>';
  }

  function meter(v, t, label) {
    const pct = t ? 100 * (t - v) / t : 100; const cls = pct > 85 ? 'hot' : pct > 65 ? 'mid' : '';
    return `<div style="margin:6px 0"><div class="row between" style="font-size:12.5px"><span class="mut">${label}</span><b class="mono">${v}/${t} free</b></div><div class="meter ${cls}"><i style="width:${pct}%"></i></div></div>`;
  }
  function hospitalsHtml() {
    return S.hospitals.map(h => `<div class="card" style="margin-bottom:10px;padding:14px">
      <div class="row between"><div><b>${esc(h.name)}</b><br><small>${esc(h.area)} · Level ${h.level} · ${esc(h.phone)}</small></div>
        <div class="row" style="gap:8px"><small>${h.accepting ? 'Accepting' : '<b style="color:var(--red)">DIVERSION</b>'}</small><div class="switch ${h.accepting ? 'on' : ''}" data-act="accept" data-h="${h.id}" title="Toggle diversion"></div></div></div>
      ${meter(h.icu_free, h.icu_total, 'ICU beds')}${meter(h.er_free, h.er_total, 'ER beds')}${meter(h.vent_free, h.vent_total, 'Ventilators')}
      <div class="row wrapx" style="gap:14px;margin:6px 0"><span class="stepper"><small>ICU</small><button data-act="adj" data-h="${h.id}" data-f="icu_free" data-d="-1">−</button><b>${h.icu_free}</b><button data-act="adj" data-h="${h.id}" data-f="icu_free" data-d="1">+</button></span>
        <span class="stepper"><small>ER</small><button data-act="adj" data-h="${h.id}" data-f="er_free" data-d="-1">−</button><b>${h.er_free}</b><button data-act="adj" data-h="${h.id}" data-f="er_free" data-d="1">+</button></span></div>
      <div>${Object.entries(CAPS).map(([k, l]) => h.caps[k] ? `<span class="chip ${h.oncall[k] ? 'ok' : ''}" title="${h.oncall[k] ? 'specialist on call' : 'no specialist on call'}">${l}${h.oncall[k] ? ' ●' : ''}</span>` : '').join('')}</div></div>`).join('');
  }
  function fleetHtml() {
    return `<table class="t"><tr><th>Unit</th><th>Status</th><th>Case</th><th>Crew / base</th></tr>` + S.ambulances.map(a => `<tr><td><b>${a.id}</b><br><span class="chip">${a.type}</span></td>
      <td><span class="dot" style="background:${AMB_COL[a.status]};color:${AMB_COL[a.status]}"></span> ${a.status.replace('_', ' ')}</td><td>${a.case_id ? `<a style="color:var(--teal)" href="#" data-case="${a.case_id}">${a.case_id}</a>` : '–'}</td>
      <td><small>${esc(a.crew)}<br>${esc(a.base)} · ${a.trips} trips</small></td></tr>`).join('') + '</table>';
  }
  function logHtml() {
    return S.events.map(e => `<div class="timeline" style="max-height:none"><div class="${e.level}"><time>${fmtClock(e.ts)}</time><span>${e.case_id ? `<a href="#" data-case="${e.case_id}" style="color:var(--teal)">${e.case_id}</a> ` : ''}${esc(e.message)}</span></div></div>`).join('');
  }

  // ------------------------------------------------------------------ analytics
  async function renderAnalytics(p) {
    if (!$('#anaRoot')) { p.innerHTML = '<div id="anaRoot"><div class="empty">Loading analytics…</div></div>'; charts = {}; anaT = 0; }
    if (Date.now() - anaT < 4000) return; anaT = Date.now();
    let a; try { a = await api('/api/analytics'); } catch (e) { return; }
    const root = $('#anaRoot'); if (!root || tab !== 'analytics') return;
    const t = a.times, n = v => v == null ? '–' : v;
    if (!root.dataset.built) {
      root.dataset.built = 1;
      root.innerHTML = `<div class="grid g2" id="anaK" style="margin-bottom:12px"></div><div class="charts">
        <div class="card"><h3>Priority mix</h3><div class="chartbox"><canvas id="c1"></canvas></div></div>
        <div class="card"><h3>Emergency types</h3><div class="chartbox"><canvas id="c2"></canvas></div></div>
        <div class="card"><h3>Cases per day</h3><div class="chartbox"><canvas id="c3"></canvas></div></div>
        <div class="card"><h3>Cases by hour of day</h3><div class="chartbox"><canvas id="c4"></canvas></div></div>
        <div class="card"><h3>Why hospitals were rejected</h3><div class="chartbox"><canvas id="c5"></canvas></div></div>
        <div class="card"><h3>Cases per hospital</h3><div class="chartbox"><canvas id="c6"></canvas></div></div></div>`;
    }
    $('#anaK').innerHTML = [['Golden-hour compliance', n(t.golden_compliance) + (t.golden_compliance != null ? '%' : ''), `${t.golden_n} arrivals`], ['Report → on scene', n(t.response) + ' min', 'avg response'], ['Report → hospital door', n(t.call_to_door) + ' min', `P1: ${n(t.p1_call_to_door)} min`],
      ['Hospital approval time', n(t.approval) + ' min', 'sim time'], ['Total cases', a.totals.cases, `${a.totals.with_photo} with photo`], ['Escalations / reroutes', a.totals.reroutes, `${a.totals.overrides} overrides`]]
      .map(([l, v, s]) => `<div class="card" style="padding:12px"><small>${l}</small><div style="font:700 26px var(--head)">${v}</div><small>${s}</small></div>`).join('');
    const base = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#8b98b8', boxWidth: 12 } } }, scales: { x: { ticks: { color: '#8b98b8' }, grid: { color: 'rgba(255,255,255,.05)' } }, y: { ticks: { color: '#8b98b8', precision: 0 }, grid: { color: 'rgba(255,255,255,.07)' }, beginAtZero: true } } };
    const noScale = { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { color: '#8b98b8', boxWidth: 12 } } } };
    const mk = (id, cfg) => { if (charts[id]) { charts[id].data = cfg.data; charts[id].update('none'); } else charts[id] = new Chart($('#' + id), cfg); };
    mk('c1', { type: 'doughnut', data: { labels: Object.keys(a.by_priority), datasets: [{ data: Object.values(a.by_priority), backgroundColor: ['#ff3b52', '#ffa31a', '#4c8dff', '#2fe38a'], borderWidth: 0 }] }, options: noScale });
    mk('c2', { type: 'bar', data: { labels: Object.keys(a.by_type), datasets: [{ data: Object.values(a.by_type), backgroundColor: '#19e3c1', borderRadius: 6 }] }, options: { ...base, plugins: { legend: { display: false } } } });
    mk('c3', { type: 'line', data: { labels: a.per_day.map(d => d.label), datasets: [{ data: a.per_day.map(d => d.count), borderColor: '#ff3b52', backgroundColor: 'rgba(255,59,82,.15)', fill: true, tension: .35 }] }, options: { ...base, plugins: { legend: { display: false } } } });
    mk('c4', { type: 'bar', data: { labels: a.per_hour.map((_, i) => i), datasets: [{ data: a.per_hour, backgroundColor: '#4c8dff', borderRadius: 3 }] }, options: { ...base, plugins: { legend: { display: false } } } });
    mk('c5', { type: 'bar', data: { labels: a.rejections.map(r => r.reason), datasets: [{ data: a.rejections.map(r => r.count), backgroundColor: '#ffa31a', borderRadius: 5 }] }, options: { ...base, indexAxis: 'y', plugins: { legend: { display: false } } } });
    mk('c6', { type: 'bar', data: { labels: a.by_hospital.map(h => h.name.split(' ')[0]), datasets: [{ data: a.by_hospital.map(h => h.count), backgroundColor: '#9b7bff', borderRadius: 5 }] }, options: { ...base, plugins: { legend: { display: false } } } });
  }

  // ------------------------------------------------------------------ actions (event delegation)
  document.addEventListener('click', async e => {
    const el = e.target.closest('[data-act],[data-case]'); if (!el) return;
    if (el.dataset.act) {
      e.stopPropagation();
      try {
        if (el.dataset.act === 'hist') { histMode = el.dataset.v === '1'; paneSig = ''; renderPane(); }
        else if (el.dataset.act === 'accept') { const h = S.hospitals.find(x => x.id == el.dataset.h); await api('/api/hospitals/' + h.id, { method: 'PATCH', json: { accepting: !h.accepting } }); toast(`${h.name}: ${h.accepting ? 'DIVERSION on' : 'accepting again'}`, h.accepting ? 'warn' : 'ok'); poll(); }
        else if (el.dataset.act === 'adj') { const h = S.hospitals.find(x => x.id == el.dataset.h), f = el.dataset.f; await api('/api/hospitals/' + h.id, { method: 'PATCH', json: { [f]: Math.max(0, h[f] + +el.dataset.d) } }); poll(); }
        else if (el.dataset.act === 'force') { if (!el.dataset.h) return toast('Pick a hospital in the case details first', 'warn'); await api(`/api/cases/${el.dataset.id}/assign`, { json: { hospital_id: +el.dataset.h } }); toast('Approved by control room – ambulance dispatched', 'ok'); poll(); }
        else if (el.dataset.act === 'cancel') { if (confirm('Cancel this case?')) { await api(`/api/cases/${el.dataset.id}/cancel`, { json: {} }); toast('Case cancelled', 'warn'); closeDrawer(); poll(); } }
        else if (el.dataset.act === 'assign') { const hid = +$('#assignSel').value; await api(`/api/cases/${el.dataset.id}/assign`, { json: { hospital_id: hid } }); toast('Hospital assigned – ambulance dispatched'); poll(); }
        else if (el.dataset.act === 'print') printSheet();
        else if (el.dataset.act === 'close') closeDrawer();
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
    e.preventDefault(); openCase(el.dataset.case);
  });

  // ------------------------------------------------------------------ case drawer
  let detail = null;
  function closeDrawer() { openId = null; $('#drawer').classList.remove('open'); $('#scrim').classList.remove('open'); }
  async function openCase(id) { openId = id; $('#drawer').classList.add('open'); $('#scrim').classList.add('open'); await refreshDrawer(); const c = detail; if (c) view.panTo(c.lat, c.lng, 15); }
  async function refreshDrawer() {
    try { detail = await api('/api/cases/' + openId); } catch (e) { return; }
    const c = detail, tri = c.triage, v = c.vision || {}, hs = S ? S.hospitals : [];
    const waiting = ['awaiting_hospital', 'no_hospital'].includes(c.status);
    const rows = r => `<tr class="${r.violations && r.violations.length && !r._ok ? 'rej' : ''}"><td><b>${esc(r.name)}</b><br><small>${r.eta_min} min · ${r.km} km</small></td><td class="mono">${r.score}</td><td>${r.violations.length ? r.violations.map(x => `<span class="chip bad">${esc(x.text)}</span>`).join('') : '<span class="chip ok">all constraints met</span>'}</td></tr>`;
    const sev = v.severity || 0;
    $('#drawer').innerHTML = `<div class="row between"><div class="row wrapx" style="gap:8px"><h2 class="mono">${esc(c.id)}</h2>${prioBadge(c.priority)} ${statusBadge(c.status)}</div><button class="btn sm" data-act="close">✕</button></div>
      <p class="mut" style="margin:6px 0 12px">${TYPE_ICON[c.type] || ''} ${esc(c.type)} · ${c.patient.age}y · ${esc(c.place)} · reported ${fmtClock(c.created_ts)}${c.reporter && c.reporter.phone ? ` · <a style="color:var(--teal)" href="tel:${esc(c.reporter.phone)}">📞 ${esc(c.reporter.name || 'caller')}</a>` : ''}</p>
      ${c.photo ? `<img class="photo" src="${esc(c.photo)}" alt="scene">` : '<div class="chip">No photo supplied</div>'}
      <div class="card" style="margin-top:12px"><h3>📷 Photo analysis <span class="badge bs info">${v.provider === 'claude' ? 'Claude vision' : v.provider === 'heuristic' ? 'basic check' : v.provider || 'none'}</span></h3>
        <div class="sevbar">${[1, 2, 3, 4].map(i => `<i class="${i <= sev ? 'on' + (sev >= 3 ? ' hot' : '') : ''}"></i>`).join('')}</div><div>${esc(v.summary || 'No photo analysis')}</div>
        ${(v.injuries || []).map(i => `<span class="chip">${esc(i)}</span>`).join('')}${(v.hazards || []).map(i => `<span class="chip bad">⚠ ${esc(i)}</span>`).join('')}</div>
      <div class="card" style="margin-top:12px"><h3>🧮 Triage: ${esc(tri.label)} · risk ${tri.risk}/100 <small>(${esc(tri.scale)})</small></h3>
        ${tri.overrides.map(o => `<div class="chip bad">⬆ ${esc(o.priority)}: ${esc(o.reason)}</div>`).join('')}
        ${tri.factors.map(f => `<div class="factor"><span>${esc(f.name)} <small>${esc(f.value)}</small></span><b class="mono">+${f.points}</b><div class="meter ${f.points >= 3 ? 'hot' : f.points >= 2 ? 'mid' : ''}"><i style="width:${Math.min(100, f.points * 25)}%"></i></div></div>`).join('') || '<small>No scored factors</small>'}
        <hr><b>Pre-alert:</b> ${tri.prealert.map(p => `<div class="chip">${esc(p)}</div>`).join('')}
        <div style="margin-top:6px"><small>Needs: ${tri.needs.capability || 'general ED'} · ${tri.needs.care_level} bed${tri.needs.ventilator ? ' · ventilator' : ''}${tri.needs.pediatric ? ' · paediatric' : ''} · transport budget ${tri.needs.budget_min} min</small></div></div>
      ${c.route ? `<div class="card" style="margin-top:12px"><h3>🏥 Constraint routing ${c.route.tier ? `<span class="badge bp2">tier ${c.route.tier}</span>` : '<span class="badge bp4">tier 0</span>'}</h3><small>${esc(c.route.note)}</small>
        <table class="t"><tr><th>Hospital</th><th>Score</th><th>Constraints</th></tr>${c.route.ranked.map(r => rows({ ...r, _ok: 1 })).join('')}${c.route.rejected.map(rows).join('')}</table></div>` : ''}
      <div class="card" style="margin-top:12px"><h3>📟 Hospital alerts</h3>${c.alerts.map(a => `<div style="padding:5px 0;border-bottom:1px solid var(--line)">${esc((hs.find(h => h.id === a.hospital_id) || {}).name || a.hospital_id)} – <span class="badge bs ${a.status === 'approved' ? 'ok' : a.status === 'pending' ? 'warn' : 'bad'}">${a.status}</span> ${a.reason ? `<small>${esc(a.reason)}</small>` : ''} <small>${a.decided_by ? 'by ' + esc(a.decided_by) : ''}</small></div>`).join('') || '<small>None</small>'}</div>
      <div class="card" style="margin-top:12px"><h3>Timeline</h3><div class="timeline" style="max-height:none">${c.timeline.map(e => `<div class="${e.level}"><time>${fmtClock(e.ts)}</time><span>${esc(e.message)}</span></div>`).join('')}</div></div>
      <div class="row wrapx noprint" style="margin-top:14px">
        ${waiting ? `<select id="assignSel" style="width:auto;flex:1">${hs.map(h => `<option value="${h.id}" ${c.hospital && c.hospital.id === h.id ? 'selected' : ''}>${esc(h.name)}${h.accepting ? '' : ' (diversion)'}</option>`).join('')}</select><button class="btn green" data-act="assign" data-id="${c.id}">Assign &amp; dispatch</button>` : ''}
        <a class="btn" target="_blank" rel="noopener" href="${MapKit.navUrl(null, [c.lat, c.lng])}">Open scene in Google Maps ↗</a>
        <button class="btn" data-act="print">🖨 Handover sheet</button>
        ${['awaiting_hospital', 'no_hospital', 'pending', 'dispatched', 'on_scene', 'transporting'].includes(c.status) ? `<button class="btn" style="color:#ff8a98" data-act="cancel" data-id="${c.id}">Cancel case</button>` : ''}</div>`;
  }
  function printSheet() {
    const c = detail; if (!c) return; const t = c.triage, w = window.open('', '_blank');
    w.document.write(`<html><head><title>${c.id}</title><style>body{font:14px Arial;padding:24px;max-width:760px;margin:auto}h1{margin:0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:6px;text-align:left}img{max-width:100%;max-height:300px}</style></head><body>
      <h1>Pre-hospital handover – ${esc(c.id)}</h1><p><b>${esc(t.label)}</b> · ${esc(c.type)} · ${c.patient.age}y ${esc(c.patient.sex)} · ${esc(c.place)}<br>Destination: <b>${esc(c.hospital ? c.hospital.name : '–')}</b> · Ambulance ${esc(c.ambulance_id || '–')}</p>
      ${c.photo ? `<img src="${location.origin}${c.photo}">` : ''}<p>${esc((c.vision || {}).summary || '')}</p>
      <h3>Triage factors</h3><table><tr><th>Factor</th><th>Value</th><th>Points</th></tr>${t.factors.map(f => `<tr><td>${esc(f.name)}</td><td>${esc(f.value)}</td><td>${f.points}</td></tr>`).join('')}</table>
      <h3>Pre-alert</h3><ul>${t.prealert.map(p => `<li>${esc(p)}</li>`).join('')}</ul><p style="font-size:11px;color:#666">Generated by GoldenHour SOS – decision-support prototype, not clinically validated. Crew assessment supersedes.</p><script>onload=()=>print()<\/script></body></html>`);
    w.document.close();
  }
})();
