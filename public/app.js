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
  between: `Day ${S.day} closed`,
  settled: 'Settlement',
}[S.phase] || S.phase);

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
let prevEventMax = null, prevForced = null;
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
  if ($('bookB')) { set('bookB', bookSideHTML(S.book.bids, 'No bids')); set('bookA', bookSideHTML(S.book.asks, 'No asks')); }
  if ($('log') && S.log) set('log', S.log.slice().reverse().map(l => `<li>${esc(l.msg)}</li>`).join(''));
  if ($('joinurl') && S.joinUrl) set('joinurl', esc(S.joinUrl));
  if ($('setsline')) set('setsline', settingsLine(S));
  if ($('eventline')) {
    const ev = (S.events || [])[(S.events || []).length - 1];
    set('eventline', ev
      ? `🃏 <b>${esc(ev.headline)}</b> <span class="small muted">(day ${ev.day}) ${esc(ev.detail || '')}</span>`
      : '');
  }

  if (S.me) {
    set('mypos', `pos <b>${signed(S.me.pos)}</b>`);
    set('mycash', `cash <b class="${numCls(S.me.cash)}">${signed(S.me.cash)}</b>`);
    if ($('submitnote')) set('submitnote', restingNoteHTML(S));
    if ($('forcedbanner')) set('forcedbanner', S.me.forced
      ? `📣 <b>ORDER:</b> ${S.me.forced.side === 'buy' ? 'BUY' : 'SELL'} <b>${S.me.forced.size}</b> before ` +
        `the close — any unfilled remainder executes automatically`
      : '');
    if ($('myfills')) set('myfills', myFillsHTML(S));
    if ($('bestinfo')) set('bestinfo', bestInfoHTML(S));
    if ($('buybtn')) {
      $('buybtn').disabled = !S.book.asks.some(o => !o.mine);
      $('sellbtn').disabled = !S.book.bids.some(o => !o.mine);
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
  if (S.phase === 'lobby') prevEventMax = null;
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
  if (s.maxPlayers) parts.push(`Max ${s.maxPlayers} players`);
  if (s.anonymous) parts.push('Anonymous trading');
  return parts.join(' · ');
}

function rosterChipsHTML(S) {
  return S.players.filter(p => p.active).map(p =>
    `<span class="chip"><span class="dot ${p.connected ? 'on' : ''}"></span>${esc(p.name)}` +
    (S.settings.roles === 'assigned' ? ` · ${roleShort(p.role)}` : '') + `</span>`).join(' ');
}

function hostRosterHTML(S) {
  const assigned = S.settings.roles === 'assigned';
  const inLobby = S.phase === 'lobby';
  const rows = S.players.map(p => {
    if (!p.active) return `<tr class="muted"><td colspan="4">${esc(p.name)} (removed)</td></tr>`;
    const roleCell = inLobby && assigned
      ? `<button class="btn mini ${p.role === 'mm' ? 'primary' : ''}" data-action="host-role" data-pid="${p.id}" data-role="mm">MM</button>
         <button class="btn mini ${p.role === 'taker' ? 'primary' : ''}" data-action="host-role" data-pid="${p.id}" data-role="taker">Taker</button>`
      : roleShort(p.role);
    const aka = S.settings.anonymous && p.alias
      ? ` <span class="small muted">= ${esc(p.alias)}</span>` : '';
    return `<tr>
      <td><span class="dot ${p.connected ? 'on' : ''}"></span>${esc(p.name)}${aka}</td>
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
  const take = (st.feesCollected || 0) + (st.interestPaid || 0);
  if (take) extras.push(`Player totals therefore sum to −${fmt(take)}.`);
  return `${podiumHTML}
    <div class="vmath"><b>V</b> = public (${pub}) + private (${priv}) = <b class="${numCls(st.V)}">${st.V}</b></div>
    ${extras.length ? `<div class="vmath" style="margin-top:8px">${extras.join('<br>')}</div>` : ''}
    <div class="tblwrap"><table class="tbl">
      <thead><tr><th class="r">#</th><th>Player</th><th>Card</th><th class="r">Pos</th>
        <th class="r">Cash</th><th class="r">Pos × V</th><th class="r">Total</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="small muted">Score = cash from filled orders + net position × V, where V is the sum of the
    points of <b>all</b> dealt cards.</p>`;
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
          <button class="btn buy big" id="buybtn" data-action="mkt-buy">BUY · lift ask</button>
          <button class="btn sell big" id="sellbtn" data-action="mkt-sell">SELL · hit bid</button>
        </div>
        <p class="small muted">Fills instantly at the best resting price(s), first come first served.
        Big orders walk the book.</p>
      </div>` : '';
    main = `
      <div id="forcedbanner" style="margin:2px 0 8px;font-weight:800;color:#e5484d"></div>
      <div id="eventline" style="margin:2px 0 8px"></div>
      ${quoteForm}${takePanel}
      <div class="panel"><h3>Order book</h3>${bookHTML()}</div>
      ${meCardPanel(S, true)}
      <div class="panel"><h3>Your fills</h3><ul class="tape" id="myfills"></ul></div>
      <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
  } else if (S.phase === 'between') {
    main = `
      <div class="panel"><h2>Day ${S.day} closed</h2>
        <p class="waiting">Overnight — the book is wiped, positions and cash carry.
        Waiting for the host to open day ${S.day + 1}…</p>
        <div id="eventline"></div></div>
      ${meCardPanel(S, true)}
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
      <div class="panel"><h3>Your fills</h3><ul class="tape" id="myfills"></ul></div>
      <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>`;
  } else if (S.phase === 'settled') {
    main = `<div class="panel"><h2>Final results</h2>${settlementHTML(S)}</div>`;
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

function cardValueFields(s) {
  const v = s.cardValues || DEFAULT_CARD_VALUES;
  return `<label>Card points (hearts &amp; spades; number cards stay face value)</label>
    <div class="formrow" style="grid-template-columns:repeat(4,1fr)">
      ${['A', 'K', 'Q', 'J'].map(r => `<div class="field"><label>${r}</label>
        <input type="number" id="set-cv-${r}" value="${v[r]}" min="-200" max="200" step="1"></div>`).join('')}
    </div>`;
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
        ${cardValueFields(s)}
        <button class="btn" type="submit">Save settings</button>
        <p class="small muted">The card count is public but <i>who</i> got one stays secret —
        no-card players' orders are indistinguishable from informed ones.</p>
      </form>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'open') {
    left = `
      <div class="panel"><h2>${S.settings.days > 1 ? `Day ${S.day} of ${S.settings.days} — market open` : 'Market open'}</h2>
        <div id="eventline" style="margin:4px 0 8px"></div>
        <div class="spreadline" id="spreadline" style="text-align:left;font-size:1.05rem;font-weight:700"></div>
        ${bookHTML()}<hr class="divider">${hostActions(S)}</div>
      <div class="panel"><h3>Players</h3><div id="roster"></div></div>`;
    right = `<div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
      ${liveTweaksForm(S)}
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'between') {
    left = `
      <div class="panel"><h2>Day ${S.day} closed</h2>
        <p class="small muted">Overnight — the book was wiped; positions and cash carry into
        day ${S.day + 1}.</p>
        ${hostActions(S)}</div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
    right = `${liveTweaksForm(S)}
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'settled') {
    left = `<div class="panel"><h2>Final results</h2>${settlementHTML(S)}</div>`;
    right = `<div class="panel"><h2>Next</h2>${hostActions(S)}</div>
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
      <div id="eventline" style="text-align:center;font-size:1.15em;margin:4px 0"></div>
      <div class="spreadline" id="spreadline"></div>
      <div class="cols">
        <div><div class="panel"><h3>Order book</h3>${bookHTML()}</div>
          <div class="panel">${publicCardsHTML(S, 'sm')}
            <p class="small muted">+ ${k} private card${k === 1 ? '' : 's'} among ${n} players${k < n ? ' — who holds one is secret' : ' (one each)'}, revealed at settlement.
            V = sum of <b>all</b> card points.</p></div></div>
        <div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
          <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
        </div>
      </div>`;
  } else if (S.phase === 'between') {
    main = `
      <div class="bigcenter" style="padding:24px 0 8px">
        <h1 style="font-size:2em">Day ${S.day} closed</h1>
        <p class="muted">Overnight — the book is wiped, positions carry into day ${S.day + 1}.</p>
        <div id="eventline" style="font-size:1.15em"></div>
      </div>
      <div class="cols">
        <div><div class="panel"><h3>Standings</h3><div id="standings"></div></div></div>
        <div><div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div></div>
      </div>`;
  } else if (S.phase === 'settled') {
    main = `<div class="panel">${settlementHTML(S, { podium: true })}</div>`;
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
      if ($('set-cv-A')) st.cardValues = Object.fromEntries(
        ['A', 'K', 'Q', 'J'].map(r => [r, +$('set-cv-' + r).value]));
      await api(R('/api/host'), { key: hostKey, action: 'settings', settings: st });
      toast('Settings saved ✓');
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
    } else if (a === 'host-role') {
      await api(R('/api/host'), { key: hostKey, action: 'role', pid: el.dataset.pid, role: el.dataset.role });
    } else if (a === 'host-kick') {
      if (!confirm(`Remove ${el.dataset.name} from the game?`)) return;
      await api(R('/api/host'), { key: hostKey, action: 'kick', pid: el.dataset.pid });
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
