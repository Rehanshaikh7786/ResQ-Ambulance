(async function () {
  let hid = 1; try { hid = +localStorage.getItem('sos_hosp') || 1; } catch (e) {}
  let V = null, sideSig = '', known = new Set(), first = true, soundOn = false;
  const CAPS = { trauma: 'Trauma', cardiac: 'Cardiac', stroke: 'Stroke', burn: 'Burn', pediatric: 'Paediatrics', obstetric: 'Obstetrics', ct: 'CT', cath_lab: 'Cath lab', nicu: 'NICU' };
  const REASONS = ['No ICU bed available', 'No ventilator free', 'Specialist not on call', 'Equipment down', 'Emergency department overloaded', 'Other'];

  const st = await api('/api/state');
  $('#hsel').innerHTML = st.hospitals.map(h => `<option value="${h.id}" ${h.id === hid ? 'selected' : ''}>${esc(h.name)}</option>`).join('');
  $('#hsel').onchange = e => { hid = +e.target.value; try { localStorage.setItem('sos_hosp', hid); } catch (x) {} known = new Set(); first = true; sideSig = ''; $('#pending').innerHTML = ''; load(); };
  document.body.addEventListener('click', () => { soundOn = true; }, { once: true });

  async function act(alertId, decision, reason) {
    try {
      await api(`/api/alerts/${alertId}/decide`, { json: { decision, reason, by: 'Hospital desk – ' + (V ? V.hospital.name : '') } });
      toast(decision === 'approve' ? '✅ Approved – ambulance being dispatched urgently' : 'Declined – next hospital will be alerted', decision === 'approve' ? 'ok' : 'warn'); load();
    } catch (e) { toast(e.message, 'err'); load(); }
  }

  function cardHtml(c) {
    const t = c.triage, v = c.vision || {};
    const viol = c.violations || [];
    return `<div class="acard ${c.priority === 'P2' ? 'p2' : ''}" data-alert="${c.alert_id}">
      <div class="row between wrapx"><div class="row wrapx" style="gap:8px">${prioBadge(c.priority)} <b class="mono" style="font-size:18px">${esc(c.id)}</b> <span class="badge bs">${TYPE_ICON[c.type] || ''} ${esc(c.type)}</span> <span class="badge bs">${c.patient.age}y</span></div>
        <div class="row" style="gap:10px"><div style="text-align:right"><small>Reply within</small><div class="mono cd" style="font:700 20px var(--head)">–</div></div><div class="ring"><span class="mono ring-s">–</span></div></div></div>
      <div class="grid" style="grid-template-columns:${c.photo ? '170px 1fr' : '1fr'};gap:14px;margin-top:12px">
        ${c.photo ? `<img src="${esc(c.photo)}" alt="scene" style="width:100%;height:150px;object-fit:cover;border-radius:12px;border:1px solid var(--line)">` : ''}
        <div><div style="font-weight:600">${esc(c.summary)}</div>
          <div class="mut" style="font-size:13.5px;margin:4px 0">${esc(v.summary || 'No photo analysis')}</div>
          ${(v.injuries || []).map(i => `<span class="chip">${esc(i)}</span>`).join('')}${(v.hazards || []).map(i => `<span class="chip bad">⚠ ${esc(i)}</span>`).join('')}
          <div style="margin-top:6px"><span class="chip ok">ETA to you ${fmtMin(c.eta_hospital)} · ${c.km_hospital} km</span><span class="chip">Risk ${t.risk}/100</span><span class="chip">${t.needs.care_level} bed${t.needs.ventilator ? ' + ventilator' : ''}</span>${t.needs.capability ? `<span class="chip">${esc(t.needs.capability)} team</span>` : ''}${t.needs.pediatric ? '<span class="chip">paediatric</span>' : ''}</div></div></div>
      <div style="margin-top:10px"><small><b>Prepare:</b> ${t.prealert.map(esc).join(' · ')}</small></div>
      ${viol.length ? `<div style="margin-top:6px">${viol.map(x => `<span class="chip bad">⚠ ${esc(x.text)}</span>`).join('')}<small> – you can still approve if you can make room</small></div>` : ''}
      <div class="row wrapx" style="margin-top:14px"><button class="btn green lg" data-a="approve" style="flex:1">✅ APPROVE – send ambulance</button>
        <select class="reason" style="width:auto;flex:1;min-width:150px">${REASONS.map(r => `<option>${r}</option>`).join('')}</select><button class="btn" data-a="decline" style="color:#ff8a98">Decline</button></div>
      <div class="auto mut" style="font-size:12px;margin-top:8px"></div></div>`;
  }

  function renderPending() {
    const box = $('#pending'), ids = new Set(V.pending.map(p => p.alert_id));
    $('#pcount').textContent = V.pending.length;
    if (!V.pending.length) { if (!$('.empty', box)) box.innerHTML = '<div class="empty card">No pending alerts. New emergencies appear here with a sound alert.</div>'; }
    else $('.empty', box)?.remove();
    $$('.acard', box).forEach(el => { if (!ids.has(+el.dataset.alert)) el.remove(); });
    V.pending.forEach(c => {
      let el = $(`.acard[data-alert="${c.alert_id}"]`, box);
      if (!el) {
        const d = document.createElement('div'); d.innerHTML = cardHtml(c); el = d.firstElementChild; box.appendChild(el);
        $('[data-a=approve]', el).onclick = () => act(c.alert_id, 'approve');
        $('[data-a=decline]', el).onclick = () => act(c.alert_id, 'decline', $('.reason', el).value);
        if (!known.has(c.alert_id) && !first) { toast(`🚨 New ${c.priority} emergency ${c.id} – decision needed`, 'err', 6000); beep(3, 988); }
        known.add(c.alert_id);
      }
      $('.cd', el).textContent = fmtCountdown(c.seconds_left); $('.ring-s', el).textContent = c.seconds_left;
      $('.ring', el).style.setProperty('--v', Math.round(100 * c.seconds_left / V.timeout));
      $('.auto', el).textContent = c.auto_in != null ? `Demo mode: simulated hospital staff will auto-reply in ~${c.auto_in}s unless you decide first.` : '';
    });
    first = false;
  }

  function renderInbound() {
    $('#inbound').innerHTML = V.inbound.length ? V.inbound.map(c => `<div class="item ${c.priority.toLowerCase()}" style="cursor:default"><div class="row between"><div class="row" style="gap:8px">${prioBadge(c.priority)} <b class="mono">${esc(c.id)}</b> ${statusBadge(c.status)}</div><b class="mono">${c.eta_remaining_min != null ? 'ETA ' + fmtMin(c.eta_remaining_min) : ''}</b></div>
      <div style="font-size:14px;margin-top:5px">${esc(c.summary)}</div><small>${c.ambulance_id || ''} · ${c.bed_type ? c.bed_type + ' bed reserved' : 'no bed reserved'}</small></div>`).join('') : '<div class="empty card">No inbound ambulances.</div>';
  }

  function meter(v, t, label, f) {
    const pct = t ? 100 * (t - v) / t : 100; const cls = pct > 85 ? 'hot' : pct > 65 ? 'mid' : '';
    return `<div style="margin:10px 0"><div class="row between"><span class="mut" style="font-size:13px">${label}</span><span class="stepper"><button data-f="${f}" data-d="-1">−</button><b class="mono">${v}</b><button data-f="${f}" data-d="1">+</button><small>/${t}</small></span></div><div class="meter ${cls}" style="margin-top:5px"><i style="width:${pct}%"></i></div></div>`;
  }
  function renderSide() {
    const h = V.hospital;
    const sig = JSON.stringify([h.icu_free, h.er_free, h.vent_free, h.or_free, h.accepting, h.oncall, V.mode]);
    if (sig === sideSig) return; sideSig = sig;
    $('#side').innerHTML = `<div class="card"><div class="row between"><h3 style="margin:0">Capacity</h3><div class="row" style="gap:8px"><small>${h.accepting ? 'Accepting' : '<b style="color:var(--red)">ON DIVERSION</b>'}</small><div class="switch ${h.accepting ? 'on' : ''}" id="acc"></div></div></div>
      ${meter(h.icu_free, h.icu_total, 'Free ICU beds', 'icu_free')}${meter(h.er_free, h.er_total, 'Free ER beds', 'er_free')}${meter(h.vent_free, h.vent_total, 'Free ventilators', 'vent_free')}${meter(h.or_free, h.or_total, 'Free operating theatres', 'or_free')}
      <small>Keeping these accurate makes routing safer. Diversion immediately re-routes ambulances already heading here.</small></div>
      <div class="card"><h3>Specialists on call</h3>${Object.entries(CAPS).filter(([k]) => h.caps[k]).map(([k, l]) => `<div class="row between" style="padding:6px 0"><span>${l}</span><div class="switch ${h.oncall[k] ? 'on' : ''}" data-oc="${k}"></div></div>`).join('')}</div>
      <div class="card"><small>${esc(h.name)} · ${esc(h.area)} · Level ${h.level}<br>${esc(h.phone)}</small></div>`;
    $('#acc').onclick = () => patch({ accepting: !h.accepting });
    $$('[data-f]', $('#side')).forEach(b => b.onclick = () => patch({ [b.dataset.f]: Math.max(0, h[b.dataset.f] + +b.dataset.d) }));
    $$('[data-oc]', $('#side')).forEach(b => b.onclick = () => patch({ oncall: { [b.dataset.oc]: !h.oncall[b.dataset.oc] } }));
  }
  async function patch(d) { try { await api('/api/hospitals/' + hid, { method: 'PATCH', json: d }); load(); } catch (e) { toast(e.message, 'err'); } }

  async function load() {
    try { V = await api(`/api/hospital/${hid}/view`); } catch (e) { return; }
    const mb = $('#modebar');
    mb.style.display = V.mode === 'auto' ? '' : 'none';
    mb.innerHTML = 'ℹ️ <b>Demo mode:</b> simulated hospital staff auto-reply to alerts after a few seconds so the whole flow runs end-to-end. Switch to <b>manual approval</b> in the Control Room (🏥 button) to decide yourself.';
    renderPending(); renderInbound(); renderSide();
  }
  (function loop() { load().finally(() => setTimeout(loop, 1500)); })();
})();
