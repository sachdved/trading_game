/* Glosten–Milgrom trading game — client for landing / player / host / board views. */
'use strict';

/* ------------------------------------------------ constants & utils */

/* URLs: /            landing (create or join a room)
         /r/CODE      player view for room CODE
         /r/CODE/host host view      /r/CODE/board  projector view */
const ROOM_M = location.pathname.match(/^\/r\/([A-Za-z]{5})(?:\/(host|board))?\/?$/);
const ROOM = ROOM_M ? ROOM_M[1].toUpperCase() : null;
const KIND = ROOM ? (ROOM_M[2] || 'player') : 'landing';
document.body.classList.add(KIND);

const R = p => `/r/${ROOM}${p}`;                       // room-scoped URL
const lsKey = k => `gm:${ROOM}:${k}`;                  // per-room localStorage
const getTok = () => localStorage.getItem(lsKey('token')) || '';
const setTok = t => localStorage.setItem(lsKey('token'), t);
const delTok = () => localStorage.removeItem(lsKey('token'));

const SUIT = { h: '♥', s: '♠', d: '♦', c: '♣' };
const RED = { h: true, d: true };

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => String(Math.round((+n) * 100) / 100);
const signed = n => (n > 0 ? '+' : '') + fmt(n);
const numCls = n => n > 0 ? 'num pos' : n < 0 ? 'num neg' : 'num';
const moneyHTML = n => `<span class="${numCls(n)}">${signed(n)}</span>`;

const DEFAULT_CARD_VALUES = { A: -40, K: 20, Q: 0, J: 0 };
function cardValues() { return S?.settings?.cardValues || DEFAULT_CARD_VALUES; }
function cardPoints(c) {
  if (!c || c.suit === 'd' || c.suit === 'c') return 0;
  const vals = cardValues();
  if (c.rank in vals) return vals[c.rank];
  return parseInt(c.rank, 10);
}

function cardHTML(c, cls = '', withPts = false) {
  let inner;
  if (!c) {
    inner = `<div class="pcard back ${cls}"><span class="st">?</span></div>`;
  } else {
    const color = RED[c.suit] ? 'red' : 'blk';
    inner = `<div class="pcard ${color} ${cls}">
      <span class="rk">${c.rank}</span><span class="st">${SUIT[c.suit]}</span>
      <span class="rk b">${c.rank}</span></div>`;
  }
  if (!withPts) return inner;
  const pts = c ? `${cardPoints(c) >= 0 ? '+' : ''}${cardPoints(c)} pts` : 'hidden';
  return `<div>${inner}<div class="cardpts">${pts}</div></div>`;
}

const phaseLabel = S => ({
  lobby: 'Lobby',
  open: S.settings.days > 1 ? `Day ${S.day}/${S.settings.days} · Market open` : 'Market open',
  trial: `Day ${S.day} closed · Investigation`,
  between: `Day ${S.day} closed`,
  settled: 'Settlement',
}[S.phase] || S.phase);

/* |points| at which a card counts as a big mover — the rule the accusation
   phase turns on. The server sends it so the two can never drift. */
const material = () => S?.materialPoints ?? 20;
const dirLabel = d => d === 'bear' ? 'bear' : 'bull';

const roleLabel = r => ({ mm: 'Market maker', taker: 'Liquidity taker', both: 'Trader (both)' }[r] || '—');
const roleShort = r => ({ mm: 'MM', taker: 'Taker', both: 'Both' }[r] || '—');

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4600);
}

async function api(path, body) {
  let r, d;
  try {
    r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body || {}) });
    d = await r.json();
  } catch {
    throw Object.assign(new Error('Network hiccup — try again.'), {});
  }
  if (!r.ok || d.error) throw Object.assign(new Error(d.error || 'Request failed'),
                                            { code: d.code, canClaim: d.canClaim });
  return d;
}

/* ------------------------------------------------ client state */

let S = null;            // latest server state
let es = null;           // EventSource
let viewKey = '';        // what the current DOM layout was built for
let skew = 0;            // serverNow - clientNow
let lastFillSeen = null; // toast only fills newer than this
let prevPhase = null, prevTapeMax = -1;
let prevEventMax = null, prevForced = null, prevVerdict = null;
let hostKey = ROOM ? (localStorage.getItem(lsKey('hostKey')) || '') : '';
let soundOn = false, audioCtx = null;

/* ------------------------------------------------ connection */

function connect() {
  if (es) es.close();
  const p = new URLSearchParams();
  if (KIND === 'player') { p.set('role', 'player'); p.set('token', getTok()); }
  if (KIND === 'host') { p.set('role', 'host'); p.set('key', hostKey); }
  if (KIND === 'board') p.set('role', 'board');
  es = new EventSource(R('/events') + '?' + p.toString());
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    document.body.classList.remove('offline');
    if (d.error) return handleErrorState(d.error);
    S = d;
    skew = d.now - Date.now();
    render();
  };
  es.onerror = () => {
    document.body.classList.add('offline');
    // A non-200 response (e.g. 503 server-full) kills EventSource permanently —
    // the browser only auto-retries network-level drops. Retry ourselves.
    if (es && es.readyState === EventSource.CLOSED) {
      setTimeout(() => { if (es && es.readyState === EventSource.CLOSED) connect(); }, 5000);
    }
  };
}

function handleErrorState(err) {
  if (es) es.close();
  document.body.classList.remove('offline');
  if (err === 'no-room') {
    buildRoomGone();
  } else if (err === 'superseded') {
    delTok();
    buildJoin('Your seat was resumed on another device. If that wasn’t you, resume it back.');
  } else if (err === 'bad-token' || err === 'reset') {
    delTok();
    buildJoin('That game is over — join the new one.');
  } else if (err === 'kicked') {
    $('app').innerHTML = `<div class="bigmsg"><h1>You were removed by the host</h1>
      <p class="muted">You can watch on the <a href="${R('/board')}">board view</a>.</p></div>`;
  }
}

function buildRoomGone() {
  if (es) es.close();
  $('app').innerHTML = `<div class="bigmsg"><h1>This room doesn't exist</h1>
    <p class="muted">The code may be wrong, or the room expired — rooms close after a
    while with nobody connected.</p>
    <p style="margin-top:18px"><a class="btn primary" href="/">Create or join a room</a></p></div>`;
}

/* ------------------------------------------------ rendering core */

function render() {
  if (!S) return;
  const key = [KIND, S.phase, S.day, S.me?.role || '', !!S.settlement,
               S.settings.roles].join('|');
  if (key !== viewKey) {
    viewKey = key;
    $('app').innerHTML = BUILD[KIND](S);
    chartCache = newsCache = '';
    if (KIND === 'player' && S.phase === 'open' && S.me?.canQuote) prefillQuote();
  }
  update();
  watchEvents();
}

const set = (id, html) => { const el = $(id); if (el && el.innerHTML !== html) el.innerHTML = html; };

function update() {
  set('phasechip', esc(phaseLabel(S)));
  updateTimer();

  if ($('roster')) set('roster', KIND === 'host' ? hostRosterHTML(S) : rosterChipsHTML(S));
  if ($('standings')) set('standings', standingsHTML(S));
  if ($('tape')) set('tape', tapeHTML(S));
  drawChart();
  if ($('bookB')) { set('bookB', bookSideHTML(S.book.bids, 'No bids')); set('bookA', bookSideHTML(S.book.asks, 'No asks')); }
  if ($('log') && S.log) set('log', S.log.slice().reverse().map(l => `<li>${esc(l.msg)}</li>`).join(''));
  if ($('joinurl') && S.joinUrl) set('joinurl', esc(S.joinUrl));
  if ($('setsline')) set('setsline', settingsLine(S));
  if ($('trialcount') && S.trial)
    set('trialcount', `${S.trial.filed} of ${S.trial.of} accusations filed`);
  drawNews();

  if (S.me) {
    set('mypos', `pos <b>${signed(S.me.pos)}</b>`);
    set('mycash', `cash <b class="${numCls(S.me.cash)}">${signed(S.me.cash)}</b>`);
    if ($('submitnote')) set('submitnote', restingNoteHTML(S));
    if ($('accusenote')) set('accusenote', accuseNoteHTML(S));
    if ($('forcedbanner')) set('forcedbanner', S.me.forced
      ? `📣 <b>ORDER:</b> ${S.me.forced.side === 'buy' ? 'BUY' : 'SELL'} <b>${S.me.forced.size}</b> before ` +
        `the close — any unfilled remainder executes automatically`
      : '');
    if ($('myfills')) set('myfills', myFillsHTML(S));
    if ($('bestinfo')) set('bestinfo', bestInfoHTML(S));
    if ($('buybtn')) {
      const ask = takeable(S, 'buy'), bid = takeable(S, 'sell');
      $('buybtn').disabled = !ask;
      $('sellbtn').disabled = !bid;
      set('buybtn', mktBtnHTML('buy', ask));
      set('sellbtn', mktBtnHTML('sell', bid));
    }
  }

  if (KIND === 'host') {
    const act = S.players.filter(p => p.active);
    set('connchip', `${act.filter(p => p.connected).length}/${act.length} connected`);
    if ($('startbtn')) $('startbtn').disabled = act.length < 2;
    if ($('spreadline')) set('spreadline', spreadHTML(S));
  }
  if (KIND === 'board' && $('spreadline')) set('spreadline', spreadHTML(S));
}

/* toasts + sounds for things that changed since the previous message */
function watchEvents() {
  if (S.me) {
    const fills = S.me.fills || [];
    const maxI = fills.length ? fills[fills.length - 1].i : -1;
    if (S.phase === 'lobby') lastFillSeen = null;
    if (lastFillSeen === null) lastFillSeen = maxI;
    else if (maxI > lastFillSeen) {
      fills.filter(f => f.i > lastFillSeen).forEach(f => {
        toast(`${f.side === 'bought' ? '🟢 Bought' : '🔴 Sold'} <b>${f.size}</b> @ <b>${fmt(f.price)}</b> ` +
              `${f.side === 'bought' ? 'from' : 'to'} ${esc(f.counterparty)}`);
      });
      if (navigator.vibrate) navigator.vibrate(60);
      lastFillSeen = maxI;
    }
  }
  const evs = S.events || [];
  const evMax = evs.length ? evs[evs.length - 1].i : -1;
  if (S.phase === 'lobby') { prevEventMax = null; prevVerdict = null; }
  else if (prevEventMax === null) prevEventMax = evMax;
  else if (evMax > prevEventMax) {
    evs.filter(ev => ev.i > prevEventMax).forEach(ev =>
      toast(`🃏 <b>${esc(ev.headline)}</b><br><span class="small">${esc(ev.detail || '')}</span>`));
    if (KIND === 'board') { blip(523); setTimeout(() => blip(392), 140); }
    prevEventMax = evMax;
  }
  if (S.me) {
    const f = S.me.forced ? `${S.me.forced.side}:${S.me.forced.size}` : '';
    if (prevForced === null) prevForced = f;
    else if (f && f !== prevForced) {
      toast(`📣 <b>Trading order received</b> — you must ${S.me.forced.side === 'buy' ? 'BUY' : 'SELL'} ` +
            `<b>${S.me.forced.size}</b> before the close`, 'err');
      if (navigator.vibrate) navigator.vibrate(200);
      prevForced = f;
    } else if (f !== prevForced) prevForced = f;
  }
  if (S.me) {
    const v = S.me.verdict;
    const key = v ? `${v.target}:${v.dir}:${v.correct}:${v.amount}` : '';
    if (prevVerdict === null) prevVerdict = key;
    else if (key && key !== prevVerdict) {
      toast(v.correct
        ? `✅ <b>You read ${esc(v.target)} right</b> — a ${dirLabel(v.dir)}. Paid ${fmt(v.amount)}.`
        : `❌ <b>${esc(v.target)} is not a ${dirLabel(v.dir)}</b> — you paid ${fmt(Math.abs(v.amount))}.`,
        v.correct ? '' : 'err');
      if (navigator.vibrate) navigator.vibrate(90);
    }
    if (key !== prevVerdict) prevVerdict = key;
  }
  if (KIND === 'board') {
    const maxT = S.tape.length ? S.tape[S.tape.length - 1].i : -1;
    if (prevTapeMax >= 0 && maxT > prevTapeMax) blip(660);
    prevTapeMax = maxT;
    if (prevPhase && prevPhase !== S.phase) { blip(440); setTimeout(() => blip(880), 130); }
  }
  prevPhase = S.phase;
}

function updateTimer() {
  const el = $('timer');
  if (!el) return;
  if (!S || S.deadline == null) { el.textContent = ''; el.className = 'timer'; return; }
  const remaining = Math.max(0, S.deadline - (Date.now() + skew));
  const secs = Math.ceil(remaining / 1000);
  el.textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
  el.className = 'timer' + (secs <= 10 ? ' danger' : secs <= 20 ? ' warn' : '');
}
setInterval(updateTimer, 250);

/* ------------------------------------------------ shared html builders */

function topbarHTML(S, extra = '') {
  return `<div class="topbar">
    <span class="title">♠♥ Trading game</span>
    <span class="phasechip" id="phasechip"></span>
    <span class="spacer"></span>
    ${extra}
    <span class="timer" id="timer"></span>
    <button class="btn mini" data-action="rules">Rules</button>
  </div>`;
}

function publicCardsHTML(S, cls = '') {
  if (!S.publicCards.length) return '';
  const total = S.publicCards.reduce((a, c) => a + cardPoints(c), 0);
  return `<div class="cardrow">${S.publicCards.map(c => cardHTML(c, cls, true)).join('')}
    <div class="small muted" style="margin-left:6px">public cards<br>sum <b class="num">${signed(total)}</b></div></div>`;
}

function settingsLine(S) {
  const s = S.settings;
  const n = S.players.filter(p => p.active).length;
  const parts = [
    `Deck: ${s.dealPool === 'hs' ? 'hearts & spades only' : 'full 52 (private cards)'}`,
    `Roles: ${s.roles === 'assigned' ? 'assigned' : 'everyone does both'}`,
    s.informedCount == null ? 'Cards: everyone gets one'
      : `Cards: only ${Math.min(s.informedCount, n) || s.informedCount} of ${n} players (who — secret)`,
    `${s.days > 1 ? s.days + ' trading days' : 'One trading day'}` +
      `${s.daySeconds ? ' × ' + (s.daySeconds % 60 === 0 ? s.daySeconds / 60 + ' min' : s.daySeconds + 's') : ' (host closes each day)'}`,
  ];
  const vals = s.cardValues || DEFAULT_CARD_VALUES;
  if (['A', 'K', 'Q', 'J'].some(r => vals[r] !== DEFAULT_CARD_VALUES[r]))
    parts.push(`Points: A=${vals.A}, K=${vals.K}, Q=${vals.Q}, J=${vals.J}`);
  if (+s.feePerUnit > 0) parts.push(`Fee: ${fmt(s.feePerUnit)}/unit`);
  if (+s.marginRate > 0) parts.push(`Margin: ${fmt(s.marginRate)}%/day`);
  if (s.eventCards) parts.push(+s.eventEverySeconds > 0
    ? `Events every ${s.eventEverySeconds}s` : 'Event cards on');
  if (s.trials) parts.push(`Investigations after each day` +
    (+s.trialSeconds > 0 ? ` (${s.trialSeconds}s)` : '') +
    ` · indemnity ×${fmt(s.indemnityRate)}, wrong costs ${fmt(s.falseAccusationFee)}`);
  if (s.maxPlayers) parts.push(`Max ${s.maxPlayers} players`);
  if (s.anonymous) parts.push('Anonymous trading');
  if ((S.bots || []).length) parts.push(`AI players: ${S.bots.length}`);
  return parts.join(' · ');
}

function rosterChipsHTML(S) {
  const botNames = new Set((S.bots || []).map(b => b.name));
  return S.players.filter(p => p.active).map(p =>
    `<span class="chip ${p.filed ? 'done' : ''}"><span class="dot ${p.connected ? 'on' : ''}"></span>${botNames.has(p.name) ? '🤖 ' : ''}${esc(p.name)}` +
    (S.settings.roles === 'assigned' ? ` · ${roleShort(p.role)}` : '') +
    (p.filed ? ' ✓' : '') + `</span>`).join(' ');
}

function hostRosterHTML(S) {
  const assigned = S.settings.roles === 'assigned';
  const inLobby = S.phase === 'lobby';
  const botNames = new Set((S.bots || []).map(b => b.name));
  const rows = S.players.map(p => {
    if (!p.active) return `<tr class="muted"><td colspan="4">${esc(p.name)} (removed)</td></tr>`;
    const roleCell = inLobby && assigned
      ? `<button class="btn mini ${p.role === 'mm' ? 'primary' : ''}" data-action="host-role" data-pid="${p.id}" data-role="mm">MM</button>
         <button class="btn mini ${p.role === 'taker' ? 'primary' : ''}" data-action="host-role" data-pid="${p.id}" data-role="taker">Taker</button>`
      : roleShort(p.role);
    const aka = S.settings.anonymous && p.alias
      ? ` <span class="small muted">= ${esc(p.alias)}</span>` : '';
    return `<tr>
      <td><span class="dot ${p.connected ? 'on' : ''}"></span>${botNames.has(p.name) ? '🤖 ' : ''}${esc(p.name)}${aka}${
        p.filed ? ' <span class="small" style="color:var(--pos)">✓ filed</span>' : ''}</td>
      <td>${roleCell}</td>
      <td class="r"><span class="num">${signed(p.pos)}</span> / ${moneyHTML(p.cash)}</td>
      <td class="r"><button class="btn mini danger" data-action="host-kick" data-pid="${p.id}" data-name="${esc(p.name)}">✕</button></td>
    </tr>`;
  }).join('');
  return `<table class="tbl"><thead><tr><th>Player</th><th>Role</th><th class="r">Pos / Cash</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">Nobody has joined yet…</td></tr>'}</tbody></table>`;
}

function standingsHTML(S) {
  const head = `<thead><tr><th>Player</th><th>Role</th><th class="r">Position</th><th class="r">Cash</th></tr></thead>`;
  if (S.standings) {  // anonymous trading: server sends pseudonymous rows
    const rows = S.standings.map(r => `
      <tr class="${r.me ? 'me' : ''}">
        <td>${esc(r.label)}${r.me ? ' <span class="small muted">(you)</span>' : ''}${r.active ? '' : ' <span class="muted small">(left)</span>'}</td>
        <td>${roleShort(r.role)}</td>
        <td class="r"><span class="num">${signed(r.pos)}</span></td>
        <td class="r">${moneyHTML(r.cash)}</td>
      </tr>`).join('');
    return `<table class="tbl">${head}<tbody>${rows}</tbody></table>
      <p class="small muted">Anonymous trading is on — real names come out at settlement.</p>`;
  }
  const rows = S.players.filter(p => p.active || p.pos !== 0 || p.cash !== 0).map(p => `
    <tr class="${S.me && p.id === S.me.id ? 'me' : ''}">
      <td>${esc(p.name)}${p.active ? '' : ' <span class="muted small">(left)</span>'}</td>
      <td>${roleShort(p.role)}</td>
      <td class="r"><span class="num">${signed(p.pos)}</span></td>
      <td class="r">${moneyHTML(p.cash)}</td>
    </tr>`).join('');
  return `<table class="tbl">${head}<tbody>${rows}</tbody></table>`;
}

function bookSideHTML(side, emptyMsg) {
  if (!side.length) return `<div class="empty">${emptyMsg}</div>`;
  return `<table class="tbl"><thead><tr><th>Player</th><th class="r">Size</th><th class="r">Price</th></tr></thead>
    <tbody>${side.map(o => `<tr class="${o.mine ? 'me' : ''}">
      <td>${esc(o.name)}${o.mine ? ' <span class="small muted">(you)</span>' : ''}</td>
      <td class="r num">${o.size}</td><td class="r num"><b>${fmt(o.price)}</b></td></tr>`).join('')}
    </tbody></table>`;
}

function bookHTML() {
  return `<div class="book">
    <div class="bidside"><h3>Bids (buyers)</h3><div id="bookB"></div></div>
    <div class="askside"><h3>Asks (sellers)</h3><div id="bookA"></div></div>
  </div>`;
}

function tapeHTML(S) {
  if (!S.tape.length) return '<li class="muted">No trades yet…</li>';
  return S.tape.slice().reverse().map(t =>
    `<li>${esc(t.buyer)} bought <b>${t.size}</b> @ <span class="px">${fmt(t.price)}</span> from ${esc(t.seller)}</li>`).join('');
}

function myFillsHTML(S) {
  const fills = (S.me.fills || []).slice().reverse();
  if (!fills.length) return '<li class="muted">No fills yet…</li>';
  return fills.map(f =>
    `<li>${f.side} <b>${f.size}</b> @ <span class="px">${fmt(f.price)}</span>
     ${f.side === 'bought' ? 'from' : 'to'} ${esc(f.counterparty)}</li>`).join('');
}

/* The price a market order would actually get: your own resting orders are
   never tradeable, so the top of the book is not always yours to take. */
function takeable(S, side) {
  return (side === 'buy' ? S.book.asks : S.book.bids).find(o => !o.mine) || null;
}

function mktBtnHTML(side, o) {
  const label = side === 'buy' ? 'Buy at best ask' : 'Sell at best bid';
  return `<span>${label}</span><span class="mktpx">` +
    (o ? `${fmt(o.price)} <span class="mktsz">× ${o.size}</span>`
       : `<span class="mktsz">no ${side === 'buy' ? 'asks' : 'bids'} resting</span>`) +
    `</span>`;
}

function bestInfoHTML(S) {
  const bb = S.book.bids[0], ba = S.book.asks[0];
  return `<span>best bid: ${bb ? `<b class="num">${fmt(bb.price)}</b> ×${bb.size} (${esc(bb.name)})` : '<span class="muted">none</span>'}</span>
    <span>best ask: ${ba ? `<b class="num">${fmt(ba.price)}</b> ×${ba.size} (${esc(ba.name)})` : '<span class="muted">none</span>'}</span>`;
}

function restingNoteHTML(S) {
  const part = (side, label) => {
    const mine = S.book[side].filter(o => o.mine);
    return mine.length ? `${label} ${mine.map(o => `${fmt(o.price)} × ${o.size}`).join(', ')}` : null;
  };
  const bits = [part('bids', 'bid'), part('asks', 'ask')].filter(Boolean);
  return bits.length
    ? `✓ Live in the book: ${bits.join(' · ')} — post again to replace, or pull them`
    : '';
}

function spreadHTML(S) {
  const bb = S.book.bids[0], ba = S.book.asks[0];
  const last = S.tape.length ? S.tape[S.tape.length - 1].price : null;
  if (!bb && !ba) return `<span class="muted">book is empty</span>${last != null ? ` · last <span class="mid">${fmt(last)}</span>` : ''}`;
  let mid = '';
  if (bb && ba) mid = ` · spread <span class="mid">${fmt(ba.price - bb.price)}</span>`;
  return `bid ${bb ? fmt(bb.price) : '—'} / ask ${ba ? fmt(ba.price) : '—'}${mid}` +
         (last != null ? ` · last <span class="mid">${fmt(last)}</span>` : '');
}

/* ------------------------------------------------ investigations */

/* One private accusation each, resolved on its own: name a trader you think is
   holding a big mover and say which way. Nobody sees anyone else's accusation,
   and only the accuser is told how theirs went — that is what keeps the market
   asymmetric into the next day instead of flattening it. */

function indemnityHint(S) {
  const v = cardValues(), rate = +S.settings.indemnityRate || 0, m = material();
  const bits = [];
  if (Math.abs(v.A) >= m) bits.push(`${fmt(Math.abs(v.A) * rate)} for an Ace`);
  if (Math.abs(v.K) >= m) bits.push(`${fmt(Math.abs(v.K) * rate)} for a King`);
  return bits.join(', ');
}

function accuseFormHTML(S) {
  const me = S.me, m = material();
  const cands = me.candidates || [];
  const acc = me.accusation;
  const pick = (name, value, cls, label, on) => `<label class="pick ${cls}">
    <input type="radio" name="${name}" value="${value}"${on ? ' checked' : ''}>
    <span>${label}</span></label>`;
  return `<div class="panel">
    <h2>🔎 Investigation — day ${S.day}</h2>
    <p class="small muted">Name one trader you think is holding a <b>big mover</b>: a card
      worth <b>−${m} or worse</b> (a <b class="num neg">bear</b>) or <b>+${m} or better</b>
      (a <b class="num pos">bull</b>). At today's values that means an Ace or a King. Read
      back over the tape — who was leaning, and which way?</p>
    <form id="accuseform">
      <div class="field"><label>Who</label>
        <div class="pickgrid">${cands.map(c =>
          pick('acc-who', esc(c), '', esc(c), acc && acc.target === c)).join('')}</div></div>
      <div class="field"><label>Holding what</label>
        <div class="pickgrid two">
          ${pick('acc-dir', 'bear', 'bear', `Bear · −${m} or worse`, acc && acc.dir === 'bear')}
          ${pick('acc-dir', 'bull', 'bull', `Bull · +${m} or better`, acc && acc.dir === 'bull')}
        </div></div>
      <div class="tradebtns">
        <button class="btn primary big" type="submit">File accusation</button>
        <button class="btn big" type="button" data-action="abstain">Abstain</button>
      </div>
      <div class="submitnote" id="accusenote"></div>
    </form>
    <p class="small muted">Right and they pay an indemnity${indemnityHint(S) ?
      ` — ${indemnityHint(S)}` : ''}, split between everyone who read them correctly.
      Wrong and you pay them <b>${fmt(S.settings.falseAccusationFee || 0)}</b>. Your
      accusation is private, and only you are told how it went — so a correct read buys
      you something the rest of the table does not have.</p>
  </div>`;
}

function accuseNoteHTML(S) {
  const acc = S.me && S.me.accusation;
  return acc ? `✓ Filed: <b>${esc(acc.target)}</b> is a <b>${dirLabel(acc.dir)}</b>
    — change it any time before the clock runs out` : '';
}

function verdictHTML(S) {
  const v = S.me && S.me.verdict;
  if (!v) return '';
  return `<div class="panel verdict ${v.correct ? 'right' : 'wrong'}">
    <h3>Your accusation, privately</h3>
    ${v.correct
      ? `<p>✅ <b>You read them right.</b> ${esc(v.target)} is holding a
         <b>${dirLabel(v.dir)}</b>, and paid you ${moneyHTML(v.amount)}.</p>
         <p class="small muted">Nobody else was told — including them. You now know
         something about V that the rest of the table does not.</p>`
      : `<p>❌ <b>Wrong.</b> ${esc(v.target)} is not a <b>${dirLabel(v.dir)}</b>, so you
         paid them <b class="num neg">${fmt(Math.abs(v.amount))}</b>.</p>
         <p class="small muted">Still worth something: that rules one direction out
         for them.</p>`}
  </div>`;
}

function trialWaitHTML(S) {
  const t = S.trial || {};
  return `<div class="panel"><h2>🔎 Investigation — day ${S.day}</h2>
    <p class="waiting">Accusations are being filed. <b id="trialcount"></b></p>
    <p class="small muted">You are not in this one — no accusation to make.</p></div>`;
}

/* ------------------------------------------------ news scroller */

/* Event cards land as a toast once and then are gone, which left everyone
   trying to remember what the news was. This crawls every event of the session
   past instead — newest first, tap for the full list. Like the chart, it is
   redrawn only when the news actually changed: re-setting the markup on every
   push would restart the animation and the crawl would never move. */

let newsCache = '';

function newsBarHTML(S, boxW) {
  const evs = S.events || [];
  if (!evs.length) return '';
  const news = evs.slice().reverse();
  const item = ev => `<span class="newsitem"><span class="newsday">Day ${ev.day}</span>` +
    `<b>${esc(ev.headline)}</b>${ev.detail ? ` — ${esc(ev.detail)}` : ''}</span>`;
  /* The mandate headline names nobody, by design — so the one trader who is
     under it needs telling, and only their own device can say so: `me.forced`
     is in no other player's payload, nor the host's, nor the board's. */
  const f = S.me && S.me.forced;
  const v = S.me && S.me.verdict;
  let mine = f ? `<span class="newsitem own"><span class="newsday">Your order</span>` +
    `<b>${f.side === 'buy' ? 'BUY' : 'SELL'} ${f.size}</b> before the close` +
    ` — that mandate is yours, and nobody else knows it</span>` : '';
  if (v) mine += `<span class="newsitem own"><span class="newsday">Your read</span>` +
    `<b>${esc(v.target)}</b> ${v.correct ? `is a ${dirLabel(v.dir)}` :
      `is not a ${dirLabel(v.dir)}`} — ${v.correct ? 'you were paid' : 'you paid'}` +
    ` ${fmt(Math.abs(v.amount))}, and only you were told</span>`;
  const items = mine + news.map(item).join('');
  /* Two identical halves make the -50% loop seamless — identical is the whole
     trick, so anything that widens one must widen both. Each is held to at
     least the strip's width, or a single short item would scroll away and leave
     the projector blank before jumping back. The crawl is then timed off the
     distance and the view's own font size, so it reads at the same words per
     second (about ten characters of it) on a phone as on a 20-foot screen. */
  const chars = news.reduce((n, ev) => n + ev.headline.length + (ev.detail || '').length + 12,
                            mine ? 90 : 0);
  const em = parseFloat(getComputedStyle(document.body).fontSize) || 16;
  const charW = em * 0.47;
  const halfW = Math.round(Math.max(chars * charW, (boxW || 320) * 0.95));
  const dur = Math.min(300, Math.max(18, Math.round(halfW / (charW * 10))));
  const half = dup => `<span class="newshalf${dup ? ' dup' : ''}" style="min-width:${halfW}px"` +
    `${dup ? ' aria-hidden="true"' : ''}>${items}</span>`;
  return `<div class="newsbar" data-action="news-all" title="See all the news so far">
    <button class="newstag" data-action="news-all">📰 News</button>
    <div class="newsscroll"><div class="newstrack" style="animation-duration:${dur}s">${
      half(false)}${half(true)}</div></div></div>`;
}

function drawNews() {
  const el = $('newsbar');
  if (!el || !S) { newsCache = ''; return; }
  const html = newsBarHTML(S, el.clientWidth);
  if (html !== newsCache) { newsCache = html; el.innerHTML = html; }
}

function showNews() {
  const news = (S?.events || []).slice().reverse();
  $('modalbody').innerHTML = `<h2>📰 The news so far</h2>
    ${news.length ? `<ul class="newslist">${news.map(ev => `<li>
        <span class="newsday">Day ${ev.day}</span><b>${esc(ev.headline)}</b>
        ${ev.detail ? `<div class="small muted">${esc(ev.detail)}</div>` : ''}</li>`).join('')}</ul>`
      : '<p class="muted">No news has been dealt yet.</p>'}
    <p class="small muted">Value shocks stick for the rest of the game. Whatever the news
    said, the card points actually in force are always the ones in the settings line.</p>`;
  $('modal').classList.remove('hidden');
}

/* ------------------------------------------------ price chart (inline SVG) */

/* The server ships fixed-interval OHLC candles in S.chart (prices only, no
   names — safe in every view). This draws them as one SVG string, redrawn from
   scratch whenever the markup would actually change. No canvas, no library:
   the viewBox is measured in CSS pixels, so strokes stay crisp and the text
   scales with the view's own font size (tiny on a phone, big on a projector). */

const CHART_MODE_KEY = 'gm:chartMode';
let chartMode = localStorage.getItem(CHART_MODE_KEY) === 'line' ? 'line' : 'candles';
let chartCache = '';     // last markup drawn, so a quiet push does not redraw

function chartPanelHTML(h, title = 'Price') {
  return `<div class="panel">
    <h3>${title}</h3>
    <div id="chart" data-h="${h}"></div>
  </div>`;
}

/* pick ~4 round price levels for the gridlines */
function niceTicks(lo, hi, count = 4) {
  const raw = (hi - lo) / count;
  if (!(raw > 0)) return [lo];
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= raw) || 10 * mag;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step)
    out.push(+t.toFixed(4));
  return out;
}

const bucketLabel = ms => !ms ? '' : ms % 60000 === 0 ? `${ms / 60000}m` : `${ms / 1000}s`;
const r1 = v => Math.round(v * 10) / 10;      // keeps the markup small

const candleTip = c => `day ${c.day} · open ${fmt(c.o)} · high ${fmt(c.h)} · low ${fmt(c.l)} ` +
  `· close ${fmt(c.c)} · ${c.v} unit${c.v === 1 ? '' : 's'} in ${c.n} print${c.n === 1 ? '' : 's'}`;

function chartHeadHTML(ch) {
  const bits = [];
  if (ch.bucketMs) bits.push(`${bucketLabel(ch.bucketMs)} candles`);
  if (ch.trades) bits.push(`${ch.trades} print${ch.trades === 1 ? '' : 's'}`);
  if (ch.last != null) bits.push(`last <b class="num">${fmt(ch.last)}</b>`);
  const btn = (m, label) => `<button class="btn mini ${chartMode === m ? 'on' : ''}"
    data-action="chart-mode" data-mode="${m}">${label}</button>`;
  return `<div class="chart-head"><span class="chart-cap">${bits.join(' · ')}</span>
    <span class="chart-toggle">${btn('candles', 'Candles')}${btn('line', 'Line')}</span></div>`;
}

function chartHTML(S) {
  const box = $('chart');
  const ch = S.chart || {};
  const cs = ch.candles || [];
  const head = chartHeadHTML(ch);
  const H = Math.max(120, +box?.dataset.h || 200);
  if (!cs.length) return head + `<div class="chart-empty" style="height:${H}px">
    Nothing has traded yet — the chart draws itself as prints hit the tape.</div>`;

  const W = Math.round(Math.min(2200, Math.max(280,
    box?.clientWidth || (KIND === 'board' ? 900 : 540))));
  const padL = 3, padR = W < 420 ? 38 : 50, padT = 8, padB = 4;
  const volH = Math.round((H - padT - padB) * 0.18);
  const plotT = padT, plotH = H - padT - padB - volH - 6, plotB = plotT + plotH;
  const volB = H - padB, plotW = W - padL - padR, x0 = padL, xR = x0 + plotW;

  /* price scale: every print, plus the live quote and (at the end) V, so the
     levels drawn on top always land inside the frame */
  const vals = [ch.lo, ch.hi];
  const bb = S.book?.bids[0], ba = S.book?.asks[0];
  if (S.phase === 'open') { if (bb) vals.push(bb.price); if (ba) vals.push(ba.price); }
  const V = S.settlement ? S.settlement.V : null;
  if (V != null) vals.push(V);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!(hi > lo)) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.09;
  lo -= padY; hi += padY;
  const y = v => plotT + (hi - v) / (hi - lo) * plotH;

  const cw = plotW / cs.length;
  const cx = i => x0 + cw * (i + 0.5);
  const bodyW = Math.max(1.5, Math.min(24, cw * 0.64));
  let g = '';

  for (const t of niceTicks(lo, hi)) {                      // gridlines + right-hand axis
    const ty = r1(y(t));
    g += `<line class="grid" x1="${x0}" y1="${ty}" x2="${xR}" y2="${ty}"/>` +
         `<text class="ax" x="${xR + 5}" y="${ty}" dy=".32em">${fmt(t)}</text>`;
  }

  if (S.phase === 'open' && bb && ba) {                     // where the market stands now
    const top = r1(y(ba.price));
    const tip = `best bid ${fmt(bb.price)} / best ask ${fmt(ba.price)}`;
    g += `<rect class="spreadband" x="${x0}" y="${top}" width="${plotW}"` +
      ` height="${Math.max(0.8, r1(y(bb.price) - top))}"><title>${tip}</title></rect>` +
      `<line class="askline" x1="${x0}" y1="${top}" x2="${xR}" y2="${top}"/>` +
      `<line class="bidline" x1="${x0}" y1="${r1(y(bb.price))}" x2="${xR}" y2="${r1(y(bb.price))}"/>`;
  }

  if (chartMode === 'line') {
    const segs = [];       // one run of closes per day: the book is wiped overnight,
    let run = [];          // so a line across the boundary would be a fiction
    cs.forEach((c, i) => {
      if (!c.n) return;
      if (run.length && c.day !== run[run.length - 1].c.day) { segs.push(run); run = []; }
      run.push({ x: r1(cx(i)), y: r1(y(c.c)), c: c });
    });
    if (run.length) segs.push(run);
    for (const seg of segs) {
      const pts = seg.map(p => `${p.x},${p.y}`).join(' ');
      if (seg.length > 1)
        g += `<polygon class="pxarea" points="${seg[0].x},${plotB} ${pts} ${seg[seg.length - 1].x},${plotB}"/>`;
      g += `<polyline class="pxline" points="${pts}"/>`;
    }
    const r = r1(Math.max(1.4, Math.min(3.6, cw * 0.18)));
    for (const seg of segs) for (const p of seg)
      g += `<circle class="pxdot" cx="${p.x}" cy="${p.y}" r="${r}"><title>${candleTip(p.c)}</title></circle>`;
  } else {
    const wickW = r1(Math.max(0.9, Math.min(3, cw * 0.16)));
    cs.forEach((c, i) => {
      if (!c.n) return;
      const x = r1(cx(i)), yo = y(c.o), yc = y(c.c);
      g += `<g class="cd ${c.c >= c.o ? 'up' : 'dn'}"><title>${candleTip(c)}</title>` +
        `<line x1="${x}" y1="${r1(y(c.h))}" x2="${x}" y2="${r1(y(c.l))}" stroke-width="${wickW}"/>` +
        `<rect x="${r1(x - bodyW / 2)}" y="${r1(Math.min(yo, yc))}" width="${r1(bodyW)}"
          height="${Math.max(1.4, r1(Math.abs(yc - yo)))}"/></g>`;
    });
  }

  if (ch.vmax) cs.forEach((c, i) => {                       // volume strip
    if (!c.n) return;
    const vh = Math.max(1, r1(c.v / ch.vmax * volH));
    g += `<rect class="vol ${c.c >= c.o ? 'up' : 'dn'}" x="${r1(cx(i) - bodyW / 2)}"
      y="${r1(volB - vh)}" width="${r1(bodyW)}" height="${vh}"/>`;
  });

  cs.forEach((c, i) => {                                    // overnight boundaries
    if (!i || c.day === cs[i - 1].day) return;
    const x = r1(x0 + cw * i);
    g += `<line class="daysep" x1="${x}" y1="${plotT}" x2="${x}" y2="${volB}"/>` +
         `<text class="daylab" x="${x + 4}" y="${plotT}" dy=".85em">day ${c.day}</text>`;
  });

  if (ch.last != null) {                                    // last print, tagged on the axis
    const ly = r1(y(ch.last)), lab = fmt(ch.last);
    const ty = r1(Math.min(Math.max(ly, 9), H - 9));        // the pill stays in frame
    g += `<line class="lastline" x1="${x0}" y1="${ly}" x2="${xR}" y2="${ly}"/>
      <g class="lasttag"><rect x="${xR + 2}" y="${ty - 8.5}" rx="3"
        width="${Math.min(padR - 4, 9 + lab.length * 6.4)}" height="17"/>
      <text x="${xR + 6}" y="${ty}" dy=".32em">${lab}</text></g>`;
  }
  if (V != null) {                                          // what it was really worth
    const vy = r1(y(V));
    g += `<line class="vline" x1="${x0}" y1="${vy}" x2="${xR}" y2="${vy}"/>
      <text class="vlab" x="${x0 + 6}" y="${vy - 5}">V = ${fmt(V)}</text>`;
  }

  return head + `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
    role="img" aria-label="Price of the ${ch.trades} trade${ch.trades === 1 ? '' : 's'} so far">${g}</svg>`;
}

function drawChart() {
  const box = $('chart');
  if (!box || !S) { chartCache = ''; return; }
  const html = chartHTML(S);
  if (html !== chartCache) { chartCache = html; box.innerHTML = html; }
}

let chartRzT = null;   // both are measured off the layout, so a resize redraws
addEventListener('resize', () => {
  clearTimeout(chartRzT);
  chartRzT = setTimeout(() => { drawChart(); drawNews(); }, 180);
});

function settlementHTML(S, { podium = false } = {}) {
  const st = S.settlement;
  const pub = st.publicCards.map(c => `${c.rank}${SUIT[c.suit]} ${cardPoints(c) >= 0 ? '+' : ''}${cardPoints(c)}`).join(', ');
  const holders = st.rows.filter(r => r.card);
  const priv = holders.length
    ? holders.map(r => `${r.card.rank}${SUIT[r.card.suit]} ${r.cardPoints >= 0 ? '+' : ''}${r.cardPoints}`).join(', ')
    : 'none dealt';
  const podiumHTML = podium ? `<div class="podium">${st.rows.slice(0, 3).map((r, i) =>
    `<div class="place p${i + 1}"><div class="medal">${['🥇', '🥈', '🥉'][i]}</div>
     <div>${esc(r.name)}</div><div class="score ${numCls(r.total)}">${signed(r.total)}</div></div>`).join('')}</div>` : '';
  const rows = st.rows.map((r, i) => `
    <tr class="${S.me && r.pid === S.me.id ? 'me' : ''}">
      <td class="r num">${i + 1}</td>
      <td>${esc(r.name)}${st.anonymous && r.alias ? ` <span class="small muted">(${esc(r.alias)})</span>` : ''}${r.active ? '' : ' <span class="muted small">(left)</span>'}</td>
      <td>${r.card ? cardHTML(r.card, 'sm') : '<span class="muted">—</span>'}</td>
      <td class="r num">${signed(r.pos)}</td>
      <td class="r">${moneyHTML(r.cash)}</td>
      <td class="r">${moneyHTML(r.posValue)}</td>
      <td class="r"><b class="${numCls(r.total)}">${signed(r.total)}</b></td>
    </tr>`).join('');
  const extras = [];
  if (st.groups) extras.push(
    `Card holders (${st.groups.informed.n}) averaged <b class="${numCls(st.groups.informed.avgTotal)}">${signed(st.groups.informed.avgTotal)}</b> —
     no-card players (${st.groups.uninformed.n}) averaged <b class="${numCls(st.groups.uninformed.avgTotal)}">${signed(st.groups.uninformed.avgTotal)}</b>.
     That gap is what information is worth here.`);
  if (st.feesCollected) extras.push(
    `The exchange collected <b>${fmt(st.feesCollected)}</b> in trading fees.`);
  if (st.interestPaid) extras.push(
    `The margin desk collected <b>${fmt(st.interestPaid)}</b> in overnight interest.`);
  if (st.indemnities) extras.push(
    `Investigations moved <b>${fmt(st.indemnities)}</b> between players — indemnities and
     wrong-accusation fees are transfers, so they do not leave the table.`);
  const take = (st.feesCollected || 0) + (st.interestPaid || 0);
  if (take) extras.push(`Player totals therefore sum to −${fmt(take)}.`);
  const trials = (st.trials || []).filter(t => t.rows.length);
  const trialTable = !trials.length ? '' : `
    <h3 style="margin-top:14px">The investigations</h3>
    <div class="tblwrap"><table class="tbl">
      <thead><tr><th class="r">Day</th><th>Accuser</th><th>Named</th><th>As</th>
        <th class="r">Paid</th></tr></thead>
      <tbody>${trials.flatMap(t => t.rows.map(r => `<tr>
        <td class="r num">${t.day}</td><td>${esc(r.accuser)}</td><td>${esc(r.target)}</td>
        <td>${r.correct ? '✅' : '❌'} ${esc(r.dir)}</td>
        <td class="r">${moneyHTML(r.amount)}</td></tr>`)).join('')}</tbody></table></div>
    <p class="small muted">A correct read cost the exposed trader an indemnity, split
      between everyone who saw it; a wrong one paid the accused. Only the accuser ever
      knew, until now.</p>`;
  return `${podiumHTML}
    <div class="vmath"><b>V</b> = public (${pub}) + private (${priv}) = <b class="${numCls(st.V)}">${st.V}</b></div>
    ${extras.length ? `<div class="vmath" style="margin-top:8px">${extras.join('<br>')}</div>` : ''}
    <div class="tblwrap"><table class="tbl">
      <thead><tr><th class="r">#</th><th>Player</th><th>Card</th><th class="r">Pos</th>
        <th class="r">Cash</th><th class="r">Pos × V</th><th class="r">Total</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="small muted">Score = cash from filled orders + net position × V, where V is the sum of the
    points of <b>all</b> dealt cards.</p>
    ${trialTable}`;
}

/* ------------------------------------------------ view builders */

const BUILD = { player: buildPlayer, host: buildHost, board: buildBoard };

/* ---------- landing (no room in the URL) ---------- */

function buildLanding() {
  $('app').innerHTML = `
    <div class="wrap" style="padding-top:7vh">
      <h1 class="center">♠♥ Trading game</h1>
      <p class="center muted">Trading with imperfect information — a market game for phones</p>
      <div class="panel">
        <h2>Host a game</h2>
        <p class="small muted">You get a 5-letter room code; friends join from their phones.
        You control the market from the host panel.</p>
        <button class="btn primary big" id="createbtn" data-action="create-room">Create a room</button>
      </div>
      <div class="panel">
        <h2>Join a game</h2>
        <form id="joinroomform">
          <div class="field"><label>Room code</label>
            <input type="text" id="j-code" maxlength="5" autocomplete="off" autocapitalize="characters"
                   spellcheck="false" placeholder="ABCDE"
                   style="text-transform:uppercase;letter-spacing:.35em;text-align:center;font-weight:800;font-size:1.25rem"></div>
          <button class="btn big" type="submit">Join</button>
        </form>
        <div id="joinroommsg"></div>
      </div>
      <div class="panel">
        <h2>New here?</h2>
        <p class="small muted">Practice solo against a simulated table — estimate the value,
        trade a quote, and learn to read the order flow. No room needed.</p>
        <a class="btn big" href="/practice">🎓 Open the practice table</a>
      </div>
      <p class="footer-note"><a href="#" data-action="rules">How the game works</a></p>
    </div>`;
}

/* ---------- player ---------- */

function buildJoin(msg = '') {
  if (es) es.close();
  $('app').innerHTML = `
    <div class="wrap" style="padding-top:10vh">
      <h1 class="center">♠♥ Trading game</h1>
      <p class="center muted">Room <b>${esc(ROOM)}</b> — trading with imperfect information</p>
      <div class="panel">
        ${msg ? `<p class="muted">${esc(msg)}</p>` : ''}
        <form id="joinform">
          <div class="field"><label>Your name</label>
            <input type="text" id="j-name" maxlength="20" autocomplete="off"
                   value="${esc(localStorage.gmName || '')}" placeholder="e.g. Vedant"></div>
          <button class="btn primary big" type="submit">Join the game</button>
        </form>
        <div id="joinmsg"></div>
      </div>
      <p class="footer-note">Watching on the projector? Open <a href="${R('/board')}">the board view</a>.
      Wrong room? <a href="/">Start over</a>.</p>
    </div>`;
}

function dealtCount(S) {
  const n = S.players.filter(p => p.active).length;
  const k = S.settings.informedCount == null ? n : Math.min(S.settings.informedCount, n);
  return { k, n };
}

function meCardPanel(S, small = false) {
  const cls = small ? 'sm' : '';
  let mine;
  if (S.me.informed === false) {
    const { k, n } = dealtCount(S);
    mine = `<div><b>No private card this game — it counts 0.</b>
      <div class="small muted">Only ${k} of ${n} players got one, and nobody knows who.
      Trade on the public cards and the order flow — no one can tell you're guessing.</div></div>`;
  } else {
    mine = `<div class="cardrow">${cardHTML(S.me.card, cls, true)}
      <div class="small muted">your <b>private</b> card —<br>don't show anyone!</div></div>`;
  }
  return `<div class="panel">
    ${publicCardsHTML(S, cls)}
    <hr class="divider">
    ${mine}
  </div>`;
}

function buildPlayer(S) {
  const me = S.me;
  const head = topbarHTML(S,
    `<span class="chip" id="mypos"></span><span class="chip" id="mycash"></span>`);
  let main = '';

  if (S.phase === 'lobby') {
    main = `
      <div class="panel">
        <h2>You're in, ${esc(me.name)}!</h2>
        <p>Your role: <b>${roleLabel(me.role)}</b></p>
        <p class="small muted" id="setsline"></p>
        <p class="waiting">Waiting for the host to deal the cards…</p>
      </div>
      <div class="panel"><h3>Players</h3><div id="roster" class="checklist"></div></div>`;
  } else if (S.phase === 'open') {
    const quoteForm = me.canQuote ? `
      <div class="panel">
        <h2>Your quote</h2>
        <form id="quoteform">
          <div class="quotegrid">
            <div class="buycol">
              <div class="field"><label>Bid (you buy at)</label>
                <input type="number" id="q-bid" inputmode="decimal" step="any" min="0.01" max="999.99" required></div>
              <div class="field"><label>Bid size</label>
                <input type="number" id="q-bidsize" inputmode="numeric" step="1" min="1" max="99" required></div>
            </div>
            <div class="sellcol">
              <div class="field"><label>Ask (you sell at)</label>
                <input type="number" id="q-ask" inputmode="decimal" step="any" min="0.02" max="999.99" required></div>
              <div class="field"><label>Ask size</label>
                <input type="number" id="q-asksize" inputmode="numeric" step="1" min="1" max="99" required></div>
            </div>
          </div>
          <div class="tradebtns">
            <button class="btn primary big" type="submit">Post / update quote</button>
            <button class="btn big" type="button" data-action="pull-quotes">Pull</button>
          </div>
          <div class="submitnote" id="submitnote"></div>
        </form>
        <p class="small muted">Posting replaces your previous quote — reprice as often as you like
        (ask above your own bid, prices above 0). If your bid reaches someone's resting ask, or your
        ask their bid, it trades <b>instantly at their price</b> — stale quotes get picked off.</p>
      </div>` : '';
    const takePanel = me.canTake ? `
      <div class="panel">
        <h2>Market orders</h2>
        <div class="bestinfo" id="bestinfo"></div>
        <label>Order size</label>
        <div class="stepper">
          <button class="btn" data-action="size-dec" type="button">−</button>
          <input type="number" id="msize" inputmode="numeric" value="1" min="1" max="99">
          <button class="btn" data-action="size-inc" type="button">+</button>
        </div>
        <div class="tradebtns">
          <button class="btn buy big mktbtn" id="buybtn" data-action="mkt-buy"></button>
          <button class="btn sell big mktbtn" id="sellbtn" data-action="mkt-sell"></button>
        </div>
        <p class="small muted">Fills instantly at the best resting price(s), first come first served.
        Big orders walk the book.</p>
      </div>` : '';
    main = `
      <div id="forcedbanner" style="margin:2px 0 8px;font-weight:800;color:#e5484d"></div>
      <div id="newsbar"></div>
      ${quoteForm}${takePanel}
      ${chartPanelHTML(176)}
      <div class="panel"><h3>Order book</h3>${bookHTML()}</div>
      ${meCardPanel(S, true)}
      <div class="panel"><h3>Your fills</h3><ul class="tape compact" id="myfills"></ul></div>
      <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
  } else if (S.phase === 'trial') {
    main = `
      <div id="newsbar"></div>
      ${me.candidates && me.candidates.length ? accuseFormHTML(S) : trialWaitHTML(S)}
      ${chartPanelHTML(190, 'Price — read the tape')}
      <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
  } else if (S.phase === 'between') {
    main = `
      <div class="panel"><h2>Day ${S.day} closed</h2>
        <p class="waiting">Overnight — the book is wiped, positions and cash carry.
        Waiting for the host to open day ${S.day + 1}…</p>
        <div id="newsbar"></div></div>
      ${verdictHTML(S)}
      ${chartPanelHTML(200, 'Price so far')}
      ${meCardPanel(S, true)}
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
      <div class="panel"><h3>Your fills</h3><ul class="tape compact" id="myfills"></ul></div>
      <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>`;
  } else if (S.phase === 'settled') {
    main = `<div id="newsbar"></div>
      <div class="panel"><h2>Final results</h2>${settlementHTML(S)}</div>
      ${chartPanelHTML(200, 'Where the market traded vs. V')}`;
  }

  return `${head}<div class="wrap">${main}</div>`;
}

function prefillQuote() {
  const q = JSON.parse(localStorage.getItem(lsKey('lastQuote')) || 'null');
  if (!q) return;
  if ($('q-bid')) {
    $('q-bid').value = q.bid; $('q-bidsize').value = q.bidSize;
    $('q-ask').value = q.ask; $('q-asksize').value = q.askSize;
  }
}

/* ---------- host ---------- */

function buildHostKeyForm(msg = '') {
  $('app').innerHTML = `
    <div class="wrap" style="padding-top:10vh">
      <h1 class="center">Host controls — room ${esc(ROOM)}</h1>
      <div class="panel">
        ${msg ? `<p class="muted">${esc(msg)}</p>` : ''}
        <form id="hostkeyform">
          <div class="field"><label>Host key (issued when the room was created)</label>
            <input type="text" id="h-key" autocomplete="off" placeholder="e.g. 3f9a2c1b0d4e5f6a"></div>
          <button class="btn primary big" type="submit">Open host panel</button>
        </form>
        <p class="small muted">The browser that created this room saved the key automatically.
        On another device, open the <b>host link</b> from that browser's panel (it carries
        <b>?key=…</b>), or paste the key here. Joining as a player instead?
        <a href="${R('')}">Go to the player view</a>.</p>
      </div>
    </div>`;
}

function trialFields(s) {
  return `<div class="formrow">
      <div class="field"><label>Investigations</label>
        <select id="set-trials"><option value="off">Off</option>
          <option value="on" ${s.trials ? 'selected' : ''}>On — after each day closes</option>
        </select></div>
      <div class="field"><label>Investigation clock (s, 0 = manual)</label>
        <input type="number" id="set-trialsec" min="0" max="600" step="5"
               value="${s.trialSeconds ?? 60}"></div>
    </div>
    <div class="formrow">
      <div class="field"><label>Indemnity (× the card's points)</label>
        <input type="number" id="set-indem" min="0" max="5" step="any"
               value="${s.indemnityRate ?? 0.5}"></div>
      <div class="field"><label>Wrong-accusation fee</label>
        <input type="number" id="set-falsefee" min="0" max="100" step="any"
               value="${s.falseAccusationFee ?? 6}"></div>
    </div>`;
}

function cardValueFields(s) {
  const v = s.cardValues || DEFAULT_CARD_VALUES;
  return `<label>Card points (hearts &amp; spades; number cards stay face value)</label>
    <div class="formrow" style="grid-template-columns:repeat(4,1fr)">
      ${['A', 'K', 'Q', 'J'].map(r => `<div class="field"><label>${r}</label>
        <input type="number" id="set-cv-${r}" value="${v[r]}" min="-200" max="200" step="1"></div>`).join('')}
    </div>`;
}

/* AI players: custom seat list — name + strategy per row, saved with the lobby
   settings. The server runs each one as an ordinary player (see bot.py). */
const BOT_TYPES = [
  ['ev', 'ev — honest EV quoter'],
  ['bluff', 'bluff — heavy bluffer'],
  ['mix', 'mix — EV with noise'],
  ['noise', 'noise — feigns uninformed'],
];
const BOT_DEFAULT_NAMES = { ev: 'EV Bot', bluff: 'Bluffer', mix: 'Mixer', noise: 'Noise Bot' };

function botRowHTML(name = '', type = 'ev') {
  return `<div class="formrow botrow">
    <div class="field"><label>Seat name</label>
      <input type="text" class="botname" maxlength="20" value="${esc(name)}"
             placeholder="${BOT_DEFAULT_NAMES[type] || 'EV Bot'}"></div>
    <div class="field"><label>Strategy</label>
      <select class="bottype">${BOT_TYPES.map(([t, label]) =>
        `<option value="${t}" ${t === type ? 'selected' : ''}>${label}</option>`).join('')}</select></div>
    <button class="btn mini danger" type="button" data-action="bot-del"
            title="Remove this AI seat">✕</button>
  </div>`;
}

function botEditorHTML(bots) {
  const rows = (bots || []).map(b => botRowHTML(b.name, b.type)).join('');
  return `<div class="field"><label>AI players — join as ordinary seats when you save</label>
    <div id="botrows">${rows}</div>
    <button class="btn mini" type="button" data-action="bot-add">＋ Add AI player</button>
    <p class="small muted">Each AI seat is driven by a strategy from <code>bot.py</code> and plays
    through the same public API as your phones: it can be kicked, accused, and settles like
    anyone else. Seat order sets the role in assigned mode; kicking an AI seat removes it
    for good.</p></div>`;
}

function liveTweaksForm(S) {
  const s = S.settings;
  return `<form class="panel" id="settingsform">
    <h3>Live rule tweaks</h3>
    <div class="formrow">
      <div class="field"><label>Trading days (you can add more)</label>
        <input type="number" id="set-days" value="${s.days}" min="1" max="10" step="1"></div>
      <div class="field"><label>Day clock (s, 0 = manual close)</label>
        <input type="number" id="set-ds" value="${s.daySeconds}" min="0" max="7200" step="1"></div>
    </div>
    <div class="formrow">
      <div class="field"><label>Fee per unit</label>
        <input type="number" id="set-fee" min="0" max="10" step="any" value="${s.feePerUnit ?? 0}"></div>
      <div class="field"><label>Anonymous trading</label>
        <select id="set-anon"><option value="off">Off</option>
          <option value="on" ${s.anonymous ? 'selected' : ''}>On</option></select></div>
    </div>
    <div class="formrow">
      <div class="field"><label>Margin rate (%/day)</label>
        <input type="number" id="set-margin" min="0" max="20" step="any" value="${s.marginRate ?? 0}"></div>
      <div class="field"><label>Event cards</label>
        <select id="set-events"><option value="off">Off</option>
          <option value="on" ${s.eventCards ? 'selected' : ''}>On</option></select></div>
      <div class="field"><label>New event every (s, 0 = day open)</label>
        <input type="number" id="set-eventsec" min="0" max="3600" step="5" value="${s.eventEverySeconds ?? 60}"></div>
    </div>
    ${trialFields(s)}
    ${cardValueFields(s)}
    <button class="btn" type="submit">Apply from now on</button>
    <p class="small muted">The day clock applies from the next day open; fees to the next trade;
    card values to settlement — a mid-game value change is a "news shock", announce it!</p>
  </form>`;
}

function hostActions(S) {
  const b = [];
  const days = S.settings.days;
  if (S.phase === 'lobby') {
    b.push(`<button class="btn primary big" id="startbtn" data-action="host-cmd" data-cmd="start">▶ Deal cards &amp; open the market</button>`);
  }
  if (S.phase === 'open') {
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="extend">+30s</button>`);
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="event">🃏 Draw event</button>`);
    b.push(S.day < days
      ? `<button class="btn primary" data-action="host-cmd" data-cmd="endDay">🌙 Close day ${S.day} now</button>`
      : `<button class="btn primary" data-action="host-cmd" data-cmd="endDay" data-confirm="Close the market and settle? All private cards will be revealed.">🏁 Close &amp; settle</button>`);
  }
  if (S.phase === 'trial') {
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="extend">+30s</button>`);
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="resolve">⚖️ Close the investigation</button>`);
  }
  if (S.phase === 'between') {
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="next">▶ Open day ${S.day + 1} of ${days}</button>`);
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="settle" data-confirm="Settle now, skipping the remaining day(s)? All private cards will be revealed.">🏁 Settle early</button>`);
  }
  if (S.phase === 'settled') {
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="rematch">↻ Rematch (same players)</button>`);
  }
  b.push(`<button class="btn danger mini" data-action="host-cmd" data-cmd="reset" data-confirm="Throw away the whole game and everyone's seats?">Reset game</button>`);
  return `<div class="actionsrow">${b.join('')}</div>`;
}

function buildHost(S) {
  const head = topbarHTML(S, `<span class="chip" id="connchip"></span>`);
  let left = '', right = '';

  if (S.phase === 'lobby') {
    const s = S.settings;
    left = `
      <div class="panel">
        <h2>Room <span style="letter-spacing:.12em">${esc(ROOM)}</span></h2>
        <p class="small muted">Players join at <b id="joinurl"></b> — put
          <a href="${R('/board')}" target="_blank">the board view</a> on the projector/TV.</p>
        <div class="actionsrow">
          <button class="btn mini" data-action="copy-invite">📋 Copy invite link</button>
          <button class="btn mini" data-action="copy-hosturl">Copy host link (for another device)</button>
        </div>
        ${hostActions(S)}
      </div>
      <div class="panel"><h3>Players</h3><div id="roster"></div></div>`;
    right = `
      <form class="panel" id="settingsform">
        <h2>Settings</h2>
        <div class="field"><label>Roles</label>
          <select id="set-roles">
            <option value="assigned" ${s.roles === 'assigned' ? 'selected' : ''}>Assigned — market makers vs liquidity takers</option>
            <option value="everyone" ${s.roles === 'everyone' ? 'selected' : ''}>Everyone quotes and takes</option>
          </select></div>
        <div class="field"><label>Private cards dealt from</label>
          <select id="set-pool">
            <option value="hs" ${s.dealPool === 'hs' ? 'selected' : ''}>Hearts &amp; spades only (≤23 players)</option>
            <option value="full" ${s.dealPool === 'full' ? 'selected' : ''}>Full deck — clubs/diamonds worth 0 (≤49)</option>
          </select></div>
        ${botEditorHTML(S.bots)}
        <div class="formrow">
          <div class="field"><label>Trading days</label>
            <input type="number" id="set-days" value="${s.days}" min="1" max="10" step="1"></div>
          <div class="field"><label>Day clock (s, 0 = you close days)</label>
            <input type="number" id="set-ds" value="${s.daySeconds}" min="0" max="7200" step="1"></div>
        </div>
        <div class="formrow">
          <div class="field"><label>Informed players — dealt a card (blank = all)</label>
            <input type="number" id="set-informed" min="0" max="49" step="1"
                   value="${s.informedCount ?? ''}" placeholder="everyone"></div>
          <div class="field"><label>Exchange fee per unit</label>
            <input type="number" id="set-fee" min="0" max="10" step="any" value="${s.feePerUnit ?? 0}"></div>
        </div>
        <div class="formrow">
          <div class="field"><label>Max players (blank = deck limit)</label>
            <input type="number" id="set-maxp" min="2" max="49" step="1"
                   value="${s.maxPlayers ?? ''}" placeholder="deck limit"></div>
          <div class="field"><label>Margin rate (%/day on borrowed cash)</label>
            <input type="number" id="set-margin" min="0" max="20" step="any" value="${s.marginRate ?? 0}"></div>
        </div>
        <div class="formrow">
          <div class="field"><label>Event cards</label>
            <select id="set-events">
              <option value="off">Off</option>
              <option value="on" ${s.eventCards ? 'selected' : ''}>On — news drops during play</option>
            </select></div>
          <div class="field"><label>New event every (s, 0 = only at day open)</label>
            <input type="number" id="set-eventsec" min="0" max="3600" step="5"
                   value="${s.eventEverySeconds ?? 60}"></div>
        </div>
        <div class="formrow">
          <div class="field"><label>Anonymous trading</label>
            <select id="set-anon">
              <option value="off">Off — real names</option>
              <option value="on" ${s.anonymous ? 'selected' : ''}>On — pseudonyms until settlement</option>
            </select></div>
        </div>
        ${trialFields(s)}
        <p class="small muted">An investigation runs after each day closes: everyone
          privately accuses one trader of holding a big mover. Right and the exposed pay
          the indemnity; wrong and the accuser pays the fee. Only accusers hear their own
          verdict, so tomorrow's market still has something to find out. Keep the
          indemnity below what trading on a card is worth, or the informed stop trading.</p>
        ${cardValueFields(s)}
        <button class="btn" type="submit">Save settings</button>
        <p class="small muted">The card count is public but <i>who</i> got one stays secret —
        no-card players' orders are indistinguishable from informed ones.</p>
      </form>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'open') {
    left = `
      <div class="panel"><h2>${S.settings.days > 1 ? `Day ${S.day} of ${S.settings.days} — market open` : 'Market open'}</h2>
        <div id="newsbar"></div>
        <div class="spreadline" id="spreadline" style="text-align:left;font-size:1.05rem;font-weight:700"></div>
        ${bookHTML()}<hr class="divider">${hostActions(S)}</div>
      <div class="panel"><h3>Players</h3><div id="roster"></div></div>`;
    right = `${chartPanelHTML(190)}
      <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>
      ${liveTweaksForm(S)}
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'trial') {
    left = `<div id="newsbar"></div>
      <div class="panel"><h2>🔎 Investigation — day ${S.day}</h2>
        <p class="small muted">Everyone is privately naming one trader they think holds a
        big mover. You cannot see the accusations, and neither can anyone else — only each
        accuser learns their own verdict, which is what keeps tomorrow's market worth
        trading. Close it when the room is done.</p>
        <p><b id="trialcount"></b></p>
        ${hostActions(S)}</div>
      <div class="panel"><h3>Players</h3><div id="roster"></div></div>`;
    right = `${chartPanelHTML(190, 'Price so far')}
      <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'between') {
    left = `<div id="newsbar"></div>
      <div class="panel"><h2>Day ${S.day} closed</h2>
        <p class="small muted">Overnight — the book was wiped; positions and cash carry into
        day ${S.day + 1}.</p>
        ${hostActions(S)}</div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
    right = `${chartPanelHTML(190, 'Price so far')}
      ${liveTweaksForm(S)}
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'settled') {
    left = `<div id="newsbar"></div>
      <div class="panel"><h2>Final results</h2>${settlementHTML(S)}</div>`;
    right = `<div class="panel"><h2>Next</h2>${hostActions(S)}</div>
      ${chartPanelHTML(190, 'Price vs. V')}
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  }

  return `${head}<div class="wrap"><div class="grid2"><div>${left}</div><div>${right}</div></div></div>`;
}

/* ---------- board ---------- */

function buildBoard(S) {
  const head = topbarHTML(S,
    `<span class="chip">room <b style="letter-spacing:.12em">${esc(ROOM)}</b></span>
     <button class="btn mini" id="soundbtn" data-action="sound">🔇 sound</button>`);
  let main = '';

  if (S.phase === 'lobby') {
    main = `
      <div class="bigcenter">
        <h1 style="font-size:2.2em">Trading with imperfect information</h1>
        <p class="muted">Grab your phone and join the game:</p>
        <div class="joinurl" id="joinurl"></div>
        <p class="small muted" id="setsline"></p>
        <hr class="divider">
        <div class="rosterchips" id="roster"></div>
      </div>`;
  } else if (S.phase === 'open') {
    const { k, n } = dealtCount(S);
    main = `
      <div id="newsbar"></div>
      <div class="spreadline" id="spreadline"></div>
      <div class="cols">
        <div>${chartPanelHTML(340)}
          <div class="panel"><h3>Order book</h3>${bookHTML()}</div></div>
        <div>
          <div class="panel">${publicCardsHTML(S, 'sm')}
            <p class="small muted">+ ${k} private card${k === 1 ? '' : 's'} among ${n} players${k < n ? ' — who holds one is secret' : ' (one each)'}, revealed at settlement.
            V = sum of <b>all</b> card points.</p></div>
          <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div>
        </div>
      </div>`;
  } else if (S.phase === 'trial') {
    main = `
      <div id="newsbar"></div>
      <div class="bigcenter">
        <h1 style="font-size:2.2em">🔎 Investigation</h1>
        <p class="muted">Day ${S.day} is closed. Everyone is naming one trader they believe
          is holding a big mover — a card worth ±${material()} or more.</p>
        <div class="joinurl" id="trialcount"></div>
        <p class="small muted">No one sees anyone else's accusation. The exposed pay an
          indemnity to whoever read them; a wrong accuser pays them a fee. Only the
          accuser is told — so what the market learns tonight, it learns unevenly.</p>
      </div>
      <div class="cols">
        <div>${chartPanelHTML(300, 'Price so far')}</div>
        <div><div class="panel"><h3>Standings</h3><div id="standings"></div></div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div></div>
      </div>`;
  } else if (S.phase === 'between') {
    main = `
      <div class="bigcenter" style="padding:24px 0 8px">
        <h1 style="font-size:2em">Day ${S.day} closed</h1>
        <p class="muted">Overnight — the book is wiped, positions carry into day ${S.day + 1}.</p>
        <div id="newsbar"></div>
      </div>
      <div class="cols">
        <div>${chartPanelHTML(300, 'Price so far')}</div>
        <div><div class="panel"><h3>Standings</h3><div id="standings"></div></div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape compact" id="tape"></ul></div></div>
      </div>`;
  } else if (S.phase === 'settled') {
    main = `<div id="newsbar"></div>
      <div class="panel">${settlementHTML(S, { podium: true })}</div>
      ${chartPanelHTML(300, 'Where the market traded vs. V')}`;
  }

  return `${head}<div class="wrap">${main}</div>`;
}

/* ------------------------------------------------ actions */

document.addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  try {
    if (f.id === 'joinroomform') {
      const code = $('j-code').value.trim().toUpperCase();
      if (!/^[A-Z]{5}$/.test(code)) {
        $('joinroommsg').innerHTML = '<p class="muted">Room codes are 5 letters — check the board or ask your host.</p>';
        return;
      }
      const r = await fetch('/api/rooms/' + code);
      if (r.ok) location.href = `/r/${code}`;
      else $('joinroommsg').innerHTML = `<p class="muted">No room <b>${esc(code)}</b> — check the code with your host (rooms expire after a while).</p>`;
    } else if (f.id === 'joinform') {
      const name = $('j-name').value;
      try {
        const d = await api(R('/api/join'), { name });
        setTok(d.token); localStorage.gmName = d.name;
        connect();
      } catch (err) {
        if ((err.code === 'taken' || err.code === 'started') && err.canClaim) {
          $('joinmsg').innerHTML = `<p class="muted">Someone named <b>${esc(name)}</b> is already in
            this game. Is that you? Resuming moves the seat to this device.</p>
            <button class="btn big" data-action="claim" data-name="${esc(name)}">Resume that seat</button>`;
        } else throw err;
      }
    } else if (f.id === 'quoteform') {
      const q = { bid: $('q-bid').value, bidSize: $('q-bidsize').value,
                  ask: $('q-ask').value, askSize: $('q-asksize').value };
      const d = await api(R('/api/quote'), { token: getTok(), ...q });
      localStorage.setItem(lsKey('lastQuote'), JSON.stringify(q));
      toast(d.traded ? `Quote posted — crossed instantly for ${d.traded} unit(s)` : 'Quote posted ✓');
    } else if (f.id === 'settingsform') {
      // both the lobby form and the live-tweaks form share this handler;
      // read only the fields the current form actually has
      const st = {};
      if ($('set-roles')) st.roles = $('set-roles').value;
      if ($('set-pool')) st.dealPool = $('set-pool').value;
      if ($('set-days')) st.days = +$('set-days').value;
      if ($('set-ds')) st.daySeconds = +$('set-ds').value;
      if ($('set-informed')) st.informedCount = $('set-informed').value === '' ? null : +$('set-informed').value;
      if ($('set-maxp')) st.maxPlayers = $('set-maxp').value === '' ? null : +$('set-maxp').value;
      if ($('set-fee')) st.feePerUnit = +$('set-fee').value;
      if ($('set-margin')) st.marginRate = +$('set-margin').value;
      if ($('set-events')) st.eventCards = $('set-events').value === 'on';
      if ($('set-eventsec')) st.eventEverySeconds = +$('set-eventsec').value;
      if ($('set-anon')) st.anonymous = $('set-anon').value === 'on';
      if ($('set-trials')) st.trials = $('set-trials').value === 'on';
      if ($('set-trialsec')) st.trialSeconds = +$('set-trialsec').value;
      if ($('set-indem')) st.indemnityRate = +$('set-indem').value;
      if ($('set-falsefee')) st.falseAccusationFee = +$('set-falsefee').value;
      if ($('set-cv-A')) st.cardValues = Object.fromEntries(
        ['A', 'K', 'Q', 'J'].map(r => [r, +$('set-cv-' + r).value]));
      let bots = null;
      if ($('botrows')) {
        // rows with a blank seat name get the default name of their strategy
        bots = [...$('botrows').querySelectorAll('.botrow')].map(r => {
          const type = r.querySelector('.bottype').value;
          const name = r.querySelector('.botname').value.trim() || BOT_DEFAULT_NAMES[type];
          return { name, type };
        });
      }
      const payload = { key: hostKey, action: 'settings', settings: st };
      if (bots) payload.bots = bots;
      await api(R('/api/host'), payload);
      toast('Settings saved ✓');
    } else if (f.id === 'accuseform') {
      const who = f.querySelector('input[name=acc-who]:checked');
      const dir = f.querySelector('input[name=acc-dir]:checked');
      if (!who || !dir) return toast('Pick a trader and which way they are leaning.', 'err');
      await api(R('/api/accuse'), { token: getTok(), target: who.value, dir: dir.value });
      toast('Accusation filed — nobody else can see it ✓');
    } else if (f.id === 'hostkeyform') {
      tryHostKey($('h-key').value.trim());
    }
  } catch (err) { toast(esc(err.message), 'err'); }
});

document.addEventListener('click', async e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const a = el.dataset.action;
  try {
    if (a === 'rules') showRules();
    else if (a === 'news-all') showNews();
    else if (a === 'modal-close') $('modal').classList.add('hidden');
    else if (a === 'create-room') {
      el.disabled = true;
      try {
        const d = await api('/api/rooms', {});
        localStorage.setItem(`gm:${d.code}:hostKey`, d.hostKey);
        location.href = `/r/${d.code}/host`;
      } finally { el.disabled = false; }
    } else if (a === 'copy-invite' || a === 'copy-hosturl') {
      const text = a === 'copy-invite' ? S?.joinUrl : S?.hostUrl;
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        toast('Link copied ✓');
      } catch { prompt('Copy this link:', text); }
    } else if (a === 'claim') {
      const d = await api(R('/api/claim'), { name: el.dataset.name });
      setTok(d.token); localStorage.gmName = d.name;
      connect();
    } else if (a === 'size-dec' || a === 'size-inc') {
      const inp = $('msize');
      inp.value = Math.max(1, Math.min(99, (+inp.value || 1) + (a === 'size-inc' ? 1 : -1)));
    } else if (a === 'pull-quotes') {
      const d = await api(R('/api/cancel'), { token: getTok() });
      toast(d.canceled ? 'Quotes pulled ✓' : 'Nothing resting to pull');
    } else if (a === 'mkt-buy' || a === 'mkt-sell') {
      el.disabled = true;
      setTimeout(() => { if (S?.phase === 'open') el.disabled = false; }, 450);
      const d = await api(R('/api/market'), {
        token: getTok(), side: a === 'mkt-buy' ? 'buy' : 'sell',
        size: +$('msize').value, reqId: Math.random().toString(36).slice(2) });
      const parts = d.fills.map(f => `${f.size} @ ${fmt(f.price)} (${esc(f.name)})`).join(', ');
      const short = d.filled < d.requested ? ` — only ${d.filled}/${d.requested}, the book ran dry` : '';
      toast(`${a === 'mkt-buy' ? '🟢 Bought' : '🔴 Sold'} ${parts}${short}`);
    } else if (a === 'host-cmd') {
      if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
      const body = { key: hostKey, action: el.dataset.cmd };
      if (el.dataset.cmd === 'extend') body.seconds = 30;
      await api(R('/api/host'), body);
    } else if (a === 'bot-add') {
      const box = $('botrows');
      if (!box) return;
      box.insertAdjacentHTML('beforeend', botRowHTML());
      box.lastElementChild.querySelector('.botname').focus();
    } else if (a === 'bot-del') {
      el.closest('.botrow')?.remove();
    } else if (a === 'host-role') {
      await api(R('/api/host'), { key: hostKey, action: 'role', pid: el.dataset.pid, role: el.dataset.role });
    } else if (a === 'host-kick') {
      if (!confirm(`Remove ${el.dataset.name} from the game?`)) return;
      await api(R('/api/host'), { key: hostKey, action: 'kick', pid: el.dataset.pid });
    } else if (a === 'abstain') {
      await api(R('/api/accuse'), { token: getTok(), target: null });
      toast('Sitting this one out — no accusation filed.');
    } else if (a === 'chart-mode') {
      chartMode = el.dataset.mode === 'line' ? 'line' : 'candles';
      localStorage.setItem(CHART_MODE_KEY, chartMode);
      drawChart();
    } else if (a === 'sound') {
      soundOn = !soundOn;
      el.textContent = soundOn ? '🔊 sound' : '🔇 sound';
      if (soundOn) blip(880);
    }
  } catch (err) { toast(esc(err.message), 'err'); }
});

/* ------------------------------------------------ rules modal */

function showRules() {
  $('modalbody').innerHTML = `
    <h2>How the game works</h2>
    <ul>
      <li><b>The asset:</b> three cards are face up for everyone; players may also hold one
        <b>private</b> card each. At the end, the asset pays <b>V = sum of the points of ALL
        dealt cards</b> (public + every private card in play).</li>
      <li><b>Card points</b> (hearts &amp; spades), by default: Ace = <b>−40</b>, King =
        <b>+20</b>, Queen &amp; Jack = 0, others = face value. All clubs &amp; diamonds = 0.
        The host can change A/K/Q/J values — current values always show in the settings
        line.</li>
      <li><b>Informed vs. uninformed:</b> the host may deal private cards to only <i>k</i>
        players, chosen at random. The count is public; the identities are secret. If you got
        no card, you hold nothing (worth 0) — but nobody else knows that.</li>
      <li><b>Other twists the host can flip:</b> a per-unit exchange fee charged to both sides
        of every trade, and anonymous trading (pseudonyms on the book, tape and standings
        until settlement). Active rules always show in the settings line.</li>
      <li><b>The market is continuous:</b> once the host opens it, everything happens live in one
        order book. Market makers keep a two-sided quote — bid, bid size, ask, ask size (prices
        &gt; 0, your ask above your own bid). Post again any time to reprice, or pull your quotes.</li>
      <li><b>Crossing:</b> if your bid reaches someone's resting ask (or your ask their bid), it
        trades <b>immediately at the resting order's price</b> — price-time priority, so stale
        quotes get picked off.</li>
      <li><b>Liquidity takers</b> hit the bid / lift the ask with market orders — filled at the
        best resting price(s), first come first served; big orders walk the book.</li>
      <li><b>Trading days:</b> the session can run over several "days". Overnight the book is
        wiped (positions and cash carry over); after the last day the market settles.</li>
      <li><b>Event cards (host option):</b> news lands at each day open and then on a repeating
        timer (default about once a minute), plus whenever the host draws a card — value
        shocks, fee changes, dividends and levies… and sometimes a
        <b>private order</b> forcing one trader to buy or sell before the close. Nobody else
        knows who got it; the unfilled part executes automatically at the close.</li>
      <li><b>Investigations (host option):</b> after a day closes, everyone privately names
        one trader they think is holding a <b>big mover</b> — a card worth ±${material()} or
        more, which by default means an Ace or a King. Read it right and they pay an
        indemnity, split between everyone who read them; read it wrong and you pay them a
        fee. <b>Only you</b> are told how your own accusation went, so a good read buys you
        information nobody else has — and being obvious in the market gets expensive.
        Everything paid is a transfer between players. Who accused whom comes out at
        settlement.</li>
      <li><b>Margin (host option):</b> negative cash is a margin loan — it is charged the set
        interest rate at every day close.</li>
      <li><b>Scoring:</b> cash from your fills <b>+ net position × V</b>. Shorts are fine;
        so are negative scores. Trade on what you know — and on what others' trades tell you.</li>
    </ul>
    <p class="center" style="margin-top:6px"><a class="btn" href="/practice" target="_blank" rel="noopener">🎓 Practice solo at the training table ↗</a></p>`;
  $('modal').classList.remove('hidden');
}

/* ------------------------------------------------ sounds & misc */

function blip(freq = 880, dur = 0.07) {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'sine'; o.frequency.value = freq;
    g.gain.value = 0.06;
    o.connect(g); g.connect(audioCtx.destination);
    o.start();
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + dur);
    o.stop(audioCtx.currentTime + dur);
  } catch { /* no audio, no problem */ }
}

// keep phones awake during play (best effort)
let wakeLock = null;
async function keepAwake() {
  try { wakeLock = await navigator.wakeLock?.request('screen'); } catch { }
}
if (KIND === 'player') {
  document.addEventListener('click', () => { if (!wakeLock) keepAwake(); }, { once: true });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') keepAwake();
  });
}

/* ------------------------------------------------ init */

async function tryHostKey(key) {
  if (!key) return buildHostKeyForm();
  try {
    const r = await fetch(R('/api/state') + '?key=' + encodeURIComponent(key));
    if (r.status === 404) return buildRoomGone();
    if (!r.ok) throw new Error('That key does not match this room.');
    hostKey = key;
    localStorage.setItem(lsKey('hostKey'), key);
    history.replaceState(null, '', R('/host'));   // strip ?key=… from the URL bar
    connect();
  } catch (err) {
    buildHostKeyForm(err.message);
  }
}

if (KIND === 'landing') {
  buildLanding();
} else if (KIND === 'host') {
  const urlKey = new URL(location.href).searchParams.get('key');
  tryHostKey(urlKey || hostKey);
} else if (KIND === 'player') {
  const m = location.hash.match(/^#t=([0-9a-f]{8,})$/);   // token hand-off link
  if (m) { setTok(m[1]); history.replaceState(null, '', R('')); }
  if (getTok()) connect();
  else buildJoin();
} else {
  connect();
}
