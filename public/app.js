/* Glosten–Milgrom trading game — client for player / host / board views. */
'use strict';

/* ------------------------------------------------ constants & utils */

const KIND = location.pathname.startsWith('/host') ? 'host'
           : location.pathname.startsWith('/board') ? 'board' : 'player';
document.body.classList.add(KIND);

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
  quote: `Round ${S.round} · Quoting`,
  market: `Round ${S.round} · Market orders`,
  between: `Round ${S.round} · Closed`,
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
let hostKey = localStorage.gmHostKey || '';
let soundOn = false, audioCtx = null;

/* ------------------------------------------------ connection */

function connect() {
  if (es) es.close();
  const p = new URLSearchParams();
  if (KIND === 'player') { p.set('role', 'player'); p.set('token', localStorage.gmToken || ''); }
  if (KIND === 'host') { p.set('role', 'host'); p.set('key', hostKey); }
  if (KIND === 'board') p.set('role', 'board');
  es = new EventSource('/events?' + p.toString());
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    document.body.classList.remove('offline');
    if (d.error) return handleErrorState(d.error);
    S = d;
    skew = d.now - Date.now();
    render();
  };
  es.onerror = () => document.body.classList.add('offline');
}

function handleErrorState(err) {
  if (es) es.close();
  document.body.classList.remove('offline');
  if (err === 'bad-token' || err === 'reset') {
    localStorage.removeItem('gmToken');
    buildJoin('That game is over — join the new one.');
  } else if (err === 'kicked') {
    $('app').innerHTML = `<div class="bigmsg"><h1>You were removed by the host</h1>
      <p class="muted">You can watch on the <a href="/board">board view</a>.</p></div>`;
  }
}

/* ------------------------------------------------ rendering core */

function render() {
  if (!S) return;
  const key = [KIND, S.phase, S.round, S.me?.role || '', !!S.settlement,
               S.settings.roles].join('|');
  if (key !== viewKey) {
    viewKey = key;
    $('app').innerHTML = BUILD[KIND](S);
    if (KIND === 'player' && S.phase === 'quote' && S.me?.canQuote) prefillQuote();
  }
  update();
  watchEvents();
}

const set = (id, html) => { const el = $(id); if (el && el.innerHTML !== html) el.innerHTML = html; };

function update() {
  set('phasechip', esc(phaseLabel(S)));
  updateTimer();

  if ($('roster')) set('roster', KIND === 'host' ? hostRosterHTML(S) : rosterChipsHTML(S));
  if ($('checklist')) set('checklist', checklistHTML(S));
  if ($('standings')) set('standings', standingsHTML(S));
  if ($('tape')) set('tape', tapeHTML(S));
  if ($('bookB')) { set('bookB', bookSideHTML(S.book.bids, 'No bids')); set('bookA', bookSideHTML(S.book.asks, 'No asks')); }
  if ($('lastquotes')) set('lastquotes', lastQuotesHTML(S));
  if ($('log') && S.log) set('log', S.log.slice().reverse().map(l => `<li>${esc(l.msg)}</li>`).join(''));
  if ($('joinurl') && S.joinUrl) set('joinurl', esc(S.joinUrl));
  if ($('setsline')) set('setsline', settingsLine(S));

  if (S.me) {
    set('mypos', `pos <b>${signed(S.me.pos)}</b>`);
    set('mycash', `cash <b class="${numCls(S.me.cash)}">${signed(S.me.cash)}</b>`);
    if ($('submitnote')) set('submitnote', S.me.quote
      ? `✓ Submitted (${fmt(S.me.quote.bid)} × ${S.me.quote.bidSize} / ${fmt(S.me.quote.ask)} × ${S.me.quote.askSize}) — you can edit until the reveal`
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
    `Quote timer: ${s.quoteSeconds ? s.quoteSeconds + 's' : 'manual'}`,
    `Market: ${s.marketSeconds ? s.marketSeconds + 's' : 'manual'}`,
  ];
  const vals = s.cardValues || DEFAULT_CARD_VALUES;
  if (['A', 'K', 'Q', 'J'].some(r => vals[r] !== DEFAULT_CARD_VALUES[r]))
    parts.push(`Points: A=${vals.A}, K=${vals.K}, Q=${vals.Q}, J=${vals.J}`);
  if (+s.feePerUnit > 0) parts.push(`Fee: ${fmt(s.feePerUnit)}/unit`);
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

function checklistHTML(S) {
  const qs = S.players.filter(p => p.active && p.hasQuote !== null && p.hasQuote !== undefined);
  if (!qs.length) return '<span class="muted">—</span>';
  return qs.map(p => `<span class="chip ${p.hasQuote ? 'done' : ''}">${p.hasQuote ? '✓' : '…'} ${esc(p.name)}</span>`).join(' ');
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
    `<li><span class="tag">R${t.round} ${t.phase === 'auction' ? 'auction' : 'mkt'}</span>
     ${esc(t.buyer)} bought <b>${t.size}</b> @ <span class="px">${fmt(t.price)}</span> from ${esc(t.seller)}</li>`).join('');
}

function myFillsHTML(S) {
  const fills = (S.me.fills || []).slice().reverse();
  if (!fills.length) return '<li class="muted">No fills yet…</li>';
  return fills.map(f =>
    `<li><span class="tag">R${f.round}</span> ${f.side} <b>${f.size}</b> @ <span class="px">${fmt(f.price)}</span>
     ${f.side === 'bought' ? 'from' : 'to'} ${esc(f.counterparty)}</li>`).join('');
}

function bestInfoHTML(S) {
  const bb = S.book.bids[0], ba = S.book.asks[0];
  return `<span>best bid: ${bb ? `<b class="num">${fmt(bb.price)}</b> ×${bb.size} (${esc(bb.name)})` : '<span class="muted">none</span>'}</span>
    <span>best ask: ${ba ? `<b class="num">${fmt(ba.price)}</b> ×${ba.size} (${esc(ba.name)})` : '<span class="muted">none</span>'}</span>`;
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

function lastQuotesHTML(S) {
  if (!S.lastQuotes) return '';
  return `<table class="tbl"><thead><tr><th>Market participant</th><th class="r">Bid size</th>
    <th class="r">Bid</th><th class="r">Ask</th><th class="r">Ask size</th></tr></thead>
    <tbody>${S.lastQuotes.map(q => `<tr><td>${esc(q.name)}</td><td class="r num">${q.bidSize}</td>
      <td class="r num">${fmt(q.bid)}</td><td class="r num">${fmt(q.ask)}</td><td class="r num">${q.askSize}</td></tr>`).join('')}
    </tbody></table>`;
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
    `The exchange collected <b>${fmt(st.feesCollected)}</b> in fees, so player totals sum to −${fmt(st.feesCollected)}.`);
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

/* ---------- player ---------- */

function buildJoin(msg = '') {
  if (es) es.close();
  $('app').innerHTML = `
    <div class="wrap" style="padding-top:10vh">
      <h1 class="center">♠♥ Trading game</h1>
      <p class="center muted">Trading with imperfect information</p>
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
      <p class="footer-note">Watching on the projector? Open <a href="/board">/board</a>.</p>
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
  } else if (S.phase === 'quote') {
    const form = me.canQuote ? `
      <div class="panel">
        <h2>Submit your quote</h2>
        <form id="quoteform">
          <div class="quotegrid">
            <div class="buycol">
              <div class="field"><label>Bid (you buy at)</label>
                <input type="number" id="q-bid" inputmode="decimal" step="0.5" min="0.01" max="999.99" required></div>
              <div class="field"><label>Bid size</label>
                <input type="number" id="q-bidsize" inputmode="numeric" step="1" min="1" max="99" required></div>
            </div>
            <div class="sellcol">
              <div class="field"><label>Ask (you sell at)</label>
                <input type="number" id="q-ask" inputmode="decimal" step="0.5" min="0.02" max="999.99" required></div>
              <div class="field"><label>Ask size</label>
                <input type="number" id="q-asksize" inputmode="numeric" step="1" min="1" max="99" required></div>
            </div>
          </div>
          <button class="btn primary big" type="submit">Submit quote</button>
          <div class="submitnote" id="submitnote"></div>
        </form>
        <p class="small muted">Both sides are required, your ask must be above your bid, and prices must be
        above 0. Your quote is <b>firm</b> once revealed.</p>
      </div>` : `
      <div class="panel"><h2>Market makers are writing quotes…</h2>
        <p class="waiting">You'll get to send market orders when the book opens.</p></div>`;
    main = `${meCardPanel(S)}${form}
      <div class="panel"><h3>Quotes in</h3><div id="checklist" class="checklist"></div></div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
  } else if (S.phase === 'market') {
    const controls = me.canTake ? `
      <div class="panel">
        <h2>Market orders open</h2>
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
        <p class="small muted">Market orders fill instantly at the best remaining price(s), first come first
        served. Big orders walk down the book.</p>
      </div>` : `
      <div class="panel"><h2>Your quotes are live</h2>
        <p class="waiting">Liquidity takers are trading against the book — watch your fills below.</p></div>`;
    main = `${controls}
      <div class="panel"><h3>Order book</h3>${bookHTML()}</div>
      <div class="panel"><h3>Your fills</h3><ul class="tape" id="myfills"></ul></div>
      <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
      ${meCardPanel(S, true)}`;
  } else if (S.phase === 'between') {
    main = `
      <div class="panel"><h2>Round ${S.round} closed</h2>
        <p class="waiting">Waiting for the host to start the next round or settle…</p></div>
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
  const q = S.me.quote || JSON.parse(localStorage.gmLastQuote || 'null');
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
      <h1 class="center">Host controls</h1>
      <div class="panel">
        ${msg ? `<p class="muted">${esc(msg)}</p>` : ''}
        <form id="hostkeyform">
          <div class="field"><label>Host key (printed in the server terminal)</label>
            <input type="text" id="h-key" autocomplete="off" placeholder="e.g. 3f9a2c1b"></div>
          <button class="btn primary big" type="submit">Open host panel</button>
        </form>
        <p class="small muted">Tip: on the computer that runs the game, just open
          <b>http://localhost:3000/host</b> — no key needed there.</p>
      </div>
    </div>`;
}

function cardValueFields(s) {
  const v = s.cardValues || DEFAULT_CARD_VALUES;
  return `<label>Card points (hearts &amp; spades; number cards stay face value)</label>
    <div class="formrow" style="grid-template-columns:repeat(4,1fr)">
      ${['A', 'K', 'Q', 'J'].map(r => `<div class="field"><label>${r}</label>
        <input type="number" id="set-cv-${r}" value="${v[r]}" min="-200" max="200" step="5"></div>`).join('')}
    </div>`;
}

function liveTweaksForm(S) {
  const s = S.settings;
  return `<form class="panel" id="settingsform">
    <h3>Live rule tweaks</h3>
    <div class="formrow">
      <div class="field"><label>Quote timer (s, 0 = manual)</label>
        <input type="number" id="set-qs" value="${s.quoteSeconds}" min="0" max="600" step="5"></div>
      <div class="field"><label>Market timer (s, 0 = manual)</label>
        <input type="number" id="set-ms" value="${s.marketSeconds}" min="0" max="600" step="5"></div>
    </div>
    <div class="formrow">
      <div class="field"><label>Fee per unit</label>
        <input type="number" id="set-fee" min="0" max="10" step="0.5" value="${s.feePerUnit ?? 0}"></div>
      <div class="field"><label>Anonymous trading</label>
        <select id="set-anon"><option value="off">Off</option>
          <option value="on" ${s.anonymous ? 'selected' : ''}>On</option></select></div>
    </div>
    ${cardValueFields(s)}
    <button class="btn" type="submit">Apply from now on</button>
    <p class="small muted">Timers apply to the next phase; fees to the next trade; card
    values to settlement — a mid-game value change is a "news shock", announce it!</p>
  </form>`;
}

function hostActions(S) {
  const b = [];
  if (S.phase === 'lobby') {
    b.push(`<button class="btn primary big" id="startbtn" data-action="host-cmd" data-cmd="start">▶ Deal cards &amp; start round 1</button>`);
  }
  if (S.phase === 'quote') {
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="reveal">Reveal &amp; match now</button>`);
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="extend">+30s</button>`);
  }
  if (S.phase === 'market') {
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="endMarket">Close the round now</button>`);
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="extend">+30s</button>`);
  }
  if (S.phase === 'between') {
    b.push(`<button class="btn primary" data-action="host-cmd" data-cmd="next">▶ Start round ${S.round + 1}</button>`);
    b.push(`<button class="btn" data-action="host-cmd" data-cmd="settle" data-confirm="Settle now? All private cards will be revealed.">🏁 Settle &amp; reveal cards</button>`);
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
        <h2>Game setup</h2>
        <p class="small muted">Players join at <b id="joinurl"></b> — put <a href="/board" target="_blank">/board</a> on the projector.</p>
        ${S.hostUrl ? `<p class="small muted">Host controls from another device: ${esc(S.hostUrl)}</p>` : ''}
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
          <div class="field"><label>Quote timer (s, 0 = manual)</label>
            <input type="number" id="set-qs" value="${s.quoteSeconds}" min="0" max="600" step="5"></div>
          <div class="field"><label>Market timer (s, 0 = manual)</label>
            <input type="number" id="set-ms" value="${s.marketSeconds}" min="0" max="600" step="5"></div>
        </div>
        <div class="formrow">
          <div class="field"><label>Players dealt a card (blank = all)</label>
            <input type="number" id="set-informed" min="0" max="49" step="1"
                   value="${s.informedCount ?? ''}" placeholder="everyone"></div>
          <div class="field"><label>Exchange fee per unit</label>
            <input type="number" id="set-fee" min="0" max="10" step="0.5" value="${s.feePerUnit ?? 0}"></div>
        </div>
        <div class="field"><label>Anonymous trading</label>
          <select id="set-anon">
            <option value="off">Off — real names on the book &amp; tape</option>
            <option value="on" ${s.anonymous ? 'selected' : ''}>On — pseudonyms until settlement</option>
          </select></div>
        ${cardValueFields(s)}
        <button class="btn" type="submit">Save settings</button>
        <p class="small muted">The card count is public but <i>who</i> got one stays secret —
        no-card players' orders are indistinguishable from informed ones.</p>
      </form>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'quote') {
    left = `
      <div class="panel"><h2>Waiting for quotes…</h2>
        <div id="checklist" class="checklist"></div><hr class="divider">${hostActions(S)}
        <p class="small muted">Reveals automatically 5s after the last quote arrives (or when the timer runs out).</p>
      </div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
    right = `<div class="panel"><h3>Players</h3><div id="roster"></div></div>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'market') {
    left = `
      <div class="panel"><h2>Market orders live</h2>
        <div class="spreadline" id="spreadline" style="text-align:left;font-size:1.05rem;font-weight:700"></div>
        ${bookHTML()}<hr class="divider">${hostActions(S)}</div>`;
    right = `<div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
      <div class="panel"><h3>Log</h3><ul class="loglist" id="log"></ul></div>`;
  } else if (S.phase === 'between') {
    left = `
      <div class="panel"><h2>Round ${S.round} closed</h2>${hostActions(S)}</div>
      <div class="panel"><h3>Standings</h3><div id="standings"></div></div>`;
    right = `<div class="panel"><h3>Last revealed quotes</h3><div id="lastquotes"></div></div>
      ${liveTweaksForm(S)}
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
  const head = topbarHTML(S, `<button class="btn mini" id="soundbtn" data-action="sound">🔇 sound</button>`);
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
  } else if (S.phase === 'quote') {
    const { k, n } = dealtCount(S);
    main = `
      <div class="cols">
        <div>
          <div class="panel"><h3>Public cards</h3>${publicCardsHTML(S, 'lg')}
            <p class="small muted">+ ${k} private card${k === 1 ? '' : 's'} among ${n} players${k < n ? ' — who holds one is secret' : ' (one each)'}, revealed at settlement.
            V = sum of <b>all</b> card points.</p></div>
          <div class="panel"><h3>Quotes in</h3><div id="checklist" class="checklist"></div>
            <p class="small muted">Market makers: submit your bid / ask on your phone.</p></div>
        </div>
        <div>
          <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
        </div>
      </div>`;
  } else if (S.phase === 'market') {
    main = `
      <div class="spreadline" id="spreadline"></div>
      <div class="cols">
        <div><div class="panel"><h3>Order book</h3>${bookHTML()}</div>
          <div class="panel">${publicCardsHTML(S, 'sm')}</div></div>
        <div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
          <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
        </div>
      </div>`;
  } else if (S.phase === 'between') {
    main = `
      <div class="cols">
        <div><div class="panel"><h3>Round ${S.round} — what everyone quoted</h3><div id="lastquotes"></div></div>
          <div class="panel">${publicCardsHTML(S, 'sm')}</div></div>
        <div>
          <div class="panel"><h3>Standings</h3><div id="standings"></div></div>
          <div class="panel"><h3>Trade tape</h3><ul class="tape" id="tape"></ul></div>
        </div>
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
    if (f.id === 'joinform') {
      const name = $('j-name').value;
      try {
        const d = await api('/api/join', { name });
        localStorage.gmToken = d.token; localStorage.gmName = d.name;
        connect();
      } catch (err) {
        if (err.code === 'taken' && err.canClaim) {
          $('joinmsg').innerHTML = `<p class="muted">Someone named <b>${esc(name)}</b> is in the game but
            not connected. Is that you?</p>
            <button class="btn big" data-action="claim" data-name="${esc(name)}">Resume that seat</button>`;
        } else throw err;
      }
    } else if (f.id === 'quoteform') {
      const q = { bid: $('q-bid').value, bidSize: $('q-bidsize').value,
                  ask: $('q-ask').value, askSize: $('q-asksize').value };
      await api('/api/quote', { token: localStorage.gmToken, ...q });
      localStorage.gmLastQuote = JSON.stringify(q);
      toast('Quote submitted ✓');
    } else if (f.id === 'settingsform') {
      // both the lobby form and the live-tweaks form share this handler;
      // read only the fields the current form actually has
      const st = {};
      if ($('set-roles')) st.roles = $('set-roles').value;
      if ($('set-pool')) st.dealPool = $('set-pool').value;
      if ($('set-qs')) st.quoteSeconds = +$('set-qs').value;
      if ($('set-ms')) st.marketSeconds = +$('set-ms').value;
      if ($('set-informed')) st.informedCount = $('set-informed').value === '' ? null : +$('set-informed').value;
      if ($('set-fee')) st.feePerUnit = +$('set-fee').value;
      if ($('set-anon')) st.anonymous = $('set-anon').value === 'on';
      if ($('set-cv-A')) st.cardValues = Object.fromEntries(
        ['A', 'K', 'Q', 'J'].map(r => [r, +$('set-cv-' + r).value]));
      await api('/api/host', { key: hostKey, action: 'settings', settings: st });
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
    else if (a === 'claim') {
      const d = await api('/api/claim', { name: el.dataset.name });
      localStorage.gmToken = d.token; localStorage.gmName = d.name;
      connect();
    } else if (a === 'size-dec' || a === 'size-inc') {
      const inp = $('msize');
      inp.value = Math.max(1, Math.min(99, (+inp.value || 1) + (a === 'size-inc' ? 1 : -1)));
    } else if (a === 'mkt-buy' || a === 'mkt-sell') {
      el.disabled = true;
      setTimeout(() => { if (S?.phase === 'market') el.disabled = false; }, 450);
      const d = await api('/api/market', {
        token: localStorage.gmToken, side: a === 'mkt-buy' ? 'buy' : 'sell',
        size: +$('msize').value, reqId: Math.random().toString(36).slice(2) });
      const parts = d.fills.map(f => `${f.size} @ ${fmt(f.price)} (${esc(f.name)})`).join(', ');
      const short = d.filled < d.requested ? ` — only ${d.filled}/${d.requested}, the book ran dry` : '';
      toast(`${a === 'mkt-buy' ? '🟢 Bought' : '🔴 Sold'} ${parts}${short}`);
    } else if (a === 'host-cmd') {
      if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
      const body = { key: hostKey, action: el.dataset.cmd };
      if (el.dataset.cmd === 'extend') body.seconds = 30;
      await api('/api/host', body);
    } else if (a === 'host-role') {
      await api('/api/host', { key: hostKey, action: 'role', pid: el.dataset.pid, role: el.dataset.role });
    } else if (a === 'host-kick') {
      if (!confirm(`Remove ${el.dataset.name} from the game?`)) return;
      await api('/api/host', { key: hostKey, action: 'kick', pid: el.dataset.pid });
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
      <li><b>Each round:</b> market makers privately submit a two-sided quote — bid, bid size, ask,
        ask size (prices &gt; 0, your ask above your own bid).</li>
      <li><b>The reveal (call auction):</b> all quotes go public. Where a bid ≥ an ask, they trade
        <b>at the ask price</b>, volume = smaller size. Best prices fill first; ties go to bigger
        size, then luck.</li>
      <li><b>Market-order window (~2 min):</b> leftover quotes stay live in the book. Liquidity
        takers hit the bid / lift the ask with market orders — first come, first served; big orders
        walk the book. Quotes are firm: no cancels.</li>
      <li><b>Round ends:</b> unfilled orders are canceled. New round, new quotes.</li>
      <li><b>Scoring:</b> cash from your fills <b>+ net position × V</b>. Shorts are fine;
        so are negative scores. Trade on what you know — and on what others' trades tell you.</li>
    </ul>`;
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
  try {
    const r = await fetch('/api/state?key=' + encodeURIComponent(key));
    if (!r.ok) throw new Error(key ? 'That key does not match — check the server terminal.' : '');
    hostKey = key;
    localStorage.gmHostKey = key;
    history.replaceState(null, '', '/host');
    connect();
  } catch (err) {
    buildHostKeyForm(err.message);
  }
}

if (KIND === 'host') {
  const urlKey = new URL(location.href).searchParams.get('key');
  if (urlKey) tryHostKey(urlKey);
  else if (hostKey) tryHostKey(hostKey);
  else tryHostKey('');   // no key needed when this browser runs on the server machine
} else if (KIND === 'player') {
  const m = location.hash.match(/^#t=([0-9a-f]{8,})$/);   // token hand-off link
  if (m) { localStorage.gmToken = m[1]; history.replaceState(null, '', '/'); }
  if (localStorage.gmToken) connect();
  else buildJoin();
} else {
  connect();
}
