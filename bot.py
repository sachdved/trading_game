"""AI opponents for the Glosten-Milgrom trading game.

Joins a room as an ordinary player through the public HTTP API (the same
endpoints the browser uses) and plays one of four strategies:

  ev      pure expected-value maximizer; honest quotes, never bluffs
  bluff   heavy bluffer; camouflages big cards, runs decoy bursts
  mix     EV backbone wrapped in an adaptive noise layer
  noise   feigns being uninformed; public-info policy plus a card tilt

Stdlib only. Design: AI_PLAYERS.md.

  python3 bot.py --url http://HOST:3000 --code KFQTR --type ev
  python3 bot.py --url http://HOST:3000 --code KFQTR --type noise --name "Ghost"
"""

import argparse
import http.client
import json
import math
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

RANKS = 'A2345678910JQK'
DEFAULT_VALUES = {'A': -40, 'K': 20, 'Q': 0, 'J': 0}


def round2(v):
    return round(v + 0.0, 2)


def rank_value(rank, values):
    if rank in values:
        return int(values[rank])
    return int(rank)


def card_value(card, values):
    if not card or card.get('suit') in ('d', 'c'):
        return 0
    return rank_value(card['rank'], values or DEFAULT_VALUES)


def _pool(settings):
    values = settings.get('cardValues') or DEFAULT_VALUES
    s = 2 * sum(rank_value(r, values) for r in RANKS)
    p = 0
    for c in (settings.get('_public') or []):
        p += card_value(c, values)
    n = 23 if settings.get('dealPool') == 'hs' else 49
    return values, s, p, n


def prior_mean(settings, public_cards, n, own_card):
    values, s, p, pool_n = _pool(dict(settings, _public=public_cards))
    k = settings.get('informedCount')
    k = n if k is None else min(int(k), n)
    c = card_value(own_card, values)
    if own_card is not None:
        return p + c + (k - 1) * (s - p - c) / (pool_n - 1)
    return p + k * (s - p) / pool_n


def public_mean(settings, public_cards, n):
    values, s, p, pool_n = _pool(dict(settings, _public=public_cards))
    k = settings.get('informedCount')
    k = n if k is None else min(int(k), n)
    return p + k * (s - p) / pool_n


def hidden_tilt(settings, public_cards, n, own_card):
    if own_card is None:
        return 0.0
    values, s, p, pool_n = _pool(dict(settings, _public=public_cards))
    k = settings.get('informedCount')
    k = n if k is None else min(int(k), n)
    c = card_value(own_card, values)
    mu = (s - p) / pool_n
    return (pool_n - k) / (pool_n - 1) * (c - mu)


def big_mover_stats(settings, public_cards, own_card, material=20, rate=0.5):
    values = settings.get('cardValues') or DEFAULT_VALUES
    pub = {(c['rank'], c['suit']) for c in public_cards}
    suits = ('h', 's') if settings.get('dealPool') == 'hs' else ('h', 's', 'd', 'c')
    bull_n = bear_n = total = 0
    bull_due = bear_due = 0.0
    for suit in suits:
        for r in RANKS:
            if (r, suit) in pub:
                continue
            if own_card and own_card.get('rank') == r and own_card.get('suit') == suit:
                continue
            total += 1
            pts = 0 if suit in ('d', 'c') else rank_value(r, values)
            if pts >= material:
                bull_n += 1
                bull_due = max(bull_due, abs(pts) * rate)
            elif pts <= -material:
                bear_n += 1
                bear_due = max(bear_due, abs(pts) * rate)
    if total == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (bull_n / total, bear_n / total, bull_due, bear_due)


# ------------------------------------------------------------------ book math

def book_extremes(book):
    bb = book['bids'][0]['price'] if book['bids'] else None
    ba = book['asks'][0]['price'] if book['asks'] else None
    return bb, ba


def depth_at(book, side, limit, max_size):
    tot = 0
    if side == 'buy':
        for o in book['asks']:
            if o.get('mine'):
                continue
            if o['price'] > limit:
                break
            tot += o['size']
            if tot >= max_size:
                return max_size
    else:
        for o in book['bids']:
            if o.get('mine'):
                continue
            if o['price'] < limit:
                break
            tot += o['size']
            if tot >= max_size:
                return max_size
    return tot


def my_quote(book):
    bid = ask = None
    for o in book['bids']:
        if o.get('mine'):
            bid = o['price']
            break
    for o in book['asks']:
        if o.get('mine'):
            ask = o['price']
            break
    if bid is not None and ask is not None:
        return (bid, ask)
    return None


def make_quote(m, fee, lam, rng, size_fn):
    spread = fee + lam
    bid = round2(max(0.5, m - spread))
    ask = round2(max(1.0, m + spread))
    if ask <= bid:
        ask = round2(bid + 0.5)
    if ask > 999.99:
        ask = 999.99
        if bid >= ask:
            bid = 999.98
    if ask <= bid:
        return None
    bs = 1 if bid < 1.0 else size_fn()
    as_ = 1 if ask < 1.0 else size_fn()
    return ('quote', bid, ask, bs, as_)


def edge_dir(view, m, fee):
    book = view['book']
    bb, ba = book_extremes(book)
    if ba is not None and m - ba - fee > 0:
        return 'buy'
    if bb is not None and bb - fee - m > 0:
        return 'sell'
    return None


def ev_take(view, m, fee, rng, max_size=99):
    d = edge_dir(view, m, fee)
    if d is None:
        return None
    limit = (m - fee) if d == 'buy' else (m + fee)
    size = depth_at(view['book'], d, limit, max_size)
    if size < 1:
        return None
    return ('market', d, size)


def mid_of(view):
    bb, ba = book_extremes(view['book'])
    if bb is not None and ba is not None:
        return (bb + ba) / 2
    return bb if bb is not None else ba


# ------------------------------------------------------------------ belief

class Belief:
    def __init__(self, kappa=0.10):
        self.kappa = kappa
        self.off = 0.0
        self.last_trade_i = -1
        self.last_trade_now = 0
        self.opp = {}
        self._snap = None
        self.last_view_now = 0

    def on_view(self, view, now, prior):
        s = view['settings']
        values = s.get('cardValues')
        fee = s.get('feePerUnit') or 0
        snap = (dict(values) if values else None, fee)
        if self._snap is not None and snap != self._snap:
            self.off *= 0.5
        self._snap = snap
        self.last_view_now = now
        self.off *= 0.9995
        m = prior + max(-20.0, min(20.0, self.off))
        tape = view.get('tape') or []
        if self.last_trade_i < 0:
            if tape:
                self.last_trade_i = tape[-1]['i']
                self.last_trade_now = now
        else:
            for t in tape:
                if t['i'] > self.last_trade_i:
                    self.last_trade_i = t['i']
                    self.last_trade_now = now
                    pull = self.kappa * (t['size'] / (1 + t['size'])) * (t['price'] - m)
                    self.off += max(-1.5, min(1.5, pull))
        self._update_opps(view)
        return m

    def _update_opps(self, view):
        book = view['book']
        centers = {}
        for side in ('bids', 'asks'):
            for o in book[side]:
                if o.get('mine'):
                    continue
                centers.setdefault(o['name'], []).append(o['price'])
        for name, prices in centers.items():
            c = sum(prices) / len(prices)
            st = self.opp.setdefault(name, {'pos': None, 'flow': 0.0, 'cred': 0.3, 'sign': 0, 'center': None})
            prev = st['center']
            if prev is None:
                st['center'] = c
            elif abs(c - prev) > 3:
                st['cred'] = max(0.1, st['cred'] * 0.5)
                st['center'] = c
            else:
                st['cred'] = min(1.0, st['cred'] + 0.001)
                st['center'] = c
        pmap = {}
        if view.get('standings'):
            for r in view['standings']:
                pmap[r['label']] = r['pos']
        else:
            for p in view['players']:
                if p.get('pos') is not None:
                    pmap[p['name']] = p['pos']
        mine_name = None
        for side in ('bids', 'asks'):
            for o in book[side]:
                if o.get('mine'):
                    mine_name = o['name']
        for name, pos in pmap.items():
            if name == mine_name:
                continue
            st = self.opp.setdefault(name, {'pos': None, 'flow': 0.0, 'cred': 0.3, 'sign': 0, 'center': None})
            if st['pos'] is None:
                st['pos'] = pos
                continue
            d = pos - st['pos']
            st['pos'] = pos
            if d:
                st['flow'] += d
                sgn = 1 if d > 0 else -1
                if st['sign'] and st['sign'] != sgn:
                    st['cred'] = max(0.1, st['cred'] * 0.5)
                elif abs(pos) >= 3:
                    st['cred'] = min(1.0, st['cred'] + 0.05)
                st['sign'] = sgn if abs(pos) >= 1 else 0

    def flow_align(self, st):
        if not st or not st.get('flow'):
            return lambda d: 0.0
        flow = st['flow']
        cred = st['cred']
        mag = min(1.0, abs(flow) / 5)
        sgn = 1 if flow > 0 else -1

        def f(d):
            want = 1 if d == 'bull' else -1
            return cred * mag * want * sgn
        return f


# ------------------------------------------------------------------ strategy

class Strategy:
    kind = 'base'
    cadence = (800, 2500)
    idle_p = 0.05
    idle_range = (5000, 15000)

    def __init__(self, rng):
        self.rng = rng
        self.next_act = 0
        self.day_seen = 0
        self.accused = False
        self.last_phase = None
        self.lam_bid = None
        self.lam_ask = None
        self.pending_fills = []
        self.last_fill_i = -1
        self.last_action = None
        self.last_action_tag = None
        self.last_bluff_pnl = 0.0
        self.new_fills = []
        self.quote_log = []
        self.stats = {'bluff_cost': 0.0, 'bluff_count': 0, 'decoy_cost': 0.0, 'decoy_count': 0}
        self.belief = Belief()

    def prior(self, view, n, card):
        return prior_mean(view['settings'], view['publicCards'], n, card)

    def lam(self, view):
        if self.lam_bid is None:
            s = view['settings']
            n = len(view['players'])
            k = s.get('informedCount')
            rho = (min(int(k), n) / max(1, n - 1)) if (k is not None and n > 1) else 1.0
            self.lam_bid = 1.0 + 3.0 * rho
            self.lam_ask = self.lam_bid
        return max(self.lam_bid, self.lam_ask)

    def on_state(self, view, now=None):
        if view.get('error'):
            return None
        now = view['now'] if now is None else now
        phase = view['phase']
        if phase != self.last_phase:
            if self.last_phase == 'trial':
                self.accused = False
            self.last_phase = phase
        if phase == 'trial':
            return self.trial_action(view, now)
        if phase != 'open':
            return None
        if view['day'] != self.day_seen:
            self.day_seen = view['day']
            self.on_day_open(view, now)
        m = self.refresh_belief(view, now)
        if now < self.next_act:
            return None
        self.next_act = self.schedule(now)
        return self.decide(view, now, m)

    def schedule(self, now):
        d = self.rng.uniform(self.cadence[0], self.cadence[1])
        if self.rng.random() < self.idle_p:
            d += self.rng.uniform(self.idle_range[0], self.idle_range[1])
        return int(now + d)

    def on_day_open(self, view, now):
        pass

    def refresh_belief(self, view, now):
        me = view.get('me') or {}
        n = len(view['players'])
        m = self.belief.on_view(view, now, self.prior(view, n, me.get('card')))
        self.consume_fills(view, now)
        self.update_lam(view, now)
        return m

    def consume_fills(self, view, now):
        fills = (view.get('me') or {}).get('fills') or []
        new = [f for f in fills if f['i'] > self.last_fill_i]
        if fills:
            self.last_fill_i = max(self.last_fill_i, fills[-1]['i'])
        self.new_fills = new
        for f in new:
            self.pending_fills.append((f['i'], f['side'], f['price'], now))
        la = self.last_action
        pnl = 0.0
        if la and la.get('tag') and la.get('side'):
            fee = view['settings'].get('feePerUnit') or 0
            want = 'bought' if la['side'] == 'buy' else 'sold'
            for f in new:
                if f['side'] == want and now - la['now'] < 4000:
                    pnl += ((la['m'] - f['price']) if f['side'] == 'bought' else (f['price'] - la['m'])) * f['size']
                    pnl -= fee * f['size']
        self.last_bluff_pnl = pnl
        self.last_action_tag = la.get('tag') if la else None
        self.last_action = None

    def note_action(self, kind, side, m, now, tag=None):
        self.last_action = {'kind': kind, 'side': side, 'm': m, 'now': now, 'tag': tag}

    def update_lam(self, view, now):
        if self.lam_bid is None:
            return
        tape_i = self.belief.last_trade_i
        keep = []
        for (fi, side, price, t0) in self.pending_fills:
            if tape_i - fi >= 6 or now - t0 >= 20000:
                mid = mid_of(view)
                cost = 0.0
                if mid is not None:
                    if side == 'bought':
                        cost = max(0.0, price - mid)
                    else:
                        cost = max(0.0, mid - price)
                if side == 'bought':
                    self.lam_bid = min(25.0, 0.85 * self.lam_bid + 0.15 * cost)
                else:
                    self.lam_ask = min(25.0, 0.85 * self.lam_ask + 0.15 * cost)
            else:
                keep.append((fi, side, price, t0))
        self.pending_fills = keep

    def forced_action(self, view, m, now):
        f = (view.get('me') or {}).get('forced')
        if not f:
            return None
        side, size = f['side'], f['size']
        bb, ba = book_extremes(view['book'])
        price = ba if side == 'buy' else bb
        if price is None or depth_at(view['book'], side, price, 1) < 1:
            return None
        deadline = view.get('deadline') or 0
        urgent = deadline > 0 and now > deadline - 60000
        if urgent or abs(price - m) <= 3:
            sz = min(size, self.rng.choice([1, 1, 2, 2, 3]))
            return ('market', side, sz)
        return None

    def log_quote(self, view, now, q):
        self.quote_log.append((now, (q[1] + q[2]) / 2,
                               public_mean(view['settings'], view['publicCards'], len(view['players']))))

    def quote_step(self, view, now, m, fee, center=None, lam=None, size_fn=None):
        book = view['book']
        lam = self.lam(view) if lam is None else lam
        spread = fee + lam
        c = m if center is None else center
        mq = my_quote(book)
        need = mq is None
        if mq is not None:
            need = abs(c - (mq[0] + mq[1]) / 2) > spread / 2
            if not need and self.new_fills:
                need = True
        if need and 2 * spread >= 2.0:
            size_fn = size_fn or (lambda: self.rng.choice([1, 1, 2, 2, 3, 4, 5, 6]))
            q = make_quote(c, fee, lam, self.rng, size_fn)
            if q:
                return q
        if mq is not None and abs(m - (mq[0] + mq[1]) / 2) > max(5.0, 2 * spread):
            return ('cancel',)
        return None

    def accusation_ev(self, view, now):
        me = view.get('me') or {}
        s = view['settings']
        material = view.get('materialPoints') or 20
        rate = s.get('indemnityRate')
        rate = 0.5 if rate is None else rate
        fee_w = s.get('falseAccusationFee')
        fee_w = 6 if fee_w is None else fee_w
        cands = me.get('candidates') or []
        if not cands:
            return None
        bb, bd, due_b, due_s = big_mover_stats(s, view['publicCards'], me.get('card'), material, rate)
        best = None
        for name in cands:
            align = self.belief.flow_align(self.belief.opp.get(name))
            for d, base, due in (('bull', bb, due_b), ('bear', bd, due_s)):
                if base <= 0 or due <= 0:
                    continue
                p = max(0.01, min(0.9, base * (1 + 2 * align(d))))
                ev = p * 0.5 * due - (1 - p) * fee_w
                if best is None or ev > best[2]:
                    best = (name, d, ev)
        return best

    def trial_action(self, view, now):
        if self.accused:
            return None
        best = self.accusation_ev(view, now)
        self.accused = True
        if best and best[2] > 0:
            return ('accuse', best[0], best[1])
        return ('accuse', None, None)

    def decide(self, view, now, m):
        return None


class EVStrategy(Strategy):
    kind = 'ev'

    def decide(self, view, now, m):
        fee = view['settings'].get('feePerUnit') or 0
        me = view.get('me') or {}
        a = self.forced_action(view, m, now)
        if a:
            self.note_action('market', a[1], m, now)
            return a
        if me.get('canTake'):
            a = ev_take(view, m, fee, self.rng, 99)
            if a:
                self.note_action('market', a[1], m, now)
                return a
        if me.get('canQuote'):
            a = self.quote_step(view, now, m, fee)
            if a:
                if a[0] == 'quote':
                    self.log_quote(view, now, a)
                return a
        return None


class BluffStrategy(Strategy):
    kind = 'bluff'

    def on_day_open(self, view, now):
        me = view.get('me') or {}
        s = view['settings']
        material = view.get('materialPoints') or 20
        c = card_value(me.get('card'), s.get('cardValues'))
        self.camo_budget = 0.4 * abs(c) if abs(c) >= material else 0.0
        self.camo_spent = 0.0
        self.decoy = None
        self.decoy_cost_day = 0.0
        self.no_react = 0
        self.decoy_stopped = False
        self.last_decoy = 0

    def decide(self, view, now, m):
        fee = view['settings'].get('feePerUnit') or 0
        me = view.get('me') or {}
        s = view['settings']
        cost = max(0.0, -self.last_bluff_pnl)
        self.last_bluff_pnl = 0.0
        if cost > 0:
            self.stats['bluff_cost'] += cost
            if self.last_action_tag == 'camo':
                self.camo_spent += cost
            elif self.last_action_tag == 'decoy':
                self.decoy_cost_day += cost
        a = self.forced_action(view, m, now)
        if a:
            self.note_action('market', a[1], m, now)
            return a
        material = view.get('materialPoints') or 20
        c = card_value(me.get('card'), s.get('cardValues'))
        if me.get('canTake'):
            a = self.taker_decide(view, now, m, fee, me, abs(c) >= material)
            if a:
                return a
        if me.get('canQuote'):
            a = self.quote_step_bluff(view, now, m, fee)
            if a:
                return a
        return None

    def taker_decide(self, view, now, m, fee, me, is_camo):
        if is_camo:
            if self.camo_spent < self.camo_budget and now - self.belief.last_trade_now < 60000:
                if self.rng.random() < 0.35:
                    a = self.cover_trade(view, now, m, fee, me)
                    if a:
                        return a
            a = ev_take(view, m, fee, self.rng, 4)
            if a:
                self.note_action('market', a[1], m, now)
            return a
        if self.decoy:
            a = self.decoy_step(view, now, m, fee)
            if a:
                return a
        else:
            if (self.rng.random() < 0.03 and not self.decoy_stopped
                    and self.decoy_cost_day < 12
                    and now - self.last_decoy > 90000
                    and now - self.belief.last_trade_now > 30000):
                a = self.start_decoy(view, now, m, fee)
                if a:
                    return a
        a = ev_take(view, m, fee, self.rng, 5)
        if a:
            self.note_action('market', a[1], m, now)
        return a

    def cover_trade(self, view, now, m, fee, me):
        d = edge_dir(view, m, fee)
        if d is not None:
            od = 'sell' if d == 'buy' else 'buy'
        else:
            pos = me.get('pos') or 0
            if pos > 0:
                od = 'buy'
            elif pos < 0:
                od = 'sell'
            else:
                od = self.rng.choice(['buy', 'sell'])
        bb, ba = book_extremes(view['book'])
        price = ba if od == 'buy' else bb
        if price is None or abs(price - m) > 15:
            return None
        sz = self.rng.choice([1, 1, 2])
        if depth_at(view['book'], od, price, sz) < 1:
            return None
        self.stats['bluff_count'] += 1
        self.note_action('market', od, m, now, 'camo')
        return ('market', od, sz)

    def start_decoy(self, view, now, m, fee):
        d = self.rng.choice(['buy', 'sell'])
        bb, ba = book_extremes(view['book'])
        price = ba if d == 'buy' else bb
        if price is None or abs(price - m) > 15:
            return None
        sz = self.rng.choice([1, 2])
        if depth_at(view['book'], d, price, sz) < 1:
            return None
        self.decoy = {'dir': d, 'sent': 0, 'exited': 0, 'phase': 'run',
                      'start_i': self.belief.last_trade_i, 'mid': mid_of(view) or m}
        self.last_decoy = now
        self.stats['decoy_count'] += 1
        self.note_action('market', d, m, now, 'decoy')
        return ('market', d, sz)

    def decoy_step(self, view, now, m, fee):
        dec = self.decoy
        if dec['phase'] == 'run':
            if self.belief.last_trade_i - dec['start_i'] >= 5:
                mid = mid_of(view)
                moved = False
                if mid is not None and dec['mid'] is not None:
                    if dec['dir'] == 'buy' and mid - dec['mid'] >= 2:
                        moved = True
                    elif dec['dir'] == 'sell' and dec['mid'] - mid >= 2:
                        moved = True
                if moved:
                    dec['phase'] = 'exit'
                else:
                    dec['phase'] = 'cut'
                    self.no_react += 1
                    if self.no_react >= 3:
                        self.decoy_stopped = True
            return None
        remaining = dec['sent'] - dec['exited']
        if remaining > 0:
            od = 'sell' if dec['dir'] == 'buy' else 'buy'
            bb, ba = book_extremes(view['book'])
            price = ba if od == 'buy' else bb
            sz = min(2, remaining)
            if price is not None and depth_at(view['book'], od, price, sz) >= 1:
                self.note_action('market', od, m, now, 'decoy')
                return ('market', od, sz)
        self.decoy = None
        return None

    def quote_step_bluff(self, view, now, m, fee):
        book = view['book']
        mq = my_quote(book)
        lam = self.lam(view)
        spread = fee + lam
        requote = mq is None
        if mq is not None:
            requote = abs(m - (mq[0] + mq[1]) / 2) > spread / 2 or bool(self.new_fills)
        if requote:
            r = self.rng.random()
            if r < 0.3:
                if mq:
                    return ('cancel',)
                return None
            if r < 0.6:
                delta = self.rng.uniform(2, 5) * self.rng.choice([-1, 1])
                q = make_quote(m + delta, fee, lam, self.rng, lambda: self.rng.choice([1, 1, 2, 2, 3]))
                if q:
                    self.note_action('quote', None, m, now, 'camo')
                    self.log_quote(view, now, q)
                    return q
            q = make_quote(m, fee, lam, self.rng, lambda: self.rng.choice([1, 1, 2, 2, 3]))
            if q:
                self.note_action('quote', None, m, now, 'camo')
                self.log_quote(view, now, q)
                return q
        if mq is not None and abs(m - (mq[0] + mq[1]) / 2) > max(5.0, 2 * spread):
            return ('cancel',)
        return None


class MixerStrategy(Strategy):
    kind = 'mix'

    def __init__(self, rng):
        super().__init__(rng)
        self.w = None
        self.debt = 0.0
        self.noise_budget_day = 6.0
        self.noise_spent_day = 0.0

    def on_day_open(self, view, now):
        self.w = self.rng.betavariate(2, 2)
        self.debt = 0.0
        self.noise_spent_day = 0.0

    def decide(self, view, now, m):
        fee = view['settings'].get('feePerUnit') or 0
        me = view.get('me') or {}
        cost = max(0.0, -self.last_bluff_pnl)
        self.last_bluff_pnl = 0.0
        if cost > 0:
            self.stats['bluff_cost'] += cost
            self.noise_spent_day += cost
        a = self.forced_action(view, m, now)
        if a:
            self.note_action('market', a[1], m, now)
            return a
        if self.w is None:
            self.w = self.rng.betavariate(2, 2)
        mid = mid_of(view)
        edge = abs(m - mid) if mid is not None else 0.0
        p_ev = max(0.1, min(0.9, self.w + 0.35 * math.tanh(edge / 3)))
        if self.rng.random() < p_ev:
            if me.get('canTake'):
                a = ev_take(view, m, fee, self.rng, 99)
                if a:
                    if a[2] > 8:
                        a = (a[0], a[1], self.rng.choice([3, 4, 5, 6, 8]))
                    self.note_action('market', a[1], m, now)
                    return a
            if me.get('canQuote'):
                a = self.quote_step(view, now, m, fee)
                if a:
                    if a[0] == 'quote':
                        self.log_quote(view, now, a)
                    return a
            return None
        return self.noise_step(view, now, m, fee, me)

    def noise_step(self, view, now, m, fee, me):
        r = self.rng.random()
        bias = -1 if self.debt > 3 else (1 if self.debt < -3 else 0)
        bb, ba = book_extremes(view['book'])
        if r < 0.45 and me.get('canTake'):
            d = edge_dir(view, m, fee)
            direction = d if d else self.rng.choice(['buy', 'sell'])
            if bias and self.rng.random() < 0.7:
                direction = 'sell' if bias < 0 else 'buy'
            price = ba if direction == 'buy' else bb
            if price is None or abs(price - m) > 15:
                return None
            sz = self.rng.choice([1, 1, 2])
            if depth_at(view['book'], direction, price, sz) < 1:
                return None
            self.debt += sz if direction == 'buy' else -sz
            self.note_action('market', direction, m, now, 'noise')
            return ('market', direction, sz)
        if r < 0.75 and me.get('canQuote'):
            center = m + self.rng.uniform(2, 4) * self.rng.choice([-1, 1])
            a = self.quote_step(view, now, m, fee, center=center)
            if a:
                if a[0] == 'quote':
                    self.log_quote(view, now, a)
                self.note_action(a[0], None, m, now, 'noise')
                return a
            return None
        if (me.get('canTake') and self.noise_spent_day < self.noise_budget_day
                and now - self.belief.last_trade_now > 30000
                and self.rng.random() < 0.3):
            direction = self.rng.choice(['buy', 'sell'])
            if bias:
                direction = 'sell' if bias < 0 else 'buy'
            price = ba if direction == 'buy' else bb
            if price is None or abs(price - m) > 15:
                return None
            sz = self.rng.choice([1, 2, 3])
            if depth_at(view['book'], direction, price, sz) < 1:
                return None
            self.debt += sz if direction == 'buy' else -sz
            self.note_action('market', direction, m, now, 'noise')
            return ('market', direction, sz)
        return None


class NoiseStrategy(Strategy):
    kind = 'noise'
    cadence = (1000, 3000)
    idle_p = 0.08
    idle_range = (5000, 20000)

    def __init__(self, rng):
        super().__init__(rng)
        self.skip_ticks = 0
        self.last_neutral = 0

    def prior(self, view, n, card):
        return public_mean(view['settings'], view['publicCards'], n)

    def decide(self, view, now, m):
        fee = view['settings'].get('feePerUnit') or 0
        me = view.get('me') or {}
        s = view['settings']
        a = self.forced_action(view, m, now)
        if a:
            self.note_action('market', a[1], m, now)
            return a
        if me.get('canTake'):
            t = hidden_tilt(s, view['publicCards'], len(view['players']), me.get('card'))
            a = self.taker_step(view, now, m, fee, t, me)
            if a:
                return a
        if me.get('canQuote'):
            a = self.quote_step(view, now, m, fee, size_fn=lambda: self.rng.choice([1, 1, 2, 3]))
            if a:
                if a[0] == 'quote':
                    self.log_quote(view, now, a)
                return a
        return None

    def taker_step(self, view, now, m, fee, t, me):
        bb, ba = book_extremes(view['book'])
        mid = mid_of(view)
        pos = me.get('pos') or 0
        tilt_off = abs(pos) >= 5 or t == 0
        if mid is not None and abs(m - mid) < 1.5 and not tilt_off:
            if now - self.last_neutral > 45000 and self.rng.random() < abs(t) / 40:
                self.last_neutral = now
                direction = 'buy' if t > 0 else 'sell'
                price = ba if t > 0 else bb
                if price is not None and depth_at(view['book'], direction, price, 1) >= 1:
                    self.note_action('market', direction, m, now)
                    return ('market', direction, 1)
            return None
        base_sizes = [1, 1, 1, 2, 2, 3]
        agree_sizes = [1, 1, 2, 2, 3, 3]
        if ba is not None and m - ba - fee > 0.5:
            return self.tilted_trade(view, now, m, t, tilt_off, 'buy', ba, base_sizes, agree_sizes)
        if bb is not None and bb - fee - m > 0.5:
            return self.tilted_trade(view, now, m, t, tilt_off, 'sell', bb, base_sizes, agree_sizes)
        return None

    def tilted_trade(self, view, now, m, t, tilt_off, direction, price, base_sizes, agree_sizes):
        agree = (direction == 'buy') == (t > 0)
        if not tilt_off and not agree:
            if self.skip_ticks > 0:
                self.skip_ticks -= 1
            else:
                self.skip_ticks = 1
            return None
        sz = self.rng.choice(agree_sizes if (not tilt_off and agree) else base_sizes)
        if depth_at(view['book'], direction, price, sz) < 1:
            sz = 1
            if depth_at(view['book'], direction, price, 1) < 1:
                return None
        self.note_action('market', direction, m, now)
        return ('market', direction, sz)

    def trial_action(self, view, now):
        if self.accused:
            return None
        self.accused = True
        if self.rng.random() < 0.6:
            return ('accuse', None, None)
        total_flow = sum(st.get('flow', 0) for st in self.belief.opp.values())
        if abs(total_flow) < 8:
            return ('accuse', None, None)
        d = 'bull' if total_flow > 0 else 'bear'
        me = view.get('me') or {}
        cands = me.get('candidates') or []
        target = None
        best_align = None
        for name in cands:
            st = self.belief.opp.get(name)
            if not st:
                continue
            align = st['flow'] * (1 if d == 'bull' else -1)
            if best_align is None or align > best_align:
                target, best_align = name, align
        if not target:
            return ('accuse', None, None)
        s = view['settings']
        material = view.get('materialPoints') or 20
        rate = s.get('indemnityRate')
        rate = 0.5 if rate is None else rate
        fee_w = s.get('falseAccusationFee')
        fee_w = 6 if fee_w is None else fee_w
        bb, bd, due_b, due_s = big_mover_stats(s, view['publicCards'], me.get('card'), material, rate)
        base = bb if d == 'bull' else bd
        due = due_b if d == 'bull' else due_s
        if base <= 0 or due <= 0:
            return ('accuse', None, None)
        p = max(0.01, min(0.9, base * 1.3))
        ev = p * 0.5 * due - (1 - p) * fee_w
        if ev > 0:
            return ('accuse', target, d)
        return ('accuse', None, None)


def make_strategy(kind, seed=None):
    rng = random.Random(seed)
    if kind == 'ev':
        return EVStrategy(rng)
    if kind == 'bluff':
        return BluffStrategy(rng)
    if kind == 'mix':
        return MixerStrategy(rng)
    if kind == 'noise':
        return NoiseStrategy(rng)
    raise ValueError('unknown bot type: %s' % kind)


# ------------------------------------------------------------------ client

DEFAULT_NAMES = {'ev': 'EV Bot', 'bluff': 'Bluffer', 'mix': 'Mixer', 'noise': 'Noise Bot'}


class Client:
    def __init__(self, url, code, name, bot_type, seed=None, allow_claim=True):
        self.base = url.rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        self.scheme = parsed.scheme or 'http'
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == 'https' else 80)
        self.code = code
        self.name = name
        self.seed = seed
        self.strategy = make_strategy(bot_type, seed)
        self.token = None
        self.pid = None
        self.allow_claim = allow_claim
        self.on_joined = None   # optional callback(pid) fired once the seat is ours
        self.stop = threading.Event()
        self.view = None
        self.view_lock = threading.Lock()
        self.bad_token = False
        self.gone = None

    def post(self, path, body):
        req = urllib.request.Request(
            self.base + '/r/' + self.code + path,
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def join(self):
        try:
            d = self.post('/api/join', {'name': self.name})
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or '{}')
            except ValueError:
                body = {}
            # A duplicate-name join means a seat with our name exists. The server
            # answers 409 'taken' in the lobby but 400 'started' mid-game, and in
            # both cases offers canClaim — so gate on the body, not the status
            # (the web client does the same). Claiming is only safe when the host
            # told us the seat is ours (embedded bots pass allow_claim=False on
            # fresh joins, so a name a human took can never be stolen mid-join).
            if self.allow_claim and body.get('canClaim') \
                    and body.get('code') in ('taken', 'started'):
                d = self.post('/api/claim', {'name': self.name})
            else:
                raise
        self.token = d['token']
        self.pid = d.get('pid')
        if self.on_joined is not None:
            self.on_joined(self.pid)

    def stream(self):
        while not self.stop.is_set() and not self.gone:
            try:
                if not self.token:
                    self.join()
                if self.scheme == 'https':
                    conn = http.client.HTTPSConnection(self.host, self.port, timeout=30)
                else:
                    conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
                qs = urllib.parse.urlencode({'role': 'player', 'token': self.token})
                conn.request('GET', '/r/%s/events?%s' % (self.code, qs))
                resp = conn.getresponse()
                if resp.status != 200:
                    detail = resp.read(200).decode('utf-8', 'replace')
                    if 'no-room' in detail or resp.status == 404:
                        self.gone = 'no-room'
                    break
                self.bad_token = False
                while not self.stop.is_set() and not self.gone:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.decode('utf-8', 'replace').strip()
                    if line.startswith('data: '):
                        try:
                            d = json.loads(line[6:])
                        except ValueError:
                            continue
                        if d.get('error'):
                            err = d['error']
                            if err == 'bad-token':
                                self.bad_token = True
                            else:
                                self.gone = err
                            break
                        with self.view_lock:
                            self.view = d
                conn.close()
            except (OSError, ValueError) as e:
                if self.stop.is_set():
                    break
                print('stream error: %r' % (e,), flush=True)
            if self.stop.is_set() or self.gone:
                break
            if self.bad_token:
                self.bad_token = False
                try:
                    self.join()
                    print('rejoined seat %s' % self.name, flush=True)
                except Exception as e:
                    print('rejoin failed: %r' % (e,), flush=True)
            time.sleep(1.0)

    def send(self, act):
        try:
            if act[0] == 'quote':
                self.post('/api/quote', {'token': self.token, 'bid': act[1], 'ask': act[2],
                                         'bidSize': act[3], 'askSize': act[4]})
            elif act[0] == 'cancel':
                self.post('/api/cancel', {'token': self.token})
            elif act[0] == 'market':
                self.post('/api/market', {'token': self.token, 'side': act[1], 'size': act[2],
                                          'reqId': uuid.uuid4().hex})
            elif act[0] == 'accuse':
                self.post('/api/accuse', {'token': self.token, 'target': act[1], 'dir': act[2]})
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or '{}')
            except ValueError:
                body = {}
            if body.get('error'):
                print('action rejected: %s' % body['error'], flush=True)
        except OSError as e:
            print('send failed: %r' % (e,), flush=True)

    def run(self):
        t = threading.Thread(target=self.stream, daemon=True)
        t.start()
        while not self.stop.is_set() and not self.gone:
            with self.view_lock:
                v = self.view
            if v is not None:
                break
            time.sleep(0.2)
        if self.stop.is_set() or self.gone:
            print('bot leaving: %s' % self.gone, flush=True)
            return
        print('bot %s joined as %s, waiting for the market to open…' % (self.strategy.kind, self.name), flush=True)
        while not self.stop.is_set() and not self.gone:
            time.sleep(0.4 + random.random() * 0.5)
            with self.view_lock:
                v = self.view
            if not v:
                continue
            if v.get('error') in ('kicked', 'reset'):
                self.gone = v['error']
                break
            if v.get('phase') == 'settled':
                st = v.get('settlement') or {}
                rows = st.get('rows') or []
                for i, r in enumerate(rows):
                    if r['name'] == self.name:
                        print('final: V=%s total=%s rank=%d/%d' % (st.get('V'), r['total'], i + 1, len(rows)), flush=True)
                break
            # Wall-clock epoch-ms (same clock as the engine's deadlines). Using the
            # view's frozen broadcast timestamp here would stall the cadence gate when
            # no new state arrives, deadlocking a quiet book at zero trades.
            act = self.strategy.on_state(v, int(time.time() * 1000))
            if act:
                self.send(act)
        if self.gone:
            print('bot leaving: %s' % self.gone, flush=True)
        else:
            print('bot done', flush=True)


def main():
    ap = argparse.ArgumentParser(description='AI opponent for the trading game')
    ap.add_argument('--url', required=True, help='server base URL, e.g. http://192.168.1.5:3000')
    ap.add_argument('--code', required=True, help='5-letter room code')
    ap.add_argument('--type', required=True, choices=['ev', 'bluff', 'mix', 'noise'])
    ap.add_argument('--name', default=None, help='seat name (default: %s per type)' % DEFAULT_NAMES)
    ap.add_argument('--seed', type=int, default=None)
    args = ap.parse_args()
    name = args.name or DEFAULT_NAMES[args.type]
    try:
        Client(args.url, args.code.upper(), name, args.type, args.seed).run()
    except KeyboardInterrupt:
        print('\nbot stopped', flush=True)
    except Exception as e:
        print('bot error: %r' % (e,), flush=True)


if __name__ == '__main__':
    main()
