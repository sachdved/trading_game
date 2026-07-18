"""Game rules & state machine for the continuous-time classroom trading game.

Pure logic, no I/O: the server owns timers, tokens, sockets, and persistence.
The game state is a plain JSON-serializable dict (camelCase keys) so it can be
snapshotted to disk and shipped to browsers as-is.

Rules implemented (continuous version of the "Trading with imperfect
information" deck):
  * 3 public cards from hearts+spades, 1 private card per informed player.
  * Trading runs as one or more consecutive "days". Within a day the market
    is fully continuous with a live limit-order book: market makers keep a
    two-sided quote (bid/size, ask/size); re-submitting replaces it, and a
    quote that crosses a resting order trades immediately at the RESTING
    order's price (price-time priority). Quotes can be pulled at any time.
    Liquidity takers send market orders that walk the book. No self-trading.
  * At the end of a day: outstanding forced orders execute against what is
    left of the book, the book is wiped overnight, and margin interest is
    charged on negative cash (host-set rate, default 0). Positions and cash
    carry over; after the last day the game settles.
  * Event cards (optional): drawn automatically when a later day opens and
    on demand by the host — public value/rule shocks, dividends and levies,
    and private forced orders (noise-trader flow).
  * Settlement: V = sum of points of ALL dealt cards; score = cash + pos * V.
  * Card points (hearts/spades): A=-40, K=+20, Q=J=0, others face value;
    diamonds/clubs = 0. Bids and asks must be strictly positive.
"""

import secrets

GAME_VERSION = 3          # bump when the game dict shape changes (snapshots)

RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
SUITS = ['h', 's', 'd', 'c']

MAX_PRICE = 999.99
MAX_SIZE = 99
MAX_NAME = 20
MAX_DAYS = 10


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
        'gameVersion': GAME_VERSION,
        'phase': 'lobby',   # lobby | open | between (overnight) | settled
        'day': 0,
        'settings': {
            'roles': 'assigned',      # assigned (MM vs taker) | everyone (all do both)
            'dealPool': 'hs',         # private cards from: hs | full deck
            'days': 1,                # trading days in the session
            'daySeconds': 300,        # length of each day; 0 = host closes days manually
            'maxPlayers': None,       # host cap on seats; None = deck limit
            'informedCount': None,    # players dealt a private card; None = everyone
            'feePerUnit': 0,          # exchange fee charged to BOTH sides, per unit
            'marginRate': 0,          # % per day charged on negative cash at day close
            'eventCards': False,      # auto-draw an event card at each later day open
            'anonymous': False,       # pseudonyms on book/tape/standings until settlement
            'cardValues': dict(DEFAULT_CARD_VALUES),   # host-manipulable A/K/Q/J points
        },
        'players': {},                # pid -> player dict
        'joinOrder': [],
        'publicCards': [],
        'book': {'bids': [], 'asks': []},   # resting orders {pid, price, size, seq}
        'seq': 0,                     # arrival counter for price-time priority
        'trades': [],                 # {i, buyer, seller, price, size, ts}
        'events': [],                 # drawn event cards {i, day, ts, id, headline, detail}
        'eventDeck': [],              # remaining event ids; refilled+shuffled when empty
        'feesCollected': 0,           # exchange take when feePerUnit > 0 (burned)
        'interestPaid': 0,            # margin interest collected at day closes (burned)
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
    limits the head count — cap on what one server instance handles comfortably.
    The host can lower it further with the maxPlayers setting."""
    base = pool_size(game) if game['settings'].get('informedCount') is None else 49
    cap = game['settings'].get('maxPlayers')
    return min(base, cap) if cap else base


def can_quote(p):
    return p['role'] in ('mm', 'both')


def can_take(p):
    return p['role'] in ('taker', 'both')


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
        cap = game['settings'].get('maxPlayers')
        if cap and len(active_players(game)) >= cap:
            raise GameError(f'The game is full — the host capped it at {cap} players.')
        raise GameError('The game is full for the current deck setting.')
    p = {'id': new_id(), 'name': name, 'role': _default_role(game),
         'card': None, 'cash': 0, 'pos': 0, 'active': True, 'forced': None}
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
        if 'days' in patch:
            v = _num(patch['days'], 'day count')
            if v != int(v) or not (1 <= v <= MAX_DAYS):
                raise GameError(f'Days must be a whole number from 1 to {MAX_DAYS}.')
            if game['phase'] != 'lobby' and int(v) < game['day']:
                raise GameError(f"Day {game['day']} is already trading — "
                                'you can only add days now.')
            s['days'] = int(v)
        if 'daySeconds' in patch:  # live-tunable: applies from the next day open
            v = _num(patch['daySeconds'], 'duration')
            if v != int(v) or (v != 0 and not (30 <= v <= 7200)):
                raise GameError('The day clock must be 0 (host closes the day) '
                                'or 30-7200 seconds.')
            s['daySeconds'] = int(v)
        if 'maxPlayers' in patch:
            if game['phase'] != 'lobby':
                raise GameError('The player cap can only be changed in the lobby.')
            v = patch['maxPlayers']
            if v in (None, ''):
                s['maxPlayers'] = None
            else:
                n = _num(v, 'player cap')
                if n != int(n) or not (2 <= n <= 49):
                    raise GameError('Max players must be a whole number from 2 to 49.')
                s['maxPlayers'] = int(n)
        if 'marginRate' in patch:  # live-tunable: charged at each day close
            v = round2(_num(patch['marginRate'], 'rate'))
            if not (0 <= v <= 20):
                raise GameError('The margin rate must be between 0 and 20 percent per day.')
            s['marginRate'] = v
        if 'eventCards' in patch:  # live-tunable: governs auto-draws at day opens
            s['eventCards'] = bool(patch['eventCards'])
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
        _pull_orders(game, pid)
    note(game, f"{p['name']} was removed by the host", now)


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

    game['phase'] = 'open'
    game['day'] = 1
    game['book'] = {'bids': [], 'asks': []}
    game['seq'] = 0
    game['trades'] = []
    game['events'] = []
    game['eventDeck'] = []
    game['feesCollected'] = 0
    game['interestPaid'] = 0
    game['settlement'] = None
    for p in players:
        p['forced'] = None
    ds = game['settings']['daySeconds']
    game['deadline'] = now + ds * 1000 if ds > 0 else None
    days = game['settings']['days']
    dealt = ('everyone holds a private card' if k_eff == len(players) else
             f'{k_eff} of {len(players)} players hold a private card (who — secret)')
    note(game, f'Market open with {len(players)} players — {dealt}'
               + (f'; day 1 of {days}' if days > 1 else ''), now)


def _execute_forced(game, now):
    """Fill outstanding forced orders against what's left of the book, then clear."""
    for p in active_players(game):
        f = p.get('forced')
        if f and f['size'] > 0:
            limit = MAX_PRICE if f['side'] == 'buy' else 0
            _match_incoming(game, p['id'], f['side'], limit, f['size'], now)
    for p in game['players'].values():
        p['forced'] = None


def _charge_margin(game, now):
    """Overnight interest on margin loans: negative cash pays rate% at day close."""
    rate = game['settings'].get('marginRate') or 0
    if not rate:
        return
    total = 0
    for p in active_players(game):
        if p['cash'] < 0:
            interest = round2(-p['cash'] * rate / 100)
            p['cash'] = round2(p['cash'] - interest)
            total = round2(total + interest)
    if total:
        game['interestPaid'] = round2(game.get('interestPaid', 0) + total)
        note(game, f'Overnight margin interest charged: {total}', now)


def _close_day(game, now):
    """End-of-day mechanics: forced orders execute, the book is wiped, margin accrues."""
    _execute_forced(game, now)
    game['book'] = {'bids': [], 'asks': []}
    _charge_margin(game, now)


def end_day(game, now):
    """Close the current day overnight; settling instead after the last day."""
    if game['phase'] != 'open':
        raise GameError('No day is open right now.')
    _close_day(game, now)
    if game['day'] >= game['settings']['days']:
        _settle(game, now)
        return 'settled'
    game['phase'] = 'between'
    game['deadline'] = None
    note(game, f"Day {game['day']} closed — the book is wiped overnight, positions carry", now)
    return 'between'


def next_day(game, now, rng):
    """Open the next trading day with a fresh, empty book (and maybe an event)."""
    if game['phase'] != 'between':
        raise GameError('The market is not between days.')
    game['day'] += 1
    game['phase'] = 'open'
    ds = game['settings']['daySeconds']
    game['deadline'] = now + ds * 1000 if ds > 0 else None
    note(game, f"Day {game['day']} of {game['settings']['days']} — market open", now)
    if game['settings'].get('eventCards'):
        draw_event(game, now, rng)


# ---------------------------------------------------------------- event cards

EVENT_CARDS = [
    {'id': 'ace-crash',   'headline': 'Rating downgrade — Aces fall 30 points'},
    {'id': 'king-rally',  'headline': 'Merger rumor — Kings gain 30 points'},
    {'id': 'royal-swap',  'headline': 'Shock reversal — Ace and King values swap'},
    {'id': 'face-lift',   'headline': 'Meme rally — Queens and Jacks gain 15'},
    {'id': 'fee-hike',    'headline': 'Regulator steps in — the trading fee rises 0.5/unit'},
    {'id': 'fee-holiday', 'headline': 'Fee holiday — exchange fees drop to zero'},
    {'id': 'dark-pool',   'headline': 'Dark pool opens — trading turns anonymous'},
    {'id': 'flash-close', 'headline': 'Flash session — the day closes in 60 seconds'},
    {'id': 'forced-buy',  'headline': 'Mandate — one trader must BUY before the close'},
    {'id': 'forced-sell', 'headline': 'Redemption — one trader must SELL before the close'},
    {'id': 'dividend',    'headline': 'Dividend — +3 cash per unit held (shorts pay)'},
    {'id': 'levy',        'headline': 'Special levy — −3 cash per unit held (shorts collect)'},
    {'id': 'short-audit', 'headline': 'Short audit — shorts pay 2/unit to the exchange'},
]


def _eligible_forced(game):
    return [p for p in active_players(game) if can_take(p) and not p.get('forced')]


def _apply_event(game, eid, now, rng):
    """Mutate the game per event card; returns the public detail line."""
    s = game['settings']
    vals = dict(s.get('cardValues') or DEFAULT_CARD_VALUES)
    clamp = lambda v: max(-200, min(200, int(v)))
    if eid == 'ace-crash':
        vals['A'] = clamp(vals['A'] - 30)
        s['cardValues'] = vals
        return f"Aces are now worth {vals['A']} points."
    if eid == 'king-rally':
        vals['K'] = clamp(vals['K'] + 30)
        s['cardValues'] = vals
        return f"Kings are now worth {vals['K']} points."
    if eid == 'royal-swap':
        vals['A'], vals['K'] = vals['K'], vals['A']
        s['cardValues'] = vals
        return f"Aces are now {vals['A']}, Kings {vals['K']}."
    if eid == 'face-lift':
        vals['Q'] = clamp(vals['Q'] + 15)
        vals['J'] = clamp(vals['J'] + 15)
        s['cardValues'] = vals
        return f"Queens are now {vals['Q']}, Jacks {vals['J']}."
    if eid == 'fee-hike':
        s['feePerUnit'] = round2(min(10, (s.get('feePerUnit') or 0) + 0.5))
        return f"The fee is now {s['feePerUnit']}/unit, charged to both sides."
    if eid == 'fee-holiday':
        s['feePerUnit'] = 0
        return 'Trading is free until further notice.'
    if eid == 'dark-pool':
        s['anonymous'] = True
        return 'Book, tape and standings show pseudonyms from now on.'
    if eid == 'flash-close':
        target = now + 60_000
        if game['deadline'] is None or game['deadline'] > target:
            game['deadline'] = target
        return 'Close your positions — the day ends in 60 seconds.'
    if eid in ('forced-buy', 'forced-sell'):
        side = 'buy' if eid == 'forced-buy' else 'sell'
        p = rng.choice(_eligible_forced(game))
        p['forced'] = {'side': side, 'size': rng.randint(1, 3)}
        return 'Who received the order — and its size — is private.'
    if eid == 'dividend':
        for p in game['players'].values():
            p['cash'] = round2(p['cash'] + 3 * p['pos'])
        return 'Every position received 3 per unit; shorts paid it.'
    if eid == 'levy':
        for p in game['players'].values():
            p['cash'] = round2(p['cash'] - 3 * p['pos'])
        return 'Every position paid 3 per unit; shorts collected it.'
    if eid == 'short-audit':
        take = 0
        for p in game['players'].values():
            if p['pos'] < 0:
                fine = round2(2 * -p['pos'])
                p['cash'] = round2(p['cash'] - fine)
                take = round2(take + fine)
        game['feesCollected'] = round2(game.get('feesCollected', 0) + take)
        return (f'Shorts paid {take} to the exchange.' if take
                else 'No shorts were found — nothing collected.')
    return ''


def draw_event(game, now, rng):
    """Draw the next applicable event card and apply it."""
    if game['phase'] != 'open':
        raise GameError('Event cards can only be drawn while the market is open.')
    eid = None
    for _ in range(len(EVENT_CARDS) + 1):
        if not game['eventDeck']:
            game['eventDeck'] = [e['id'] for e in EVENT_CARDS]
            rng.shuffle(game['eventDeck'])
        cand = game['eventDeck'].pop()
        if cand in ('forced-buy', 'forced-sell') and not _eligible_forced(game):
            continue   # nobody can receive an order right now; skip this card
        eid = cand
        break
    if eid is None:
        raise GameError('No applicable event card right now.')
    detail = _apply_event(game, eid, now, rng)
    card = next(e for e in EVENT_CARDS if e['id'] == eid)
    game['events'].append({'i': len(game['events']), 'day': game['day'], 'ts': now,
                           'id': eid, 'headline': card['headline'], 'detail': detail})
    note(game, f"Event card: {card['headline']}", now)
    return {'headline': card['headline'], 'detail': detail}


# ---------------------------------------------------------------- the book

def _pull_orders(game, pid):
    """Remove a player's resting orders from both sides; returns how many."""
    book = game['book']
    n = sum(1 for o in book['bids'] + book['asks'] if o['pid'] == pid)
    book['bids'] = [o for o in book['bids'] if o['pid'] != pid]
    book['asks'] = [o for o in book['asks'] if o['pid'] != pid]
    return n


def _rest(game, side, pid, price, size):
    game['seq'] += 1
    game['book'][side].append({'pid': pid, 'price': price, 'size': size,
                               'seq': game['seq']})
    if side == 'bids':
        game['book'][side].sort(key=lambda o: (-o['price'], o['seq']))
    else:
        game['book'][side].sort(key=lambda o: (o['price'], o['seq']))


def _apply_trade(game, buyer, seller, price, size, now):
    b, s = game['players'][buyer], game['players'][seller]
    fee = game['settings'].get('feePerUnit', 0) or 0
    b['pos'] += size
    b['cash'] = round2(b['cash'] - (price + fee) * size)
    s['pos'] -= size
    s['cash'] = round2(s['cash'] + (price - fee) * size)
    if fee:
        game['feesCollected'] = round2(game.get('feesCollected', 0) + 2 * fee * size)
    game['trades'].append({'i': len(game['trades']), 'buyer': buyer, 'seller': seller,
                           'price': price, 'size': size, 'ts': now})


def _match_incoming(game, pid, side, price, size, now):
    """Cross an incoming order ('buy' or 'sell') against the resting book at the
    RESTING orders' prices, best first; returns the unfilled remainder."""
    key = 'asks' if side == 'buy' else 'bids'
    levels = game['book'][key]
    rem = size
    fills = []
    for o in levels:
        if rem == 0:
            break
        if (o['price'] > price) if side == 'buy' else (o['price'] < price):
            break
        if o['pid'] == pid:
            continue  # never trade with yourself
        q = min(rem, o['size'])
        if side == 'buy':
            _apply_trade(game, pid, o['pid'], o['price'], q, now)
        else:
            _apply_trade(game, o['pid'], pid, o['price'], q, now)
        o['size'] -= q
        rem -= q
        fills.append({'pid': o['pid'], 'price': o['price'], 'size': q})
    game['book'][key] = [o for o in levels if o['size'] > 0]
    return rem, fills


def submit_quote(game, pid, q, now):
    """Post (or replace) a two-sided quote. A side that crosses the book trades
    immediately at the resting price; the remainder rests."""
    if game['phase'] != 'open':
        raise GameError('The market is not open right now.')
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

    _pull_orders(game, pid)   # a new quote replaces the old one atomically
    traded = 0
    rem, _f = _match_incoming(game, pid, 'buy', bid, bid_size, now)
    traded += bid_size - rem
    if rem:
        _rest(game, 'bids', pid, bid, rem)
    rem, _f = _match_incoming(game, pid, 'sell', ask, ask_size, now)
    traded += ask_size - rem
    if rem:
        _rest(game, 'asks', pid, ask, rem)
    return {'traded': traded}


def cancel_quotes(game, pid, now):
    """Pull all of a player's resting orders."""
    if game['phase'] != 'open':
        raise GameError('The market is not open right now.')
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    n = _pull_orders(game, pid)
    if n:
        note(game, f"{p['name']} pulled their quotes", now)
    return {'canceled': n}


def market_order(game, pid, side, size, now):
    if game['phase'] != 'open':
        raise GameError('The market is not open right now.')
    p = game['players'].get(pid)
    if not p or not p['active']:
        raise GameError('Unknown player.')
    if not can_take(p):
        raise GameError('Market makers do not send market orders in this game.')
    if side not in ('buy', 'sell'):
        raise GameError('Side must be buy or sell.')
    sz = _size(size, 'Size')

    # a market order crosses at any price — reuse the matcher with no price limit
    limit = MAX_PRICE if side == 'buy' else 0
    rem, fills = _match_incoming(game, pid, side, limit, sz, now)
    if not fills:
        raise GameError('No asks available to buy from right now.' if side == 'buy'
                        else 'No bids available to sell to right now.')
    f = p.get('forced')
    if f and f['side'] == side:   # voluntary fills count toward a forced order
        f['size'] -= sz - rem
        if f['size'] <= 0:
            p['forced'] = None
    return {'requested': sz, 'filled': sz - rem, 'fills': fills}


def settle(game, now):
    """Close the market now: run day-close mechanics if mid-day, then score."""
    if game['phase'] not in ('open', 'between'):
        raise GameError('The market has not opened — nothing to settle.')
    if game['phase'] == 'open':
        _close_day(game, now)
    _settle(game, now)


def _settle(game, now):
    """Reveal cards and score everyone (books are already closed)."""
    game['book'] = {'bids': [], 'asks': []}
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
                          'interestPaid': game.get('interestPaid', 0),
                          'anonymous': bool(game['settings'].get('anonymous'))}
    game['phase'] = 'settled'
    game['deadline'] = None
    winner = rows[0]['name'] if rows else '—'
    note(game, f'Market closed & settled: V = {v}. Top score: {winner}', now)


def rematch(game, now):
    """Back to the lobby with the same (still-active) players."""
    if game['phase'] != 'settled':
        raise GameError('A rematch is available after settlement.')
    game['joinOrder'] = [pid for pid in game['joinOrder'] if game['players'][pid]['active']]
    game['players'] = {pid: game['players'][pid] for pid in game['joinOrder']}
    for p in game['players'].values():
        p.update(card=None, cash=0, pos=0, informed=None, alias=None, forced=None)
    game.update(phase='lobby', day=0, publicCards=[], book={'bids': [], 'asks': []},
                seq=0, trades=[], events=[], eventDeck=[], feesCollected=0,
                interestPaid=0, settlement=None, deadline=None)
    note(game, 'Rematch — back to the lobby with the same players', now)


def on_deadline(game, now, rng):
    """Called by the server when the day clock runs out."""
    if game['deadline'] is None or now < game['deadline']:
        return None
    if game['phase'] == 'open':
        return 'settle' if end_day(game, now) == 'settled' else 'endDay'
    game['deadline'] = None
    return 'clear'


# ---------------------------------------------------------------- views

def view_for(game, kind, pid=None, extras=None):
    """Build the JSON state tailored to one audience (never leaks private cards)."""
    ex = extras or {}
    now = ex.get('now', 0)
    conn = ex.get('connections', {})
    started = game['phase'] != 'lobby'
    # anonymity hides who is behind each order/position, not who is in the room:
    # rosters keep real names; trading surfaces (book/tape/standings) get pseudonyms.
    anon = bool(game['settings'].get('anonymous')) and kind in ('player', 'board') and started

    def disp(i):
        p = game['players'].get(i)
        if not p:
            return '—'
        return (p.get('alias') or p['name']) if anon else p['name']

    players = []
    for i in game['joinOrder']:
        p = game['players'][i]
        entry = {
            'id': p['id'], 'name': p['name'], 'role': p['role'], 'active': p['active'],
            'connected': conn.get(p['id'], 0) > 0,
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
        'phase': game['phase'], 'day': game['day'],
        'settings': game['settings'], 'deadline': game['deadline'],
        'publicCards': game['publicCards'],
        'players': players,
        'book': {'bids': book_side('bids'), 'asks': book_side('asks')},
        'tape': [{'i': t['i'],
                  'buyer': disp(t['buyer']), 'seller': disp(t['seller']),
                  'price': t['price'], 'size': t['size']}
                 for t in game['trades'][-30:]],
        'events': [{'i': e['i'], 'day': e['day'], 'headline': e['headline'],
                    'detail': e['detail']}
                   for e in game['events'][-6:]],
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
            'cash': p['cash'], 'pos': p['pos'], 'forced': p.get('forced'),
            'canQuote': can_quote(p), 'canTake': can_take(p),
            'fills': [{'i': t['i'],
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
    if kind in ('host', 'board'):
        base['roomCode'] = ex.get('roomCode')
    return base
