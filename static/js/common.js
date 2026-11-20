/* Shared helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const o = Object.assign({ headers: {} }, opts);
  if (o.json !== undefined) { o.method = o.method || 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.json); }
  let r;
  try { r = await fetch(path, o); } catch (e) { throw new Error('Network error – is the server running?'); }
  let data = null; try { data = await r.json(); } catch (e) {}
  if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('Login required'); }
  if (!r.ok) { const err = new Error((data && data.error) || ('Request failed (' + r.status + ')')); err.code = data && data.code; err.status = r.status; throw err; }
  return data;
}

function toast(msg, type = 'ok', ms = 3800) {
  let box = $('.toasts'); if (!box) { box = document.createElement('div'); box.className = 'toasts'; document.body.appendChild(box); }
  const t = document.createElement('div'); t.className = 'toast ' + type; t.textContent = msg; box.appendChild(t);
  setTimeout(() => { t.style.opacity = 0; t.style.transition = '.3s'; setTimeout(() => t.remove(), 300); }, ms);
}

const SimClock = {
  ts: Date.now() / 1000, at: Date.now() / 1000, speed: 1, paused: false,
  sync(s) { this.ts = s.ts; this.at = Date.now() / 1000; this.speed = s.speed; this.paused = s.paused; },
  now() { return this.paused ? this.ts : this.ts + (Date.now() / 1000 - this.at) * this.speed; }
};
const fmtClock = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtDT = ts => new Date(ts * 1000).toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
const fmtMin = m => m == null ? '–' : (m < 1 ? '<1 min' : Math.round(m) + ' min');
const fmtCountdown = sec => { sec = Math.max(0, Math.round(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); };

const STATUS = {
  awaiting_hospital: ['Awaiting hospital', 'warn'], no_hospital: ['NO HOSPITAL', 'bad'], pending: ['Queued – no ambulance', 'bad'],
  dispatched: ['Ambulance en route', 'info'], on_scene: ['On scene', 'info'], transporting: ['Transporting', 'info'],
  at_hospital: ['At hospital', 'ok'], completed: ['Completed', 'ok'], cancelled: ['Cancelled', '']
};
const statusBadge = s => { const [t, c] = STATUS[s] || [s, '']; return `<span class="badge bs ${c}">${t}</span>`; };
const prioBadge = p => `<span class="badge b${p.toLowerCase()}">${p}</span>`;
const PCOL = { P1: '#ff3b52', P2: '#ffa31a', P3: '#4c8dff', P4: '#2fe38a' };
const TYPE_ICON = { Trauma: '🩹', Cardiac: '❤️', Stroke: '🧠', Burn: '🔥', Obstetric: '🤰', Respiratory: '🫁', Medical: '⚕️' };
const AMB_COL = { available: '#2fe38a', reserved: '#ffb020', dispatched: '#ff3b52', on_scene: '#ff7a45', transporting: '#ff3b52', at_hospital: '#9b7bff', returning: '#8b98b8' };

function beep(times = 2, freq = 880) {
  try {
    const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
    for (let i = 0; i < times; i++) {
      const o = ctx.createOscillator(), g = ctx.createGain(); o.frequency.value = freq; o.type = 'square';
      g.gain.setValueAtTime(.0001, ctx.currentTime + i * .28); g.gain.exponentialRampToValueAtTime(.15, ctx.currentTime + i * .28 + .02); g.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + i * .28 + .2);
      o.connect(g); g.connect(ctx.destination); o.start(ctx.currentTime + i * .28); o.stop(ctx.currentTime + i * .28 + .22);
    }
  } catch (e) {}
}

/* Smoothly animates ambulances along real road geometry using the simulation clock. */
class LegAnimator {
  constructor(view) { this.view = view; this.paths = {}; }
  ensure(a) {
    if (!a.leg || !a.leg_from) return;
    const key = [a.id, a.leg, a.leg_from, a.leg_to].join('|');
    if (this.paths[a.id] && this.paths[a.id].key === key) return;
    this.paths[a.id] = { key, pts: [a.leg_from, a.leg_to] };
    this.view.route(a.leg_from, a.leg_to).then(pts => { if (this.paths[a.id] && this.paths[a.id].key === key) this.paths[a.id].pts = pts; });
  }
  at(a) {
    if (!a.leg || !a.leg_dur) return { pos: [a.lat, a.lng], t: null, pts: null };
    const t = Math.max(0, Math.min(1, (SimClock.now() - a.leg_start) / a.leg_dur));
    const p = this.paths[a.id];
    const pts = p ? p.pts : [a.leg_from, a.leg_to];
    return { pos: MapKit.pointAlong(pts, t) || [a.lat, a.lng], t, pts };
  }
}

function firstAid(info) {
  const out = [];
  out.push(['red', 'Stay safe & stay with the patient', 'Keep away from traffic, fire or hazards. Do not move the person unless they are in danger – especially with head, neck or back injury.']);
  if (info.status_not_breathing) out.push(['red', 'If the person stops breathing normally – start hands-only CPR', 'Push hard and fast in the centre of the chest, 5–6 cm deep, 100–120 times a minute. Do not stop until help arrives. Use the metronome below.']);
  if (info.bleeding && info.bleeding !== 'None') out.push(['red', 'Control the bleeding', 'Press firmly on the wound with a clean cloth. If it soaks through, add more on top – do not remove it. Do not remove objects stuck in the wound. Raise the limb if possible.']);
  const t = info.type;
  if (info.burn || t === 'Burn') out.push(['', 'Burns', 'Cool the burn under cool running water for 20 minutes. Remove rings/watches near the burn unless stuck. Cover loosely with clean cling film. No ice, butter or creams.']);
  if (t === 'Cardiac') out.push(['', 'Chest pain', 'Sit them down, keep calm, loosen tight clothing. Do not let them walk or drive. If they collapse and are not breathing normally, start CPR.']);
  if (t === 'Stroke') out.push(['', 'Suspected stroke', 'Note the time symptoms started and tell the crew. Give nothing to eat or drink. Lay them down with head slightly raised and check breathing.']);
  if (t === 'Obstetric') out.push(['', 'Pregnancy emergency', 'Lay her on her left side, keep her warm and calm. Do not let her push unless the baby is crowning. Keep towels ready.']);
  if (t === 'Respiratory') out.push(['', 'Breathing difficulty', 'Sit upright, loosen clothing, keep calm. Help them use their own prescribed inhaler if they have one.']);
  if (t === 'Trauma') out.push(['', 'Injury', 'Keep the person still and warm. Do not give food or drink. Support any suspected broken limb in the position found.']);
  out.push(['', 'Keep your phone free', 'The crew may call you. Send someone to the road to wave the ambulance in, and unlock gates/lifts.']);
  return out;
}

let _metro = null;
function cprMetronome(on) {
  if (_metro) { clearInterval(_metro); _metro = null; }
  if (on) { beep(1, 660); _metro = setInterval(() => beep(1, 660), 545); }   // 110 bpm
  return !!_metro;
}

function simSpeedLabel(s) { return s <= 1 ? 'Real time' : s + '× demo speed'; }
