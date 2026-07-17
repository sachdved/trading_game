# Multi-room internet hosting — as built, and how to put it up

> **Status: implemented.** The server is now an Among Us-style multi-room service:
> anyone who opens your URL clicks **Create a room**, gets a 5-letter code, and friends
> join with the code (or the invite link). Every room is an independent game with its
> own host key. This file is the operator's guide: what was built, and the exact steps
> to put it on the internet.

---

## 1. What was built

### The shape

One `server.py` process hosts many **rooms**. Each room owns everything the old
single-game server kept in globals: a game dict, session tokens, live SSE streams, a
phase timer, a host key, and a snapshot file. `engine.py` (all game rules) is
untouched — it always operated on a game dict passed in.

| URL | What |
|---|---|
| `/` | landing page — create a room, or join one by code |
| `/r/KFQTR` | player view for room `KFQTR` |
| `/r/KFQTR/board` | projector view |
| `/r/KFQTR/host` | host panel (key issued at creation; creator's browser stores it) |
| `/r/KFQTR/api/…`, `/r/KFQTR/events` | room-scoped API + event stream |
| `POST /api/rooms` | create a room → `{code, hostKey, joinUrl, hostUrl}` |
| `GET /api/rooms/KFQTR` | does this room exist? (used by the join form) |
| `/healthz` | `{rooms, clients, uptimeSeconds}` for monitoring |

### The parts that a public server needs (all built in)

- **Room codes**: 5 letters from an alphabet without look-alikes (no I/L/O). Codes are
  meant to be short and typeable; a room holds nothing sensitive and joining after the
  deal is blocked anyway.
- **Host auth is per room.** Creating a room returns its host key; the creator's
  browser stores it (per-room, in localStorage) and goes straight into the host panel.
  Another device? Use the *Copy host link* button — the link carries `?key=…`. The old
  "localhost browsers are host" bypass is **gone**: on a public box behind a proxy,
  every request arrives from loopback, so that bypass would have made everyone host.
- **Lifecycle**: a reaper thread deletes rooms with no connected clients after
  ~2 h idle (~30 min once settled, or for rooms created but never used). Snapshot
  files are deleted with them.
- **Caps**: max 40 concurrent rooms, 400 SSE connections total, 120 per room
  (all env-tunable). Beyond them: a clean 503, not a melting server.
- **Rate limits**: per-IP, in memory — 5 room-creations and 30 joins per minute
  (env-tunable, `0` disables). Behind a proxy set `TRUST_PROXY=1` so the limiter
  keys on `X-Forwarded-For` instead of the proxy's own address.
- **Persistence**: each room snapshots (debounced) to `STATE_DIR/<CODE>.json`;
  a restart resumes every live room, timers included. `--fresh` wipes them.
- **Client**: the web app parses the room from the URL, namespaces its
  localStorage per room (you can play in two rooms in two tabs), shows a landing
  page at `/`, and a friendly "room expired" page when a code is dead.

The full env-var contract lives in [EXTENDING.md](EXTENDING.md) §1. Tests:
`python3 tests.py` — 200 checks including two-room isolation, cross-room key
rejection, the reaper, TTL tiers, rate limiting (incl. X-Forwarded-For keying),
connection caps, seat takeover, and a full game over HTTP.

### Honest limits (accepted trade-offs)

- **One instance only.** Rooms live in one process's memory. Never run replicas
  behind a load balancer. This comfortably covers friends-scale (hundreds of
  connections); if you ever outgrow it, that's an async rewrite, not a hosting tweak.
- **Deploys/restarts drop unsaved moments** (snapshots are debounced ~0.25 s, so in
  practice you lose almost nothing if `STATE_DIR` is on a persistent volume; without
  a volume, deploys drop all rooms). Prefer deploying when `/healthz` is quiet.

---

## 2. Putting it on the internet — step by step

You need: a **domain** (~$10/yr, optional but nice), **one always-on server**
(~$5/mo), and **HTTPS** (free either way). Two good shapes — pick one.

### Option A — Fly.io (least ops, ~5 minutes)

The repo's `Dockerfile` already works. One-time setup:

```bash
# 1. install flyctl and sign up (needs a credit card)
brew install flyctl
fly auth signup                       # or: fly auth login

# 2. from the trading_game folder
fly launch --no-deploy                # answer: no Postgres, no Redis, don't deploy yet
```

`fly launch` writes `fly.toml`. Edit it to match (app name will differ):

```toml
app = "gm-trading-game"

[build]

[http_service]
  internal_port = 3000
  force_https = true
  auto_stop_machines = "off"          # IMPORTANT: sleeping breaks SSE + timers
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
# 3. a 1 GB volume so rooms survive restarts, then ship it
fly volumes create gmdata --size 1 --region <your-region>
fly deploy
fly scale count 1                     # exactly one machine — this matters

# 4. check it's alive
curl https://gm-trading-game.fly.dev/healthz
```

Custom domain (optional): `fly certs add game.yourdomain.com`, then add the
CNAME/A records it prints at your DNS provider, and change `JOIN_URL` to match
(`fly deploy` after editing).

Updating later: `fly deploy` (drops rooms not yet snapshotted — check `/healthz`
first). Logs: `fly logs`.

### Option B — a small VPS + Caddy (most control, ~$5/mo)

Hetzner CX22 / DigitalOcean basic droplet / AWS Lightsail all work. Ubuntu 24.04:

```bash
# on the server, as root
adduser --system --group game
mkdir -p /opt/trading-game && cd /opt/trading-game
# copy the folder up from your machine (from your laptop):
#   rsync -av --exclude state --exclude __pycache__ trading_game/ root@<ip>:/opt/trading-game/

apt install -y python3 caddy
```

`/etc/systemd/system/trading-game.service`:

```ini
[Unit]
Description=Glosten-Milgrom trading game
After=network.target

[Service]
WorkingDirectory=/opt/trading-game
ExecStart=/usr/bin/python3 /opt/trading-game/server.py
Environment=PORT=3000
Environment=JOIN_URL=https://game.yourdomain.com
Environment=STATE_DIR=/var/lib/trading-game
Environment=TRUST_PROXY=1
DynamicUser=yes
StateDirectory=trading-game
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/caddy/Caddyfile` (Caddy fetches the TLS certificate automatically):

```caddyfile
game.yourdomain.com {
    reverse_proxy 127.0.0.1:3000
}
```

DNS: an **A record** for `game.yourdomain.com` → the server's IP. Then:

```bash
systemctl daemon-reload
systemctl enable --now trading-game caddy
curl https://game.yourdomain.com/healthz
```

Logs: `journalctl -u trading-game -f`. Update: rsync the folder again,
`systemctl restart trading-game` (rooms resume from `STATE_DIR`).

If you use **nginx** instead of Caddy, SSE must not be buffered:

```nginx
location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

### Either way, afterwards

- Point a free uptime monitor (UptimeRobot / Better Stack) at `https://…/healthz`.
- Share the bare URL. People do the rest: create room → board on the TV via the
  invite link → friends type the code.
- Watch usage via `/healthz` (`rooms`, `clients`) and the `[gm] room XXXXX created`
  log lines.

### What being the operator means

No accounts, no emails, no payments — the worst an abuser gets is a rude name in a
room (hosts can kick) or a burst of room creation (rate-limited, capped). Things
you may eventually want, in impact order: a QR code on the board, an operator page
listing rooms, a profanity filter on names. None are needed to launch.
