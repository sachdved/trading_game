#!/usr/bin/env python3
"""Tests for the trading game: engine unit tests + a full game over HTTP.

Run:  python3 tests.py
"""

import collections
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

# Windows pipes (CI, redirects) default to cp1252, which can't print ✓/✗.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASSED = 0


def ok(cond, msg):
    global PASSED
    if not cond:
        raise AssertionError(msg)
    PASSED += 1


def expect_error(fn, fragment=''):
    try:
        fn()
    except E.GameError as e:
        assert fragment.lower() in str(e).lower(), f'wrong error: {e}'
        global PASSED
        PASSED += 1
        return e
    raise AssertionError(f'expected a GameError containing {fragment!r}')


NOW = 1_000_000_000_000
RNG3 = random.Random(3)


def make_game(names, roles='everyone', started=True, rng=None, days=1):
    g = E.create_game()
    g['settings']['roles'] = roles
    g['settings']['days'] = days
    g['settings']['daySeconds'] = 0
    players = [E.add_player(g, n, NOW) for n in names]
    if roles == 'everyone':
        for p in players:
            p['role'] = 'both'
    if started:
        E.start_game(g, NOW, rng or random.Random(7))
    return g, {p['name']: p for p in players}


# ================================================================ unit tests

def test_cards():
    pts = lambda r, s: E.card_points({'rank': r, 'suit': s})
    ok(pts('A', 'h') == -40 and pts('A', 's') == -40, 'aces are -40')
    ok(pts('K', 's') == 20, 'kings are +20')
    ok(pts('Q', 'h') == 0 and pts('J', 's') == 0, 'Q/J are 0')
    ok(pts('10', 'h') == 10 and pts('7', 's') == 7 and pts('2', 'h') == 2, 'face value')
    ok(pts('A', 'd') == 0 and pts('K', 'c') == 0, 'diamonds/clubs are 0')
    ok(len(E.build_deck(['h', 's'])) == 26, 'hs deck size')
    ok(len(E.build_deck(E.SUITS)) == 52, 'full deck size')


def test_lobby_and_roles():
    g = E.create_game()
    a = E.add_player(g, '  Ana  Lee ', NOW)
    ok(a['name'] == 'Ana Lee', 'names are trimmed/collapsed')
    b = E.add_player(g, 'Bob', NOW)
    c = E.add_player(g, 'Cy', NOW)
    ok((a['role'], b['role'], c['role']) == ('mm', 'taker', 'mm'), 'roles alternate')
    expect_error(lambda: E.add_player(g, 'ana lee', NOW), 'taken')
    expect_error(lambda: E.add_player(g, '   ', NOW), 'name')
    E.set_settings(g, {'roles': 'everyone'}, NOW)
    ok(all(p['role'] == 'both' for p in g['players'].values()), 'everyone mode -> both')
    E.set_settings(g, {'roles': 'assigned'}, NOW)
    E.set_role(g, a['id'], 'taker', NOW)
    ok(g['players'][a['id']]['role'] == 'taker', 'host can flip a role')
    # lobby kick fully removes the player
    E.kick_player(g, c['id'], NOW)
    ok(c['id'] not in g['players'] and c['id'] not in g['joinOrder'], 'lobby kick deletes')


def test_settings_days():
    g = E.create_game()
    E.set_settings(g, {'days': 3, 'daySeconds': 120}, NOW)
    ok(g['settings']['days'] == 3 and g['settings']['daySeconds'] == 120,
       'days and day clock round-trip')
    expect_error(lambda: E.set_settings(g, {'days': 0}, NOW), '1 to')
    expect_error(lambda: E.set_settings(g, {'days': 2.5}, NOW), 'whole number')
    expect_error(lambda: E.set_settings(g, {'daySeconds': 10}, NOW), '30-7200')

    # mid-game: days can grow ("overtime"), never shrink below the current day
    g2, _ = make_game(['A', 'B'], days=2)
    E.set_settings(g2, {'days': 4}, NOW)
    ok(g2['settings']['days'] == 4, 'host can add days mid-game')
    g2['day'] = 2
    expect_error(lambda: E.set_settings(g2, {'days': 1}, NOW), 'only add')
    E.set_settings(g2, {'days': 2}, NOW)
    ok(g2['settings']['days'] == 2, 'days can shrink down to the current day')


def test_deal():
    g, _ = make_game(['Ana', 'Bob', 'Cy'], rng=random.Random(3))
    ok(len(g['publicCards']) == 3, 'three public cards')
    ok(all(c['suit'] in ('h', 's') for c in g['publicCards']), 'public cards are hearts/spades')
    cards = [p['card'] for p in g['players'].values()]
    ok(all(c is not None for c in cards), 'everyone has a private card')
    ok(all(c['suit'] in ('h', 's') for c in cards), 'hs pool deals only hearts/spades')
    seen = {(c['rank'], c['suit']) for c in cards + g['publicCards']}
    ok(len(seen) == 6, 'no duplicate cards')
    ok(g['phase'] == 'open' and g['day'] == 1, 'the market opens on day 1')

    # hearts+spades-only caps at 23 players (26 cards - 3 public)
    g2 = E.create_game()
    g2['settings']['roles'] = 'everyone'
    for i in range(23):
        E.add_player(g2, f'P{i}', NOW)
    expect_error(lambda: E.add_player(g2, 'TooMany', NOW), 'full')


def test_deal_full_pool():
    g = E.create_game()
    g['settings'].update(roles='everyone', dealPool='full', daySeconds=0)
    for i in range(30):
        p = E.add_player(g, f'P{i}', NOW)
        p['role'] = 'both'
    E.start_game(g, NOW, random.Random(5))
    ok(all(c['suit'] in ('h', 's') for c in g['publicCards']), 'public still hearts/spades')
    dealt = [tuple(p['card'].values()) for p in g['players'].values()]
    ok(len(set(dealt)) == 30, '30 distinct private cards from the full deck')


def test_quote_validation():
    g, ps = make_game(['Ana', 'Bob'])
    ana = ps['Ana']['id']
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 0, 'bidSize': 1, 'ask': 5, 'askSize': 1}, NOW), 'above 0')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': -3, 'bidSize': 1, 'ask': 5, 'askSize': 1}, NOW), 'above 0')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 6, 'bidSize': 1, 'ask': 5, 'askSize': 1}, NOW), 'cross')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 5, 'bidSize': 1, 'ask': 5, 'askSize': 1}, NOW), 'cross')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 1, 'bidSize': 0, 'ask': 5, 'askSize': 1}, NOW), 'whole number')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 1, 'bidSize': 2.5, 'ask': 5, 'askSize': 1}, NOW), 'whole number')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 1, 'bidSize': 100, 'ask': 5, 'askSize': 1}, NOW), 'whole number')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 'zz', 'bidSize': 1, 'ask': 5, 'askSize': 1}, NOW), 'valid')
    E.submit_quote(g, ana, {'bid': '10.505', 'bidSize': 2, 'ask': 12, 'askSize': 2}, NOW)
    ok(g['book']['bids'][0]['price'] in (10.5, 10.51), 'prices rounded to 2dp')
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 1, 'ask': 11, 'askSize': 1}, NOW)
    ok([(o['price'], o['size']) for o in g['book']['bids']] == [(9, 1)]
       and [(o['price'], o['size']) for o in g['book']['asks']] == [(11, 1)],
       'resubmission replaces the previous quote in the book')


def test_continuous_matching():
    g, ps = make_game(['Ana', 'Bob'])
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    E.submit_quote(g, ana, {'bid': 10, 'bidSize': 2, 'ask': 12, 'askSize': 2}, NOW)
    r = E.submit_quote(g, bob, {'bid': 12, 'bidSize': 3, 'ask': 13, 'askSize': 1}, NOW)
    ok(r['traded'] == 2, 'incoming bid crossed the resting ask on arrival')
    ok(len(g['trades']) == 1, 'one trade')
    t = g['trades'][0]
    ok(t['buyer'] == bob and t['seller'] == ana, 'aggressive bid buys from the resting ask')
    ok(t['price'] == 12 and t['size'] == 2, 'trades at the RESTING price for min(sizes)')
    ok(ps['Bob']['pos'] == 2 and ps['Bob']['cash'] == -24, 'buyer position/cash')
    ok(ps['Ana']['pos'] == -2 and ps['Ana']['cash'] == 24, 'seller position/cash')
    bids = [(o['pid'], o['price'], o['size']) for o in g['book']['bids']]
    asks = [(o['pid'], o['price'], o['size']) for o in g['book']['asks']]
    ok(bids == [(bob, 12, 1), (ana, 10, 2)], 'leftover bid rests, book sorted')
    ok(asks == [(bob, 13, 1)], 'the untouched ask rests')

    # price-time priority: equal prices fill in arrival order; aggressors walk levels
    g, ps = make_game(['X', 'A', 'B', 'C'])
    E.submit_quote(g, ps['A']['id'], {'bid': 1, 'bidSize': 1, 'ask': 11, 'askSize': 2}, NOW)
    E.submit_quote(g, ps['B']['id'], {'bid': 2, 'bidSize': 1, 'ask': 11, 'askSize': 2}, NOW)
    E.submit_quote(g, ps['C']['id'], {'bid': 3, 'bidSize': 1, 'ask': 14, 'askSize': 1}, NOW)
    r = E.submit_quote(g, ps['X']['id'], {'bid': 12, 'bidSize': 5, 'ask': 99, 'askSize': 1}, NOW)
    ok(r['traded'] == 4, 'crossed both 11-asks, stopped below 14')
    fills = [(t['seller'], t['price'], t['size']) for t in g['trades']]
    ok(fills == [(ps['A']['id'], 11, 2), (ps['B']['id'], 11, 2)],
       'equal-priced asks fill in arrival order, each at its own price')
    ok(ps['X']['pos'] == 4 and ps['X']['cash'] == -44, 'buyer paid 4 x 11')
    ok([(o['pid'], o['size']) for o in g['book']['bids']][0] == (ps['X']['id'], 1),
       'unfilled remainder rests as a bid')

    # pulling quotes
    r = E.cancel_quotes(g, ps['C']['id'], NOW)
    ok(r['canceled'] == 2, 'cancel pulls both resting orders')
    ok(all(o['pid'] != ps['C']['id'] for o in g['book']['bids'] + g['book']['asks']),
       'canceled orders are gone from the book')
    r = E.cancel_quotes(g, ps['C']['id'], NOW)
    ok(r['canceled'] == 0, 'cancel is idempotent')


def test_market_orders():
    g, ps = make_game(['Ana', 'Bob', 'Cy'])
    ana, bob, cy = (ps[n]['id'] for n in ('Ana', 'Bob', 'Cy'))
    E.submit_quote(g, ana, {'bid': 8, 'bidSize': 2, 'ask': 10, 'askSize': 2}, NOW)
    E.submit_quote(g, bob, {'bid': 7, 'bidSize': 1, 'ask': 12, 'askSize': 1}, NOW)
    E.submit_quote(g, cy, {'bid': 5, 'bidSize': 1, 'ask': 11, 'askSize': 3}, NOW)
    ok(len(g['trades']) == 0, 'no quotes crossed on entry')

    r = E.market_order(g, cy, 'buy', 4, NOW)  # asks: Ana 10x2, Cy 11x3, Bob 12x1 — skips own
    ok(r['filled'] == 3 and r['requested'] == 4, 'buy walks the book, skipping own ask')
    ok([(f['pid'], f['price'], f['size']) for f in r['fills']] == [(ana, 10, 2), (bob, 12, 1)],
       'fills at each resting price, never self-trading')
    ok(ps['Cy']['pos'] == 3 and ps['Cy']['cash'] == -32, 'cash = -(2*10 + 1*12)')
    ok([o['pid'] for o in g['book']['asks']] == [cy], 'only own ask remains')

    r = E.market_order(g, bob, 'sell', 1, NOW)  # bids: Ana 8x2, Bob 7x1, Cy 5x1
    ok(r['fills'][0]['pid'] == ana and r['fills'][0]['price'] == 8, 'sell hits the best bid')
    expect_error(lambda: E.market_order(g, cy, 'buy', 1, NOW), 'no asks')

    # role gating: assigned-mode MMs cannot take, takers cannot quote
    g2, ps2 = make_game(['M', 'T'], roles='assigned', started=False)
    g2['players'][ps2['M']['id']]['role'] = 'mm'
    g2['players'][ps2['T']['id']]['role'] = 'taker'
    E.start_game(g2, NOW, random.Random(1))
    E.submit_quote(g2, ps2['M']['id'], {'bid': 5, 'bidSize': 1, 'ask': 7, 'askSize': 1}, NOW)
    expect_error(lambda: E.submit_quote(g2, ps2['T']['id'], {'bid': 5, 'bidSize': 1, 'ask': 7, 'askSize': 1}, NOW),
                 'market makers')
    expect_error(lambda: E.market_order(g2, ps2['M']['id'], 'buy', 1, NOW), 'market makers')
    r = E.market_order(g2, ps2['T']['id'], 'buy', 1, NOW)
    ok(r['filled'] == 1 and r['fills'][0]['price'] == 7, 'taker lifts the MM ask')


def test_day_flow_and_settle():
    g, ps = make_game(['Ana', 'Bob'], days=2)
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    ok(g['day'] == 1 and g['settings']['days'] == 2, 'starts on day 1')
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 3, 'ask': 10, 'askSize': 3}, NOW)
    E.submit_quote(g, bob, {'bid': 10, 'bidSize': 3, 'ask': 20, 'askSize': 1}, NOW)
    ok(ps['Bob']['pos'] == 3 and ps['Bob']['cash'] == -30, 'Bob bought 3 @ 10 on entry')

    out = E.end_day(g, NOW)
    ok(out == 'between' and g['phase'] == 'between', 'day 1 closes overnight')
    ok(not g['book']['bids'] and not g['book']['asks'], 'overnight wipes the book')
    ok(ps['Bob']['pos'] == 3 and ps['Bob']['cash'] == -30, 'positions and cash carry')
    expect_error(lambda: E.submit_quote(g, ana, {'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1}, NOW),
                 'not open')
    expect_error(lambda: E.market_order(g, bob, 'buy', 1, NOW), 'not open')

    E.next_day(g, NOW, random.Random(1))
    ok(g['phase'] == 'open' and g['day'] == 2, 'day 2 opens with a fresh book')
    expect_error(lambda: E.next_day(g, NOW, random.Random(1)), 'between')

    # deterministic scoring: override the dealt cards
    g['publicCards'] = [{'rank': 'K', 'suit': 's'}, {'rank': '5', 'suit': 'h'}, {'rank': 'Q', 'suit': 'h'}]
    g['players'][ana]['card'] = {'rank': 'A', 'suit': 's'}   # -40
    g['players'][bob]['card'] = {'rank': '9', 'suit': 'h'}   # +9
    out = E.end_day(g, NOW)
    ok(out == 'settled' and g['phase'] == 'settled', 'closing the last day settles')
    st = g['settlement']
    ok(st['V'] == 20 + 5 + 0 - 40 + 9, 'V sums ALL card points')       # == -6
    rows = {r['name']: r for r in st['rows']}
    # Bob: bought 3 @ 10 -> cash -30, pos +3 -> total -30 + 3*(-6) = -48
    ok(rows['Bob']['total'] == -48, "Bob's score = cash + pos*V")
    ok(rows['Ana']['total'] == 48, "Ana's score mirrors (zero-sum)")
    ok(st['rows'][0]['name'] == 'Ana', 'rows sorted by total desc')
    ok(abs(sum(r['total'] for r in st['rows'])) < 1e-9, 'game is zero-sum')

    E.rematch(g, NOW)
    ok(g['phase'] == 'lobby' and g['day'] == 0, 'rematch returns to lobby')
    ok(all(p['cash'] == 0 and p['pos'] == 0 and p['card'] is None for p in g['players'].values()),
       'rematch wipes positions and cards')
    ok(len(g['players']) == 2, 'rematch keeps players')

    # the host can settle early from an overnight break
    g3, _ = make_game(['P', 'Q'], days=3)
    E.end_day(g3, NOW)
    ok(g3['phase'] == 'between', 'day 1 of 3 closed')
    E.settle(g3, NOW)
    ok(g3['phase'] == 'settled', 'settle early from overnight works')


def test_kick_and_deadline():
    g, ps = make_game(['Ana', 'Bob', 'Cy'])
    ana, bob, cy = (ps[n]['id'] for n in ('Ana', 'Bob', 'Cy'))
    E.submit_quote(g, ana, {'bid': 1, 'bidSize': 1, 'ask': 9, 'askSize': 2}, NOW)
    E.submit_quote(g, bob, {'bid': 2, 'bidSize': 1, 'ask': 8, 'askSize': 2}, NOW)
    E.submit_quote(g, cy, {'bid': 3, 'bidSize': 1, 'ask': 7, 'askSize': 2}, NOW)
    E.kick_player(g, cy, NOW)
    ok(all(o['pid'] != cy for o in g['book']['bids'] + g['book']['asks']),
       'kick pulls resting orders')
    ok(not g['players'][cy]['active'], 'mid-game kick deactivates, keeps the card in V')

    # the day clock closes days; the last day's clock settles the game
    g2, _ = make_game(['A', 'B'], started=False, days=2)
    g2['settings']['daySeconds'] = 60
    E.start_game(g2, NOW, random.Random(1))
    ok(g2['deadline'] == NOW + 60_000, 'day clock armed at open')
    ok(E.on_deadline(g2, NOW + 59_000, random.Random(1)) is None, 'not yet')
    ok(E.on_deadline(g2, NOW + 60_001, random.Random(1)) == 'endDay', 'clock closes day 1')
    ok(g2['phase'] == 'between' and g2['deadline'] is None, 'overnight has no clock')
    E.next_day(g2, NOW + 70_000, random.Random(1))
    ok(g2['deadline'] == NOW + 130_000, 'day 2 clock armed')
    ok(E.on_deadline(g2, NOW + 130_001, random.Random(1)) == 'settle', 'last day clock settles')
    ok(g2['phase'] == 'settled', 'game settled by the clock')


def test_chart_series():
    """The OHLC series the price chart draws: candles must agree with the raw
    prints, days must sit side by side, and the payload must stay bounded."""
    g, ps = make_game(['Ana', 'Bob'], days=2)
    g['settings']['daySeconds'] = 300
    ana, bob = ps['Ana']['id'], ps['Bob']['id']

    empty = E.chart_series(g, NOW)
    ok(empty['candles'] == [] and empty['trades'] == 0 and empty['last'] is None,
       'no trades yet -> an empty series, not a crash')

    # four prints inside one 10s bucket (300s day / 40 candles -> 10s candles),
    # then one more a minute later
    for px, size, t in [(10, 2, NOW), (12, 1, NOW + 1000), (9, 3, NOW + 2000),
                        (11, 1, NOW + 3000), (14, 2, NOW + 60_000)]:
        E.submit_quote(g, ana, {'bid': px, 'bidSize': size, 'ask': px + 50,
                                'askSize': 1}, t)
        E.market_order(g, bob, 'sell', size, t)

    c = E.chart_series(g, NOW + 60_000)
    ok(c['bucketMs'] == 10_000, f"day clock sets a 10s candle (got {c['bucketMs']})")
    ok(c['trades'] == 5 and c['last'] == 14, 'series counts every print, last = latest')
    ok(c['lo'] == 9 and c['hi'] == 14, 'lo/hi span the whole history')
    first = c['candles'][0]
    ok((first['o'], first['h'], first['l'], first['c']) == (10, 12, 9, 11),
       f'first candle is OHLC of its four prints (got {first})')
    ok(first['v'] == 7 and first['n'] == 4, 'candle carries volume and print count')
    ok(c['vmax'] == 7, 'vmax is the busiest bucket')
    ok(len(c['candles']) == 7, f'the 60s gap is kept as empty buckets (got {len(c["candles"])})')
    ok([x['n'] for x in c['candles'][1:-1]] == [0] * 5, 'the quiet buckets are empty')
    ok(all(x['day'] == 1 for x in c['candles']), 'day 1 candles are stamped day 1')

    # an open day runs on to `now` so the live chart shows the clock moving, but
    # the trailing lull is capped (25% of the day's span, min 10s) — an idle
    # market must not squash the session's trading into a sliver
    ok(len(E.chart_series(g, NOW + 65_000)['candles']) == 7, 'a short lull just extends')
    later = E.chart_series(g, NOW + 90_000)
    ok(len(later['candles']) == 8,
       f'a long lull is capped at +15s, not +30s (got {len(later["candles"])})')
    ok(len(E.chart_series(g, NOW + 3600_000)['candles']) == 8,
       'an hour of silence does not zoom the chart out')

    # overnight: the gap between days is dropped, not drawn as empty buckets
    E.end_day(g, NOW + 120_000)
    E.next_day(g, NOW + 8 * 3600_000, random.Random(1))
    E.submit_quote(g, ana, {'bid': 20, 'bidSize': 1, 'ask': 70, 'askSize': 1},
                   NOW + 8 * 3600_000)
    E.market_order(g, bob, 'sell', 1, NOW + 8 * 3600_000)
    c2 = E.chart_series(g, NOW + 8 * 3600_000)
    days = [x['day'] for x in c2['candles']]
    ok(days.count(2) == 1 and days[-1] == 2, 'day 2 opens a single new candle')
    ok(len(c2['candles']) < 20, f'the 8h overnight gap is dropped (got {len(c2["candles"])})')
    ok(c2['candles'][-1]['c'] == 20, "day 2's print closes the last candle")

    # a long manual-close session still ships a bounded series
    g2, ps2 = make_game(['Ana', 'Bob'])
    g2['settings']['daySeconds'] = 0
    a2, b2 = ps2['Ana']['id'], ps2['Bob']['id']
    for i in range(60):
        t = NOW + i * 30_000
        E.submit_quote(g2, a2, {'bid': 10 + i % 5, 'bidSize': 1, 'ask': 90, 'askSize': 1}, t)
        E.market_order(g2, b2, 'sell', 1, t)
    c3 = E.chart_series(g2, NOW + 60 * 30_000)
    ok(len(c3['candles']) <= E.CHART_MAX,
       f'the series is capped at {E.CHART_MAX} candles (got {len(c3["candles"])})')
    ok(c3['trades'] == 60, 'the print count still reports the whole session')

    # it rides along in every view, and it never says who traded
    ex = {'now': NOW + 60_000, 'connections': {}}
    for kind, pid in (('board', None), ('host', None), ('player', ana)):
        v = E.view_for(g, kind, pid, ex)
        ok(v['chart']['candles'], f'{kind} view carries the chart series')
        blob = json.dumps(v['chart'])
        ok('Ana' not in blob and 'Bob' not in blob and ana not in blob,
           f'{kind} chart names nobody')
        ok(json.dumps(v), f'{kind} view with a chart is JSON serializable')


def test_view_privacy():
    g, ps = make_game(['Ana', 'Bob'])
    ana = ps['Ana']['id']
    ex = {'now': NOW, 'connections': {}}
    for kind in ('board', 'host'):
        v = E.view_for(g, kind, None, ex)
        ok(all('card' not in p for p in v['players']), f'{kind} view leaks no player cards')
        ok(v['settlement'] is None, f'{kind} view: no settlement before the end')
    v = E.view_for(g, 'player', ana, ex)
    ok(v['me']['card'] == g['players'][ana]['card'], 'player sees own card')
    ok(all('card' not in p for p in v['players']), 'player list has no cards')
    ok(v['day'] == 1 and v['phase'] == 'open', 'view carries phase and day')
    ok(json.dumps(v), 'views are JSON serializable')
    v2 = E.view_for(g, 'player', 'nonexistent', ex)
    ok(v2.get('error') == 'reset', 'unknown pid -> reset signal')


def test_informed_axis():
    g = E.create_game()
    g['settings']['daySeconds'] = 0
    E.set_settings(g, {'roles': 'everyone', 'informedCount': 2}, NOW)
    for i in range(5):
        E.add_player(g, f'P{i}', NOW)
    E.start_game(g, NOW, random.Random(11))
    holders = [p for p in g['players'].values() if p['card']]
    ok(len(holders) == 2, 'exactly k players are dealt a card')
    ok(all(p['informed'] == (p['card'] is not None) for p in g['players'].values()),
       'informed flag == holds a card')
    ok(len({p['alias'] for p in g['players'].values()}) == 5, 'pseudonyms are distinct')

    ex = {'now': NOW, 'connections': {}}
    for kind in ('board', 'host'):
        v = E.view_for(g, kind, None, ex)
        ok(all('informed' not in q and 'card' not in q for q in v['players']),
           f'{kind} view does not reveal who is informed')
    uninf = next(p for p in g['players'].values() if not p['informed'])
    inf = next(p for p in g['players'].values() if p['informed'])
    vu = E.view_for(g, 'player', uninf['id'], ex)
    ok(vu['me']['card'] is None and vu['me']['informed'] is False,
       'uninformed player sees no card')
    vi = E.view_for(g, 'player', inf['id'], ex)
    ok(vi['me']['card'] == inf['card'] and vi['me']['informed'] is True,
       'informed player sees their card')

    E.settle(g, NOW)
    st = g['settlement']
    ok(len(st['rows']) == 5, 'every player appears in the results')
    expected_v = (sum(E.card_points(c) for c in g['publicCards'])
                  + sum(E.card_points(p['card']) for p in holders))
    ok(st['V'] == expected_v, 'V = public cards + only the dealt private cards')
    ok(st['groups']['informed']['n'] == 2 and st['groups']['uninformed']['n'] == 3,
       'settlement reports group sizes')
    ok(all(r['card'] is None and r['cardPoints'] == 0 for r in st['rows'] if not r['informed']),
       'no-card rows carry zero card points')

    # k = 0: a pure common-knowledge market
    g0 = E.create_game()
    g0['settings']['daySeconds'] = 0
    E.set_settings(g0, {'roles': 'everyone', 'informedCount': 0}, NOW)
    for i in range(2):
        E.add_player(g0, f'Z{i}', NOW)
    E.start_game(g0, NOW, random.Random(2))
    ok(all(p['card'] is None for p in g0['players'].values()), 'k=0: nobody holds a card')
    E.settle(g0, NOW)
    ok(g0['settlement']['V'] == sum(E.card_points(c) for c in g0['publicCards']),
       'k=0: V is the public cards alone')

    # with a limited k, the deck no longer caps the head count
    g3 = E.create_game()
    E.set_settings(g3, {'informedCount': 5}, NOW)
    for i in range(30):
        E.add_player(g3, f'N{i}', NOW)
    ok(len(E.active_players(g3)) == 30, '30 players join a hearts/spades game when k=5')
    expect_error(lambda: E.set_settings(g3, {'informedCount': None}, NOW), 'too many')
    expect_error(lambda: E.set_settings(g3, {'informedCount': 24}, NOW), 'fit')

    g4, _ = make_game(['A', 'B'])
    expect_error(lambda: E.set_settings(g4, {'informedCount': 1}, NOW), 'lobby')


def test_fee_and_anonymous():
    g, ps = make_game(['Ana', 'Bob'], started=False)
    E.set_settings(g, {'feePerUnit': 0.5, 'anonymous': True}, NOW)
    E.start_game(g, NOW, random.Random(9))
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 1, 'ask': 12, 'askSize': 1}, NOW)
    E.submit_quote(g, bob, {'bid': 12, 'bidSize': 2, 'ask': 13, 'askSize': 1}, NOW)
    # Bob's bid crossed Ana's ask on entry: 1 @ 12
    ok(ps['Bob']['cash'] == -12.5 and ps['Ana']['cash'] == 11.5,
       'fee charged to both sides of the trade')
    ok(g['feesCollected'] == 1.0, 'exchange collects 2 x fee x units')

    ex = {'now': NOW, 'connections': {}}
    vb = E.view_for(g, 'board', None, ex)
    ok(vb['tape'][0]['buyer'].startswith('Trader '), 'board tape is pseudonymous')
    ok(all(o['name'].startswith('Trader ') for o in vb['book']['bids'] + vb['book']['asks']),
       'board book is pseudonymous')
    ok(bool(vb['standings']) and all(r['label'].startswith('Trader ') for r in vb['standings']),
       'board standings are pseudonymous')
    ok(all('pos' not in q and 'cash' not in q for q in vb['players']),
       'anonymous mode strips positions from the named roster')
    vh = E.view_for(g, 'host', None, ex)
    ok(vh['tape'][0]['buyer'] == 'Bob' and vh['standings'] is None,
       'host view keeps real names')
    vp = E.view_for(g, 'player', ana, ex)
    ok(vp['me']['fills'][0]['counterparty'].startswith('Trader '),
       'fills hide the counterparty too')

    E.settle(g, NOW)
    st = g['settlement']
    ok(st['feesCollected'] == 1.0 and st['anonymous'] is True,
       'settlement reports the exchange take')
    ok(abs(sum(r['total'] for r in st['rows']) + 1.0) < 1e-9, 'totals sum to minus the fees')
    ok(all(r['alias'] and r['name'] for r in st['rows']), 'settlement reveals name and alias')


def test_card_values():
    g, ps = make_game(['Ana', 'Bob'], started=False)
    E.set_settings(g, {'cardValues': {'A': -80, 'K': 50}}, NOW)
    ok(g['settings']['cardValues'] == {'A': -80, 'K': 50, 'Q': 0, 'J': 0},
       'partial card-value patch merges over defaults')
    E.start_game(g, NOW, random.Random(1))
    g['publicCards'] = [{'rank': 'A', 'suit': 's'}, {'rank': 'K', 'suit': 'h'},
                        {'rank': 'Q', 'suit': 'h'}]
    g['players'][ps['Ana']['id']]['card'] = {'rank': 'J', 'suit': 's'}
    g['players'][ps['Bob']['id']]['card'] = {'rank': '9', 'suit': 'h'}
    E.settle(g, NOW)
    ok(g['settlement']['V'] == -80 + 50 + 0 + 0 + 9, 'settlement uses the host card values')

    g2, _ = make_game(['C', 'D'])
    before = dict(g2['settings']['cardValues'])
    expect_error(lambda: E.set_settings(g2, {'cardValues': {'5': 1}}, NOW), 'a, k, q')
    expect_error(lambda: E.set_settings(g2, {'cardValues': {'A': 999}}, NOW), '-200')
    ok(g2['settings']['cardValues'] == before, 'a failed patch leaves settings untouched')
    ok(E.card_points({'rank': 'A', 'suit': 'h'}) == -40, 'module defaults are unchanged')


def test_player_cap():
    g = E.create_game()
    E.set_settings(g, {'maxPlayers': 3, 'roles': 'everyone'}, NOW)
    for i in range(3):
        E.add_player(g, f'P{i}', NOW)
    expect_error(lambda: E.add_player(g, 'P3', NOW), 'full')
    expect_error(lambda: E.set_settings(g, {'maxPlayers': 2}, NOW), 'too many')
    E.set_settings(g, {'maxPlayers': None}, NOW)
    ok(E.capacity(g) == 23, 'blank cap falls back to the deck limit')
    expect_error(lambda: E.set_settings(g, {'maxPlayers': 1}, NOW), '2 to 49')
    expect_error(lambda: E.set_settings(g, {'maxPlayers': 2.5}, NOW), 'whole number')


def test_margin_interest():
    g, ps = make_game(['Ana', 'Bob'], days=2)
    E.set_settings(g, {'marginRate': 10}, NOW)
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 3, 'ask': 10, 'askSize': 3}, NOW)
    E.market_order(g, bob, 'buy', 3, NOW)          # Bob: cash -30
    E.end_day(g, NOW)
    ok(ps['Bob']['cash'] == -33.0, 'overnight interest charged on negative cash')
    ok(ps['Ana']['cash'] == 30, 'positive cash earns nothing')
    ok(g['interestPaid'] == 3.0, 'interest accrues to the exchange take')
    E.settle(g, NOW)                                # from overnight: no second charge
    ok(ps['Bob']['cash'] == -33.0 and g['interestPaid'] == 3.0,
       'settling from overnight does not double-charge')
    st = g['settlement']
    ok(st['interestPaid'] == 3.0, 'settlement reports the interest take')
    ok(abs(sum(r['total'] for r in st['rows']) + 3.0) < 1e-9,
       'totals sum to minus the interest collected')

    # settling straight out of an open day charges that day exactly once
    g2, ps2 = make_game(['C', 'D'])
    E.set_settings(g2, {'marginRate': 20}, NOW)
    E.submit_quote(g2, ps2['C']['id'], {'bid': 5, 'bidSize': 2, 'ask': 6, 'askSize': 2}, NOW)
    E.market_order(g2, ps2['D']['id'], 'buy', 2, NOW)   # D: cash -12
    E.settle(g2, NOW)
    ok(ps2['D']['cash'] == -14.4 and g2['interestPaid'] == 2.4,
       'early settle from an open day charges the margin once')
    expect_error(lambda: E.set_settings(g2, {'marginRate': 25}, NOW), '0 and 20')


def test_event_cards():
    g, ps = make_game(['Ana', 'Bob'])
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    rng = random.Random(1)
    expect_error(lambda: E.draw_event(E.create_game(), NOW, rng), 'open')

    # rig the deck for deterministic draws
    r = E.draw_event(g, NOW, rng, 'ace-crash')
    ok(g['settings']['cardValues']['A'] == -70, 'value shock moved the ace')
    ok(g['events'][-1]['id'] == 'ace-crash' and 'Aces' in r['headline'], 'event logged')

    E.draw_event(g, NOW, rng, 'fee-hike')
    ok(g['settings']['feePerUnit'] == 0.5, 'fee shock raised the fee')
    E.draw_event(g, NOW, rng, 'fee-holiday')
    ok(g['settings']['feePerUnit'] == 0, 'fee holiday zeroed the fee')

    E.draw_event(g, NOW, rng, 'flash-close')
    ok(g['deadline'] == NOW + 60_000, 'flash close set a 60s deadline')
    g['deadline'] = None

    # dividends and levies move cash with positions, zero-sum
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 2, 'ask': 10, 'askSize': 2}, NOW)
    E.market_order(g, bob, 'buy', 2, NOW)   # Bob +2 @ -20 cash; Ana -2 @ +20 cash
    E.draw_event(g, NOW, rng, 'dividend')
    ok(ps['Bob']['cash'] == -14 and ps['Ana']['cash'] == 14, 'dividend pays +3/unit; shorts pay')
    E.draw_event(g, NOW, rng, 'short-audit')
    ok(ps['Ana']['cash'] == 10 and g['feesCollected'] == 4, 'short audit: shorts pay 2/unit')

    # forced orders: private, voluntary fills count, remainder executes at the close
    E.draw_event(g, NOW, rng, 'forced-buy')
    target = next(p for p in g['players'].values() if p.get('forced'))
    other = next(p for p in g['players'].values() if not p.get('forced'))
    ok(target['forced']['side'] == 'buy' and 1 <= target['forced']['size'] <= 3,
       'forced order issued')
    ex = {'now': NOW, 'connections': {}}
    ok('forced' not in json.dumps(E.view_for(g, 'board', None, ex))
       and 'forced' not in json.dumps(E.view_for(g, 'host', None, ex)),
       'board/host views never mention the order')
    ok(E.view_for(g, 'player', other['id'], ex)['me']['forced'] is None,
       'other players see no order')
    ok(E.view_for(g, 'player', target['id'], ex)['me']['forced'] == target['forced'],
       'the target sees their own order')

    size = target['forced']['size']
    E.submit_quote(g, other['id'], {'bid': 1, 'bidSize': 1, 'ask': 8, 'askSize': 5}, NOW)
    E.market_order(g, target['id'], 'buy', 1, NOW)
    ok((target['forced'] is None) if size == 1 else target['forced']['size'] == size - 1,
       'voluntary buys count toward the forced order')
    trades_before = len(g['trades'])
    E.end_day(g, NOW)   # days=1: the close also settles
    ok(all(p['forced'] is None for p in g['players'].values()), 'orders cleared at the close')
    if size > 1:
        ok(len(g['trades']) > trades_before, 'the unfilled remainder executed at the close')
    ok(g['phase'] == 'settled', 'day closed and settled')

    # eventCards on: opening a later day auto-draws
    g3, _ = make_game(['P', 'Q'], days=2)
    E.set_settings(g3, {'eventCards': True}, NOW)
    E.end_day(g3, NOW)
    E.next_day(g3, NOW, random.Random(3))
    ok(len(g3['events']) == 1 and g3['events'][0]['day'] == 2, 'day open auto-drew an event')

    # the news scroller replays a session's worth of headlines, not just the last
    # few — and never the private half of an event
    g4, ps4 = make_game(['Ana', 'Bob'], days=1)
    for i in range(E.NEWS_KEPT + 6):
        E.draw_event(g4, NOW + i * 1000, random.Random(i),
                     'dividend' if i % 2 else 'levy')
    ex4 = {'now': NOW, 'connections': {}}
    news = E.view_for(g4, 'board', None, ex4)['events']
    ok(len(news) == E.NEWS_KEPT, f'the view carries {E.NEWS_KEPT} headlines (got {len(news)})')
    ok(news[-1]['i'] == len(g4['events']) - 1, 'and they are the most recent ones')
    ok(all({'i', 'day', 'headline', 'detail'} == set(n) for n in news),
       'each headline carries only what the scroller shows')
    E.draw_event(g4, NOW, random.Random(4), 'forced-sell')
    target4 = next(p for p in g4['players'].values() if p.get('forced'))
    for kind, pid in (('board', None), ('host', None), ('player', ps4['Ana']['id'])):
        head = E.view_for(g4, kind, pid, ex4)['events'][-1]
        ok(target4['name'] not in json.dumps(head) and str(target4['forced']['size'])
           not in head['headline'],
           f'{kind} news does not say who got the private order')
        ok('private' in head['detail'], f'{kind} news says only that it was private')


def test_event_randomization():
    """Cards are sampled from what can actually land, not dealt off a shuffled
    deck: sessions differ from each other, a card sits out a cooldown, and a
    card that would do nothing is never dealt (its headline would be a lie)."""
    def table():
        g, ps = make_game(['Ana', 'Bob', 'Cy', 'Dee'])
        g['settings']['eventCards'] = True
        return g, ps

    seen, sets, repeats, targets = set(), set(), 0, collections.Counter()
    for seed in range(120):
        g, ps = table()
        rng = random.Random(seed)
        ids = []
        for i in range(12):
            E.draw_event(g, NOW + i * 1000, rng)
            ev = g['events'][-1]
            ids.append(ev['id'])
            if ev['id'] in ('forced-buy', 'forced-sell'):
                targets[next(p['name'] for p in g['players'].values() if p.get('forced'))] += 1
            for p in g['players'].values():
                p['forced'] = None      # so the mandate cards stay drawable
        for j, e in enumerate(ids):
            if e in ids[max(0, j - E.EVENT_COOLDOWN):j]:
                repeats += 1
        seen.update(ids)
        sets.add(frozenset(ids))
    ok(repeats == 0, f'no card repeats inside its {E.EVENT_COOLDOWN}-draw cooldown (got {repeats})')
    all_ids = {c['id'] for c in E.EVENT_CARDS}
    ok(seen == all_ids, f'every card is reachable (missed {all_ids - seen})')
    ok(len(sets) > 100, f'sessions draw different cards, not one permutation ({len(sets)}/120)')
    ok(len(targets) == 4 and min(targets.values()) > sum(targets.values()) / 8,
       f'mandates spread over every eligible trader ({dict(targets)})')

    # a card that would land as a no-op is held back, and cannot be staged either
    g, _ = table()
    ok(not E._event_applicable(g, 'fee-holiday', NOW), 'no fee holiday when trading is free')
    expect_error(lambda: E.draw_event(g, NOW, random.Random(0), 'fee-holiday'), 'not change')
    E.draw_event(g, NOW, random.Random(0), 'fee-hike')
    ok(E._event_applicable(g, 'fee-holiday', NOW), 'once there is a fee, the holiday can land')
    E.draw_event(g, NOW, random.Random(0), 'dark-pool')
    ok(not E._event_applicable(g, 'dark-pool', NOW), 'the dark pool only opens once')
    g['settings']['cardValues'] = {'A': -200, 'K': -200, 'Q': 200, 'J': 200}
    for eid in ('ace-crash', 'royal-swap', 'face-lift'):
        ok(not E._event_applicable(g, eid, NOW), f'{eid} is held back at the clamp')
    ok(E._event_applicable(g, 'king-rally', NOW), 'a king rally still has room')
    g['deadline'] = NOW + 30_000
    ok(not E._event_applicable(g, 'flash-close', NOW),
       'no flash close when the day already ends sooner than that')

    for p in g['players'].values():          # everyone already has an order
        p['forced'] = {'side': 'buy', 'size': 1}
    pool = E._event_pool(g, NOW)
    ok('forced-buy' not in pool and 'forced-sell' not in pool,
       'mandates are not dealt with nobody left to receive one')
    expect_error(lambda: E.draw_event(g, NOW, random.Random(0), 'no-such-card'), 'No such event')


def test_forced_order_privacy():
    """The news says a trader must trade; it never says which. Only the trader
    under the mandate is told, and only in their own payload."""
    g, ps = make_game(['Ana', 'Bob', 'Cy'])
    g['settings']['eventCards'] = True
    E.draw_event(g, NOW, random.Random(3), 'forced-buy')
    target = next(p for p in g['players'].values() if p.get('forced'))
    others = [p for p in g['players'].values() if not p.get('forced')]
    ok(len(others) == 2, 'exactly one trader is under the mandate')

    ev = g['events'][-1]
    ok('one trader' in ev['headline'] and 'private' in ev['detail'],
       'the headline is broad and the detail says the rest is private')
    ok(target['name'] not in ev['headline'] + ev['detail'], 'the event names nobody')

    ex = {'now': NOW, 'connections': {}}

    def mandates(node):
        """every live forced order anywhere in a payload, however nested"""
        if isinstance(node, dict):
            return ([node['forced']] if node.get('forced') else
                    [m for k, v in node.items() if k != 'forced' for m in mandates(v)])
        if isinstance(node, list):
            return [m for v in node for m in mandates(v)]
        return []

    # nothing anywhere in another audience's payload can pick the trader out
    for kind, pid in [('board', None), ('host', None)] + [('player', p['id']) for p in others]:
        v = E.view_for(g, kind, pid, ex)
        ok(mandates(v) == [], f'{kind} payload carries no live mandate anywhere')
        ok(target['name'] not in json.dumps(v['events']), f'{kind} news names nobody')
    for p in others:
        ok(E.view_for(g, 'player', p['id'], ex)['me']['forced'] is None,
           f"{p['name']} is not told they have an order")
    own = E.view_for(g, 'player', target['id'], ex)
    ok(mandates(own) == [target['forced']], 'the mandate appears in exactly one payload')
    ok(own['me']['forced']['side'] == 'buy' and own['me']['forced']['size'] >= 1,
       'and that trader sees their own side and size')

    # the host's own log must not tie a name to the mandate either — names show
    # up in it (people joined), so this has to be checked line by line
    log = E.view_for(g, 'host', None, ex)['log']
    ok(any('must' in e['msg'] for e in log), 'the log does record that a mandate went out')
    ok(not any(target['name'] in e['msg'] and 'must' in e['msg'] for e in log),
       'but no single log line names the trader who got it')

    # and it stays private right up to the close, when the fill prints like any
    # other trade — order flow is public, the instruction never was
    E.submit_quote(g, others[0]['id'], {'bid': 2, 'bidSize': 5, 'ask': 8, 'askSize': 5}, NOW)
    E.end_day(g, NOW)
    ok(all(p['forced'] is None for p in g['players'].values()), 'mandates clear at the close')


def test_event_timer():
    # events on with an interval: one at the day open, then every interval via the clock
    g, ps = make_game(['A', 'B'], started=False, days=1)
    g['settings']['daySeconds'] = 0            # manual day, so only the event clock ticks
    E.set_settings(g, {'eventCards': True, 'eventEverySeconds': 60}, NOW)
    E.start_game(g, NOW, random.Random(5))
    ok(len(g['events']) == 1, 'an event is dealt at the start of the day')
    ok(g['eventDeadline'] == NOW + 60_000, 'the next event is armed one interval out')
    # neutralize the opening card's side effects, then rig harmless draws
    g['deadline'] = None
    # positions are flat, so the draws below change no cash
    ok(E.on_deadline(g, NOW + 59_000, random.Random(0)) is None, 'nothing before the interval')
    ok(len(g['events']) == 1, 'still just the opening event')
    ok(E.on_deadline(g, NOW + 60_001, random.Random(0)) == 'event', 'the interval fires an event')
    ok(len(g['events']) == 2, 'a second event was dealt on the clock')
    ok(g['eventDeadline'] == NOW + 60_001 + 60_000, 'the clock re-armed for the next interval')
    E.end_day(g, NOW + 70_000)                 # days=1 -> settles
    ok(g['eventDeadline'] is None and g['phase'] == 'settled', 'closing clears the event clock')

    # interval 0 = only at the day open, no periodic ticks
    g2, _ = make_game(['C', 'D'], started=False)
    g2['settings']['daySeconds'] = 0
    E.set_settings(g2, {'eventCards': True, 'eventEverySeconds': 0}, NOW)
    E.start_game(g2, NOW, random.Random(6))
    ok(len(g2['events']) == 1 and g2['eventDeadline'] is None,
       'interval 0 draws at open but arms no periodic clock')

    expect_error(lambda: E.set_settings(E.create_game(), {'eventEverySeconds': 5}, NOW),
                 '15-3600')

    # toggling events mid-day arms / clears the clock immediately
    g4, _ = make_game(['E', 'F'])              # started, open, day clock manual
    g4['settings']['daySeconds'] = 0
    E.set_settings(g4, {'eventCards': True, 'eventEverySeconds': 30}, NOW)
    ok(g4['eventDeadline'] == NOW + 30_000, 'enabling events mid-day arms the clock')
    E.set_settings(g4, {'eventCards': False}, NOW)
    ok(g4['eventDeadline'] is None, 'disabling events clears the clock')


def _trial_table(names=('Ana', 'Bob', 'Cy', 'Dee'), days=2, hand=None):
    """A dealt table with investigations on and a hand we choose, so a verdict is
    a known quantity rather than a coin flip."""
    g, ps = make_game(list(names), days=days)
    g['settings'].update(trials=True, trialSeconds=0, indemnityRate=0.5,
                         falseAccusationFee=6)
    hand = hand or {'Ana': ('A', 's'), 'Bob': ('K', 'h'), 'Cy': ('Q', 's'), 'Dee': ('7', 'h')}
    for name, (rank, suit) in hand.items():
        if name in ps:                 # smaller tables just take the first few
            ps[name]['card'] = {'rank': rank, 'suit': suit}
    return g, ps


def test_trial_flow_and_payoffs():
    """A day close opens an investigation; a correct read costs the exposed one
    indemnity however many people saw it; a wrong one pays the accused."""
    g, ps = _trial_table()
    ok([E.card_case(g, ps[n]) for n in ('Ana', 'Bob', 'Cy', 'Dee')]
       == ['bear', 'bull', None, None],
       'only the big movers are accusable — an Ace is a bear, a King a bull')
    ok(E.card_case(g, ps['Dee']) is None,
       'a 7 is worth something positive but is not a big mover')

    ok(E.end_day(g, NOW) == 'trial', 'closing a day opens the investigation')
    ok(g['phase'] == 'trial' and not g['book']['bids'], 'the book was still wiped first')

    E.file_accusation(g, ps['Bob']['id'], 'Ana', 'bear', NOW)
    E.file_accusation(g, ps['Cy']['id'], 'Ana', 'bear', NOW)
    E.file_accusation(g, ps['Dee']['id'], 'Cy', 'bull', NOW)
    E.file_accusation(g, ps['Ana']['id'], None, None, NOW)      # abstain
    ex = {'now': NOW, 'connections': {}}
    ok(E.view_for(g, 'board', None, ex)['trial'] == {'day': 1, 'of': 4, 'filed': 3},
       'the public count is filed-of-total and nothing else')

    ok(E.resolve_trial(g, NOW) == 'between', 'resolving goes overnight, not to settlement')
    # Ana holds the Ace: 40 x 0.5 = 20, split between Bob and Cy
    ok(ps['Ana']['cash'] == -20, f"the exposed Ace paid one indemnity of 20 (got {ps['Ana']['cash']})")
    ok(ps['Bob']['cash'] == 10, 'split between the two who read it')
    ok(ps['Cy']['cash'] == 10 + 6, 'Cy also collected the fee for being wrongly named')
    ok(ps['Dee']['cash'] == -6, 'the wrong accuser paid the fee')
    ok(abs(sum(p['cash'] for p in g['players'].values())) < 1e-9,
       'every indemnity is a transfer: cash still sums to zero')
    ok(all(p['accusation'] is None for p in g['players'].values()),
       'accusations are cleared for the next one')

    v = ps['Bob']['verdict']
    ok(v['correct'] and v['dir'] == 'bear' and v['amount'] == 10, "Bob's verdict is his own")
    ok(ps['Ana']['verdict'] is None, 'an abstainer gets no verdict')
    ok(ps['Dee']['verdict']['correct'] is False and ps['Dee']['verdict']['amount'] == -6,
       'a wrong accuser is told they were wrong')

    rec = g['trials'][-1]
    ok(rec['filed'] == 3 and rec['exposed'] == 1 and rec['moved'] == 26,
       f'the record adds up (got {rec})')

    # a second day, and closing the last one settles through the investigation
    E.next_day(g, NOW, RNG3)
    ok(E.end_day(g, NOW) == 'trial', 'the last day also opens an investigation')
    E.file_accusation(g, ps['Cy']['id'], 'Bob', 'bull', NOW)
    ok(E.resolve_trial(g, NOW) == 'settled', 'resolving the last one settles the game')
    st = g['settlement']
    ok(abs(sum(r['total'] for r in st['rows'])) < 1e-9, 'the game is still zero-sum')
    ok(st['indemnities'] == 26 + 10, 'settlement reports what the investigations moved')
    ok(len(st['trials']) == 2 and len(st['trials'][1]['rows']) == 1,
       'and reveals every accusation at the end')
    ok(ps['Bob']['cash'] == 10 - 10, 'the King paid 20 x 0.5 = 10 for being read')


def test_trial_privacy():
    """The count is public. The accusation is not, the verdict is not, and under
    anonymity a correct read must not hand over a real name."""
    g, ps = _trial_table()
    E.end_day(g, NOW)
    E.file_accusation(g, ps['Bob']['id'], 'Ana', 'bear', NOW)
    E.file_accusation(g, ps['Cy']['id'], 'Dee', 'bull', NOW)
    ex = {'now': NOW, 'connections': {}}

    def accusations(node):
        """every filed accusation or verdict anywhere in a payload"""
        if isinstance(node, dict):
            found = [v for k, v in node.items() if k in ('accusation', 'verdict') and v]
            return found + [m for k, v in node.items()
                            if k not in ('accusation', 'verdict') for m in accusations(v)]
        if isinstance(node, list):
            return [m for v in node for m in accusations(v)]
        return []

    for kind in ('board', 'host'):
        v = E.view_for(g, kind, None, ex)
        ok(accusations(v) == [], f'{kind} payload carries no accusation at all')
        ok(v['trial']['filed'] == 2, f'{kind} still sees how many are in')
        ok('Ana' not in json.dumps(v['trial']), f'{kind} count names nobody')
    for name in ('Ana', 'Dee'):     # the accused, and an innocent bystander
        v = E.view_for(g, 'player', ps[name]['id'], ex)
        ok(accusations(v) == [], f'{name} is not told they have been named')
    own = E.view_for(g, 'player', ps['Bob']['id'], ex)
    ok(accusations(own) == [{'target': 'Ana', 'dir': 'bear'}],
       'Bob sees exactly one accusation: his own')
    ok(sorted(own['me']['candidates']) == ['Ana', 'Cy', 'Dee'],
       'and cannot accuse himself')

    E.resolve_trial(g, NOW)
    for name in ('Ana', 'Dee'):
        ok(E.view_for(g, 'player', ps[name]['id'], ex)['me']['verdict'] is None,
           f'{name} learns nothing from being accused')

    # anonymity: accuse the pseudonym, hear about the pseudonym
    g2, ps2 = _trial_table()
    g2['settings']['anonymous'] = True
    E.end_day(g2, NOW)
    alias = ps2['Ana']['alias']
    bob2 = E.view_for(g2, 'player', ps2['Bob']['id'], ex)
    ok(alias in bob2['me']['candidates'] and 'Ana' not in bob2['me']['candidates'],
       'under anonymity you accuse the alias, not the name')
    expect_error(lambda: E.file_accusation(g2, ps2['Bob']['id'], 'Ana', 'bear', NOW),
                 'No such trader')
    E.file_accusation(g2, ps2['Bob']['id'], alias, 'bear', NOW)
    E.resolve_trial(g2, NOW)
    verdict = E.view_for(g2, 'player', ps2['Bob']['id'], ex)['me']['verdict']
    ok(verdict['correct'] and verdict['target'] == alias,
       'the verdict is right and still pseudonymous')
    ok('Ana' not in json.dumps(verdict), 'a correct read does not hand over the real name')
    ok(E.view_for(g2, 'player', ps2['Bob']['id'], ex)['me']['card'] == ps2['Bob']['card'],
       'and the accuser still only sees their own card')


def test_trial_rules_and_edges():
    """Validation, the minimum table, materiality tracking the host's values, and
    the clock."""
    g, ps = _trial_table()
    expect_error(lambda: E.file_accusation(g, ps['Bob']['id'], 'Ana', 'bear', NOW),
                 'No investigation')
    E.end_day(g, NOW)
    expect_error(lambda: E.file_accusation(g, ps['Bob']['id'], 'Bob', 'bear', NOW),
                 'cannot accuse yourself')
    expect_error(lambda: E.file_accusation(g, ps['Bob']['id'], 'Nobody', 'bear', NOW),
                 'No such trader')
    expect_error(lambda: E.file_accusation(g, ps['Bob']['id'], 'Ana', 'sideways', NOW),
                 'bear or a bull')
    E.file_accusation(g, ps['Bob']['id'], 'Ana', 'bear', NOW)
    E.file_accusation(g, ps['Bob']['id'], 'Cy', 'bull', NOW)     # replaces it
    ok(E.view_for(g, 'player', ps['Bob']['id'], {'now': NOW, 'connections': {}})
       ['me']['accusation'] == {'target': 'Cy', 'dir': 'bull'}, 'filing again replaces')
    expect_error(lambda: E.resolve_trial(E.create_game(), NOW), 'No investigation')

    # two players is not a guessing game
    g2, _ = _trial_table(names=('Ana', 'Bob'), days=2)
    ok(E.end_day(g2, NOW) == 'between', 'under three players there is no investigation')
    ok(g2['phase'] == 'between' and not g2['trials'], 'and nothing is recorded')

    # materiality follows the values the host (or an event card) set
    g3, ps3 = _trial_table()
    E.set_settings(g3, {'cardValues': {'A': -10, 'K': 20, 'Q': 0, 'J': 0}}, NOW)
    ok(E.card_case(g3, ps3['Ana']) is None, 'an Ace worth -10 is no longer a big mover')
    ok(E.card_case(g3, ps3['Bob']) == 'bull', 'the King still is')
    E.end_day(g3, NOW)
    E.file_accusation(g3, ps3['Bob']['id'], 'Ana', 'bear', NOW)
    E.resolve_trial(g3, NOW)
    ok(ps3['Bob']['cash'] == -6 and ps3['Ana']['cash'] == 6,
       'so accusing that Ace is now simply wrong')

    # the clock closes it, and settling early honours what was filed
    g4, ps4 = _trial_table(days=2)
    g4['settings']['trialSeconds'] = 45
    E.end_day(g4, NOW)
    ok(g4['deadline'] == NOW + 45_000, 'the investigation clock is armed')
    ok(E.on_deadline(g4, NOW + 44_000, RNG3) is None, 'nothing before it runs out')
    E.file_accusation(g4, ps4['Bob']['id'], 'Ana', 'bear', NOW)
    ok(E.on_deadline(g4, NOW + 46_000, RNG3) == 'endTrial', 'the clock closes it')
    ok(g4['phase'] == 'between' and ps4['Bob']['cash'] == 20, 'and it was scored')

    g5, ps5 = _trial_table(days=3)
    E.end_day(g5, NOW)
    E.file_accusation(g5, ps5['Cy']['id'], 'Bob', 'bull', NOW)
    E.settle(g5, NOW)
    ok(g5['phase'] == 'settled' and ps5['Cy']['cash'] == 10,
       'settling early still scores the accusations people committed to')

    # off by default: no phase, no state
    g6, ps6 = _trial_table(days=2)
    g6['settings']['trials'] = False
    ok(E.end_day(g6, NOW) == 'between', 'with investigations off a day close goes overnight')
    ok(E.view_for(g6, 'board', None, {'now': NOW, 'connections': {}})['trial'] is None,
       'and no view carries a trial')


UNIT_TESTS = [test_cards, test_lobby_and_roles, test_settings_days, test_deal,
              test_deal_full_pool, test_quote_validation, test_continuous_matching,
              test_market_orders, test_day_flow_and_settle,
              test_kick_and_deadline, test_chart_series, test_view_privacy,
              test_informed_axis, test_fee_and_anonymous, test_card_values,
              test_player_cap, test_margin_interest, test_event_cards,
              test_event_randomization, test_forced_order_privacy, test_event_timer,
              test_trial_flow_and_payoffs, test_trial_privacy, test_trial_rules_and_edges]


# ================================================================ multi-room units
# (import the server module in-process to exercise rooms/reaper/rate limits
# without HTTP — the module has no import-time side effects)

def test_rooms_and_reaper():
    tmp = tempfile.mkdtemp()
    os.environ['STATE_DIR'] = tmp
    import server as SRV
    SRV.STATE_DIR = tmp   # in case the module was imported earlier with another env

    with SRV.LOCK:
        room = SRV.create_room()
        code = room.code
        SRV.save_room(room)
    ok(re.fullmatch(r'[A-Z]{5}', code) and code in SRV.ROOMS, 'room created with a 5-letter code')
    ok(all(ch in SRV.CODE_ALPHABET for ch in code), 'code avoids look-alike letters')
    ok(os.path.exists(SRV.room_file(code)), 'room snapshot written')

    SRV.reap_rooms(SRV.now_ms())
    ok(code in SRV.ROOMS, 'fresh room is not reaped')

    # an empty lobby expires on the short TTL
    room.last_active = SRV.now_ms() - SRV.SETTLED_TTL_MS - 1000
    SRV.reap_rooms(SRV.now_ms())
    ok(code not in SRV.ROOMS, 'idle empty room reaped')
    ok(not os.path.exists(SRV.room_file(code)), 'snapshot deleted on reap')

    # a room with a live connection is never reaped, however old
    with SRV.LOCK:
        r2 = SRV.create_room()
        r2.clients.add(SRV.Client('board'))
        r2.last_active = SRV.now_ms() - SRV.ROOM_TTL_MS - 1000
    SRV.reap_rooms(SRV.now_ms())
    ok(r2.code in SRV.ROOMS, 'connected room survives past its TTL')
    r2.clients.clear()
    SRV.reap_rooms(SRV.now_ms())
    ok(r2.code not in SRV.ROOMS, 'disconnected idle room reaped')

    # live (mid-game) rooms use the LONG TTL, not the settled/empty one
    with SRV.LOCK:
        r3 = SRV.create_room()
        E.add_player(r3.game, 'Zoe', NOW)
        r3.game['phase'] = 'open'
        r3.last_active = SRV.now_ms() - SRV.SETTLED_TTL_MS - 60_000
    SRV.reap_rooms(SRV.now_ms())
    ok(r3.code in SRV.ROOMS, 'mid-game room outlives the short settled TTL')
    r3.last_active = SRV.now_ms() - SRV.ROOM_TTL_MS - 1000
    SRV.reap_rooms(SRV.now_ms())
    ok(r3.code not in SRV.ROOMS, 'mid-game room reaped after the live TTL')

    # old-format snapshots (previous game version) are dropped at boot
    stale = os.path.join(tmp, 'QQQQQ.json')
    with open(stale, 'w', encoding='utf-8') as f:
        json.dump({'code': 'QQQQQ', 'game': {'phase': 'lobby'}, 'hostKey': 'x'}, f)
    SRV.load_rooms()
    ok('QQQQQ' not in SRV.ROOMS and not os.path.exists(stale),
       'stale-version snapshot is discarded at boot')

    # per-IP rate limiter
    SRV.RATE_LIMITS['create'] = (2, 60)
    ok(SRV.allow('9.9.9.9', 'create'), 'first create allowed')
    ok(SRV.allow('9.9.9.9', 'create'), 'second create allowed')
    ok(not SRV.allow('9.9.9.9', 'create'), 'third create blocked')
    ok(SRV.allow('8.8.4.4', 'create'), 'other IPs have their own bucket')
    ok(all(SRV.allow('127.0.0.1', 'create') for _ in range(5)),
       'loopback (operator browser / tunnel) is exempt from rate limits')

    # client_ip: rightmost X-Forwarded-For hop, and only when TRUST_PROXY is on
    class Stub:
        headers = {'X-Forwarded-For': '6.6.6.6, 7.7.7.7'}
        client_address = ('127.0.0.1', 1234)
    old_tp = SRV.TRUST_PROXY
    SRV.TRUST_PROXY = True
    ok(SRV.Handler.client_ip(Stub()) == '7.7.7.7',
       'client_ip uses the rightmost (proxy-appended) XFF hop')
    SRV.TRUST_PROXY = False
    ok(SRV.Handler.client_ip(Stub()) == '127.0.0.1', 'XFF ignored without TRUST_PROXY')
    SRV.TRUST_PROXY = old_tp

    # room cap
    SRV.MAX_ROOMS = len(SRV.ROOMS)

    def make():
        with SRV.LOCK:
            SRV.create_room()
    expect_error(make, 'room limit')
    SRV.MAX_ROOMS = 40


# ================================================================ HTTP integration

BASE = None


def req(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
    if data:
        headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            raw = resp.read()
            ct = resp.headers.get('Content-Type', '')
            return resp.status, (json.loads(raw) if 'json' in ct else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def spawn_server(state_dir, fresh=True, extra_env=None):
    env = dict(os.environ, PORT='0', STATE_DIR=state_dir, PYTHONUNBUFFERED='1',
               JOIN_URL='http://game.test',
               RATE_CREATES_PER_MIN='1000', RATE_JOINS_PER_MIN='1000')
    env.update(extra_env or {})
    args = [sys.executable, 'server.py'] + (['--fresh'] if fresh else [])
    proc = subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(__file__)),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding='utf-8', errors='replace')
    port = None
    deadline = time.time() + 10
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        m = re.search(r'listening on http://127\.0\.0\.1:(\d+)', line)
        if m:
            port = int(m.group(1))
            break
    if not port:
        proc.kill()
        raise AssertionError('server did not start')
    return proc, port


def sse_open(port, path, headers=''):
    """Open an SSE stream raw and keep the socket alive."""
    s = socket.create_connection(('127.0.0.1', port), timeout=6)
    s.sendall(f'GET {path} HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n{headers}\r\n'.encode())
    s.settimeout(6)
    return s


def sse_read_until(s, want, limit=65536):
    buf = b''
    try:
        while want not in buf and len(buf) < limit:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf


def sse_snoop(port, path, want, limit=65536):
    """Open an SSE stream, read until `want` (bytes) appears or it closes."""
    s = sse_open(port, path)
    buf = sse_read_until(s, want, limit)
    s.close()
    return buf


def test_integration():
    global BASE
    state_dir = tempfile.mkdtemp()
    proc, port = spawn_server(state_dir)
    BASE = f'http://127.0.0.1:{port}'
    try:
        # landing page + static assets
        for path in ('/', '/app.js', '/style.css'):
            code, body = req('GET', path)
            ok(code == 200, f'{path} serves')

        # the solo practice table serves as its own page
        code, body = req('GET', '/practice')
        ok(code == 200 and b'Practice Table' in body, '/practice serves the trainer')

        # create two rooms
        code, r1 = req('POST', '/api/rooms', {})
        ok(code == 200 and re.fullmatch(r'[A-Z]{5}', r1['code']) and r1['hostKey'],
           'room 1 created with code + host key')
        ok(r1['joinUrl'] == f"http://game.test/r/{r1['code']}",
           'join URL is built from JOIN_URL')
        code, r2 = req('POST', '/api/rooms', {})
        ok(code == 200 and r2['code'] != r1['code'], 'room 2 gets its own code')
        A, KA = r1['code'], r1['hostKey']
        B, KB = r2['code'], r2['hostKey']
        bogus = next(c for c in ('ZZZZZ', 'YYYYY', 'XXXXX') if c not in (A, B))

        # room views serve the app shell
        for path in (f'/r/{A}', f'/r/{A}/host', f'/r/{A}/board'):
            code, body = req('GET', path)
            ok(code == 200 and b'app' in body, f'{path} serves the app shell')

        # room lookup endpoint (used by the join form)
        code, d = req('GET', f'/api/rooms/{A}')
        ok(code == 200 and d['code'] == A and d['phase'] == 'lobby', 'room lookup works')
        code, d = req('GET', f'/api/rooms/{bogus}')
        ok(code == 404 and d.get('code') == 'no-room', 'unknown room lookup 404s')

        # host auth is strictly per room — no localhost bypass anymore
        code, _ = req('GET', f'/r/{A}/api/state?key=')
        ok(code == 403, 'missing key is rejected even from localhost')
        code, _ = req('GET', f'/r/{A}/api/state?key={KB}')
        ok(code == 403, "room B's host key does not open room A")
        code, _ = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(code == 200, "the room's own key works")

        def host(rc, key, action, **kw):
            return req('POST', f'/r/{rc}/api/host', {'key': key, 'action': action, **kw})

        code, _ = host(B, KA, 'settle')
        ok(code == 403, 'cross-room host actions are rejected')

        code, _ = host(A, KA, 'settings', settings={'roles': 'everyone', 'days': 2,
                                                    'daySeconds': 0, 'informedCount': 3,
                                                    'feePerUnit': 0, 'anonymous': False})
        ok(code == 200, 'host can save settings')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['settings']['informedCount'] == 3 and st['settings']['days'] == 2,
           'new settings round-trip')

        # players join room A
        toks = {}
        for n in ('Ana', 'Bob', 'Cy'):
            code, d = req('POST', f'/r/{A}/api/join', {'name': n})
            ok(code == 200 and d['token'], f'{n} joined room A')
            toks[n] = d['token']

        # rooms are isolated: same name joins room B independently
        code, dB = req('POST', f'/r/{B}/api/join', {'name': 'Ana'})
        ok(code == 200, 'the same name can join a different room')
        code, d = req('POST', f'/r/{B}/api/quote',
                      {'token': toks['Ana'], 'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1})
        ok(code == 400 and d.get('code') == 'badtoken', "room A's token is worthless in room B")
        code, stB = req('GET', f'/r/{B}/api/state?key={KB}')
        ok(len(stB['players']) == 1 and stB['phase'] == 'lobby',
           'room B has only its own player')

        # unknown room APIs 404 with a signal the client understands
        code, d = req('POST', f'/r/{bogus}/api/join', {'name': 'X'})
        ok(code == 404 and d.get('code') == 'no-room', 'joining a dead room 404s')

        code, d = req('POST', f'/r/{A}/api/join', {'name': 'ana'})
        ok(code == 409 and d.get('code') == 'taken' and d.get('canClaim') is True,
           'name clash offers a seat claim when disconnected')

        # seat claim rotates the token
        code, d = req('POST', f'/r/{A}/api/claim', {'name': 'Ana'})
        ok(code == 200, 'claim works while seat is disconnected')
        old_ana, toks['Ana'] = toks['Ana'], d['token']

        code, _ = host(A, KA, 'start')
        ok(code == 200, 'game starts')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['phase'] == 'open' and st['day'] == 1, 'market opens on day 1')
        code, d = req('POST', f'/r/{A}/api/join', {'name': 'Late'})
        ok(code == 400 and d.get('code') == 'started', 'no joins after the deal')

        code, d = req('POST', f'/r/{A}/api/quote',
                      {'token': old_ana, 'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1})
        ok(code == 400, 'stale token rejected after claim')

        # mid-game seat resume: a duplicate-name join offers the claim flow…
        code, d = req('POST', f'/r/{A}/api/join', {'name': 'Bob'})
        ok(code == 400 and d.get('code') == 'started' and d.get('canClaim') is True,
           'mid-game duplicate-name join offers seat resume')
        # …and claiming works even while the seat still LOOKS connected (dead
        # phones keep their SSE stream alive for minutes) — the old stream is
        # evicted with a 'superseded' notice
        s_bob = sse_open(port, f"/r/{A}/events?role=player&token={toks['Bob']}")
        sse_read_until(s_bob, b'"phase"')
        code, d = req('POST', f'/r/{A}/api/claim', {'name': 'Bob'})
        ok(code == 200 and d['token'], 'claim succeeds while the seat looks connected')
        toks['Bob'] = d['token']
        buf = sse_read_until(s_bob, b'superseded')
        s_bob.close()
        ok(b'superseded' in buf, "the old device's stream is evicted with a superseded notice")

        # live quotes: Bob's bid crosses Ana's resting ask on arrival
        code, d = req('POST', f'/r/{A}/api/quote',
                      {'token': toks['Ana'], 'bid': 10, 'bidSize': 2, 'ask': 12, 'askSize': 2})
        ok(code == 200 and d.get('traded') == 0, 'Ana quotes into an empty book')
        code, d = req('POST', f'/r/{A}/api/quote',
                      {'token': toks['Bob'], 'bid': 12, 'bidSize': 1, 'ask': 13, 'askSize': 1})
        ok(code == 200 and d.get('traded') == 1, "Bob's bid crossed instantly")
        code, d = req('POST', f'/r/{A}/api/quote',
                      {'token': toks['Cy'], 'bid': 5, 'bidSize': 1, 'ask': 30, 'askSize': 1})
        ok(code == 200, 'Cy quoted wide')

        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(len(st['tape']) == 1 and st['tape'][0]['price'] == 12 and st['tape'][0]['size'] == 1
           and st['tape'][0]['buyer'] == 'Bob' and st['tape'][0]['seller'] == 'Ana',
           'the cross printed at the resting ask price')
        ok([o['name'] for o in st['book']['asks']] == ['Ana', 'Bob', 'Cy'], 'asks rest sorted')

        # market orders (Cy lifts, then hits)
        code, d = req('POST', f'/r/{A}/api/market',
                      {'token': toks['Cy'], 'side': 'buy', 'size': 2, 'reqId': 'r1'})
        ok(code == 200 and d['filled'] == 2, 'market buy filled')
        ok([(f['name'], f['price']) for f in d['fills']] == [('Ana', 12), ('Bob', 13)],
           'buy walked Ana then Bob')
        code, d2 = req('POST', f'/r/{A}/api/market',
                       {'token': toks['Cy'], 'side': 'buy', 'size': 2, 'reqId': 'r1'})
        ok(code == 200 and d2 == d, 'duplicate reqId returns the cached fill (no double trade)')

        code, d = req('POST', f'/r/{A}/api/market',
                      {'token': toks['Cy'], 'side': 'sell', 'size': 3, 'reqId': 'r2'})
        ok(code == 200 and d['filled'] == 2, 'sell partial-fills, skipping own bid')
        ok(all(f['name'] == 'Ana' and f['price'] == 10 for f in d['fills']), 'sold to Ana @ 10')

        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        cy = next(p for p in st['players'] if p['name'] == 'Cy')
        ok(cy['pos'] == 0 and cy['cash'] == -5, "Cy's book: -25 buys +20 sells")

        # Cy pulls the rest of his quote
        code, d = req('POST', f'/r/{A}/api/cancel', {'token': toks['Cy']})
        ok(code == 200 and d['canceled'] == 2, 'cancel pulls both resting orders')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(not st['book']['bids'] and not st['book']['asks'], 'the book is now empty')

        # day 1 closes overnight; day 2 opens; then the game settles
        code, _ = host(A, KA, 'endDay')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['phase'] == 'between' and st['day'] == 1, 'day 1 closed overnight')
        code, _ = host(A, KA, 'next')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['phase'] == 'open' and st['day'] == 2, 'day 2 open')
        code, _ = host(A, KA, 'event')
        ok(code == 200, 'host drew an event card')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['events'] and st['events'][-1]['headline'], 'the event shows up in state')
        # investigations over HTTP: turn them on, close the last day into one,
        # file an accusation, and have the host close it
        code, _ = host(A, KA, 'settings', settings={'trials': True, 'trialSeconds': 0})
        ok(code == 200, 'investigations can be switched on mid-game')
        code, _ = host(A, KA, 'endDay')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['phase'] == 'trial' and st['trial']['of'] == 3,
           'closing the last day opened an investigation')
        code, d = req('POST', f'/r/{A}/api/accuse',
                      {'token': toks['Cy'], 'target': 'Ana', 'dir': 'bear'})
        ok(code == 200 and d['accusation'] == {'target': 'Ana', 'dir': 'bear'},
           'an accusation files over HTTP')
        code, d = req('POST', f'/r/{A}/api/accuse',
                      {'token': toks['Cy'], 'target': 'Cy', 'dir': 'bear'})
        ok(code == 400, 'accusing yourself is refused')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['trial']['filed'] == 1 and 'Ana' not in json.dumps(st['trial']),
           'the host sees how many are in, never who was named')
        ok('accusation' not in json.dumps(st['players']),
           "and no player row carries anyone's accusation")
        code, _ = host(A, KA, 'resolve')
        code, st = req('GET', f'/r/{A}/api/state?key={KA}')
        ok(st['phase'] == 'settled', 'closing the investigation settled the game')
        stl = st['settlement']
        # the day-2 event draw may have been a value shock — score with the
        # game's CURRENT card values, exactly like the engine does
        vals = st['settings']['cardValues']
        pub = sum(E.card_points(c, vals) for c in stl['publicCards'])
        priv = sum(r['cardPoints'] for r in stl['rows'])
        ok(stl['V'] == pub + priv, 'V = all public + private points')
        for r in stl['rows']:
            ok(abs(r['total'] - (r['cash'] + r['pos'] * stl['V'])) < 1e-9,
               f"{r['name']}: total = cash + pos*V")
        ok(abs(sum(r['total'] for r in stl['rows'])) < 1e-9, 'zero-sum across players')

        # room A's game did not leak into room B
        code, stB = req('GET', f'/r/{B}/api/state?key={KB}')
        ok(stB['phase'] == 'lobby' and not stB['tape'], 'room B is untouched by room A')

        # SSE: room stream pushes state; unknown room says so
        buf = sse_snoop(port, f'/r/{A}/events?role=board', b'"settled"')
        ok(b'text/event-stream' in buf and b'data: ' in buf and b'"settled"' in buf,
           'SSE stream pushes the room state')
        buf = sse_snoop(port, f'/r/{bogus}/events?role=board', b'no-room')
        ok(b'no-room' in buf, 'SSE for a dead room reports no-room')

        # health endpoint for monitoring
        code, h = req('GET', '/healthz')
        ok(code == 200 and h['ok'] and h['rooms'] >= 2, 'healthz reports room count')

        # persistence across restart (per-room snapshot files). Wait for the
        # debounced save to land first: on Windows terminate() kills without
        # running the SIGTERM flush.
        room_a_file = os.path.join(state_dir, A + '.json')
        save_deadline = time.time() + 3
        while time.time() < save_deadline:
            try:
                with open(room_a_file, encoding='utf-8') as f:
                    if json.load(f)['game']['phase'] == 'settled':
                        break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        proc.terminate()
        proc.wait(timeout=5)
        proc2, port2 = spawn_server(state_dir, fresh=False)
        BASE = f'http://127.0.0.1:{port2}'
        try:
            code, st2 = req('GET', f'/r/{A}/api/state?key={KA}')
            ok(st2['phase'] == 'settled' and st2['settlement']['V'] == stl['V'],
               'room A survives a server restart')
            code, st2 = req('GET', f'/r/{B}/api/state?key={KB}')
            ok(code == 200 and st2['phase'] == 'lobby' and len(st2['players']) == 1,
               'room B (and its host key) survive the restart too')
        finally:
            proc2.terminate()
            proc2.wait(timeout=5)
        proc = None
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_rate_limit_http():
    """Limits key on the rightmost X-Forwarded-For hop (the proxy-appended one),
    so spoofed leftmost entries cannot mint fresh buckets; unproxied loopback
    (the tunnel / local-laptop case) is exempt."""
    global BASE
    proc, port = spawn_server(tempfile.mkdtemp(),
                              extra_env={'RATE_CREATES_PER_MIN': '2', 'TRUST_PROXY': '1'})
    BASE = f'http://127.0.0.1:{port}'
    try:
        codes = [req('POST', '/api/rooms', {},
                     headers={'X-Forwarded-For': f'1.1.1.{i}, 9.9.9.9'})[0]
                 for i in range(3)]
        ok(codes == [200, 200, 429],
           f'third create from one real IP is 429 despite spoofed XFF prefixes (got {codes})')
        code, _ = req('POST', '/api/rooms', {},
                      headers={'X-Forwarded-For': '1.1.1.9, 8.8.8.8'})
        ok(code == 200, 'a different real client IP gets its own bucket')
        codes = [req('POST', '/api/rooms', {})[0] for _ in range(3)]
        ok(codes == [200, 200, 200],
           'direct loopback traffic (tunnel case) is never rate limited')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_caps_http():
    global BASE
    proc, port = spawn_server(tempfile.mkdtemp(),
                              extra_env={'MAX_ROOMS': '2', 'MAX_CLIENTS_PER_ROOM': '1'})
    BASE = f'http://127.0.0.1:{port}'
    try:
        code1, r1 = req('POST', '/api/rooms', {})
        code2, _ = req('POST', '/api/rooms', {})
        code3, d = req('POST', '/api/rooms', {})
        ok((code1, code2, code3) == (200, 200, 503) and d.get('code') == 'busy',
           f'room cap returns 503 busy (got {(code1, code2, code3)})')
        A = r1['code']
        s1 = sse_open(port, f'/r/{A}/events?role=board')
        buf = sse_read_until(s1, b'data: ')
        ok(b'data: ' in buf, 'first SSE stream connects')
        buf2 = sse_snoop(port, f'/r/{A}/events?role=board', b'full')
        s1.close()
        ok(b'503' in buf2 and b'full' in buf2, 'per-room SSE cap returns 503')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # per-IP SSE cap: one hostile IP cannot eat the server-wide connection budget
    proc, port = spawn_server(tempfile.mkdtemp(),
                              extra_env={'MAX_CLIENTS_PER_IP': '1', 'TRUST_PROXY': '1'})
    BASE = f'http://127.0.0.1:{port}'
    try:
        code, r1 = req('POST', '/api/rooms', {})
        A = r1['code']
        s1 = sse_open(port, f'/r/{A}/events?role=board',
                      headers='X-Forwarded-For: 5.5.5.5\r\n')
        buf = sse_read_until(s1, b'data: ')
        ok(b'data: ' in buf, 'first stream from an IP connects')
        s2 = sse_open(port, f'/r/{A}/events?role=board',
                      headers='X-Forwarded-For: 5.5.5.5\r\n')
        buf2 = sse_read_until(s2, b'full')
        s2.close()
        ok(b'503' in buf2 and b'full' in buf2, 'second stream from the same IP is capped')
        s3 = sse_open(port, f'/r/{A}/events?role=board',
                      headers='X-Forwarded-For: 6.6.6.6\r\n')
        buf3 = sse_read_until(s3, b'data: ')
        s3.close()
        s1.close()
        ok(b'data: ' in buf3, 'a different IP still connects')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ================================================================ runner

def main():
    failures = 0
    for t in UNIT_TESTS + [test_rooms_and_reaper, test_integration,
                           test_rate_limit_http, test_caps_http]:
        try:
            t()
            print(f'  ✓ {t.__name__}')
        except AssertionError as e:
            failures += 1
            print(f'  ✗ {t.__name__}: {e}')
        except Exception as e:
            failures += 1
            print(f'  ✗ {t.__name__}: {type(e).__name__}: {e}')
    print(f'\n{PASSED} checks passed, {failures} test(s) failed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
