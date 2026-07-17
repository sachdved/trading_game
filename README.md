# The Glosten–Milgrom trading game

A party-sized market game about trading with imperfect information, played from phones
on the same wifi. One laptop **is the exchange**: it deals the cards, collects quotes,
runs the call auction, fills market orders in time priority, tracks everyone's cash and
positions, and settles the game — nobody has to keep an order book by hand.

Nothing to install beyond Python: 100% standard library (3.9+), one folder, no build.

## Play it with friends (2 minutes)

One person hosts on a laptop; everyone else joins from their phone **on the same wifi**.

1. **Get Python 3** if you don't have it — most Macs and Linux boxes already do.
   Otherwise: [python.org/downloads](https://www.python.org/downloads/) (Windows:
   tick *"Add python.exe to PATH"*).
2. **Get this folder** — download the repo ZIP (or have a friend send it) and unzip.
3. **Start it:**
   - **macOS:** double-click `Start Trading Game.command` (first time: right-click →
     *Open*, since it's a downloaded script)
   - **Windows:** double-click `start-trading-game.bat`
   - **any terminal:** `python3 server.py`
4. Your browser opens the **host panel** by itself (launchers only — otherwise open
   `http://localhost:3000/host`; no key needed on the hosting computer). If the OS asks
   to allow incoming connections, allow it.
5. Put the **board view** on the TV/projector — it shows the join address in huge type;
   players type it into their phones and enter a name.

The three URLs (also printed in the terminal):

| URL | Who opens it |
|---|---|
| `http://<laptop-ip>:3000` | **Players**, on their phones (same wifi) |
| `http://<laptop-ip>:3000/board` | **TV / projector** — public info only, big type |
| `http://localhost:3000/host` | **The host**, on the hosting laptop (key-free there); from another device use the `/host?key=…` link from the terminal or the host panel |

> **Wifi gotchas:** everyone must be on the *same* network, and guest/hotel/corporate
> wifi often blocks phone-to-laptop traffic ("client isolation"). If phones can't load
> the join page, start a phone hotspot and put the laptop + everyone on it — works
> every time.

## Running a session (host script)

1. Open the host panel and put `/board` on the projector.
2. In the lobby, pick settings (defaults are sensible) and flip anyone's role if you like.
3. **Deal cards & start round 1.** Everyone sees the 3 public cards; each player sees
   their own private card on their phone.
4. **Quote phase** (default 90s): market makers submit bid / bid size / ask / ask size.
   It auto-reveals 5 seconds after the last quote is in, or when the timer ends.
5. **Reveal:** the call auction matches crossing quotes automatically and posts the
   trades to the tape. Leftover quotes become the live order book.
6. **Market phase** (default 120s): takers hit the bid / lift the ask from their phones,
   filled first-come-first-served. Fills pop up as notifications for both sides.
7. When the round closes, click **Start round N+1** (typically 3–5 rounds total),
   or **Settle & reveal cards** — the board shows the podium, V, and every score.
8. **Rematch** keeps the same players and re-deals.

The host never enters a single order — you only advance the phases (and even that is
automatic if the timers are on). `+30s` and "end now" buttons override the clock.

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
- Each round every market maker submits a two-sided quote. Prices must be strictly
  positive and your ask must be above your own bid (max price 999.99, sizes 1–99).
- **Call auction at the reveal:** orders match where bid ≥ ask, settle **at the ask**,
  volume = the smaller size. Lowest asks and highest bids fill first; ties favor larger
  size, then random. Everything is revealed with names, like writing it on the slide.
- **Market phase:** unfilled quotes rest in the book. Market orders fill by time
  priority; an order bigger than the best level walks down the book at each level's own
  price; you never trade with yourself. Quotes are firm — no cancels.
- **Round close:** all remaining orders are canceled. Next round starts from fresh quotes.
- **Settlement:** V = sum of the points of **all** dealt cards (public + every private
  card actually in play). Score = cash from filled orders + net position × V. Shorting
  and negative scores are allowed; the game is zero-sum, minus any exchange fees the
  host enabled (the settlement screen accounts for the exchange's take).

### Judgment calls where the deck was ambiguous

The slide says orders match "where the bid exceeds the ask" and that "remaining orders
are canceled" right after matching — taken literally, market orders would have nothing
to trade against. The app implements the (clearly intended) playable version:

1. Bids **equal** to an ask do cross (standard exchange behavior; price = the ask).
2. Leftover quotes stay live **through the market-order window**, and are canceled when
   the round closes.
3. A market order larger than the best level walks the book rather than being rejected.
4. In "assigned roles" mode, market makers quote only and takers take only; use
   "everyone" mode (default suggestion for small groups) to let all players do both.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Roles | assigned (alternating) | or "everyone quotes and takes" |
| Private-card deck | hearts & spades | "full deck" for more cards in play |
| Quote timer | 90s | 0 = advance manually; auto-reveals when all quotes are in; changeable mid-game |
| Market timer | 120s | the deck's "two minutes"; 0 = manual; changeable mid-game |
| Players dealt a card | everyone | set *k* to create **informed vs. uninformed** traders: k random players get a card, the rest get nothing (worth 0). The count is public; *who* is secret — even from the host. Settlement compares the groups' average scores. |
| Exchange fee per unit | 0 | charged to **both** sides of every fill and kept by the exchange; flip it on between rounds and watch spreads widen |
| Anonymous trading | off | book, tape and standings show stable pseudonyms (Trader 1, 2, …) until settlement; the host still sees real names |
| Card points (A/K/Q/J) | −40 / +20 / 0 / 0 | host-editable, even mid-game as a "news shock"; number cards stay face value |

Fee, anonymity, timers and card points are also editable **between rounds** from the
host panel ("Live rule tweaks"). Roles, deck, and the informed count are fixed once
cards are dealt. Active non-default rules always show in the settings line that
players and the board see.

## Practical notes

- **Reconnects:** phones can sleep/refresh freely — sessions resume automatically. If
  someone's device dies, they can rejoin from a new device with the same name ("resume
  seat"), as long as the old device is offline.
- **Crash-safe:** state auto-saves to `state.json`; restarting `python3 server.py`
  resumes mid-game (timers included). Start over with `python3 server.py --fresh`,
  or the host panel's *Reset game*.
- **Late arrivals** can't join after the deal (V is fixed by the dealt cards) — they can
  watch `/board`.
- **Kicking** someone mid-game keeps their dealt card in V (the card was dealt), pulls
  their resting orders, and freezes their P&L.
- **Remote players:** the join URL only works on your network. For remote folks,
  tunnel it: `brew install cloudflared && cloudflared tunnel --url http://localhost:3000`
  (or ngrok / Tailscale) and set `JOIN_URL=https://…` when starting the server so the
  board shows the right link. (When tunneled, host controls need the `?key=…` link —
  the key-free localhost login deliberately stays off for tunneled traffic.) For real
  web hosting, see [EXTENDING.md](EXTENDING.md).
- Env vars: `PORT` (default 3000, tries 3000–3010), `HOST_KEY`, `JOIN_URL`, `STATE_FILE`.
- This is a friendly living-room/classroom game, not a hardened service — run it on a
  trusted network.

## Sharing it with friends

Send them this folder (ZIP it, or point them at the repo) — that's the whole install.
Everything above the rules section is written for someone who has never seen a
terminal; the launchers cover Mac and Windows. MIT licensed (see `LICENSE`), so copy,
tweak, and re-share freely. If you put it on GitHub, the included workflow
(`.github/workflows/tests.yml`) runs the test suite on Linux/macOS/Windows and on
Python 3.9 through 3.13 on every push.

## Tests

```bash
python3 tests.py    # engine unit tests + a full game driven over HTTP
```

## Hosting it, changing the rules, informed traders

See **[EXTENDING.md](EXTENDING.md)** — deployment recipes (tunnel / Fly.io / VPS), a
map of where every exchange rule lives so you can manipulate the mechanics, and the
informed vs. uninformed trader mechanic (now built in as a host setting).

## Files

```
server.py                    HTTP + Server-Sent-Events server, timers, persistence
engine.py                    game rules: dealing, matching, market orders, scoring
public/                      the web client (player / host / board views)
tests.py                     155 checks: matching, scoring, privacy, full-game HTTP run
Start Trading Game.command   macOS double-click launcher
start-trading-game.bat       Windows double-click launcher
Dockerfile                   optional, for container hosting (see EXTENDING.md)
```
