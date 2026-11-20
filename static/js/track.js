(async function () {
  let view = null, anim = null, last = null, aidDone = false, fitted = false, metro = false;
  view = await MapKit.create($('#map'), { dark: true, zoom: 14 });
  anim = new LegAnimator(view);

  $('#share').onclick = async () => {
    const url = location.href;
    try { if (navigator.share) await navigator.share({ title: 'Emergency tracking', url }); else { await navigator.clipboard.writeText(url); toast('Tracking link copied'); } } catch (e) {}
  };
  $('#metro').onclick = () => { metro = cprMetronome(!metro); $('#metro').textContent = metro ? '⏹ Stop metronome' : '🫀 Start CPR metronome (110/min)'; };
  $('#cancel').onclick = async () => {
    if (!confirm('Cancel this emergency? Only do this if help is no longer needed.')) return;
    try { await api(`/api/track/${CODE}/cancel`, { method: 'POST' }); toast('Emergency cancelled', 'warn'); refresh(); } catch (e) { toast(e.message, 'err'); }
  };

  function hero(c) {
    const h = $('#hero'); const hosp = c.hospital ? esc(c.hospital.name) : 'a hospital';
    let cls = 'wait', big = '', title = '', sub = '';
    const eta = c.eta_remaining_min;
    switch (c.status) {
      case 'awaiting_hospital': cls = 'wait'; title = TS('⏳ Ambulance reserved – waiting for hospital approval');
        sub = `Nearest ambulance <b>${esc(c.ambulance ? c.ambulance.id : '…')}</b> is being held for you. <b>${hosp}</b> has been alerted with your photo${c.alert ? ` (waiting ${c.alert.wait_s}s)` : ''}. The moment they approve, the ambulance leaves <b>urgently</b>.`; break;
      case 'no_hospital': cls = 'wait'; title = TS('⚠ Control room is finding a hospital'); sub = 'All nearby hospitals are busy or declined. A dispatcher is assigning a destination right now – stay with the patient.'; break;
      case 'pending': cls = 'wait'; title = TS('⏳ Approved – waiting for the next free ambulance'); sub = 'All ambulances are currently busy. You are first in the queue. Please also call 112.'; break;
      case 'dispatched': cls = ''; title = TS('🚨 Ambulance dispatched urgently'); big = fmtMin(eta); sub = `<b>${esc(c.ambulance.id)}</b> is on its way with lights &amp; sirens. <b>${hosp}</b> approved and is preparing.`; break;
      case 'on_scene': cls = 'ok'; title = TS('🚑 Ambulance has arrived'); sub = 'The crew is treating the patient. Follow their instructions.'; break;
      case 'transporting': cls = 'ok'; title = TS('🏥 On the way to hospital'); big = fmtMin(eta); sub = `Heading to <b>${hosp}</b>. The hospital has been pre-alerted.`; break;
      case 'at_hospital': cls = 'ok'; title = TS('✅ Arrived at hospital'); sub = `Patient handed over at <b>${hosp}</b>.`; break;
      case 'completed': cls = 'off'; title = TS('✔ Emergency completed'); sub = 'Thank you for acting quickly. Take care.'; break;
      case 'cancelled': cls = 'off'; title = TS('Cancelled'); sub = 'This emergency was cancelled.'; break;
    }
    h.className = 'status-hero ' + cls;
    h.innerHTML = `<div class="row between wrapx"><div style="flex:1;min-width:230px"><h2 style="font-size:22px;margin-bottom:8px">${title}</h2><div>${sub}</div>
      ${c.reroutes ? `<div class="chip bad" style="margin-top:8px">Destination changed ${c.reroutes}×</div>` : ''}</div>
      ${big ? `<div class="eta mono">${big.replace(/(\d+)/, '$1')}<small> ${T('ETA')}</small></div>` : ''}</div>
      ${c.golden_remaining_min != null && c.status !== 'cancelled' ? `<div class="chip" style="margin-top:10px">⏱ Golden-hour target: ${Math.max(0, Math.round(c.golden_remaining_min))} min left</div>` : ''}`;
  }

  let prevStatus = null;
  function notifyStatus(c) {
    if (prevStatus && prevStatus !== c.status && 'Notification' in window && Notification.permission === 'granted' && ['dispatched', 'on_scene', 'transporting', 'at_hospital', 'completed'].includes(c.status)) {
      const m = { dispatched: '🚨 Ambulance dispatched urgently', on_scene: '🚑 Ambulance has arrived', transporting: '🏥 On the way to hospital', at_hospital: '✅ Arrived at hospital', completed: '✔ Emergency completed' }[c.status];
      try { new Notification('GoldenHour SOS · ' + c.id, { body: TS(m), icon: '/static/icons/icon-192.png' }); } catch (e) {}
    }
    prevStatus = c.status;
  }
  $('#notif').onclick = async () => { const p = await Notification.requestPermission(); $('#notif').style.display = p === 'granted' ? 'none' : ''; if (p === 'granted') toast('🔔 You will be notified when status changes'); };
  if (!('Notification' in window) || Notification.permission === 'granted') $('#notif').style.display = 'none';
  document.addEventListener('langchange', () => { if (last) { aidDone = false; render(last); } });

  function render(c) {
    notifyStatus(c);
    $('#prio').innerHTML = `<span class="badge b${c.priority.toLowerCase()}">${esc(c.label)}</span> <span class="badge bs">${TYPE_ICON[c.type] || ''} ${esc(c.type)}</span>`;
    hero(c);
    $('#steps').innerHTML = c.steps.map(s => `<li class="${s.done ? 'done' : ''} ${s.active ? 'active' : ''}">${esc(T(s.label))}</li>`).join('');
    $('#log').innerHTML = c.timeline.slice().reverse().map(e => `<div class="${e.level}"><time>${fmtClock(e.ts)}</time><span>${esc(e.message)}</span></div>`).join('');
    $('#cancel').style.display = ['awaiting_hospital', 'no_hospital', 'pending', 'dispatched'].includes(c.status) ? '' : 'none';

    // ambulance card
    const a = c.ambulance, ac = $('#ambcard');
    if (a && !['completed', 'cancelled'].includes(c.status)) {
      ac.style.display = '';
      ac.innerHTML = `<div class="row between wrapx"><div><h3>🚑 ${esc(a.id)} <span class="badge bs">${esc(a.type)}</span></h3><div class="mut">${esc(a.crew)}</div></div>
        <div class="row wrapx"><a class="btn sm teal" href="tel:${esc(a.phone)}">📞 Call crew</a>${c.hospital ? `<a class="btn sm" target="_blank" rel="noopener" href="${MapKit.navUrl([c.lat, c.lng], [c.hospital.lat, c.hospital.lng])}">Route to hospital ↗</a>` : ''}</div></div>`;
    } else ac.style.display = 'none';

    // photo
    if (c.photo || (c.vision && c.vision.summary)) {
      $('#photocard').style.display = '';
      $('#photo').innerHTML = (c.photo ? `<img class="photo" src="${esc(c.photo)}" alt="Incident photo">` : '') +
        `<div style="margin-top:8px"><div class="sevbar">${[1, 2, 3, 4].map(i => `<i class="${i <= (c.vision.severity || 0) ? 'on' + (c.vision.severity >= 3 ? ' hot' : '') : ''}"></i>`).join('')}</div>
        <div style="font-size:14px">${esc(c.vision.summary || '')}</div>
        ${(c.vision.injuries || []).map(i => `<span class="chip">${esc(i)}</span>`).join('')}${(c.vision.hazards || []).map(i => `<span class="chip bad">⚠ ${esc(i)}</span>`).join('')}</div>`;
    }
    // first aid (once, but refresh if breathing issue)
    if (!aidDone) {
      aidDone = true;
      $('#aid').innerHTML = firstAid({ type: c.first_aid_type, bleeding: c.bleeding, burn: c.burn, status_not_breathing: ['P1', 'P2'].includes(c.priority) })
        .map(([k, t, b]) => `<div class="aid ${k}"><b>${esc(T(t))}</b>${esc(T(b))}</div>`).join('');
    }
    if (['completed', 'cancelled'].includes(c.status)) { $('#aidcard').style.display = 'none'; cprMetronome(false); }
  }

  function drawMap(c) {
    view.marker('inc', { lat: c.lat, lng: c.lng, kind: 'incident', color: PCOL[c.priority], label: c.priority, pulse: !['completed', 'cancelled', 'on_scene', 'transporting', 'at_hospital'].includes(c.status), title: 'You are here', z: 5 });
    if (c.hospital) view.marker('hosp', { lat: c.hospital.lat, lng: c.hospital.lng, kind: 'hospital', color: '#4c8dff', title: c.hospital.name, z: 3 }); else view.removeMarker('hosp');
    const a = c.ambulance;
    if (a) { anim.ensure(a); } else { view.removeMarker('amb'); view.removeLine('route'); }
    if (!fitted) { fitted = true; const pts = [[c.lat, c.lng]]; if (c.hospital) pts.push([c.hospital.lat, c.hospital.lng]); if (a) pts.push([a.lat, a.lng]); view.fit(pts, 70); }
  }

  function frame() {
    if (last && last.ambulance) {
      const a = last.ambulance, r = anim.at(a);
      if (r && r.pos) {
        view.marker('amb', { lat: r.pos[0], lng: r.pos[1], kind: 'ambulance', color: AMB_COL[a.status] || '#ff3b52', title: a.id, z: 9 });
        if (r.pts && a.leg && a.leg !== 'returning') view.line('route', MapKit.remainingPath(r.pts, r.t), { color: '#ff3b52', weight: 5 }); else view.removeLine('route');
      }
    }
    setTimeout(frame, 120);
  }
  frame();

  async function refresh() {
    try {
      const c = await api('/api/track/' + CODE);
      last = c; SimClock.sync(c.sim); $('#main').style.display = ''; $('#notfound').style.display = 'none';
      render(c); drawMap(c);
    } catch (e) {
      if (e.status === 404) { $('#main').style.display = 'none'; $('#notfound').style.display = ''; }
    }
  }
  (function loop() { refresh().finally(() => setTimeout(loop, 1500)); })();
})();
