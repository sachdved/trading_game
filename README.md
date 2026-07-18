# The Glosten–Milgrom trading game

A party-sized market game about trading with imperfect information, played from phones
on the same wifi. One laptop **is the exchange**: it deals the cards, runs a live
price-time-priority order book with continuous trading, tracks everyone's cash and
positions, and settles the game — nobody has to keep an order book by hand.

Nothing to install beyond Python: 100% standard library (3.9+), one folder, no build.

## Play it with friends (2 minutes)

One person hosts on a laptop; everyone else joins from their phone **on the same wifi**.

1. **Get Python 3** if you don't have it — most Macs and Linux boxes already do.
   Otherwise: [python.org/downloads](https://www.python.org/downloads/) (Windows:
   tick *"Add python.exe to PATH"*).
2. **Get this folder** — download the repo ZIP (or have a friend send it) and unzip.
3. **Start it:**
   - **macOS:** double-click `Start_Trading_Game.command` (first time: right-click →
     *Open*, since it's a downloaded script)
   - **Windows:** double-click `start-trading-game.bat`
   - **any terminal:** `python3 server.py`
4. Your browser opens the **game page** by itself (launchers only — otherwise open
   `http://localhost:3000`). Click **Create a room** — you land in the host panel and
   get a 5-letter **room code** (say `KFQTR`). If the OS asks to allow incoming
   connections, allow it.
5. Put the **board view** on the TV/projector — it shows the join address (with the
   room code) in huge type; players type it into their phones and enter a name.

The server can host **many rooms at once** — friends can even run their own game in a
second room on the same laptop. The URLs:

| URL | Who opens it |
|---|---|
| `http://<laptop-ip>:3000` | The **landing page** — create a room, or join one by code |
| `http://<laptop-ip>:3000/r/KFQTR` | **Players**, on their phones (same wifi) |
| `http://<laptop-ip>:3000/r/KFQTR/board` | **TV / projector** — public info only, big type |
| `http://<laptop-ip>:3000/r/KFQTR/host` | **The host.** The browser that created the room is let in automatically; from another device use the room's *Copy host link* button (it carries `?key=…`) |

> **Wifi gotchas:** everyone must be on the *same* network, and guest/hotel/corporate
> wifi often blocks phone-to-laptop traffic ("client isolation"). If phones can't load
> the join page, start a phone hotspot and put the laptop + everyone on it — works
> every time.

## Running a session (host script)

1. Create a room, open its host panel, and put the room's board view on the projector.
2. In the lobby, pick settings (defaults are sensible) and flip anyone's role if you like.
3. **Deal cards & open the market.** Everyone sees the 3 public cards; each player sees
   their own private card on their phone.
4. **Trading is live and continuous** (default: one 5-minute day). Market makers post —
   and re-post, and pull — two-sided quotes; takers hit the bid / lift the ask. Anything
   that crosses trades the instant it arrives; fills pop up on both phones.
5. If you configured multiple **days**, each day ends with the book wiped overnight
   (positions and cash carry). Click **Open day N+1** when everyone's ready.
6. After the last day (clock, or **Close & settle**) the board shows the podium, V, and
   every score.
7. **Rematch** keeps the same players and re-deals.

The host never enters a single order — the market runs itself; you just open and close
the days (automatic when the day clock is on). `+30s` and close-now buttons override it.

## The rules, as implemented

- Three public cards are dealt from hearts + spades; by default each player also gets
  one private card (from hearts/spades, or the full deck — clubs and diamonds are
  worth 0).
- **Informed vs. uninformed (optional):** the host can deal private cards to only *k*
  randomly chosen players. Everyone knows *k*; nobody knows *who*. No-card players
  hold nothing (worth 0) and trade on public information and order flow alone — and
  since their orders look identical to informed ones, market makers face true adverse
  selection. Settlement reveals who held cards and compares average scores.
- **Card points** (hearts & spades), by default: Ace = **−40**, King = **+20**,
  Queen & Jack = 0, others = face value. Clubs & diamonds = 0. A/K/Q/J values are
  host-adjustable.
- Trading runs in one or more continuous **days** (host setting). Within a day, market
  makers keep a live two-sided quote: bid / bid size / ask / ask size. Prices must be
  strictly positive and your ask must be above your own bid (max price 999.99, sizes
  1–99). Re-posting **replaces** your previous quote; you can also pull your quotes.
- **Crossing on arrival:** the book is a standard price-time-priority limit-order book.
  If an incoming bid meets a resting ask (bid ≥ ask), it trades immediately **at the
  resting order's price** — stale quotes get picked off, exactly like real markets.
- **Market orders** fill against the best resting price(s), first come first served; an
  order bigger than the best level walks down the book at each level's own price; you
  never trade with yourself.
- **Overnight:** when a day ends, outstanding forced orders execute against what's left
  of the book, all resting orders are canceled, and margin interest (if set) is charged
  on negative cash. Positions and cash carry into the next day's empty book.
- **Event cards (optional):** public news drawn at day opens or by the host — value and
  fee shocks, dividends and levies, flash closes — plus private forced orders that
  inject noise-trader flow nobody can attribute.
- **Settlement:** V = sum of the points of **all** dealt cards (public + every private
  card actually in play). Score = cash from filled orders + net position × V. Shorting
  and negative scores are allowed; the game is zero-sum, minus any exchange fees the
  host enabled (the settlement screen accounts for the exchange's take).

### Departures from the original deck

The classroom deck runs sealed-bid rounds with a call auction at a reveal; this app
implements the **continuous** version instead: quotes are live and repriceable, crossing
orders trade instantly at the resting price, and a session can span several trading days
with the book wiped overnight. Other conventions kept from standard exchanges: bids
**equal** to an ask do cross (price = the resting ask), oversized orders walk the book
rather than being rejected, and in "assigned roles" mode market makers quote while
takers take — use "everyone" mode (good for small groups) to let all players do both.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Roles | assigned (alternating) | or "everyone quotes and takes" |
| Private-card deck | hearts & spades | "full deck" for more cards in play |
| Trading days | 1 | run several continuous sessions back-to-back — the book is wiped overnight, positions carry. Can be raised mid-game ("overtime") |
| Day clock | 300s | length of each trading day; 0 = the host closes days manually; changeable mid-game (applies from the next day) |
| Max players | deck limit | host cap on seats in this room (2–49); the deck still caps it when lower. Server-wide caps (rooms, connections) are env vars — see EXTENDING.md |
| Margin rate | 0 %/day | negative cash is a margin loan: charged this rate at every day close; the take is reported at settlement like fees |
| Event cards | off | one card auto-draws each time a later day opens, plus a host **Draw event** button: value shocks, fee changes, dividends/levies, forced anonymity, flash closes — and private **forced orders** (one trader must buy/sell before the close; unfilled parts execute automatically) |
| Players dealt a card | everyone | set *k* to create **informed vs. uninformed** traders: k random players get a card, the rest get nothing (worth 0). The count is public; *who* is secret — even from the host. Settlement compares the groups' average scores. |
| Exchange fee per unit | 0 | charged to **both** sides of every fill and kept by the exchange; flip it on between rounds and watch spreads widen |
| Anonymous trading | off | book, tape and standings show stable pseudonyms (Trader 1, 2, …) until settlement; the host still sees real names |
| Card points (A/K/Q/J) | −40 / +20 / 0 / 0 | host-editable, even mid-game as a "news shock"; number cards stay face value |

Fee, anonymity, margin rate, event cards, the day count/clock and card points are also
editable **mid-game** from the host panel ("Live rule tweaks"). Roles, deck, player cap,
and the informed count are fixed once cards are dealt. Active non-default rules always
show in the settings line that players and the board see.

## Practical notes

- **Reconnects:** phones can sleep/refresh freely — sessions resume automatically. If
  someone's device dies, they can rejoin from a new device with the same name ("resume
  seat") — even if the dead device still *looks* connected. The seat moves to the new
  device; the old one (if actually alive) is told and can resume it right back.
- **Crash-safe:** every room auto-saves to `state/<CODE>.json`; restarting
  `python3 server.py` resumes all rooms mid-game (timers included). Start over with
  `python3 server.py --fresh`, or a room's *Reset game* button.
- **Rooms expire** after ~2 hours with nobody connected (~30 min once settled or if
  never used) — so a public server tidies up after itself.
- **Late arrivals** can't join after the deal (V is fixed by the dealt cards) — they can
  watch the board view.
- **Kicking** someone mid-game keeps their dealt card in V (the card was dealt), pulls
  their resting orders, and freezes their P&L.
- **Remote players:** the join URL only works on your network. For remote folks,
  tunnel it: `brew install cloudflared && cloudflared tunnel --url http://localhost:3000`
  (or ngrok / Tailscale) and set `JOIN_URL=https://…` when starting the server so
  boards show the right links. For real web hosting — your own always-on server where
  anyone can create a room, Among Us-style — see **[MULTIROOM.md](MULTIROOM.md)**.
- Env vars: `PORT` (default 3000, tries 3000–3010), `JOIN_URL`, `STATE_DIR`, plus the
  operator knobs in [EXTENDING.md](EXTENDING.md) (rate limits, caps, TTLs,
  `TRUST_PROXY`).
- Each room's host key is its only admin credential — the creator's browser stores it
  automatically; share it only via the *Copy host link* button.

## Sharing it with friends

Send them this folder (ZIP it, or point them at the repo) — that's the whole install.
Everything above the rules section is written for someone who has never seen a
terminal; the launchers cover Mac and Windows. MIT licensed (see `LICENSE`), so copy,
tweak, and re-share freely. If you put it on GitHub, the included workflow
(`.github/workflows/tests.yml`) runs the test suite on Linux/macOS/Windows and on
Python 3.9 and 3.13 on every push.

## Tests

```bash
python3 tests.py    # engine unit tests + a full game driven over HTTP
```

## Hosting it, changing the rules, informed traders

- **[MULTIROOM.md](MULTIROOM.md)** — put it on the internet as a multi-room service
  (anyone opens your URL, creates a room, plays with friends), with step-by-step
  deployment for Fly.io and a VPS.
- **[EXTENDING.md](EXTENDING.md)** — the operator env-var contract, deployment
  recipes, a map of where every exchange rule lives so you can manipulate the
  mechanics, and the informed vs. uninformed trader mechanic (built in as a host
  setting).

## Files

```
server.py                    multi-room HTTP + Server-Sent-Events server, timers,
                             room registry, rate limits, per-room persistence
engine.py                    game rules: dealing, matching, market orders, scoring
public/                      the web client (landing / player / host / board views)
tests.py                     257 checks: matching, scoring, privacy, rooms, reaper,
                             rate limits, seat takeover, full-game HTTP run
Start_Trading_Game.command   macOS double-click launcher
start-trading-game.bat       Windows double-click launcher
Dockerfile                   optional, for container hosting (see MULTIROOM.md)
state/                       per-room snapshots (created at runtime, gitignored)
```
