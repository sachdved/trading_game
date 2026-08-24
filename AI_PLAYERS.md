# AI players for the trading game — design doc

Computer opponents that join a room as ordinary players and play through the
public HTTP API — no server changes, no host key, no privileged access. A bot
sees exactly what a human's phone sees, and can do exactly what a human can do.

Four bot types, which are points on one tradeoff: **how much of the card's
edge you extract vs how much of your card you leak.**

| Type | One-liner |
|---|---|
| **EV Bot** | Pure expected-value maximizer. Risk-neutral, never bluffs, always trades the sign of its current estimate of V. Its quotes are an *honest signal* of what it thinks V is. Maximum extraction, maximum leakage. |
| **Bluffer** | Heavy bluffer. Frequently trades and quotes *against* its own estimate to camouflage big cards and to misdirect the tape. Accepts real short-term losses as the price of concealment and deception. High extraction, noisy fingerprint. |
| **Mixer** | An EV backbone wrapped in an adaptive noise layer. Per decision it is either the EV Bot or the Bluffer, with the blend weight drifting so the table can't pin down which it is. |
| **Noise player** | Feigns being uninformed. Even when dealt a card, it trades only on public information — the card just quietly tilts the *size and timing* of otherwise-justified trades, never their direction. Minimum extraction, ~zero leakage: to the table it is statistically indistinguishable from a player who was dealt no card. |

---

## 1. The game from the bot's perspective

### 1.1 Objective

Settlement scores every player (engine `_settle`, `engine.py:836`):

```
total = cash + pos × V
```

where `V = sum of the points of all dealt cards` — the 3 public cards plus
every private card actually dealt (private to their holders). The game is
zero-sum (minus exchange fees/margin interest, which are burned). So a bot's
entire problem is: **estimate V, hold net exposure in the direction
`V − price`, and pay as little as possible in fees, slippage, and
adverse selection.**

There is no cash constraint (negative cash is just margin; interest only if
the host sets `marginRate`), no position cap, and shorting is free — so
risk-neutral decisions are unbounded in principle and only capped by order
size (≤ 99) and available liquidity.

### 1.2 Card values and the deck

- Hearts/spades: A = −40, K = +20, Q = J = 0, number cards = face value
  (A/K/Q/J are host-adjustable, and event cards can change them mid-game).
  Clubs/diamonds = 0.
- 3 public cards always come from hearts+spades. Private cards come from
  `dealPool`: the remaining 23 hearts/spades, or 49 cards from the full deck.
- `informedCount` = k (None = everyone): only k randomly chosen players are
  dealt a card. **k is public; who is secret.**

### 1.3 What a bot can observe (its information set)

Everything below is in the player view (`engine.view_for`, `engine.py:1180`)
pushed over SSE on every state change:

| Signal | Notes |
|---|---|
| `publicCards` | 3 cards, values known exactly |
| `settings` | `cardValues`, `feePerUnit`, `dealPool`, `informedCount`, `days`, `marginRate`, `anonymous`, trials settings — all re-readable live, since host tweaks and event cards mutate them |
| `me` | own card (or explicit no-card notice), `informed` flag, `cash`, `pos`, `forced` (a private mandate to buy/sell N units), `canQuote`/`canTake`, own last 20 `fills` with counterparties, own `accusation`/`verdict` |
| `book` | full resting book: price/size/name of every order, `mine` flag |
| `tape` | **last 30 trades only** — the bot must accumulate its own history across pushes |
| `players` | every player's `pos` and `cash`, live — **unless `anonymous` is on**, when positions are only visible under stable pseudonyms and *cannot be linked to names* by a player (the alias map is host-only). Under anonymity all per-player attribution degrades to aggregate flow |
| `events` | last 16 event cards (headlines + detail) — value shocks, fee changes, dividends/levies, flash closes, forced mandates |
| `chart` | OHLC candles over the whole session (up to 72) — useful for long-run price stats the 30-trade tape can't give |
| `deadline` | day clock (and flash-close pulls it in to 60s) |
| `trial` | during investigations: only the count of filed accusations is public |

What a bot can **never** see: other players' cards, who the other informed
players are, other players' accusations, whether an event's forced mandate
went to someone else.

### 1.4 Actions available (HTTP API, same as the browser)

| Endpoint | Payload | Effect |
|---|---|---|
| `POST /r/CODE/api/join` | `{name}` | Join; returns `{token, pid, name}` |
| `GET /r/CODE/events?role=player&token=…` | — | SSE stream of full player views (`retry: 1500`, 15s heartbeats) |
| `POST /r/CODE/api/quote` | `{token, bid, ask, bidSize, askSize}` | Replace quote. Both sides required, sizes 1–99, `ask > bid`, prices > 0. **A side that crosses the book trades immediately at the resting price; the remainder rests** (`engine.submit_quote`, `engine.py:751`) |
| `POST /r/CODE/api/cancel` | `{token}` | Pull all resting orders |
| `POST /r/CODE/api/market` | `{token, side, size, reqId}` | Market order, walks the book; `reqId` gives idempotent retries. Fails with no resting liquidity on that side |
| `POST /r/CODE/api/accuse` | `{token, target, dir}` or `{token, target: null}` | File one accusation (bull/bear) during a trial, or abstain |
| `POST /r/CODE/api/claim` | `{name}` | Resume a seat after token loss |

Role gating (assigned mode): `mm` can only quote, `taker` can only send
market orders, `both` can do both (everyone mode). Roles alternate by join
order, so **which sub-policy a bot runs depends on when it joins** — every
strategy must implement both a taker policy and a quoter policy.

### 1.5 Phase flow a bot must track

`lobby → open → (trial →) between → … → settled` (one `trial`/`between`
cycle per day). Overnight: the book is wiped, forced orders execute at any
price against leftovers, margin interest is charged, positions/cash carry.
A bot may be kicked mid-game (its card stays in V) — it must exit cleanly on
`{"error": "kicked"}` / `{"error": "reset"}`.

---

## 2. The math core: estimating V and acting on it

Shared by all three bot types (it is the "brain"); the strategies differ only
in how they act on it.

### 2.1 The prior (closed form, recomputed on every event/host tweak)

Let `v(r)` be the current value of rank `r` in a heart/spade suit (from
`settings.cardValues`), `S = 2·Σ_r v(r)` the h+s deck total (68 at defaults),
`P` the sum of the 3 public cards, `N` the pool size (23 or 49),
`S_pool = S − P`, `n` active players, `k` cards dealt, `c` own card value
(0 if no card). By symmetry of the random deal:

```
own card:   E[V] = P + c + (k−1) · (S_pool − c) / (N − 1)
no card:    E[V] = P + k   · S_pool / N
```

The prior standard deviation of the *undealt* part has a closed form too
(variance of a without-replacement sample):

```
σ² = (k−1)·(1 − (k−1)/(N−1))·σ_pool²      (own-card case, symmetric for no-card)
```

where `σ_pool²` is the population variance over the N pool cards. This gives
the bot a calibrated uncertainty band — needed both for accusation EV (which
is a probability) and for the quoter's adverse-selection spread.

### 2.2 The posterior (online, heuristic — this is a party game, not a paper)

Maintain `m = E[V | info]` and an effective `σ`. Start from the prior, then
apply small, decaying updates as evidence arrives:

1. **Tape flow.** A taker buy of size `s` at price `p` says "someone's
   estimate of V is at least about p." Normal-normal style pull:
   `m += κ · s · (p − m) / (1 + s)` with `κ ≈ 0.05–0.2`, clipped per update
   (±1.5). Weight the trade by the aggressor's credibility (below). Self
   trades are excluded.
2. **Position levels** (when anonymity is off): a player's persistent
   nonzero `pos` is a weak directional signal — they accumulated it at prices
   they thought were good. Small κ per standing unit per minute.
3. **Other quotes.** A quoter's midpoint is a (possibly strategic) statement
   of their estimate. Weight: medium; down-weight players flagged as bluffing
   (see §5.3 — the bot keeps a per-opponent "bluff score").
4. **Credibility of each opponent.** Per player track: signed net flow,
   persistence (does flow keep one sign?), aggressiveness (trades far from
   mid?), size consistency. Persistent + far-from-mid + aligned with their
   public position → high credibility (their tape trades get a larger κ, and
   their quotes get more weight). Reversing flow → credibility collapses.
5. **Events.** Any `cardValues`/`feePerUnit` change → recompute the prior
   from scratch (§2.1) and halve the accumulated tape offset (old evidence is
   about *which* cards are held, not *what they're worth* — the mapping
   changed). Dividends/levies/short audits are one-time cash events: no belief
   update. Flash close → switch to end-of-day behavior (§2.5).

`m` is clamped to a sane range (`P + c ± 2σ_prior·3`). All updates are
bounded so a single trade can't move the bot's world.

### 2.3 Taker rule (risk-neutral)

Per-unit EV of buying at price `p`: `m − p − fee`. Buying is linear in size,
so a risk-neutral taker takes **all** liquidity priced inside the edge:

```
buy:  walk the asks while price + fee ≤ m     (≤ 99 units per order)
sell: walk the bids while price − fee ≥ m
else: do nothing (price inside the fee band → no edge)
```

Because the bot's own walking moves the book, the "while" condition is what
self-terminates it: it stops exactly when the market has absorbed its
information. A pure EV taker with a big edge (e.g., holding the Ace) will
therefore dump large size early — that is correct and it is also its tell;
the Bluffer and Mixer exist to hide exactly this.

### 2.4 Quoter rule (with adverse selection)

A resting bid gets hit preferentially when *better-informed* flow buys from
it, so the EV of a fill at price `b` is `E[V | hit at b] − b − fee < m − b − fee`.
Glosten–Milgrom's whole point. The bot prices this in:

```
bid = m − (fee + λ)        ask = m + (fee + λ)
```

`λ` = adverse-selection premium, estimated online:

- Initialize `λ₀ = 1 + 3·ρ₀`, where `ρ₀` = estimated informed fraction of
  opponents (`k/(n−1)` when `informedCount` is set, ~0.5 otherwise, 1 when
  everyone has a card and the pool is thin).
- After each of the bot's own fills, watch the next ~6 trades / 20s: if the
  mid drifts ≥ 1 point in the taker's favor, mark the fill "informed" with
  cost = drift; else cost 0. EWMA: `λ ← (1−α)λ + α·cost`, `α ≈ 0.15`,
  clipped to `[0, 25]`. Separate EWMA per side; quote half-spread uses the max.

Quote only when the spread is worth offering (`2·(fee + λ)` ≥ a floor, e.g.
2 points); otherwise **stand aside** (no quote) — nothing forces an MM to
quote, and a wide losing quote is worse than none. Re-quote (replace) when
`|m − center| > half-spread/2`, after any fill, or when the book around it
changed materially. Quote sizes are small (1–6): a deep quote is a target.

The EV Bot's quote center is therefore an honest, continuously broadcast
statement of its posterior `m`. That is a feature (efficient market) and its
fatal weakness (its card leaks through its quotes) — which is precisely the
asymmetry the Bluffer exploits.

### 2.5 End-of-day / flash-close behavior

- **Flash close (60s):** if the bot's position is on the edge side of the
  current book (i.e., the book already offers better than `m` on the exit
  side), flatten what the book can fill; otherwise hold — with no further
  trading, holding a position is worth `pos·m`, which is what the EV rule
  says anyway.
- **Overnight (days > 1):** event shocks at the next day open have
  approximately zero mean, so a risk-neutral bot carries positions across
  nights. Its beliefs about *who holds what* keep sharpening from the
  accumulated tape, so later days it should trade *more* confidently.
- **Forced orders (`me.forced`):** mandatory by close, and the unfilled
  remainder executes at any price against book leftovers (`_execute_forced`,
  `engine.py:439`) — potentially at a terrible price. Execute the mandate
  early, in slices, when the marginal price is closest to `m`; if the
  mandate is against the bot's edge, do it in small slices throughout the day
  rather than in one lump at the close. The Bluffer treats a forced mandate
  as *free camouflage* — it's genuine flow it can hide its real activity in.

---

## 3. Architecture

Stdlib-only Python (matching the repo), no dependencies, no build.

```
bot.py          the live client: transport + brain + 3 strategies, one file
                (sections: transport / belief / ev / bluff / mix / main)
bot_sim.py      in-process evaluation harness (drives strategies against
                engine.py directly, seeded, no network)
```

### 3.1 `bot.py` layout

```
class Client            # transport
  join(url, code, name)          # POST /api/join
  stream()                  # SSE read loop (http.client, reads `data:` lines,
                             # tolerates `:hb` heartbeats and `retry:`)
  send(action)               # serialized POSTs; market orders carry a fresh
                             # reqId; retries once on network error (idempotent
                             # via reqId)
  on 'bad-token' → rejoin by name → 'taken' + canClaim → POST /api/claim

class Belief              # §2: prior(), update_tape(), update_quote(),
                          # update_event(), per-opponent credibility, m/σ
                          # pure functions of (view, settings, history)

class Strategy            # on_state(view, now) -> list[Action]
  Action ∈ Quote(bid,ask,bidSize,askSize) | Cancel | Market(side,size)
         | Accuse(target,dir|None) | Hold

class EVStrategy(Strategy)
class BluffStrategy(Strategy)
class MixerStrategy(Strategy)   # composes EV + Bluff, owns the blend state
class NoiseStrategy(Strategy)   # public-info policy + card tilt (§7)

def main()                # CLI:
    # python3 bot.py --url http://host:3000 --code KFQTR --type ev|bluff|mix|noise
    #                [--name "EV Bot"] [--seed 7]
```

Loop: an SSE reader thread updates `latest_view` under a lock; a decision
thread ticks every 0.4–1.5 s (jittered) and calls `strategy.on_state(view)`.
Actions are emitted at most one per tick through the serialized `send`.

**Humanization (all types):** minimum 0.8–2.5 s between actions, ~5% chance
of a 5–15 s idle, re-quote only when the desired quote actually differs
(≥ 1 point), order sizes from a human-ish distribution
`{1,1,2,2,3,3,5,5,8}` (takers may walk larger), never the same size twice in
a row. A perfectly periodic 100 ms re-quoter is both bot-like and pointless.

**No-cheating invariant:** strategies consume *only* `view_for()`-shaped
dicts (the sim round-trips every view through `json.dumps/loads` before
handing it over) plus their own internal state. They never touch the `game`
dict. The Bluffer's "what am I hiding" is its *own card* — which it is
legitimately allowed to know.

### 3.2 Playing against them

```
python3 server.py                                # host the room as usual
python3 bot.py --url http://LAPTOP:3000 --code KFQTR --type ev
python3 bot.py --url http://LAPTOP:3000 --code KFQTR --type bluff
python3 bot.py --url http://LAPTOP:3000 --code KFQTR --type mix
python3 bot.py --url http://LAPTOP:3000 --code KFQTR --type noise
```

Each bot is an ordinary seat: pick the roles/deck/informed-count settings,
join the bots by name before or after humans (join order determines role in
assigned mode), and start the game. The host can kick them, turn on
anonymity, enable trials, change card values mid-game — the bots must cope
with all of it because they cope the same way humans do.

---

## 4. Type 1 — the pure EV Bot

**Philosophy:** the rational-risk-neutral benchmark. It never lies. Every
action maximizes immediate expected score given the posterior. It is the
"efficient market" of the room: its quotes and flow move the price toward the
true aggregate information, and it is systematically profitable against
players who leave money on the table — and systematically *readable* against
players who watch.

- **Taker policy:** §2.3 exactly, no caps, no hesitation. Big edge → big size,
  early, repeatedly until the edge is gone.
- **Quoter policy:** §2.4 exactly: honest center, `fee + λ` half-spread,
  small sizes, stand aside when the spread isn't worth offering.
- **Multi-day:** carries positions (zero-mean shocks), sharpens beliefs,
  trades more confidently day over day.
- **Investigations:** files an accusation only when EV-positive.
  `P(correct)` comes from the target's credibility score (§2.2.4) against the
  base rate of big movers (A/K) given `cardValues`. Files when
  `P · 0.5·|pts|·indemnityRate > (1−P) · falseAccusationFee` (the 0.5
  discounts for possibly sharing the indemnity). Otherwise abstains — a wrong
  accusation costs 6.
- **Forced orders:** §2.5, minimum-cost slicing.
- **Known weaknesses (by design):** quote center leaks `m` → leaks the card
  when `m` is far from the prior (e.g., holding the Ace, its quotes sit far
  below the table's expectation); large early directional flow leaks the
  card's sign; it reacts naively to bluffs (it updates on tape, so a
  convincing fake move shifts its `m`).

Tuning knobs: `κ` (tape gain), `λ₀`, `λ` EWMA `α` and cap, quote size
range, requote threshold, accusation discount.

---

## 5. Type 2 — the Bluffer (heavy)

**Philosophy:** information is the game. A big card is worth up to 40 points
of trading edge *plus* up to 20 points of investigation indemnity — but an
obvious big-card flow hands most of that away. The Bluffer pays real, bounded
losses on individual trades to (a) make its tape look like noise, and (b)
occasionally move the market with fake information and profit from the
followers.

It has two modes, chosen from its own card:

### 5.1 Mode A — CAMOUFLAGE (own card is a big mover, |pts| ≥ 20)

The card is valuable; conceal it.

1. **Sliced real flow.** The EV target flow (e.g., "short 30 units") is
   executed in slices of 1–4 at jittered intervals across the day, not in
   one dump.
2. **Cover trades.** On a fraction `f ≈ 0.35` of its action ticks (jittered),
   trade 1–2 units *against* its flow (buy a little while it needs to sell).
   Per-unit cost ≈ `|m − price| + fee` — a real loss, explicitly budgeted:
   total camouflage cost ≤ `camo_budget · |pts|` (default 0.4 → ~16 points on
   an Ace, ~8 on a King). When the budget is spent, cover trading stops and
   only slicing remains.
3. **Quote deception** (when its role allows quoting): quote center =
   `m ± δ` with `δ ~ Uniform(2, 5)` re-drawn per requote, or quote wide/far
   (looking casual) on ~30% of requotes, or stand aside irregularly. The
   honest-channel leak (§2.4) is deliberately broken.
4. **Mandates as cover.** If a forced order hits, its real activity hides
   inside the mandate's flow (mandate direction is public knowledge only in
   that *someone* has it — the bot's own mandate is visible only to it).
5. **Stop rule:** if the table shows no reaction at all (no one else is
   trading, tape dead), camouflage is unneeded — drop cover frequency to
   ~0.1. Camouflage is insurance against watchers.

### 5.2 Mode B — DECEPTION (small/no card)

No big card to hide, so fake having one, opportunistically and cheaply.

1. **Decoy bursts.** When the market is quiet (no trades in ~30s) and with
   frequency ≈ 1 per 60–120 s, initiate a directional burst of 2–5 units
   (market orders, or a crossing quote) as if acting on a big card.
   *Ride-or-fade:* if price moves ≥ 2 points in the burst's direction within
   the next ~5 trades (others bit), keep adding 1–2 units and then exit the
   position into the strength. If nobody reacts, cut after the first slice —
   never marry a decoy.
2. **Budget:** total decoy losses per day ≤ 12 points. Three consecutive
   non-reactive decoys → stop decoying for the rest of the day (this table
   isn't gullible).
3. **Everything else is EV-lite:** when not decoying, trade like the EV Bot
   but with small sizes (a "sharply dressed small fish" is more plausible
   than a whale posting 99).
4. **Accusation as misdirection:** if it has built a visible "big-mover
   signature" on some opponent (their flow matches a bull/bear), accuse *them*
   — a correct read that also steers suspicion away from the Bluffer's own
   odd flow. Otherwise abstain, except when strictly EV-positive.

### 5.3 What makes "bluff a lot" work instead of just bleeding

- **Small per-bluff size, high frequency.** One 5-unit opposite trade reads
  as "crazy"; five 1-unit opposite trades across the day read as "noisy
  player." The observer's posterior after any single opposite trade barely
  moves — that dilution *is* the camouflage.
- **Plausibility floor:** a bluff must look like something a small-informed
  player would do (sizes 1–2, prices near the book). Bluffs that are
  obviously dumb (crossing 20 points from mid) are skipped.
- **Budget discipline:** camouflage ≤ 40% of the card's points; decoys ≤
  12/day. The Bluffer is allowed to lose money on bluffs, never allowed to
  *bankroll* the room.
- **Honest accounting:** every bluff's cost is logged; at day end the Bluffer
  should show: card edge captured − camouflage cost ≥ EV Bot's edge × ~0.7.
  If the sim shows otherwise, the bluff rate drops (the parameter exists for
  this).

Known weaknesses: against pure EV opponents (who don't care what the tape
says about *them*), camouflage is pure cost; decoys against non-reactive
tables lose their budget; it is the first type a competent human starts to
fade once they recognize the pattern.

---

## 6. Type 3 — the Mixer

**Philosophy:** the two pure types are both readable — the EV Bot by its
honest quotes, the Bluffer by its noise fingerprint. The Mixer makes the
*classification itself* uncertain: day by day, and tick by tick, it is
sometimes the efficient trader and sometimes the bluffer. Its real product is
**edge that survives observation**: it keeps the EV Bot's flow target as a
backbone (so it never systematically abandons a good edge) and wraps it in
adaptive noise (so the table can't fit a clean model to it).

Mechanics:

1. **Daily blend draw.** At each day open draw `w ~ Beta(2, 2)` (mean 0.5,
   spread: sometimes 0.2-ish, sometimes 0.8-ish). `w` is the day's base
   EV-ness. Streaks happen (a 0.85 day, then a 0.3 day) — that's the point;
   a constant 0.5 is as predictable as a pure type.
2. **Edge modulation.** Per decision tick, with current edge
   `e = |m − mid|`:
   `p_EV = clip(0.1, 0.9, w + 0.35·tanh(e / 3))`.
   Big edge → mostly EV (don't throw away a 15-point edge on bluffs);
   no edge → the coin is close to the daily draw.
3. **The backbone.** The Mixer's *intended* net flow is always the EV
   backbone: position target = what the EV Bot would have done, executed in
   human-sized slices.
4. **The noise layer.** On ticks where the coin says "bluff", it draws from a
   *reduced* bluff menu: cover trades (size 1–2), quote-center offsets
   (±2–4), and *half-budget* decoys only. It never runs the Bluffer's full
   5-unit decoy bursts — the backbone can't be corrupted.
5. **Net-flow consistency ("bluff debt").** Track `debt` = signed sum of
   noise-layer flow since the day opened. When `|debt| > 3`, the next noise
   actions are biased to the *opposite* sign until debt is repaid. This
   guarantees the long-run observable flow ≈ the backbone, so opponents who
   correctly average out the noise still see the true direction — the Mixer
   gives up *timing* concealment to guarantee *total* correctness. (A pure
   Bluffer skips this; it doesn't care if its noise nets out, because it has
   no backbone to protect.)
6. **Credibility bookkeeping:** the Mixer also maintains the per-opponent
   credibility model and, when it sees an *EV Bot* (honest wide quotes far
   from prior), exploits it: trade *with* the EV Bot's flow direction while
   its quotes are still on the right side of the new `m` (the EV Bot's own
   information, rented).

Net effect vs the field: Mixer ≥ EV Bot against bluffers and humans
(concealment pays), Mixer ≤ EV Bot against other EV players (noise costs a
little), Mixer strictly more robust to any mix — the property you want from
the "default" computer opponent.

Note the difference from the Noise player (§7): the Mixer *wraps* an EV
backbone in noise — the direction of its flow is always the true edge, the
noise only hides the *timing*. The Noise player *replaces* its policy with
the public-info policy and lets the card tilt only inside the noise band —
it hides the *card itself*.

---

## 7. Type 4 — the Noise player (feigned-uninformed)

**Philosophy:** the other types assume the card is worth revealing *a bit*.
The Noise player's axiom is the opposite: **the best flow is flow that could
have come from nobody with a card.** It plays exactly the public-info policy
a true uninformed player would play, and lets its card act only as a silent
tilt on the *size and timing* of trades that public information already
justifies. Direction is never taken from the card.

Why it works: the `informedCount` mechanic makes uninformed orders
indistinguishable from informed ones — the game is built so that a no-card
player's tape *is* the camouflage a card-holder needs. An MM's
adverse-selection pricing (§2.4) is calibrated to exactly this flow, so a
Noise player's orders are treated as benign and filled at the prices the
market offers the uninformed. Against a flow-reading opponent (the EV Bot's
credibility model, §2.2.4), its signature carries zero information about its
card.

### 7.1 The public-info reference and the hidden tilt

The Noise player computes, and trades only from, the **no-card** estimate —
the no-card line of §2.1:

```
m_pub = P + k · S_pool / N
```

even when it holds a card. It never looks at `me.card` to choose a side.
What it *does* use its card for is the **hidden tilt** — how much its true
expectation is shifted relative to `m_pub` (exact, by subtracting the two
closed-form priors in §2.1):

```
t = (N − k) / (N − 1) · (c − μ_pool),   μ_pool = S_pool / N
```

At the defaults (N=23, k=4) this is ≈ −37 for an Ace, ≈ +15 for a King,
≈ 0 for a Q/J. Note the reference is the *pool mean*, not zero: an
uninformed player already expects you to hold an average card worth ~+2.6,
so a 5 of hearts is barely news. Every tilt rule below keys off `sign(t)`
and `|t|`, and `t = 0` when the player genuinely has no card — so the same
code path *is* the optimal policy for a real no-card player.

**Stealth invariant:** conditional on public information, the distribution
of the Noise player's flow does not depend on its card. Concretely:
(a) trade direction only from `m_pub` vs price; (b) sizes always drawn from
the uninformed envelope `{1,1,2,2,3}` — the card may only skew the draw
*within* the envelope; (c) timing from a fixed cadence the card may only
modulate by ±20%; (d) quote center always `m_pub ± (fee + λ)` — the card
never enters the quote.

### 7.2 The tilt (how it quietly extracts)

1. **Sizing tilt.** On a public-info trade whose direction agrees with
   `sign(t)`, skew the size draw toward the top of the envelope (P(size 3)
   up ~2×); when it disagrees, skew toward 1 — or drop the trade with
   probability proportional to `|t|/40`.
2. **Timing tilt.** When `sign(t)` agrees, act on the first qualifying
   price (pick off the top of the book); when it disagrees, wait one extra
   tick — by then the edge has usually closed and the trade is simply never
   made.
3. **Neutral-zone trades.** When `|m_pub − mid| < 1.5`, a true uninformed
   player mostly passes but occasionally trades one random unit. The Noise
   player does exactly that, with the "random" unit taken in the direction
   of `sign(t)` with probability `|t|/40`. A 1-unit coin-flip trade is
   indistinguishable from uninformed noise — this is the quietest channel,
   and the *only* channel in which the card may open a trade.
4. **Soft position cap.** The tilt may never push net position past ±5
   units (a casual uninformed player doesn't hold more); beyond that the
   tilt stops and only plain public-info trades continue.

### 7.3 As a quoter (mm role)

Quote at `m_pub ± (fee + λ)` with small sizes and infrequent requotes — a
casual market maker. The card is then **pure free optionality**: its true
expectation sits `t` above/below the quoted center, so every fill it takes
is quietly more (or less) +EV than a true uninformed MM's fill, without the
quote ever moving. It never widens or offsets quotes to exploit the card —
a moved quote is a leak.

### 7.4 Investigations

Abstains most of the time (~60% — uninformed players guess less). When it
does file, it uses only what an uninformed player could see: aggregate tape
and public positions (when anonymity is off) — no per-player deep
modeling. Files only when that aggregate read is strongly EV-positive. A
surgical accusation would be a tell: an "uninformed" player who keeps
accusing *the right person* is an informed player in disguise.

### 7.5 End of day

Nothing special: it never accumulates a big position, so overnight event
shocks and flash closes don't threaten it. It simply keeps playing the
public-info policy until the close.

### 7.6 The tradeoff, honestly stated

The constraint costs real money on a big card. Holding the Ace (−40): the
EV Bot shorts ~30 units and captures most of the 40; the Noise player can
only ride public-info sells and suppress public-info buys, expecting to
capture roughly **40–60% of the EV Bot's edge on a big card at ~zero
leakage**, and on a small card roughly a no-card player's score plus a small
hidden bonus. That is the deliberate bargain: less profit, and a card that
can't be read — which also means it can't be accused at trial with better
than random odds. It is the type to run when the room is watchful (trials
on, humans reading the tape) and the least exciting to watch.

Tuning knobs: sizing-skew factor (2×), timing modulation (±20%),
neutral-zone threshold (1.5) and probability scale (`|t|/40`), soft position
cap (5), trial abstention floor (0.6).

---

## 8. Evaluation: the in-process harness (`bot_sim.py`)

Fast iteration without a browser or a room. The sim drives the real
`engine.py` with a virtual clock, and hands every bot **only its
`view_for()` output** (json round-tripped) — the same anti-cheat invariant as
the live client.

Loop (500 ms virtual ticks):
1. `now += tick`; if a deadline crossed, `engine.on_deadline(game, now, rng)`.
2. For each bot (respecting its humanized cadence): `view = view_for(game,
   'player', pid, {'now': now, 'connections': …})` → `strategy.on_state` →
   `engine.submit_quote / market_order / cancel_quotes / file_accusation`.
3. Repeat until `phase == 'settled'`; record `settlement.rows`.

Rooms per trial (6 seats): all four bot types (EV, Bluffer, Mixer, Noise) +
2 **human sims** with deliberately simple policies (a random-sized taker
with 30% quote attempts at the prior midpoint; a fixed-spread naive MM).
This is the field a party table resembles; the isolated pairings below test
the interesting matchups directly.

**Baselines & head-to-heads:** each type also plays against (a) all-random,
(b) all-naive-MM, (c) mirror matches (EV vs EV, Bluffer vs Bluffer, Mixer vs
Mixer), and (d) **EV vs Noise** — the cleanest test of whether flow-reading
pays off at all in this game: the EV Bot's credibility model should *fail*
to detect the Noise player's card, and the Noise player should still finish
ahead on a big-card deal.

**Metrics, per 300+ seeded games across the settings grid:**

| Metric | What it measures | Expected order |
|---|---|---|
| mean total score | overall strength | Mixer ≈ EV ≥ Bluffer |
| win rate vs field | party experience | Mixer highest |
| leakage: `corr(quote center − prior, own card pts)` | how readable the card is via quotes | EV high, Mixer mid, Bluffer low |
| leakage: `corr(net flow sign, card sign)` | readability via tape | same |
| bluff cost / bluff count | discipline | Bluffer highest cost, must stay ≤ budget |
| induced impact per decoy | are bluffs *working*? | > 0 only vs reactive sims |
| indistinguishability: offline classifier "holds a big card" from flow+quotes | stealth quality | Noise ≈ base rate (chance), EV high, Mixer mid |
| accusation success & EV | trial play | all ≥ break-even |

Settings grid: `informedCount ∈ {0, n/2, n}`, `feePerUnit ∈ {0, 1}`,
trials off/on, anonymity off/on, `days ∈ {1, 3}`, event cards off/on.
An on/off anonymity check is the key robustness test: under anonymity the
credibility model (§2.2) must degrade gracefully to aggregate-flow only, and
the Bluffer's camouflage value should *drop* (nobody can link flow to a name
to begin with) while the Noise player should hold its ground or improve —
its flow was already unattributable.

Pass bar for v1: Mixer and EV Bot beat the best human sim by a clear margin
in mean score; Bluffer's mean score within ~30% of EV Bot's while its
leakage is at least half of the EV Bot's; Noise player's
indistinguishability score at chance level while it still finishes ahead of
a no-card human sim; no type loses its *budget* (bluff/camo cost overruns)
in any settings cell.

---

## 9. Parameter sheet (initial values, all tunable)

| Param | EV | Bluffer | Mixer | Noise | Meaning |
|---|---|---|---|---|---|
| `κ` tape gain | 0.10 | 0.10 | 0.10 | 0 (public prior only) | posterior pull per tape trade |
| `λ₀`, `λ cap` | 1+3ρ₀, 25 | same | same | same | adverse-selection premium |
| quote center | `m` | `m ± δ` | `m`, offsets on bluff ticks | `m_pub` (always) | quoted midpoint |
| quote size | 1–6 | 1–6 (offset/far 30%) | 1–6 (offsets on bluff ticks) | 1–3 | resting quote size |
| requote threshold | ½ spread | ½ spread | ½ spread | ½ spread | when to replace a quote |
| `f_cover` | — | 0.35 | on bluff ticks, size 1–2 | — | cover-trade frequency |
| camo budget | — | 0.4·\|pts\| | 0.2·\|pts\| | — | max camouflage loss per big card |
| decoy freq / size | — | ~1/90s, 2–5 | ~1/180s, 1–3 (half) | — | fake-big-mover bursts |
| decoy budget/day | — | 12 | 6 | — | max decoy loss per day |
| sizing-skew / timing-mod | — | — | — | 2× / ±20% | the card's tilt inside the noise band |
| neutral-zone threshold / scale | — | — | — | 1.5 / `\|t\|/40` | the quiet channel's opening and rate |
| bluff-debt cap | — | — (not tracked) | 3 | — | max signed noise-flow drift |
| soft position cap | — (unbounded) | — | — | ±5 | cap on tilted/accumulated flow |
| `w` daily draw | — | — | Beta(2,2) | — | day's base EV-ness |
| `p_EV` modulation | — | — | 0.1+0.35·tanh(e/3) around `w` | — | edge-aware blend |
| action cadence | 0.8–2.5 s | same | same | 1.0–3.0 s | humanized minimum interval |
| idle chance | 5% / 5–15 s | same | same | 8% / 5–20 s | humanized lulls |
| trial abstention | EV filter | EV + misdirection | EV filter | 0.6 floor + EV filter | when to file an accusation |
| accuse EV discount | 0.5 | 0.5 | 0.5 | 0.5 | assumed indemnity sharing |

---

## 10. Edge cases checklist

- **Role gating** — assigned mode: a bot that lands on `taker` runs only the
  taker policy (no quotes), one on `mm` only the quoter policy; bluffing for
  a taker-role bot happens through market-order cover trades, for an
  mm-role bot through quote deception. Sim must cover both seatings.
- **Anonymity on** — no name↔alias map for players: per-opponent
  attribution disabled; credibility model runs on the bot's own
  counterparty history (`me.fills` carries names… under anonymity it carries
  aliases, and the bot maps *its own* alias only). Degrade to aggregate flow.
- **No liquidity** — `market_order` raises when the side is empty; the bot
  must treat that as a no-op and re-evaluate next tick (or, as an MM/both
  player, use a crossing quote — remembering the remainder *rests*).
- **Two-sided quote requirement** — never "withdraw one side"; to stop
  offering a side, move it far (e.g., 1.01) with size 1, or cancel entirely.
- **Forced mandate against the edge** — slice all day, never dump at the
  close (leftover executes at any price).
- **Flash close** — 60 s: stop opening new noise, finish the backbone.
- **Host mid-game tweaks** — card values, fees, margins, days: recompute the
  prior from `settings` on every push; treat as the same channel as events.
- **Trial phase** — the bot must file (or explicitly abstain) before
  `trialSeconds` expires; if it's been silent all day and is holding a big
  card, *abstaining* is the safer tell-reduction (filing is optional).
- **Token lifecycle** — SSE `bad-token` → rejoin by name → `taken`
  + `canClaim` → `claim`. Server restarts preserve tokens (snapshots
  persist `room.tokens`); room expiry (`no-room`) → exit.
- **Kicked / reset** — `{"error": "kicked"}` / `{"error": "reset"}` → clean
  shutdown, log final state.
- **Rate limits** — joins are 30/min/IP; the bot joins once and never
  rejoins spuriously (claim only on bad-token).
- **Multiple bots, one room** — they are separate players with separate
  names/tokens; no self-trading is possible across them, but the bot must
  not assume its counterparties are bots (it can't know).
- **Noise player with `informedCount = 0`** — its tilt is exactly zero and
  it becomes the optimal no-card player; a correctness reference the sim can
  assert (its score should track the human sims' uninformed baseline).

---

## 11. Out of scope (v1) and roadmap

**Out of scope for v1:** any server modification; learning from past games;
per-bot network models; exploiting the host API; anything that reads the
`game` dict.

**Roadmap:**
1. **v1** — this doc: closed-form belief, the four policies, sim harness,
   tuned parameters, pass bar from §8.
2. **v2** — per-opponent learned noise (each human gets a personal `τ`, so
   the tape weight in §2.2.4 adapts to *who* is at the table); full
   Bayesian posterior (the prior is conjugate; the tape likelihood can be a
   proper normal per aggressor); an offline **indistinguishability
    classifier** on the sim's flow logs (a simple, non-learning feature score
    over flow persistence, quote-center deviation from the public prior,
    size distribution) — the number the Bluffer and Noise players are tuned
   against; bluff calibration from measured table reactivity (if the sims
   show decoys work 2× better than assumed, the Bluffer's budget relaxes —
   the sim numbers drive this).
3. **v3** — replay learning: feed recorded sessions (the board's trade tape
   is already public) into a small model that predicts per-player informed
   status; possibly an RL fine-tune of the Mixer's noise layer. Only after
   v1/v2 show the hand-tuned policies are near their practical ceiling.

**Open questions for the sim to answer (not to guess at):**
- Does the Bluffer's camouflage value survive against the *Mixer* (which
  also makes per-player flow ambiguous)?
- How much does the EV Bot's quote honesty *help* the room as a whole (price
  efficiency at settlement) even while it loses to the Bluffer?
- Is the Noise player's 40–60% big-card capture estimate right, and does it
  hold when the *other* bots are all running the same stealth logic (a
  table full of Noise players has no flow to read at all)?
- Under `informedCount = 0` (pure common knowledge), all four types should
  converge to the same public-info policy — a good internal consistency
  test (the Noise player is that policy exactly).
