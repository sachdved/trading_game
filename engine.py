"""Game rules & state machine for the Glosten-Milgrom classroom trading game.

Pure logic, no I/O: the server owns timers, tokens, sockets, and persistence.
The game state is a plain JSON-serializable dict (camelCase keys) so it can be
snapshotted to disk and shipped to browsers as-is.

Rules implemented (from the "Trading with imperfect information" deck):
  * 3 public cards from hearts+spades, 1 private card per player.
  * Each round market makers submit a two-sided quote (bid/size, ask/size).
  * Reveal = call auction: orders cross where bid >= ask, settle AT THE ASK,
    volume = min(sizes); lowest asks / highest bids fill first, ties favor
    larger size, then random.
  * Leftover quotes rest in the book for the market-order phase; market
    orders fill by time priority, walking the book, never self-trading.
  * At round close all remaining orders are canceled.
  * Settlement: V = sum of points of ALL dealt cards; score = cash + pos * V.
  * Card points (hearts/spades): A=-40, K=+20, Q=J=0, others face value;
    diamonds/clubs = 0. Bids and asks must be strictly positive.
"""

import secrets

RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
SUITS = ['h', 's', 'd', 'c']

MAX_PRICE = 999.99
MAX_SIZE = 99
MAX_NAME = 20
ALL_IN_GRACE_MS = 5000


class GameError(Exception):
    """A user-facing rules/validation error."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


DEFAULT_CARD_VALUES = {'A': -40, 'K': 20, 'Q': 0, 'J': 0}


def card_points(card, values=None):
    """Point value of a card. `values` overrides A/K/Q/J (host-manipulable);
    number cards are always face value, diamonds/clubs always 0."""
    if card['suit'] in ('d', 'c'):
        return 0
    r = card['rank']
    vals = values or DEFAULT_CARD_VALUES
    if r in vals:
        return vals[r]
    return int(r)


def build_deck(suits):
    return [{'rank': r, 'suit': s} for s in suits for r in RANKS]


def new_id():
    return secrets.token_hex(6)


def round2(v):
    return round(v + 0.0, 2)


def create_game():
    return {
        'phase': 'lobby',   # lobby | quote | market | between | settled
        'round': 0,
        'settings': {
            'roles': 'assigned',      # assigned (MM vs taker) | everyone (all do both)
            'dealPool': 'hs',         # private cards from: hs | full deck
            'quoteSeconds': 90,       # 0 = no timer (host advances manually)
            'marketSeconds': 120,
            'informedCount': None,    # players dealt a private card; None = everyone
            'feePerUnit': 0,          # exchange fee charged to BOTH sides, per unit
            'anonymous': False,       # pseudonyms on book/tape/standings until settlement
            'cardValues': dict(DEFAULT_CARD_VALUES),   # host-manipulable A/K/Q/J points
        },
        'players': {},                # pid -> player dict
        'joinOrder': [],
        'publicCards': [],
        'quotes': {},                 # pid -> {bid, bidSize, ask, askSize} (current round)
        'lastQuotes': None,           # revealed quotes of the latest reveal (for display)
        'book': {'bids': [], 'asks': []},   # resting orders {pid, price, size}
        'trades': [],                 # {i, round, phase, buyer, seller, price, size, ts}
        'feesCollected': 0,           # exchange take when feePerUnit > 0 (burned)
        'log': [],
        'deadline': None,             # epoch ms or None
        'settlement': None,
    }


def note(game, msg, now):
    game['log'].append({'ts': now, 'msg': msg})
    if len(game['log']) > 250:
        del game['log'][:len(game['log']) - 250]


# ---------------------------------------------------------------- helpers

def active_players(game):
    return [game['players'][pid] for pid in game['joinOrder']
            if game['players'][pid]['active']]


def pool_size(game):
    # 3 public cards come from hearts+spades either way.
    return (26 - 3) if game['settings']['dealPool'] == 'hs' else (52 - 3)


def capacity(game):
    """Max players that can join. When only k players get cards, the deck no longer
    limits the head count — cap on what one server instance handles comfortably."""
    return pool_size(game) if game['settings'].get('informedCount') is None else 49


def can_quote(p):
    return p['role'] in ('mm', 'both')


def can_take(p):
    return p['role'] in ('taker', 'both')


def quoters(game):
    return [p for p in active_players(game) if can_quote(p)]


def all_quotes_in(game):
    qs = quoters(game)
    return bool(qs) and all(game['quotes'].get(p['id']) for p in qs)


def find_active_by_name(game, name):
    name = str(name or '').strip().lower()
    for p in game['players'].values():
        if p['active'] and p['name'].lower() == name:
            return p
    return None


def _num(v, what='number'):
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise GameError(f'Enter a valid {what}.')
    if n != n or n in (float('inf'), float('-inf')):
        raise GameError(f'Enter a valid {what}.')
    return n


def _size(v, label):
    n = _num(v, 'size')
    if n != int(n) or not (1 <= n <= MAX_SIZE):
        raise GameError(f'{label} must be a whole number from 1 to {MAX_SIZE}.')
    return int(n)


def _maybe_shorten(game, now):
    """Once every market maker has quoted, reveal after a short grace period."""
    if game['phase'] == 'quote' and all_quotes_in(game):
        if game['deadline'] is None:
            note(game, 'All quotes are in — host can reveal', now)
        elif game['deadline'] > now + ALL_IN_GRACE_MS:
            game['deadline'] = now + ALL_IN_GRACE_MS
            note(game, 'All quotes are in — revealing in 5 seconds', now)


# ---------------------------------------------------------------- lobby

def add_player(game, raw_name, now):
    if game['phase'] != 'lobby':
        raise GameError('The game has already started. Watch on the board view, '
                        'or ask the host for a reset.', code='started')
    name = ' '.join(str(raw_name or '').split())[:MAX_NAME]
    if not name:
        raise GameError('Enter a name.')
    if find_active_by_name(game, name):
        raise GameError('That name is already taken.', code='taken')
    if len(active_players(game)) >= capacity(game):
        raise GameError('The game is full for the current deck setting.')
    p = {'id': new_id(), 'name': name, 'role': _default_role(game),
         'card': None, 'cash': 0, 'pos': 0, 'active': True}
    game['players'][p['id']] = p
    game['joinOrder'].append(p['id'])
    note(game, f'{name} joined', now)
    return p


def _default_role(game):
    if game['settings']['roles'] == 'everyone':
        return 'both'
    act = active_players(game)
    mm = sum(1 for p in act if p['role'] == 'mm')
    return 'mm' if mm <= len(act) - mm else 'taker'


def _reassign_roles(game):
    act = active_players(game)
    if game['settings']['roles'] == 'everyone':
        for p in act:
            p['role'] = 'both'
    else:
        for i, p in enumerate(act):
            p['role'] = 'mm' if i % 2 == 0 else 'taker'


def set_settings(game, patch, now):
    s = game['settings']
    patch = patch or {}
    old = dict(s)
    try:
        for key in ('quoteSeconds', 'marketSeconds'):
            if key in patch:
                v = _num(patch[key], 'duration')
                if v != int(v) or (v != 0 and not (10 <= v <= 600)):
                    raise GameError('Timers must be 0 (manual) or 10-600 seconds.')
                s[key] = int(v)
        if 'feePerUnit' in patch:  # live-tunable: applies to trades from now on
            v = round2(_num(patch['feePerUnit'], 'fee'))
            if not (0 <= v <= 10):
                raise GameError('The fee must be between 0 and 10 per unit.')
            s['feePerUnit'] = v
        if 'anonymous' in patch:   # live-tunable: display only, pseudonyms are stable
            s['anonymous'] = bool(patch['anonymous'])
        if 'cardValues' in patch:  # live-tunable on purpose: a mid-game "news shock"
            merged = dict(s.get('cardValues') or DEFAULT_CARD_VALUES)
            for rank, v in (patch['cardValues'] or {}).items():
                if rank not in DEFAULT_CARD_VALUES:
                    raise GameError('Only A, K, Q and J values can be changed.')
                n = _num(v, 'card value')
                if n != int(n) or not (-200 <= n <= 200):
                    raise GameError('Card values must be whole numbers from -200 to 200.')
                merged[rank] = int(n)
            s['cardValues'] = merged
        if 'roles' in patch and patch['roles'] != s['roles']:
            if game['phase'] != 'lobby':
                raise GameError('Roles can only be changed in the lobby.')
            if patch['roles'] not in ('assigned', 'everyone'):
                raise GameError('Unknown roles mode.')
            s['roles'] = patch['roles']
        if 'dealPool' in patch and patch['dealPool'] != s['dealPool']:
            if game['phase'] != 'lobby':
                raise GameError('The deck can only be changed in the lobby.')
            if patch['dealPool'] not in ('hs', 'full'):
                raise GameError('Unknown deck setting.')
            s['dealPool'] = patch['dealPool']
        if 'informedCount' in patch:
            if game['phase'] != 'lobby':
                raise GameError('The informed count can only be changed in the lobby '
                                '(it decides the deal).')
            v = patch['informedCount']
            if v in (None, '', 'all'):
                s['informedCount'] = None
            else:
                n = _num(v, 'count')
                if n != int(n) or not (0 <= n <= 49):
                    raise GameError('Informed count must be a whole number from 0 to 49.')
                s['informedCount'] = int(n)

        # cross-field consistency
        k = s.get('informedCount')
        if k is not None and k > pool_size(game):
            raise GameError(f'Only {pool_size(game)} private cards fit the current deck.')
        if len(active_players(game)) > capacity(game):
            raise GameError('Too many players have already joined for those settings.')
    except GameError:
        s.clear()
        s.update(old)
        raise
    if s['roles'] != old.get('roles'):
        _reassign_roles(game)
    note(game, 'Settings updated', now)


def set_role(game, pid, role, now):
    if game['phase'] != 'lobby':
        raise GameError('Roles can only be changed in the lobby.')
    if game['settings']['roles'] != 'assigned':
        raise GameError('Switch to assigned-roles mode first.')
    if role not in ('mm', 'taker'):
        raise GameError('Unknown role.')
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    p['role'] = role
    note(game, f"{p['name']} is now a {'market maker' if role == 'mm' else 'liquidity taker'}", now)


def kick_player(game, pid, now):
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    if game['phase'] == 'lobby':
        del game['players'][pid]
        game['joinOrder'].remove(pid)
    else:
        # Mid-game: their dealt card stays in V; they just stop trading.
        p['active'] = False
        game['quotes'].pop(pid, None)
        game['book']['bids'] = [o for o in game['book']['bids'] if o['pid'] != pid]
        game['book']['asks'] = [o for o in game['book']['asks'] if o['pid'] != pid]
    note(game, f"{p['name']} was removed by the host", now)
    _maybe_shorten(game, now)


# ---------------------------------------------------------------- game flow

def start_game(game, now, rng):
    if game['phase'] != 'lobby':
        raise GameError('The game has already started.')
    players = active_players(game)
    if len(players) < 2:
        raise GameError('Need at least 2 players.')
    if not any(can_quote(p) for p in players):
        raise GameError('Need at least one market maker.')

    hs = build_deck(['h', 's'])
    rng.shuffle(hs)
    game['publicCards'] = hs[:3]
    if game['settings']['dealPool'] == 'hs':
        pool = hs[3:]
    else:
        pub = game['publicCards']
        pool = [c for c in build_deck(SUITS)
                if not any(c['rank'] == q['rank'] and c['suit'] == q['suit'] for q in pub)]
        rng.shuffle(pool)
    # informed axis: only k randomly-chosen players are dealt a card (weight 0 for
    # the rest — effectively no card). The count is public; identities are secret.
    k = game['settings'].get('informedCount')
    k_eff = len(players) if k is None else min(k, len(players))
    if len(pool) < k_eff:
        raise GameError('Not enough cards in the deck for that many informed players.')
    informed_ids = set(rng.sample([p['id'] for p in players], k_eff))
    for p in players:
        p['informed'] = p['id'] in informed_ids
        p['card'] = pool.pop(0) if p['informed'] else None

    # stable pseudonyms, in case the host turns anonymous trading on
    nums = list(range(1, len(players) + 1))
    rng.shuffle(nums)
    for p, n_ in zip(players, nums):
        p['alias'] = f'Trader {n_}'

    game['round'] = 1
    game['phase'] = 'quote'
    game['quotes'] = {}
    game['lastQuotes'] = None
    game['book'] = {'bids': [], 'asks': []}
    game['trades'] = []
    game['feesCollected'] = 0
    game['settlement'] = None
    qs = game['settings']['quoteSeconds']
    game['deadline'] = now + qs * 1000 if qs > 0 else None
    dealt = ('everyone holds a private card' if k_eff == len(players) else
             f'{k_eff} of {len(players)} players hold a private card (who — secret)')
    note(game, f'Game started with {len(players)} players — {dealt}; round 1 quoting begins', now)


def submit_quote(game, pid, q, now):
    if game['phase'] != 'quote':
        raise GameError('Quotes are only accepted during the quoting phase.')
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    if not can_quote(p):
        raise GameError('Only market makers submit quotes.')
    bid = round2(_num(q.get('bid'), 'bid'))
    ask = round2(_num(q.get('ask'), 'ask'))
    bid_size = _size(q.get('bidSize'), 'Bid size')
    ask_size = _size(q.get('askSize'), 'Ask size')
    if bid <= 0:
        raise GameError('Your bid must be strictly above 0.')
    if bid > MAX_PRICE or ask > MAX_PRICE:
        raise GameError(f'Prices must be at most {MAX_PRICE}.')
    if ask <= bid:
        raise GameError('Your ask must be above your own bid — you may not cross yourself.')
    fresh = pid not in game['quotes']
    game['quotes'][pid] = {'bid': bid, 'bidSize': bid_size, 'ask': ask, 'askSize': ask_size}
    if fresh:
        _maybe_shorten(game, now)
    return {'allIn': all_quotes_in(game)}


def _apply_trade(game, buyer, seller, price, size, phase, now):
    b, s = game['players'][buyer], game['players'][seller]
    fee = game['settings'].get('feePerUnit', 0) or 0
    b['pos'] += size
    b['cash'] = round2(b['cash'] - (price + fee) * size)
    s['pos'] -= size
    s['cash'] = round2(s['cash'] + (price - fee) * size)
    if fee:
        game['feesCollected'] = round2(game.get('feesCollected', 0) + 2 * fee * size)
    game['trades'].append({'i': len(game['trades']), 'round': game['round'],
                           'phase': phase, 'buyer': buyer, 'seller': seller,
                           'price': price, 'size': size, 'ts': now})


def reveal(game, now, rng):
    """End the quote phase: run the call auction, then open market orders."""
    if game['phase'] != 'quote':
        raise GameError('Not in the quoting phase.')
    items = list(game['quotes'].items())
    bids = [{'pid': pid, 'price': q['bid'], 'size': q['bidSize'], 'r': rng.random()}
            for pid, q in items]
    asks = [{'pid': pid, 'price': q['ask'], 'size': q['askSize'], 'r': rng.random()}
            for pid, q in items]
    bids.sort(key=lambda o: (-o['price'], -o['size'], o['r']))
    asks.sort(key=lambda o: (o['price'], -o['size'], o['r']))

    bi = ai = fills = units = 0
    while bi < len(bids) and ai < len(asks):
        b, a = bids[bi], asks[ai]
        if b['price'] < a['price']:
            break
        # bid < ask is enforced per player, so a self-cross cannot reach here.
        if b['pid'] == a['pid']:
            raise GameError('Internal error: self-cross in the auction.')
        size = min(b['size'], a['size'])
        _apply_trade(game, b['pid'], a['pid'], a['price'], size, 'auction', now)
        b['size'] -= size
        a['size'] -= size
        fills += 1
        units += size
        if b['size'] == 0:
            bi += 1
        if a['size'] == 0:
            ai += 1

    game['book']['bids'] = [{'pid': o['pid'], 'price': o['price'], 'size': o['size']}
                            for o in bids[bi:] if o['size'] > 0]
    game['book']['asks'] = [{'pid': o['pid'], 'price': o['price'], 'size': o['size']}
                            for o in asks[ai:] if o['size'] > 0]
    game['lastQuotes'] = [{'pid': pid, **q} for pid, q in items]

    empty_book = not game['book']['bids'] and not game['book']['asks']
    if empty_book:
        game['phase'] = 'between'
        game['deadline'] = None
        why = 'no quotes were submitted' if not items else 'nothing left on the book'
        note(game, f"Round {game['round']} reveal: {fills} auction fill(s), "
                   f"{units} unit(s) — {why}, market phase skipped", now)
    else:
        game['phase'] = 'market'
        ms = game['settings']['marketSeconds']
        game['deadline'] = now + ms * 1000 if ms > 0 else None
        note(game, f"Round {game['round']} reveal: {fills} auction fill(s) for "
                   f"{units} unit(s) — market orders open", now)


def market_order(game, pid, side, size, now):
    if game['phase'] != 'market':
        raise GameError('Market orders are only accepted during the market phase.')
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    if not can_take(p):
        raise GameError('Market makers do not send market orders in this game.')
    if side not in ('buy', 'sell'):
        raise GameError('Side must be buy or sell.')
    sz = _size(size, 'Size')

    key = 'asks' if side == 'buy' else 'bids'
    levels = game['book'][key]
    rem = sz
    fill_list = []
    for o in levels:
        if rem == 0:
            break
        if o['pid'] == pid:
            continue  # never trade with yourself
        q = min(rem, o['size'])
        if side == 'buy':
            _apply_trade(game, pid, o['pid'], o['price'], q, 'market', now)
        else:
            _apply_trade(game, o['pid'], pid, o['price'], q, 'market', now)
        o['size'] -= q
        rem -= q
        fill_list.append({'pid': o['pid'], 'price': o['price'], 'size': q})
    game['book'][key] = [o for o in levels if o['size'] > 0]

    if not fill_list:
        raise GameError('No asks available to buy from right now.' if side == 'buy'
                        else 'No bids available to sell to right now.')
    return {'requested': sz, 'filled': sz - rem, 'fills': fill_list}


def end_market(game, now):
    if game['phase'] != 'market':
        raise GameError('Not in the market phase.')
    game['phase'] = 'between'
    game['book'] = {'bids': [], 'asks': []}
    game['deadline'] = None
    note(game, f"Round {game['round']} closed — remaining orders canceled", now)


def next_round(game, now):
    if game['phase'] != 'between':
        raise GameError('Finish the current phase first.')
    game['round'] += 1
    game['phase'] = 'quote'
    game['quotes'] = {}
    qs = game['settings']['quoteSeconds']
    game['deadline'] = now + qs * 1000 if qs > 0 else None
    note(game, f"Round {game['round']} quoting begins", now)


def settle(game, now):
    if game['phase'] != 'between':
        raise GameError('End the market phase before settling.')
    vals = game['settings'].get('cardValues')
    participants = [game['players'][pid] for pid in game['joinOrder']]
    cards = list(game['publicCards']) + [p['card'] for p in participants if p['card']]
    v = sum(card_points(c, vals) for c in cards)
    rows = [{'pid': p['id'], 'name': p['name'], 'role': p['role'], 'active': p['active'],
             'informed': p['card'] is not None, 'alias': p.get('alias'),
             'card': p['card'],
             'cardPoints': card_points(p['card'], vals) if p['card'] else 0,
             'pos': p['pos'], 'cash': p['cash'],
             'posValue': round2(p['pos'] * v), 'total': round2(p['cash'] + p['pos'] * v)}
            for p in participants]
    rows.sort(key=lambda r: (-r['total'], r['name']))
    inf = [r for r in rows if r['informed']]
    uninf = [r for r in rows if not r['informed']]
    groups = None
    if inf and uninf:
        groups = {'informed': {'n': len(inf),
                               'avgTotal': round2(sum(r['total'] for r in inf) / len(inf))},
                  'uninformed': {'n': len(uninf),
                                 'avgTotal': round2(sum(r['total'] for r in uninf) / len(uninf))}}
    game['settlement'] = {'V': v, 'publicCards': game['publicCards'], 'rows': rows,
                          'groups': groups,
                          'feesCollected': game.get('feesCollected', 0),
                          'anonymous': bool(game['settings'].get('anonymous'))}
    game['phase'] = 'settled'
    game['deadline'] = None
    winner = rows[0]['name'] if rows else '—'
    note(game, f'Settled: V = {v}. Top score: {winner}', now)


def rematch(game, now):
    """Back to the lobby with the same (still-active) players."""
    if game['phase'] != 'settled':
        raise GameError('A rematch is available after settlement.')
    game['joinOrder'] = [pid for pid in game['joinOrder'] if game['players'][pid]['active']]
    game['players'] = {pid: game['players'][pid] for pid in game['joinOrder']}
    for p in game['players'].values():
        p.update(card=None, cash=0, pos=0, informed=None, alias=None)
    game.update(phase='lobby', round=0, publicCards=[], quotes={}, lastQuotes=None,
                book={'bids': [], 'asks': []}, trades=[], feesCollected=0,
                settlement=None, deadline=None)
    note(game, 'Rematch — back to the lobby with the same players', now)


def on_deadline(game, now, rng):
    """Called by the server when the phase timer fires."""
    if game['deadline'] is None or now < game['deadline']:
        return None
    if game['phase'] == 'quote':
        reveal(game, now, rng)
        return 'reveal'
    if game['phase'] == 'market':
        end_market(game, now)
        return 'endMarket'
    game['deadline'] = None
    return 'clear'


# ---------------------------------------------------------------- views

def view_for(game, kind, pid=None, extras=None):
    """Build the JSON state tailored to one audience (never leaks private cards)."""
    ex = extras or {}
    now = ex.get('now', 0)
    conn = ex.get('connections', {})
    started = game['round'] > 0
    # anonymity hides who is behind each order/position, not who is in the room:
    # rosters keep real names; trading surfaces (book/tape/standings) get pseudonyms.
    anon = bool(game['settings'].get('anonymous')) and kind in ('player', 'board') and started

    def disp(i):
        p = game['players'].get(i)
        if not p:
            return '—'
        return (p.get('alias') or p['name']) if anon else p['name']

    in_quote = game['phase'] == 'quote'
    players = []
    for i in game['joinOrder']:
        p = game['players'][i]
        entry = {
            'id': p['id'], 'name': p['name'], 'role': p['role'], 'active': p['active'],
            'connected': conn.get(p['id'], 0) > 0,
            'hasQuote': (bool(game['quotes'].get(p['id'])) if in_quote and can_quote(p) else None),
        }
        if not anon:  # with anonymity on, positions must not be linkable to names
            entry['pos'] = p['pos']
            entry['cash'] = p['cash']
        if kind == 'host' and p.get('alias'):
            entry['alias'] = p['alias']
        players.append(entry)

    standings = None
    if anon:
        def alias_key(p):
            try:
                return int((p.get('alias') or '0').split()[-1])
            except ValueError:
                return 0
        standings = [{'label': p.get('alias') or p['name'], 'role': p['role'],
                      'pos': p['pos'], 'cash': p['cash'], 'active': p['active'],
                      'me': p['id'] == pid}
                     for p in sorted((game['players'][i] for i in game['joinOrder']),
                                     key=alias_key)
                     if p['active'] or p['pos'] or p['cash']]

    def book_side(side):
        return [{'name': disp(o['pid']), 'mine': o['pid'] == pid,
                 'price': o['price'], 'size': o['size']}
                for o in game['book'][side]]

    base = {
        'v': 1, 'now': now, 'kind': kind,
        'phase': game['phase'], 'round': game['round'],
        'settings': game['settings'], 'deadline': game['deadline'],
        'publicCards': game['publicCards'],
        'players': players,
        'book': {'bids': book_side('bids'), 'asks': book_side('asks')},
        'tape': [{'i': t['i'], 'round': t['round'], 'phase': t['phase'],
                  'buyer': disp(t['buyer']), 'seller': disp(t['seller']),
                  'price': t['price'], 'size': t['size']}
                 for t in game['trades'][-30:]],
        'lastQuotes': ([{'name': disp(q['pid']), 'bid': q['bid'], 'bidSize': q['bidSize'],
                         'ask': q['ask'], 'askSize': q['askSize']}
                        for q in game['lastQuotes']]
                       if game['lastQuotes'] and game['phase'] in ('market', 'between') else None),
        'standings': standings,
        'settlement': game['settlement'],
    }

    if kind == 'player':
        p = game['players'].get(pid)
        if not p:
            return {'error': 'reset', 'now': now}
        if not p['active']:
            return {'error': 'kicked', 'now': now}
        base['me'] = {
            'id': p['id'], 'name': p['name'], 'role': p['role'], 'card': p['card'],
            'informed': p.get('informed'),
            'cash': p['cash'], 'pos': p['pos'],
            'canQuote': can_quote(p), 'canTake': can_take(p),
            'quote': game['quotes'].get(pid) if in_quote else None,
            'fills': [{'i': t['i'], 'round': t['round'], 'phase': t['phase'],
                       'side': 'bought' if t['buyer'] == pid else 'sold',
                       'price': t['price'], 'size': t['size'],
                       'counterparty': disp(t['seller'] if t['buyer'] == pid else t['buyer'])}
                      for t in game['trades'] if pid in (t['buyer'], t['seller'])][-20:],
        }
    elif kind == 'host':
        base['log'] = game['log'][-80:]
        base['joinUrl'] = ex.get('joinUrl')
        base['hostUrl'] = ex.get('hostUrl')
        base['capacity'] = capacity(game)
    elif kind == 'board':
        base['joinUrl'] = ex.get('joinUrl')
    return base
