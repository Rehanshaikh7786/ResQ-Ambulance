(async function () {
  let aid = 'AMB-01'; try { aid = localStorage.getItem('sos_amb') || aid; } catch (e) {}
  let V = null, fitted = '';
  const view = await MapKit.create($('#map'), { dark: true, zoom: 14 });
  const st = await api('/api/state');
  $('#asel').innerHTML = st.ambulances.map(a => `<option value="${a.id}" ${a.id === aid ? 'selected' : ''}>${a.id} (${a.type})</option>`).join('');
  $('#asel').onchange = e => { aid = e.target.value; try { localStorage.setItem('sos_amb', aid); } catch (x) {} fitted = ''; load(); };

  const MSG = {
    available: ['Standing by at base', 'You will be alerted when a hospital approves a case.', 'off'],
    reserved: ['Held for an emergency', 'Stay ready – waiting for hospital approval. Dispatch can come any second.', 'wait'],
    dispatched: ['🚨 GO – urgent dispatch', 'Hospital approved. Lights & sirens. Drive safely.', ''],
    on_scene: ['On scene', 'Assess, stabilise, load the patient.', 'ok'],
    transporting: ['Transporting to hospital', 'Hospital has been pre-alerted.', 'ok'],
    at_hospital: ['At hospital – handover', 'Complete the handover; you will be released automatically.', 'ok'],
    returning: ['Returning to base', 'Available for re-tasking once back.', 'off']
  };

  async function load() {
    try { V = await api('/api/driver/' + aid); } catch (e) { return; }
    const a = V.ambulance, c = V.case, [t, s, cls] = MSG[a.status] || [a.status, '', 'off'];
    $('#status').className = 'status-hero ' + cls;
    $('#status').innerHTML = `<h2 style="font-size:24px">${t}</h2><div style="margin-top:6px">${s}</div><div class="mut" style="margin-top:8px">${a.id} · ${a.type} · ${esc(a.crew)} · ${a.trips} trips today</div>`;
    view.marker('amb', { lat: a.lat, lng: a.lng, kind: 'ambulance', color: AMB_COL[a.status], title: a.id, z: 9 });
    const job = $('#job');
    if (!c) { job.innerHTML = '<div class="empty card">No active assignment.</div>'; view.removeMarker('inc'); view.removeMarker('hosp'); view.removeLine('l'); if (fitted !== 'idle') { fitted = 'idle'; view.panTo(a.lat, a.lng, 14); } return; }
    view.marker('inc', { lat: c.lat, lng: c.lng, kind: 'incident', color: PCOL[c.priority], label: c.priority, pulse: true, title: 'Patient', z: 5 });
    if (c.hospital) view.marker('hosp', { lat: c.hospital.lat, lng: c.hospital.lng, kind: 'hospital', color: '#4c8dff', title: c.hospital.name, z: 3 });
    const toScene = ['reserved', 'dispatched'].includes(a.status);
    const dest = toScene ? [c.lat, c.lng] : (c.hospital ? [c.hospital.lat, c.hospital.lng] : [c.lat, c.lng]);
    const key = c.id + a.status;
    if (fitted !== key && ['dispatched', 'transporting', 'on_scene'].includes(a.status)) { fitted = key; view.fit([[a.lat, a.lng], dest], 70); view.route([a.lat, a.lng], dest).then(p => view.line('l', p, { color: '#ff3b52', weight: 5 })); }
    const v = c.vision || {};
    job.innerHTML = `<div class="card"><div class="row between wrapx"><div class="row" style="gap:8px">${prioBadge(c.priority)} <b class="mono">${esc(c.id)}</b> <span class="badge bs">${TYPE_ICON[c.type] || ''} ${esc(c.type)}</span></div><span class="mut">${esc(c.place)}</span></div>
      <p style="margin:10px 0 6px;font-weight:600">${esc(c.summary)}</p>
      ${c.photo ? `<img class="photo" src="${esc(c.photo)}" alt="scene">` : ''}<p class="mut" style="font-size:14px">${esc(v.summary || '')}</p>
      ${(v.hazards || []).map(i => `<span class="chip bad">⚠ ${esc(i)}</span>`).join('')}
      <div style="margin-top:8px"><b>Pre-alert sent:</b> ${c.triage.prealert.map(p => `<span class="chip">${esc(p)}</span>`).join('')}</div></div>
      <div class="grid g2"><a class="btn teal lg" target="_blank" rel="noopener" href="${MapKit.navUrl([a.lat, a.lng], [c.lat, c.lng])}">🧭 Navigate to patient</a>
        ${c.hospital ? `<a class="btn lg" target="_blank" rel="noopener" href="${MapKit.navUrl([c.lat, c.lng], [c.hospital.lat, c.hospital.lng])}">🏥 Navigate to ${esc(c.hospital.name)}</a>` : ''}
        ${c.reporter && c.reporter.phone ? `<a class="btn" href="tel:${esc(c.reporter.phone)}">📞 Call caller ${esc(c.reporter.name || '')}</a>` : ''}
        ${c.hospital ? `<a class="btn" href="tel:${esc(c.hospital.phone)}">📞 Call hospital</a>` : ''}</div>`;
  }
  (function loop() { load().finally(() => setTimeout(loop, 1500)); })();
})();
