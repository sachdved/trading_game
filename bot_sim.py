"""In-process evaluation harness for bot.py's strategies (AI_PLAYERS.md §8).

Drives the real engine with a virtual clock and feeds each bot only its
view_for() output, json round-tripped — exactly what the live client sees
over the wire, so the sim and the live bot cannot drift apart.

  python3 bot_sim.py --games 30
  python3 bot_sim.py --games 20 --fee 1 --informed half --anon
  python3 bot_sim.py --games 20 --trials off --days 3
  python3 bot_sim.py --games 40 --mirror
"""

import argparse
import json
import random

import engine as E
import bot as B

BOT_TYPES = ['ev', 'bluff', 'mix', 'noise']
MIRROR_TYPES = ['ev', 'noise', 'ev', 'noise']


def default_cfg():
    return {
        'roles': 'assigned', 'dealPool': 'hs', 'days': 1, 'daySeconds': 300,
        'informedCount': None, 'feePerUnit': 0, 'marginRate': 0,
        'eventCards': False, 'eventEverySeconds': 60,
        'trials': True, 'trialSeconds': 60, 'indemnityRate': 0.5,
        'falseAccusationFee': 6, 'anonymous': False,
    }


class HumanSim:
    kind = 'human'

    def __init__(self, rng):
        self.rng = rng
        self.next_act = 0
        self.accused = False
        self.day_seen = 0
        self.quote_log = []
        self.stats = {'bluff_cost': 0.0, 'bluff_count': 0, 'decoy_cost': 0.0, 'decoy_count': 0}

    def on_state(self, view, now):
        if view.get('error'):
            return None
        phase = view['phase']
        if phase == 'trial':
            if self.accused:
                return None
            self.accused = True
            if self.rng.random() < 0.5:
                return ('accuse', None, None)
            cands = (view.get('me') or {}).get('candidates') or []
            if not cands:
                return ('accuse', None, None)
            return ('accuse', self.rng.choice(cands), self.rng.choice(['bull', 'bear']))
        if phase != 'open':
            return None
        me = view.get('me') or {}
        s = view['settings']
        if now < self.next_act:
            return None
        self.next_act = now + 1500 + self.rng.randint(0, 2500)
        fee = s.get('feePerUnit') or 0
        m = B.prior_mean(s, view['publicCards'], len(view['players']), me.get('card'))
        book = view['book']
        bb, ba = B.book_extremes(book)
        if me.get('canTake'):
            if ba is not None and m - ba - fee > 1:
                sz = self.rng.choice([1, 1, 2, 2, 3])
                if B.depth_at(book, 'buy', ba, sz) >= 1:
                    return ('market', 'buy', sz)
            if bb is not None and bb - fee - m > 1:
                sz = self.rng.choice([1, 1, 2, 2, 3])
                if B.depth_at(book, 'sell', bb, sz) >= 1:
                    return ('market', 'sell', sz)
        if me.get('canQuote'):
            mq = B.my_quote(book)
            if mq is None or abs(m - (mq[0] + mq[1]) / 2) > 2:
                q = B.make_quote(m, fee, 3.0, self.rng, lambda: self.rng.choice([2, 3, 4]))
                if q:
                    self.quote_log.append((now, (q[1] + q[2]) / 2,
                                           B.public_mean(s, view['publicCards'], len(view['players']))))
                    return q
        return None


def make_seat(kind, seed):
    if kind == 'human':
        return HumanSim(random.Random(seed))
    return B.make_strategy(kind, seed)


def execute(game, pid, act, now):
    try:
        if act[0] == 'quote':
            E.submit_quote(game, pid, {'bid': act[1], 'ask': act[2],
                                       'bidSize': act[3], 'askSize': act[4]}, now)
        elif act[0] == 'cancel':
            E.cancel_quotes(game, pid, now)
        elif act[0] == 'market':
            E.market_order(game, pid, act[1], act[2], now)
        elif act[0] == 'accuse':
            E.file_accusation(game, pid, act[1], act[2], now)
    except E.GameError:
        pass


def run_game(cfg, types, seed):
    rng = random.Random(seed)
    game = E.create_game()
    names = []
    for i, t in enumerate(types):
        names.append('%s %d' % (t.capitalize(), i + 1) if types.count(t) > 1 else t.capitalize())
    pids = [E.add_player(game, nm, 0)['id'] for nm in names]
    strats = [make_seat(t, seed * 1000 + i) for i, t in enumerate(types)]
    E.set_settings(game, cfg, 0)
    now = 1_700_000_000_000
    E.start_game(game, now, rng)
    tick = 500
    limit = now + cfg['days'] * cfg['daySeconds'] * 1000 + cfg['days'] * 90_000 + 120_000
    trial_since = None
    while game['phase'] != 'settled' and now < limit:
        now += tick
        if game['phase'] == 'trial' and game['deadline'] is None:
            if trial_since is None:
                trial_since = now
            elif now - trial_since > 10_000:
                try:
                    E.resolve_trial(game, now)
                except E.GameError:
                    pass
                trial_since = None
        else:
            trial_since = None
        dl = [d for d in (game.get('deadline'), game.get('eventDeadline')) if d is not None]
        if dl and now >= min(dl):
            try:
                E.on_deadline(game, now, rng)
            except E.GameError:
                pass
            if game['phase'] == 'between':
                try:
                    E.next_day(game, now, rng)
                except E.GameError:
                    pass
        if game['phase'] in ('open', 'trial'):
            for pid, strat in zip(pids, strats):
                if game['phase'] == 'trial' or now >= getattr(strat, 'next_act', 0):
                    view = E.view_for(game, 'player', pid, {'now': now, 'connections': {}})
                    view = json.loads(json.dumps(view))
                    act = strat.on_state(view, now)
                    if act:
                        execute(game, pid, act, now)
    if game['phase'] != 'settled':
        try:
            E.settle(game, now)
        except E.GameError:
            pass
    return game, names, strats


def game_metrics(game, strats, types):
    rows = game['settlement']['rows']
    flow = {}
    for t in game['trades']:
        flow[t['buyer']] = flow.get(t['buyer'], 0) + t['size']
        flow[t['seller']] = flow.get(t['seller'], 0) - t['size']
    out = []
    by_pid = {r['pid']: r for r in rows}
    players = [game['players'][p] for p in game['joinOrder']]
    for p, strat, typ in zip(players, strats, types):
        row = by_pid[p['id']]
        big = E.card_case(game, p) is not None
        f = flow.get(p['id'], 0)
        qlog = strat.quote_log
        flagged_quote = any(abs(q[1] - q[2]) >= 3 for q in qlog) if qlog else False
        out.append({
            'type': typ, 'total': row['total'],
            'won': row is rows[0],
            'big': big, 'flow': f,
            'flagged': (abs(f) >= 5) or flagged_quote,
            'qdev': [q[1] - q[2] for q in qlog],
            'card_pts': E.card_points(p['card'], game['settings'].get('cardValues')) if p['card'] else 0,
            'stats': strat.stats,
        })
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def fmt(v, spec='.2f'):
    if v is None:
        return '  n/a '
    return format(v, spec)


def run_batch(cfg, types, n_games, base_seed, label):
    per = {t: {
        'totals': [], 'wins': 0, 'big_totals': [], 'qpts': [], 'fpts': [],
        'det': [0, 0], 'fal': [0, 0], 'bluff_cost': [], 'decoys': [],
    } for t in set(types)}
    for g in range(n_games):
        game, names, strats = run_game(cfg, types, base_seed + g)
        if game['settlement'] is None:
            continue
        for m in game_metrics(game, strats, types):
            d = per[m['type']]
            d['totals'].append(m['total'])
            d['wins'] += 1 if m['won'] else 0
            if m['big']:
                d['big_totals'].append(m['total'])
                d['det'][0] += 1
                d['det'][1] += 1 if m['flagged'] else 0
            else:
                d['fal'][0] += 1
                d['fal'][1] += 1 if m['flagged'] else 0
            d['qpts'].append((m['qdev'], m['card_pts']))
            d['fpts'].append((m['flow'], m['card_pts']))
            d['bluff_cost'].append(m['stats']['bluff_cost'])
            d['decoys'].append(m['stats']['decoy_count'] + m['stats']['bluff_count'])
    print('=' * 88)
    print('settings: %s   (%d games, %d seats: %s)' % (label, n_games, len(types), '+'.join(types)))
    print('-' * 88)
    order = BOT_TYPES if not label.startswith('mirror') else MIRROR_TYPES
    order = [t for i, t in enumerate(order) if t in per and order.index(t) == i]
    print('%-8s %6s %9s %7s %10s %10s %9s %9s %9s' % (
        'type', 'games', 'avgTotal', 'winRate', 'bigCardAvg', 'leakQuote', 'leakFlow', 'detect', 'falseAl'))
    for t in order:
        d = per[t]
        n = len(d['totals'])
        if not n:
            continue
        qpts = [(qv, cp) for qdevs, cp in d['qpts'] for qv in qdevs]
        fq = pearson([x[0] for x in qpts], [x[1] for x in qpts])
        ff = pearson([x[0] for x in d['fpts']], [x[1] for x in d['fpts']])
        det = '%.2f' % (d['det'][1] / d['det'][0]) if d['det'][0] else 'n/a'
        fal = '%.2f' % (d['fal'][1] / d['fal'][0]) if d['fal'][0] else 'n/a'
        big_avg = sum(d['big_totals']) / len(d['big_totals']) if d['big_totals'] else None
        print('%-8s %6d %9.2f %6.0f%% %10s %10s %9s %9s %9s' % (
            t, n, sum(d['totals']) / n, 100.0 * d['wins'] / n,
            fmt(big_avg), fmt(fq, '.2f'), fmt(ff, '.2f'), det, fal))
    for t in order:
        d = per[t]
        if d['bluff_cost']:
            print('%-8s      bluff cost avg %.2f/game, bluff/decoy actions avg %.2f/game' % (
                t, sum(d['bluff_cost']) / len(d['bluff_cost']), sum(d['decoys']) / len(d['decoys']) if d['decoys'] else 0))
    print()


def main():
    ap = argparse.ArgumentParser(description='bot evaluation harness')
    ap.add_argument('--games', type=int, default=30)
    ap.add_argument('--fee', type=float, default=0)
    ap.add_argument('--trials', choices=['on', 'off'], default='on')
    ap.add_argument('--anon', action='store_true')
    ap.add_argument('--informed', choices=['all', 'half', 'none'], default='all')
    ap.add_argument('--days', type=int, default=1)
    ap.add_argument('--events', action='store_true')
    ap.add_argument('--mirror', action='store_true', help='EV vs Noise pairings (4 seats)')
    ap.add_argument('--seed', type=int, default=1)
    args = ap.parse_args()

    cfg = default_cfg()
    cfg['feePerUnit'] = args.fee
    cfg['trials'] = args.trials == 'on'
    cfg['anonymous'] = args.anon
    cfg['days'] = args.days
    cfg['eventCards'] = args.events
    if args.informed == 'half':
        cfg['informedCount'] = 3
    elif args.informed == 'none':
        cfg['informedCount'] = 0

    label = 'fee=%.1f trials=%s anon=%s informed=%s days=%d events=%s' % (
        args.fee, args.trials, args.anon, args.informed, args.days, args.events)
    if args.mirror:
        run_batch(cfg, MIRROR_TYPES, args.games, args.seed, 'mirror: ' + label)
    else:
        run_batch(cfg, BOT_TYPES + ['human', 'human'], args.games, args.seed, label)


if __name__ == '__main__':
    main()
