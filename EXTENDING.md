# Hosting, customizing the exchange & the informed-trader design

This is the operator/developer companion to [README.md](README.md). It covers three
things: putting the game on the web, changing the exchange's rules, and the design for
**informed vs. uninformed traders** (a separate axis from market maker vs. liquidity
taker — built in as the `informedCount` host setting; see section 3).

---

## 1. Web hosting

### Sharing the software itself (no hosting at all)

For local-wifi play, "distribution" just means giving someone the folder: ZIP it or
point them at the repo, they double-click `Start Trading Game.command` (macOS) or
`start-trading-game.bat` (Windows), and their laptop becomes the exchange. The repo
ships with an MIT `LICENSE`, a `.gitignore`, and a GitHub Actions workflow that runs
the full test suite on Linux/macOS/Windows across Python 3.9–3.13 on every push.
Everything below is only for putting a copy on the actual internet.

### What the app needs from a host

The architecture dictates the deployment shape, so read this first:

- **One process, one instance — many rooms.** The server hosts any number of
  independent rooms (each a 5-letter code, its own game and host key), but all of them
  live in the memory of a single `server.py` process (with per-room JSON snapshots on
  disk). Never run 2+ replicas or workers behind a load balancer — players would land
  on different copies of their room.
- **Always-on.** Clients hold open Server-Sent-Events connections and phase timers run
  server-side. "Scale-to-zero" / free tiers that sleep on idle will pause games
  mid-round. Use an always-on tier.
- **Plain HTTP/1.1, no WebSockets.** Any reverse proxy works if it doesn't buffer
  streaming responses (see proxy notes). The app heartbeats every 15s, so ordinary
  60s idle timeouts are fine.
- **HTTPS strongly preferred** for anything off your LAN (phones trust it, and the
  screen wake-lock feature only works in secure contexts).
- **Capacity:** one thread per connected client; comfortable to a few hundred
  concurrent connections across all rooms (the default caps below stop it well before
  it hurts). It is a party/classroom game, not a hardened public service.
- **Housekeeping is built in:** rooms with nobody connected expire (~2 h idle, ~30 min
  once settled or never used), snapshots are deleted with them, and room creation /
  joining is rate-limited per IP.

### Environment contract

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 3000 (tries 3000–3010; `0` = random) | listen port |
| `JOIN_URL` | `http://<lan-ip>:<port>` | public base URL used in every join/host link — set it to your domain in production |
| `STATE_DIR` | `./state` | per-room snapshots (`<CODE>.json`); put it on a persistent volume if you want rooms to survive restarts |
| `TRUST_PROXY` | off | set `1` behind a reverse proxy so rate limits key on the **rightmost** `X-Forwarded-For` hop (the one your proxy appended — spoof-proof) instead of the proxy's IP |
| `MAX_ROOMS` | 40 | concurrent rooms; creation returns 503 beyond this |
| `MAX_CLIENTS` / `MAX_CLIENTS_PER_ROOM` | 400 / 120 | SSE connection caps (server-wide / per room) |
| `RATE_CREATES_PER_MIN` / `RATE_JOINS_PER_MIN` | 5 / 30 | per-IP rate limits (0 disables); direct loopback traffic (the operator's browser, or a whole classroom behind a cloudflared/ngrok tunnel) is exempt |
| `ROOM_TTL_MINUTES` / `SETTLED_TTL_MINUTES` | 120 / 30 | idle lifetimes for live rooms / finished-or-empty rooms |
| `--fresh` (flag) | — | discard all saved room snapshots on boot |
| `--open` (flag) | — | open the landing page in the default browser after startup (what the double-click launchers pass) |

**Host auth is per room.** Creating a room returns its host key; the creator's browser
stores it and is let straight into `/r/CODE/host`. From any other device, use the
room's *Copy host link* button (the link carries `?key=…`). There is **no**
localhost/operator bypass: the machine the server runs on has no special powers over
rooms, which is exactly what you want on a shared public box.

### Option A — tunnel from your laptop (best for a one-off session)

No hosting at all; your laptop stays the server:

```bash
brew install cloudflared
python3 server.py &
cloudflared tunnel --url http://localhost:3000
# cloudflared prints https://<random>.trycloudflare.com — restart server.py with:
JOIN_URL=https://<random>.trycloudflare.com python3 server.py
```

Pros: free, instant, HTTPS included, works for Zoom-remote players. Cons: your laptop
must stay awake and on the network; the URL changes every run. (ngrok or
`tailscale serve` work the same way.) Rooms created before the `JOIN_URL` restart show
stale links — create rooms after it.

### Option B — container platform (Fly.io / Railway / Render / anything)

The app containerizes in five lines — no dependencies to install:

```dockerfile
# Dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY server.py engine.py ./
COPY public ./public
CMD ["python3", "server.py"]
```

**Fly.io example** (cheap, single small VM, persistent volume):

```toml
# fly.toml
app = "gm-trading-game"

[build]

[http_service]
  internal_port = 3000
  force_https = true
  auto_stop_machines = "off"    # IMPORTANT: never sleep (SSE + timers)
  min_machines_running = 1

[env]
  PORT = "3000"
  STATE_DIR = "/data/state"
  TRUST_PROXY = "1"
  JOIN_URL = "https://gm-trading-game.fly.dev"

[mounts]
  source = "gmdata"
  destination = "/data"
```

```bash
fly launch --no-deploy          # then paste the toml above
fly volumes create gmdata --size 1
fly deploy && fly scale count 1 # exactly one machine
```

Platform gotchas: **Render's free tier sleeps on idle** (breaks the game — use a paid
always-on instance and attach a persistent disk for `STATE_DIR`); Railway is fine on
any always-on plan (set the env vars in the dashboard, keep 1 replica).

### Option C — small VPS or an internal VM (systemd + Caddy)

The most controllable option, and the right shape for a company-internal box:

```ini
# /etc/systemd/system/trading-game.service
[Unit]
Description=Glosten-Milgrom trading game
After=network.target

[Service]
WorkingDirectory=/opt/trading-game
ExecStart=/usr/bin/python3 /opt/trading-game/server.py
Environment=PORT=3000
Environment=JOIN_URL=https://game.example.com
Environment=STATE_DIR=/var/lib/trading-game
Environment=TRUST_PROXY=1
DynamicUser=yes
StateDirectory=trading-game
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```caddyfile
# /etc/caddy/Caddyfile — Caddy terminates TLS automatically and streams SSE correctly
game.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

`systemctl enable --now trading-game caddy` and you're live. On an internal VM without
public DNS, skip Caddy and point people at `http://<vm>:3000` directly.

If you use **nginx** instead of Caddy, streaming needs:

```nginx
location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # SSE must not be buffered
    proxy_read_timeout 1h;
}
```

### Go-live checklist

- [ ] Exactly **one** instance/replica, on an **always-on** tier
- [ ] `JOIN_URL` set to the public URL (it's what every join/host link is built from)
- [ ] `TRUST_PROXY=1` if a reverse proxy or platform edge sits in front
- [ ] `STATE_DIR` on a persistent volume *if* you want rooms to survive restarts (optional)
- [ ] HTTPS in front (platform or Caddy)
- [ ] Point an uptime monitor at `/healthz` (reports room/client counts)
- [ ] Deploys drop in-memory rooms not yet snapshotted — prefer deploying when
      `/healthz` shows few rooms
- [ ] Remember anyone with the URL can create rooms and join them — that's the point,
      but the per-IP rate limits and `MAX_ROOMS` are your abuse valves

---

## 2. Customizing the exchange rules

### Where things live

The separation of concerns is strict, which is what makes rule-hacking safe:

- **`engine.py`** — every game rule, as small functions over one JSON-able `game` dict.
  No I/O, no clocks (`now` and `rng` are always passed in). *This is the only file you
  touch to change how the market works.*
- **`server.py`** — transport only: HTTP, SSE fan-out, the room registry, phase
  timers, snapshots, auth, rate limits. It calls engine functions under a lock and
  never interprets rules. Every room is an independent game dict, so rule changes in
  `engine.py` apply to all rooms alike.
- **`public/app.js`** — rendering only; it draws whatever state the server pushes.
  You touch it only when a new rule needs a knob or a display.
- **`tests.py`** — run `python3 tests.py` after *every* rule change; the matching,
  conservation, and privacy tests are the safety net.

### Rule → location map

Several of these are **already host settings** — no code needed (see the README
settings table): per-unit **fee**, **anonymous trading**, **card point values**
(A/K/Q/J), **timers**, and the **informed count**. The map below covers those plus the
rules that are still one-line code edits:

| Rule you want to manipulate | Where | What to edit |
|---|---|---|
| Card point values (A=−40, K=+20, …) | **built in** — host setting `cardValues` | `engine.card_points()` if you want to touch number cards / off-suits too |
| Per-trade transaction cost *c* (the deck's Props. on costs) | **built in** — host setting `feePerUnit`, applied in `engine._apply_trade()` | split maker/taker instead of symmetric, rebate models, … |
| Anonymity (hide who quoted/traded) | **built in** — host setting `anonymous`; pseudonyms assigned in `start_game()`, applied in `view_for()` | e.g. anonymize the quote checklist too |
| Round timers / manual advancement | **built in** — settings `quoteSeconds` / `marketSeconds` (0 = manual), live-tunable | `engine.ALL_IN_GRACE_MS` for the 5s all-quotes-in grace |
| Who gets information | **built in** — host setting `informedCount` (see section 3) | |
| What counts as a cross (`bid ≥ ask` vs. strict `>`) | `engine.reveal()` | the `if b['price'] < a['price']: break` comparison |
| Trade price (at the ask vs. midpoint) | `engine.reveal()` | the `price=a['price']` passed to `_apply_trade` |
| Priority / tie-breaking | `engine.reveal()` | the two `sort(key=…)` lines (`price`, then `-size`, then random) |
| Tick size / price & size limits | `engine.MAX_PRICE`, `MAX_SIZE`, `round2`, `_num`, `_size` | validation & rounding |
| Self-cross / self-trade prohibitions | `engine.submit_quote()` (ask>own bid), `engine.market_order()` (skips own orders) | the checks |
| Two-sided quote requirement / one-sided quotes | `engine.submit_quote()` validation + how `reveal()` builds `bids`/`asks` | allow size 0 on one side |
| Quotes firm vs. cancelable in the market phase | add a `cancel_quote()` engine function + a host/player action in `server.py`/`app.js` | new feature |
| Book carried across rounds vs. wiped | `engine.end_market()` (clears), `next_round()` | keep the book instead of clearing |
| Number of public cards, deal pools | `engine.start_game()` | the dealing block |
| Scoring formula | `engine.settle()` | `total = cash + pos*V` |

Classroom playbook with the built-in knobs: run a clean baseline game; rematch with a
**fee** and watch spreads widen and prices oscillate (negative serial correlation — the
deck's transaction-cost proposition); rematch with `informedCount` well below the
player count and watch the market maker's adverse-selection problem appear for real;
turn on **anonymity** and see how much harder inference from the tape gets; change an
**ace's value** between rounds as a public news shock and watch the repricing.

### Recipe: promoting a rule to a host-controllable setting

The shipped `feePerUnit` setting is the worked result of this recipe — read its five
touch points in the code as the reference example. The same steps apply to any knob:

1. **Default** — `engine.create_game()`: add the key to `settings`
   (see `'feePerUnit': 0`).
2. **Validate** — `engine.set_settings()`: add a clause (range-check it; decide whether
   it is *lobby-only* — anything that affects dealt cards/roles must be, like
   `informedCount` — or *live-tunable* like the timers, fee, anonymity and card
   values). Note the whole function validates-then-commits: on any error it restores
   the previous settings.
3. **Apply** — wherever the rule bites (the fee lives in `engine._apply_trade()`, which
   also accrues `feesCollected` so settlement can explain why totals no longer sum to
   zero).
4. **Expose** — `app.js`: add the input to the lobby settings form and/or
   `liveTweaksForm()`, read it in the `settingsform` submit handler (it reads only the
   fields present in the active form), and surface it in `settingsLine()` so players
   see the rule in force.
5. **Test** — add a case to `tests.py` (validation + effect; see
   `test_fee_and_anonymous`), then `python3 tests.py`.

### Invariants to keep (whatever you change)

- **Conservation:** positions sum to 0 and cash sums to 0 across a trade (unless you
  deliberately add fees) — the tests enforce this.
- **Privacy:** private cards must never appear in board/host views or in other
  players' payloads. `test_view_privacy` guards this; extend it when you add fields.
- **Purity:** engine functions take `now` and `rng` as arguments — no `time.time()` or
  bare `random` inside `engine.py`, or tests stop being deterministic and snapshot
  resume breaks.
- **Serializability:** the `game` dict must stay `json.dumps`-able (no sets, objects,
  tuples-as-keys) or snapshots and SSE break.
- **Locking:** any new server endpoint mutating a room must do so inside `with LOCK:`
  and finish with `touched(room)`.

### Snapshot compatibility

Each `state/<CODE>.json` is a dump of that room's game dict. After changing the state
*shape*, old snapshots may lack your new keys — either read them defensively
(`settings.get('feePerUnit', 0)`) or just restart with `--fresh` after deploying a
rules change. Mid-game rule upgrades are not worth supporting.

---

## 3. Informed vs. uninformed traders

> **Status: implemented.** One deliberate change from the first draft of this spec: an
> uninformed player is simply **not dealt a card at all** (they hold nothing, worth 0)
> rather than holding a hidden one. V is the public cards plus only the *k* dealt
> private cards.

### Concept

In Glosten–Milgrom, what matters is that *some* arrivals know more about V and the
market maker cannot tell which. With every player holding a card, everyone is equally
informed. The `informedCount` setting fixes that:

- **Information is orthogonal to role.** A player is (market maker | taker) ×
  (informed | uninformed); the two axes are assigned independently.
- Only *k* randomly chosen players are dealt a private card; those cards (plus the
  public ones) make up V.
- An **informed** trader sees their own card, as usual.
- An **uninformed** trader gets an explicit "no card this game — it counts 0" notice
  on their phone, and trades on public cards and order flow alone.
- **The count k is public** (settings line, board, host log) — it has to be, because
  E[V] depends on how many hidden cards exist. **The identities are secret**, so a
  no-card player's order is indistinguishable from an informed one.

This produces real adverse selection: a market maker quoting against a mixed crowd
cannot tell whether the order hitting them is informed — which is precisely
Propositions 1–5 territory. (Defaulting `informedCount` to "everyone" reproduces the
original game exactly, and `informedCount = 0` gives a pure common-knowledge baseline
round.)

### Host setting & assignment (as built)

- Lobby setting **"Players dealt a card"** (`informedCount`): blank = everyone
  (the default — reproduces the original game exactly), or an integer `0…49`.
- Assignment happens **once, at the deal** (`start_game`), uniformly at random among
  active players using the injected `rng` (deterministic in tests). Re-randomized on
  every rematch. Not reassigned on kick/disconnect (if an informed player leaves, the
  effective count just drops).
- With a limited *k*, the deck stops capping the head count: a hearts/spades game can
  seat up to 49 players as long as `k ≤ 23`. `set_settings` cross-validates all of
  this and rolls back on any error.
- **Possible later variant: "classic GM"** — restrict informed status to liquidity
  takers, so market makers are always uninformed, as in the paper.

### Who sees what (the secrecy rules — the important part)

| Audience | During play | At settlement |
|---|---|---|
| The player themself | informed: their card; uninformed: an explicit "no card — counts 0" notice | everything |
| Other players / board | **nothing** — no badges, no per-person hints | card (or —) per row + group averages |
| Host panel | **nothing per-person** (the host projects their screen; don't tempt fate) | everything |
| Settings line (public) | aggregate only: "Cards: 3 of 12 players (who — secret)" | — |

Rationale: if anyone can see who is informed, the adverse-selection lesson collapses —
you'd just fade the informed players' quotes. The privacy tests in `tests.py` assert
that no `informed`/`card` fields appear in board, host, or other-player payloads.

### Settlement payoff (as built)

The results table shows each player's card — or "—" for no-card players — and when the
groups are mixed, a comparison line: average score of card holders vs. no-card
players, the live reproduction of the deck's "payoff by aces exposed" histogram
punchline. `settlement.groups` carries `{informed: {n, avgTotal}, uninformed: …}` if
you want to chart it.

### Where it lives (as built)

| File | What |
|---|---|
| `engine.py` `create_game` / `set_settings` | `settings['informedCount']` (None = everyone); lobby-only, cross-validated against deck & head count with rollback |
| `engine.py` `start_game` | `rng.sample` picks the k card holders; everyone else gets `card = None`; pseudonyms (`Trader n`) assigned here too |
| `engine.py` `view_for` | `me['informed']`; an uninformed player's JSON never contains a card; board/host player lists carry no informed flags |
| `engine.py` `settle` | rows gain `informed` (= holds a card); `groups` averages; all players appear, card or not |
| `engine.py` `rematch` | clears `informed`/`alias` for a fresh random draw next deal |
| `app.js` | lobby settings input; `settingsLine` shows "Cards: k of n"; `meCardPanel` no-card notice; `settlementHTML` card column + group line |
| `tests.py` `test_informed_axis` | exact k for a seeded rng; k=0 and k=None edges; view privacy; V counts only dealt cards; group math; capacity interplay; lobby-only enforcement |

No `server.py` changes were needed — the setting flows through the generic
`settings` host action. Migration: pre-feature `state.json` snapshots settle fine
(card presence is the source of truth); when in doubt, restart `--fresh`.

### Variants worth considering

- **Face-down cards that still count:** the first draft of this spec — uninformed
  players hold a card that counts toward V but is hidden even from them. Keeps V's
  distribution independent of k; costs the clean "no card = weight 0" story.
- **Insider tiers:** informed traders additionally peek at N *other players'* cards
  (peeking at undealt cards is useless — they don't count toward V). Turns "informed"
  into a dial rather than a switch: signal quality maps to Proposition 5's
  "finer information → wider spreads".
- **Noisy signals:** tell informed traders `V ± ε` instead of card identities — closer
  to the paper, less card-flavored.
- **Paid information:** auction off informed slots pre-game (score deduction) — price
  of information becomes endogenous.
