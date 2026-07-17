#!/usr/bin/env python3
"""Tests for the trading game: engine unit tests + a full game over HTTP.

Run:  python3 tests.py
"""

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


def make_game(names, roles='everyone', started=True, rng=None):
    g = E.create_game()
    g['settings']['roles'] = roles
    g['settings']['quoteSeconds'] = 0
    g['settings']['marketSeconds'] = 0
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


def test_deal():
    g, _ = make_game(['Ana', 'Bob', 'Cy'], rng=random.Random(3))
    ok(len(g['publicCards']) == 3, 'three public cards')
    ok(all(c['suit'] in ('h', 's') for c in g['publicCards']), 'public cards are hearts/spades')
    cards = [p['card'] for p in g['players'].values()]
    ok(all(c is not None for c in cards), 'everyone has a private card')
    ok(all(c['suit'] in ('h', 's') for c in cards), 'hs pool deals only hearts/spades')
    seen = {(c['rank'], c['suit']) for c in cards + g['publicCards']}
    ok(len(seen) == 6, 'no duplicate cards')
    ok(g['phase'] == 'quote' and g['round'] == 1, 'game starts in round 1 quoting')

    # hearts+spades-only caps at 23 players (26 cards - 3 public)
    g2 = E.create_game()
    g2['settings']['roles'] = 'everyone'
    for i in range(23):
        E.add_player(g2, f'P{i}', NOW)
    expect_error(lambda: E.add_player(g2, 'TooMany', NOW), 'full')


def test_deal_full_pool():
    g = E.create_game()
    g['settings'].update(roles='everyone', dealPool='full', quoteSeconds=0, marketSeconds=0)
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
    ok(g['quotes'][ana]['bid'] == 10.5 or g['quotes'][ana]['bid'] == 10.51, 'prices rounded to 2dp')
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 1, 'ask': 11, 'askSize': 1}, NOW)
    ok(g['quotes'][ana]['bid'] == 9, 'resubmission replaces the quote')


def test_all_in_shortens_deadline():
    g, ps = make_game(['Ana', 'Bob'], started=False)
    g['settings']['quoteSeconds'] = 90
    E.start_game(g, NOW, random.Random(1))
    ok(g['deadline'] == NOW + 90_000, 'quote deadline set')
    E.submit_quote(g, ps['Ana']['id'], {'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1}, NOW + 1000)
    ok(g['deadline'] == NOW + 90_000, 'deadline unchanged while quotes are missing')
    E.submit_quote(g, ps['Bob']['id'], {'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1}, NOW + 2000)
    ok(g['deadline'] == NOW + 2000 + 5000, 'all-in shortens the deadline to +5s')


def test_auction_matching():
    g, ps = make_game(['Ana', 'Bob'])
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    E.submit_quote(g, ana, {'bid': 10, 'bidSize': 2, 'ask': 12, 'askSize': 2}, NOW)
    E.submit_quote(g, bob, {'bid': 12, 'bidSize': 3, 'ask': 13, 'askSize': 1}, NOW)
    E.reveal(g, NOW, random.Random(2))
    ok(len(g['trades']) == 1, 'one auction trade')
    t = g['trades'][0]
    ok(t['buyer'] == bob and t['seller'] == ana, 'aggressive bid buys from the ask')
    ok(t['price'] == 12 and t['size'] == 2, 'settles AT THE ASK for min(sizes)')
    ok(ps['Bob']['pos'] == 2 and ps['Bob']['cash'] == -24, 'buyer position/cash')
    ok(ps['Ana']['pos'] == -2 and ps['Ana']['cash'] == 24, 'seller position/cash')
    bids = [(o['pid'], o['price'], o['size']) for o in g['book']['bids']]
    asks = [(o['pid'], o['price'], o['size']) for o in g['book']['asks']]
    ok(bids == [(bob, 12, 1), (ana, 10, 2)], 'leftover bids rest in the book, sorted')
    ok(asks == [(bob, 13, 1)], 'leftover asks rest in the book')
    ok(g['phase'] == 'market', 'reveal opens the market phase')
    ok(g['lastQuotes'] and len(g['lastQuotes']) == 2, 'revealed quotes stored for display')


def test_auction_priority_and_walk():
    # ties: larger size fills first at the same price
    g, ps = make_game(['A', 'B', 'C'])
    E.submit_quote(g, ps['A']['id'], {'bid': 1, 'bidSize': 1, 'ask': 10, 'askSize': 1}, NOW)
    E.submit_quote(g, ps['B']['id'], {'bid': 1.5, 'bidSize': 1, 'ask': 10, 'askSize': 5}, NOW)
    E.submit_quote(g, ps['C']['id'], {'bid': 10, 'bidSize': 3, 'ask': 30, 'askSize': 1}, NOW)
    E.reveal(g, NOW, random.Random(4))
    ok(len(g['trades']) == 1, 'single fill')
    t = g['trades'][0]
    ok(t['seller'] == ps['B']['id'] and t['size'] == 3 and t['price'] == 10,
       'same price: larger ask size fills first')

    # an aggressive bid walks multiple asks at their own prices
    g, ps = make_game(['X', 'A', 'B', 'C'])
    E.submit_quote(g, ps['X']['id'], {'bid': 13, 'bidSize': 5, 'ask': 99, 'askSize': 1}, NOW)
    E.submit_quote(g, ps['A']['id'], {'bid': 1, 'bidSize': 1, 'ask': 11, 'askSize': 2}, NOW)
    E.submit_quote(g, ps['B']['id'], {'bid': 2, 'bidSize': 1, 'ask': 12, 'askSize': 2}, NOW)
    E.submit_quote(g, ps['C']['id'], {'bid': 3, 'bidSize': 1, 'ask': 14, 'askSize': 1}, NOW)
    E.reveal(g, NOW, random.Random(4))
    fills = [(t['seller'], t['price'], t['size']) for t in g['trades']]
    ok(fills == [(ps['A']['id'], 11, 2), (ps['B']['id'], 12, 2)],
       'bid walks the asks, paying each ask price')
    ok(ps['X']['pos'] == 4 and ps['X']['cash'] == -46, 'buyer paid 2*11 + 2*12')
    ok([(o['pid'], o['size']) for o in g['book']['bids']][0] == (ps['X']['id'], 1),
       'unfilled remainder rests')


def test_market_orders():
    g, ps = make_game(['Ana', 'Bob', 'Cy'])
    ana, bob, cy = (ps[n]['id'] for n in ('Ana', 'Bob', 'Cy'))
    E.submit_quote(g, ana, {'bid': 8, 'bidSize': 2, 'ask': 10, 'askSize': 2}, NOW)
    E.submit_quote(g, bob, {'bid': 7, 'bidSize': 1, 'ask': 12, 'askSize': 1}, NOW)
    E.submit_quote(g, cy, {'bid': 5, 'bidSize': 1, 'ask': 11, 'askSize': 3}, NOW)
    E.reveal(g, NOW, random.Random(2))
    ok(len(g['trades']) == 0 and g['phase'] == 'market', 'no crossing quotes, market opens')

    r = E.market_order(g, cy, 'buy', 4, NOW)  # asks: Ana 10x2, Cy 11x3, Bob 12x1 — skips own
    ok(r['filled'] == 3 and r['requested'] == 4, 'buy walks the book, skipping own ask')
    ok([(f['pid'], f['price'], f['size']) for f in r['fills']] == [(ana, 10, 2), (bob, 12, 1)],
       'fills at each ask price, never self-trading')
    ok(ps['Cy']['pos'] == 3 and ps['Cy']['cash'] == -32, 'cash = -(2*10 + 1*12)')
    ok([o['pid'] for o in g['book']['asks']] == [cy], 'only own ask remains')

    r = E.market_order(g, bob, 'sell', 1, NOW)  # bids: Ana 8x2, Bob 7x1, Cy 5x1
    ok(r['fills'][0]['pid'] == ana and r['fills'][0]['price'] == 8, 'sell hits the best bid')
    expect_error(lambda: E.market_order(g, cy, 'buy', 1, NOW), 'no asks')

    # role gating: assigned-mode MMs cannot take
    g2, ps2 = make_game(['M', 'T'], roles='assigned', started=False)
    g2['players'][ps2['M']['id']]['role'] = 'mm'
    g2['players'][ps2['T']['id']]['role'] = 'taker'
    E.start_game(g2, NOW, random.Random(1))
    E.submit_quote(g2, ps2['M']['id'], {'bid': 5, 'bidSize': 1, 'ask': 7, 'askSize': 1}, NOW)
    expect_error(lambda: E.submit_quote(g2, ps2['T']['id'], {'bid': 5, 'bidSize': 1, 'ask': 7, 'askSize': 1}, NOW),
                 'market makers')
    E.reveal(g2, NOW, random.Random(1))
    expect_error(lambda: E.market_order(g2, ps2['M']['id'], 'buy', 1, NOW), 'market makers')
    r = E.market_order(g2, ps2['T']['id'], 'buy', 1, NOW)
    ok(r['filled'] == 1 and r['fills'][0]['price'] == 7, 'taker lifts the MM ask')


def test_round_flow_and_settle():
    g, ps = make_game(['Ana', 'Bob'])
    ana, bob = ps['Ana']['id'], ps['Bob']['id']
    E.submit_quote(g, ana, {'bid': 9, 'bidSize': 3, 'ask': 10, 'askSize': 3}, NOW)
    E.submit_quote(g, bob, {'bid': 10, 'bidSize': 3, 'ask': 20, 'askSize': 1}, NOW)
    E.reveal(g, NOW, random.Random(2))          # Bob buys 3 @ 10 from Ana
    E.end_market(g, NOW)
    ok(g['phase'] == 'between' and not g['book']['bids'] and not g['book']['asks'],
       'round close cancels the book')
    E.next_round(g, NOW)
    ok(g['phase'] == 'quote' and g['round'] == 2 and g['quotes'] == {}, 'round 2 starts clean')
    E.reveal(g, NOW, random.Random(2))          # nobody quoted -> skips market
    ok(g['phase'] == 'between', 'empty reveal skips the market phase')

    # deterministic scoring: override the dealt cards
    g['publicCards'] = [{'rank': 'K', 'suit': 's'}, {'rank': '5', 'suit': 'h'}, {'rank': 'Q', 'suit': 'h'}]
    g['players'][ana]['card'] = {'rank': 'A', 'suit': 's'}   # -40
    g['players'][bob]['card'] = {'rank': '9', 'suit': 'h'}   # +9
    E.settle(g, NOW)
    st = g['settlement']
    ok(st['V'] == 20 + 5 + 0 - 40 + 9, 'V sums ALL card points')       # == -6
    rows = {r['name']: r for r in st['rows']}
    # Bob: bought 3 @ 10 -> cash -30, pos +3 -> total -30 + 3*(-6) = -48
    ok(rows['Bob']['total'] == -48, "Bob's score = cash + pos*V")
    ok(rows['Ana']['total'] == 48, "Ana's score mirrors (zero-sum)")
    ok(st['rows'][0]['name'] == 'Ana', 'rows sorted by total desc')
    ok(abs(sum(r['total'] for r in st['rows'])) < 1e-9, 'game is zero-sum')
    ok(g['phase'] == 'settled', 'phase is settled')

    E.rematch(g, NOW)
    ok(g['phase'] == 'lobby' and g['round'] == 0, 'rematch returns to lobby')
    ok(all(p['cash'] == 0 and p['pos'] == 0 and p['card'] is None for p in g['players'].values()),
       'rematch wipes positions and cards')
    ok(len(g['players']) == 2, 'rematch keeps players')


def test_kick_and_deadline():
    g, ps = make_game(['Ana', 'Bob', 'Cy'])
    ana, bob, cy = (ps[n]['id'] for n in ('Ana', 'Bob', 'Cy'))
    E.submit_quote(g, ana, {'bid': 1, 'bidSize': 1, 'ask': 9, 'askSize': 2}, NOW)
    E.submit_quote(g, bob, {'bid': 2, 'bidSize': 1, 'ask': 8, 'askSize': 2}, NOW)
    E.submit_quote(g, cy, {'bid': 3, 'bidSize': 1, 'ask': 7, 'askSize': 2}, NOW)
    E.reveal(g, NOW, random.Random(2))
    E.kick_player(g, cy, NOW)
    ok(all(o['pid'] != cy for o in g['book']['bids'] + g['book']['asks']),
       'kick pulls resting orders')
    ok(not g['players'][cy]['active'], 'mid-game kick deactivates, keeps the card in V')

    # timer: quote phase auto-reveals, market phase auto-closes
    g2, ps2 = make_game(['A', 'B'], started=False)
    g2['settings'].update(quoteSeconds=60, marketSeconds=60)
    E.start_game(g2, NOW, random.Random(1))
    ok(E.on_deadline(g2, NOW + 59_000, random.Random(1)) is None, 'not yet')
    ok(E.on_deadline(g2, NOW + 60_001, random.Random(1)) == 'reveal', 'quote deadline reveals')
    ok(g2['phase'] == 'between', 'no quotes -> straight to between')


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
    ok(json.dumps(v), 'views are JSON serializable')
    v2 = E.view_for(g, 'player', 'nonexistent', ex)
    ok(v2.get('error') == 'reset', 'unknown pid -> reset signal')


def test_informed_axis():
    g = E.create_game()
    g['settings'].update(quoteSeconds=0, marketSeconds=0)
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

    E.reveal(g, NOW, random.Random(1))   # nobody quoted -> straight to between
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
    g0['settings'].update(quoteSeconds=0, marketSeconds=0)
    E.set_settings(g0, {'roles': 'everyone', 'informedCount': 0}, NOW)
    for i in range(2):
        E.add_player(g0, f'Z{i}', NOW)
    E.start_game(g0, NOW, random.Random(2))
    ok(all(p['card'] is None for p in g0['players'].values()), 'k=0: nobody holds a card')
    E.reveal(g0, NOW, random.Random(2))
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
    E.reveal(g, NOW, random.Random(3))   # Bob buys 1 @ 12 from Ana
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

    E.end_market(g, NOW)
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
    E.reveal(g, NOW, random.Random(1))
    E.settle(g, NOW)
    ok(g['settlement']['V'] == -80 + 50 + 0 + 0 + 9, 'settlement uses the host card values')

    before = dict(g['settings']['cardValues'])
    expect_error(lambda: E.set_settings(g, {'cardValues': {'5': 1}}, NOW), 'a, k, q')
    expect_error(lambda: E.set_settings(g, {'cardValues': {'A': 999}}, NOW), '-200')
    ok(g['settings']['cardValues'] == before, 'a failed patch leaves settings untouched')
    ok(E.card_points({'rank': 'A', 'suit': 'h'}) == -40, 'module defaults are unchanged')


UNIT_TESTS = [test_cards, test_lobby_and_roles, test_deal, test_deal_full_pool,
              test_quote_validation, test_all_in_shortens_deadline,
              test_auction_matching, test_auction_priority_and_walk,
              test_market_orders, test_round_flow_and_settle,
              test_kick_and_deadline, test_view_privacy,
              test_informed_axis, test_fee_and_anonymous, test_card_values]


# ================================================================ HTTP integration

BASE = None


def req(method, path, body=None, host_header=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'} if data else {}
    if host_header:  # spoof a non-localhost Host to exercise real key auth
        headers['Host'] = host_header
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            raw = resp.read()
            ct = resp.headers.get('Content-Type', '')
            return resp.status, (json.loads(raw) if 'json' in ct else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def spawn_server(state_file, fresh=True):
    env = dict(os.environ, PORT='0', HOST_KEY='testkey', STATE_FILE=state_file,
               PYTHONUNBUFFERED='1')
    args = [sys.executable, 'server.py'] + (['--fresh'] if fresh else [])
    proc = subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(__file__)),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
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


def test_integration():
    global BASE
    tmp = tempfile.mkdtemp()
    state_file = os.path.join(tmp, 'state.json')
    proc, port = spawn_server(state_file)
    BASE = f'http://127.0.0.1:{port}'
    try:
        # static views
        for path in ('/', '/host', '/board'):
            code, body = req('GET', path)
            ok(code == 200 and b'app' in body, f'{path} serves the app shell')
        code, _ = req('GET', '/app.js')
        ok(code == 200, 'app.js served')

        # host auth: real key checks apply to non-local requests…
        code, _ = req('GET', '/api/state?key=wrong', host_header='game.example.com')
        ok(code == 403, 'bad host key rejected for remote requests')
        code, _ = req('GET', '/api/state?key=testkey', host_header='game.example.com')
        ok(code == 200, 'correct key works for remote requests')
        # …while the browser on the server machine itself needs no key
        code, _ = req('GET', '/api/state?key=')
        ok(code == 200, 'localhost browser is host without a key')

        def host(action, **kw):
            return req('POST', '/api/host', {'key': 'testkey', 'action': action, **kw})

        code, _ = host('settings', settings={'roles': 'everyone', 'quoteSeconds': 0,
                                             'marketSeconds': 0, 'informedCount': 3,
                                             'feePerUnit': 0, 'anonymous': False})
        ok(code == 200, 'host can save settings')
        code, st = req('GET', '/api/state?key=testkey')
        ok(st['settings']['informedCount'] == 3, 'new settings round-trip')

        # players join
        toks = {}
        for n in ('Ana', 'Bob', 'Cy'):
            code, d = req('POST', '/api/join', {'name': n})
            ok(code == 200 and d['token'], f'{n} joined')
            toks[n] = d['token']
        code, d = req('POST', '/api/join', {'name': 'ana'})
        ok(code == 409 and d.get('code') == 'taken' and d.get('canClaim') is True,
           'name clash offers a seat claim when disconnected')

        # seat claim rotates the token
        code, d = req('POST', '/api/claim', {'name': 'Ana'})
        ok(code == 200, 'claim works while seat is disconnected')
        old_ana, toks['Ana'] = toks['Ana'], d['token']

        code, _ = host('start')
        ok(code == 200, 'game starts')
        code, d = req('POST', '/api/join', {'name': 'Late'})
        ok(code == 400 and d.get('code') == 'started', 'no joins after the deal')

        code, d = req('POST', '/api/quote',
                      {'token': old_ana, 'bid': 1, 'bidSize': 1, 'ask': 2, 'askSize': 1})
        ok(code == 400, 'stale token rejected after claim')

        # round 1 quotes
        for name, q in {
            'Ana': {'bid': 10, 'bidSize': 2, 'ask': 12, 'askSize': 2},
            'Bob': {'bid': 12, 'bidSize': 1, 'ask': 13, 'askSize': 1},
            'Cy': {'bid': 5, 'bidSize': 1, 'ask': 30, 'askSize': 1},
        }.items():
            code, d = req('POST', '/api/quote', {'token': toks[name], **q})
            ok(code == 200, f'{name} quoted')

        code, st = req('GET', '/api/state?key=testkey')
        ok(st['phase'] == 'quote' and all(p['hasQuote'] for p in st['players']),
           'state shows all quotes in')

        code, _ = host('reveal')
        code, st = req('GET', '/api/state?key=testkey')
        ok(st['phase'] == 'market', 'reveal opens the market')
        ok(len(st['tape']) == 1 and st['tape'][0]['price'] == 12 and st['tape'][0]['size'] == 1
           and st['tape'][0]['buyer'] == 'Bob' and st['tape'][0]['seller'] == 'Ana',
           'auction crossed Bob@12 with Ana ask, at the ask')
        ok([o['name'] for o in st['book']['asks']] == ['Ana', 'Bob', 'Cy'], 'asks rest sorted')

        # market orders (Cy lifts, then hits)
        code, d = req('POST', '/api/market',
                      {'token': toks['Cy'], 'side': 'buy', 'size': 2, 'reqId': 'r1'})
        ok(code == 200 and d['filled'] == 2, 'market buy filled')
        ok([(f['name'], f['price']) for f in d['fills']] == [('Ana', 12), ('Bob', 13)],
           'buy walked Ana then Bob')
        code, d2 = req('POST', '/api/market',
                       {'token': toks['Cy'], 'side': 'buy', 'size': 2, 'reqId': 'r1'})
        ok(code == 200 and d2 == d, 'duplicate reqId returns the cached fill (no double trade)')

        code, d = req('POST', '/api/market',
                      {'token': toks['Cy'], 'side': 'sell', 'size': 3, 'reqId': 'r2'})
        ok(code == 200 and d['filled'] == 2, 'sell partial-fills, skipping own bid')
        ok(all(f['name'] == 'Ana' and f['price'] == 10 for f in d['fills']), 'sold to Ana @ 10')

        code, st = req('GET', '/api/state?key=testkey')
        cy = next(p for p in st['players'] if p['name'] == 'Cy')
        ok(cy['pos'] == 0 and cy['cash'] == -5, "Cy's book: -25 buys +20 sells")

        # close & settle
        host('endMarket')
        code, _ = host('settle')
        code, st = req('GET', '/api/state?key=testkey')
        ok(st['phase'] == 'settled', 'settled')
        stl = st['settlement']
        pub = sum(E.card_points(c) for c in stl['publicCards'])
        priv = sum(r['cardPoints'] for r in stl['rows'])
        ok(stl['V'] == pub + priv, 'V = all public + private points')
        for r in stl['rows']:
            ok(abs(r['total'] - (r['cash'] + r['pos'] * stl['V'])) < 1e-9,
               f"{r['name']}: total = cash + pos*V")
        ok(abs(sum(r['total'] for r in stl['rows'])) < 1e-9, 'zero-sum across players')

        # SSE smoke test
        s = socket.create_connection(('127.0.0.1', port), timeout=5)
        s.sendall(b'GET /events?role=board HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n')
        s.settimeout(5)
        buf = b''
        while b'"phase"' not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        ok(b'text/event-stream' in buf and b'data: ' in buf and b'"settled"' in buf,
           'SSE stream pushes the current state')

        # persistence across restart. Wait for the debounced save to land first:
        # on Windows terminate() kills without running the SIGTERM flush.
        save_deadline = time.time() + 3
        while time.time() < save_deadline:
            try:
                with open(state_file, encoding='utf-8') as f:
                    if json.load(f)['game']['phase'] == 'settled':
                        break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        proc.terminate()
        proc.wait(timeout=5)
        proc2, port2 = spawn_server(state_file, fresh=False)
        BASE = f'http://127.0.0.1:{port2}'
        try:
            code, st2 = req('GET', '/api/state?key=testkey')
            ok(st2['phase'] == 'settled' and st2['settlement']['V'] == stl['V'],
               'game survives a server restart')
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


# ================================================================ runner

def main():
    failures = 0
    for t in UNIT_TESTS + [test_integration]:
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
