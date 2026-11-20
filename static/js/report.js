(async function () {
  const S = { incident: null, conscious: 'Alert', breathing: 'Normal', bleeding: 'None', age_group: 'adult', bleedTouched: false, flags: {}, blob: null, lat: null, lng: null, busy: false };
  let view = null;
  const cfg = await MapKit.config();

  // ---------------------------------------------------------------- incident tiles & pills
  $$('#tiles .tile').forEach(t => t.onclick = () => {
    $$('#tiles .tile').forEach(x => x.classList.remove('on')); t.classList.add('on'); S.incident = t.dataset.k;
    $('#c-stroke').style.display = S.incident === 'stroke' ? '' : 'none';
    $('#c-preg').style.display = S.incident === 'pregnancy' ? '' : 'none';
    $('#c-fire').style.display = S.incident === 'fire' ? '' : 'none';
    if (S.incident === 'fire') { $$('[data-g=bleeding] .pill')[0].click(); }
  });
  $$('.pills[data-g]').forEach(g => $$('.pill', g).forEach(p => p.onclick = () => {
    $$('.pill', g).forEach(x => x.classList.remove('on')); p.classList.add('on'); S[g.dataset.g] = p.dataset.v;
    if (g.dataset.g === 'bleeding' && p.isTrusted) S.bleedTouched = true;
  }));
  $$('.pill.tg').forEach(p => p.onclick = () => { p.classList.toggle('on'); S.flags[p.dataset.n] = p.classList.contains('on'); });

  // ---------------------------------------------------------------- photo
  const pick = f => { if (f) handlePhoto(f); };
  $('#btnCam').onclick = () => $('#fileCam').click();
  $('#btnGal').onclick = () => $('#fileGal').click();
  $('#fileCam').onchange = e => pick(e.target.files[0]);
  $('#fileGal').onchange = e => pick(e.target.files[0]);
  const dz = $('#dropzone');
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('over'); }; dz.ondragleave = () => dz.classList.remove('over');
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove('over'); pick(e.dataTransfer.files[0]); };
  $('#btnRetake').onclick = () => { S.blob = null; $('#previewWrap').style.display = 'none'; dz.style.display = ''; $('#finding').innerHTML = ''; };

  function compress(file, max = 1600) {
    return new Promise((res, rej) => {
      const url = URL.createObjectURL(file); const img = new Image();
      img.onload = () => {
        const k = Math.min(1, max / Math.max(img.width, img.height)); const c = document.createElement('canvas');
        c.width = Math.round(img.width * k); c.height = Math.round(img.height * k); c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url); c.toBlob(b => b ? res(b) : rej(new Error('Could not process image')), 'image/jpeg', .85);
      };
      img.onerror = () => { URL.revokeObjectURL(url); rej(new Error('That file is not a readable image')); };
      img.src = url;
    });
  }

  async function handlePhoto(file) {
    if (!file.type.startsWith('image/')) return toast('Please choose an image file', 'err');
    try { S.blob = await compress(file); } catch (e) { return toast(e.message, 'err'); }
    $('#preview').src = URL.createObjectURL(S.blob);
    dz.style.display = 'none'; $('#previewWrap').style.display = ''; $('#scan').style.display = '';
    $('#finding').innerHTML = '<div class="finding"><span class="spin"></span> &nbsp;Analysing photo for injuries…</div>';
    const fd = new FormData(); fd.append('photo', S.blob, 'scene.jpg');
    try {
      const r = await api('/api/analyze', { method: 'POST', body: fd });
      renderFinding(r);
      if (!S.bleedTouched && r.bleeding && r.bleeding !== 'None') {
        const v = r.bleeding === 'Severe' ? 'Heavy' : 'Some'; $$('[data-g=bleeding] .pill').find(p => p.dataset.v === v)?.click();
      }
    } catch (e) { $('#finding').innerHTML = `<div class="finding bad">Could not analyse photo (${esc(e.message)}). You can still send your SOS.</div>`; }
    $('#scan').style.display = 'none';
  }

  function renderFinding(r) {
    const bad = r.injury_detected && r.severity >= 2;
    const bars = [1, 2, 3, 4].map(i => `<i class="${i <= r.severity ? 'on' + (r.severity >= 3 ? ' hot' : '') : ''}"></i>`).join('');
    const who = r.provider === 'claude' ? '🤖 Claude vision' : '🔍 Basic local check (add an AI key for full detection)';
    $('#finding').innerHTML = `<div class="finding ${bad ? 'bad' : ''}">
      <div class="row between wrapx"><b>${r.injury_detected ? '⚠ Possible injury detected' : '✓ No obvious injury detected'}</b><span class="badge bs info">${who}</span></div>
      <div class="sevbar">${bars}</div>
      <div style="font-size:14px">${esc(r.summary || '')}</div>
      <div style="margin-top:6px">${r.bleeding !== 'None' ? `<span class="chip bad">Bleeding: ${esc(r.bleeding)}</span>` : ''}${r.burn ? `<span class="chip bad">Burn ~${Math.round(r.burn_tbsa)}%</span>` : ''}${r.unresponsive_visible ? '<span class="chip bad">Person motionless</span>' : ''}${(r.injuries || []).map(i => `<span class="chip">${esc(i)}</span>`).join('')}${(r.hazards || []).map(i => `<span class="chip bad">⚠ ${esc(i)}</span>`).join('')}</div>
      ${!r.quality.ok || r.quality.note ? `<div class="mut" style="font-size:12.5px;margin-top:6px">ℹ ${esc(r.quality.note)}</div>` : ''}
      <div class="mut" style="font-size:12px;margin-top:6px">Automatic analysis only supports triage – the ambulance crew will verify on scene.</div></div>`;
  }

  // ---------------------------------------------------------------- location
  function setLoc(lat, lng, pan) {
    S.lat = lat; S.lng = lng;
    view.marker('me', { lat, lng, kind: 'pin', color: '#ff3b52', title: 'Incident location', z: 10 });
    $('#locTxt').textContent = `📍 ${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    $('#outside').style.display = 'none';
    if (pan) view.panTo(lat, lng, 16);
  }
  view = await MapKit.create($('#map'), { dark: true, zoom: 13, onClick: (a, b) => setLoc(a, b) });
  function gps() {
    if (!navigator.geolocation) return toast('GPS not available – tap the map instead', 'warn');
    $('#locTxt').textContent = 'Getting your location…';
    navigator.geolocation.getCurrentPosition(p => setLoc(p.coords.latitude, p.coords.longitude, true),
      () => { $('#locTxt').textContent = 'Location blocked – tap the map to place the pin'; toast('Could not get GPS – tap the map to set the location', 'warn'); },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 20000 });
  }
  let qt = null;
  $('#q').addEventListener('input', e => {
    clearTimeout(qt); const v = e.target.value.trim(); const box = $('#qres');
    if (v.length < 2) { box.style.display = 'none'; return; }
    qt = setTimeout(async () => {
      let res = []; try { res = await MapKit.search(v); } catch (x) {}
      box.innerHTML = res.length ? res.map((r, i) => `<div data-i="${i}">📍 ${esc(r.name)}<small class="mut">${esc(r.label)}</small></div>`).join('') : '<div class="mut">No match – tap the map instead</div>';
      box.style.display = ''; box._res = res;
      $$('div[data-i]', box).forEach(d => d.onclick = () => { const r = box._res[+d.dataset.i]; setLoc(r.lat, r.lng, true); $('#place').value = r.name; $('#q').value = r.name; box.style.display = 'none'; });
    }, 350);
  });
  document.addEventListener('click', e => { if (!e.target.closest('.searchbox')) $('#qres').style.display = 'none'; });
  $('#btnGps').onclick = gps;
  gps();

  // ---------------------------------------------------------------- submit
  const steps = $$('#prog div');
  function setStep(n) { steps.forEach((d, i) => { d.className = i < n ? 'done' : i === n ? 'on' : ''; d.firstElementChild.className = i < n ? '' : 'spin'; d.firstElementChild.textContent = i < n ? '✔' : ''; }); }

  $('#f').onsubmit = async e => {
    e.preventDefault(); if (S.busy) return;
    if (!S.incident) { toast('Please choose what happened (step 1)', 'warn'); $('#tiles').scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    if (S.lat == null) { toast('Please set the location – use GPS or tap the map', 'warn'); $('#map').scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    S.busy = true; $('#send').disabled = true;
    const fd = new FormData();
    fd.append('incident', S.incident); ['conscious', 'breathing', 'bleeding', 'age_group'].forEach(k => fd.append(k, S[k]));
    Object.entries(S.flags).forEach(([k, v]) => v && fd.append(k, '1'));
    if (S.incident === 'stroke') fd.append('onset_min', $('#onset_min').value || 30);
    fd.append('lat', S.lat); fd.append('lng', S.lng); fd.append('place', $('#place').value); fd.append('name', $('#name').value);
    fd.append('phone', $('#phone').value); fd.append('notes', $('#notes').value); fd.append('website', $('input[name=website]').value);
    if (S.blob) fd.append('photo', S.blob, 'scene.jpg');
    $('#ov').style.display = 'grid'; setStep(0); let n = 0; const iv = setInterval(() => { n = Math.min(3, n + 1); setStep(n); }, 700);
    try {
      const r = await api('/api/report', { method: 'POST', body: fd });
      clearInterval(iv); setStep(4); $('#ovh').textContent = 'SOS sent ✔';
      try { localStorage.setItem('sos_last', r.code); } catch (_) {}
      setTimeout(() => location.href = r.track_url, 900);
    } catch (err) {
      clearInterval(iv); $('#ov').style.display = 'none'; S.busy = false; $('#send').disabled = false;
      if (err.code === 'outside_area') {
        const o = $('#outside'); o.style.display = 'block';
        o.innerHTML = `<b>Outside demo area.</b> ${esc(err.message)}<br><button type="button" class="btn sm" id="demoLoc" style="margin-top:8px">Use a demo location in Pune</button>`;
        $('#demoLoc').onclick = () => setLoc(cfg.center[0] + (Math.random() - .5) * .03, cfg.center[1] + (Math.random() - .5) * .03, true);
      } else toast(err.message, 'err', 6000);
    }
  };
})();
