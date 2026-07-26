#!/usr/bin/env python3
"""The multi-room exchange server for the Glosten-Milgrom trading game.

Anyone who opens the landing page can create a *room* (an independent game
with its own 5-letter code and host key) — like a party-game lobby. Rooms
live in memory, snapshot to STATE_DIR/<CODE>.json, and expire when idle.

Python 3.9+ standard library only — run with:  python3 server.py
Options via env:
  PORT (default 3000, 0 = random)     JOIN_URL   (public base URL for links)
  STATE_DIR (default ./state)         TRUST_PROXY=1 (honor X-Forwarded-For)
  MAX_ROOMS / MAX_CLIENTS / MAX_CLIENTS_PER_ROOM / MAX_CLIENTS_PER_IP
  RATE_CREATES_PER_MIN / RATE_JOINS_PER_MIN
  ROOM_TTL_MINUTES / SETTLED_TTL_MINUTES
Flags: --fresh  (discard all saved room snapshots and start clean)

Views served:
  /                     landing page (create a room / join by code)
  /practice             solo practice table (self-contained tutorial)
  /r/CODE               player view (join from phones)
  /r/CODE/board         projector view (public info only)
  /r/CODE/host?key=K    host control panel (key issued at room creation)
  /healthz              room/client counts for monitoring
"""

import json
import os
import queue
import random
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine as E

VERSION = '2.0.0'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
STATE_DIR = os.environ.get('STATE_DIR', os.path.join(BASE_DIR, 'state'))
FRESH = '--fresh' in sys.argv


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ''))
    except ValueError:
        return default


MAX_ROOMS = _env_int('MAX_ROOMS', 40)
MAX_CLIENTS = _env_int('MAX_CLIENTS', 400)              # SSE streams, all rooms
MAX_CLIENTS_PER_ROOM = _env_int('MAX_CLIENTS_PER_ROOM', 120)
MAX_CLIENTS_PER_IP = _env_int('MAX_CLIENTS_PER_IP', 60)  # generous: one house = one IP
ROOM_TTL_MS = _env_int('ROOM_TTL_MINUTES', 120) * 60_000
SETTLED_TTL_MS = _env_int('SETTLED_TTL_MINUTES', 30) * 60_000
REAP_EVERY_S = 60
RATE_LIMITS = {                                          # per client IP
    'create': (_env_int('RATE_CREATES_PER_MIN', 5), 60),
    'join': (_env_int('RATE_JOINS_PER_MIN', 30), 60),
}
TRUST_PROXY = os.environ.get('TRUST_PROXY', '') not in ('', '0', 'false')

LOCK = threading.RLock()
RNG = random.Random()
ROOMS = {}           # code -> Room
BUCKETS = {}         # (kind, ip) -> deque of request timestamps
BASE_URL = None      # public base URL, filled in once we know the port
STARTED = time.time()

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ'   # no I/L/O — phone-typeable
CODE_LEN = 5
ROOM_PATH_RE = re.compile(rf'^/r/([A-Za-z]{{{CODE_LEN}}})(/.*)?$')


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- rooms

class Client:
    __slots__ = ('q', 'kind', 'pid', 'token', 'ip')

    def __init__(self, kind, pid=None, token=None, ip=None):
        self.q = queue.Queue(maxsize=100)
        self.kind = kind
        self.pid = pid
        self.token = token
        self.ip = ip


class Room:
    """One independent game: state, sessions, live streams, timers."""

    def __init__(self, code, host_key=None):
        self.code = code
        self.game = E.create_game()
        self.tokens = {}          # token -> pid
        self.clients = set()      # live SSE connections
        self.recent_req = {}      # pid -> OrderedDict(reqId -> cached response)
        self.host_key = host_key or secrets.token_hex(8)
        self.timer = None         # phase-deadline timer
        self.save_timer = None    # debounced snapshot timer
        self.created = now_ms()
        self.last_active = now_ms()

    def alive(self):
        return ROOMS.get(self.code) is self

    def join_url(self):
        return f'{BASE_URL}/r/{self.code}' if BASE_URL else None

    def host_url(self):
        return f'{BASE_URL}/r/{self.code}/host?key={self.host_key}' if BASE_URL else None

    def connections(self):
        counts = {}
        for c in self.clients:
            if c.kind == 'player' and c.pid:
                counts[c.pid] = counts.get(c.pid, 0) + 1
        return counts


def new_code():
    while True:
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in ROOMS:
            return code


def create_room():
    """Caller must hold LOCK."""
    if len(ROOMS) >= MAX_ROOMS:
        raise E.GameError('The server is at its room limit right now — try again later.',
                          code='busy')
    room = Room(new_code())
    ROOMS[room.code] = room
    E.note(room.game, f'Room {room.code} created', now_ms())
    persist_soon(room)
    return room


def total_clients():
    return sum(len(r.clients) for r in ROOMS.values())


def clients_from_ip(ip):
    """Streams already held by one client IP. Loopback (the operator's browser, or a
    whole party funneled through a cloudflared/ngrok tunnel) is never capped."""
    if ip in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return 0
    return sum(1 for r in ROOMS.values() for c in r.clients if c.ip == ip)


def room_ttl_ms(room):
    g = room.game
    done = g['phase'] == 'settled' or (g['phase'] == 'lobby' and not E.active_players(g))
    return SETTLED_TTL_MS if done else ROOM_TTL_MS


def reap_rooms(now):
    """Delete rooms nobody is connected to that have been idle past their TTL."""
    removed = []
    with LOCK:
        for room in list(ROOMS.values()):
            if room.clients:
                continue
            if now - room.last_active <= room_ttl_ms(room):
                continue
            if room.timer:
                room.timer.cancel()
            if room.save_timer:
                room.save_timer.cancel()
            del ROOMS[room.code]
            delete_room_file(room.code)
            removed.append(room.code)
        # tidy stale rate-limit buckets while we are here
        cutoff = time.time() - 300
        for key in [k for k, d in BUCKETS.items() if not d or d[-1] < cutoff]:
            del BUCKETS[key]
    for code in removed:
        print(f'[gm] room {code} expired and was removed', flush=True)
    return removed


def reaper_loop():
    while True:
        time.sleep(REAP_EVERY_S)
        try:
            reap_rooms(now_ms())
        except Exception as e:   # never let the reaper thread die
            print(f'[gm] reaper error: {e!r}', flush=True)


# ---------------------------------------------------------------- rate limiting

def allow(ip, kind):
    limit, per = RATE_LIMITS[kind]
    if limit <= 0:
        return True
    # Loopback is exempt: it is either the operator's own browser or a tunnel
    # (cloudflared/ngrok) funneling a whole classroom through one local peer —
    # rate-limiting that shared address would lock out legitimate players.
    # (Behind a real reverse proxy, set TRUST_PROXY=1 so per-client IPs apply.)
    if ip in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return True
    with LOCK:
        d = BUCKETS.setdefault((kind, ip), deque())
        cutoff = time.time() - per
        while d and d[0] < cutoff:
            d.popleft()
        if len(d) >= limit:
            return False
        d.append(time.time())
        return True


# ---------------------------------------------------------------- persistence

def room_file(code):
    return os.path.join(STATE_DIR, code + '.json')


def save_room(room):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = room_file(room.code) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'code': room.code, 'game': room.game, 'tokens': room.tokens,
                       'hostKey': room.host_key, 'created': room.created,
                       'lastActive': room.last_active}, f)
        os.replace(tmp, room_file(room.code))
    except OSError as e:
        print(f'[gm] failed to save room {room.code}: {e}', flush=True)


def delete_room_file(code):
    try:
        os.remove(room_file(code))
    except OSError:
        pass


def persist_soon(room):
    if room.save_timer:
        room.save_timer.cancel()

    def run():
        with LOCK:
            if room.alive():
                save_room(room)
    room.save_timer = threading.Timer(0.25, run)
    room.save_timer.daemon = True
    room.save_timer.start()


def load_rooms():
    """Restore saved rooms at boot (or wipe them with --fresh)."""
    try:
        files = [f for f in os.listdir(STATE_DIR) if f.endswith('.json')]
    except OSError:
        return
    now = now_ms()
    for fname in files:
        path = os.path.join(STATE_DIR, fname)
        if FRESH:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            with open(path, encoding='utf-8') as f:
                snap = json.load(f)
            code = str(snap.get('code') or '').upper()
            if (not re.fullmatch(f'[A-Z]{{{CODE_LEN}}}', code)
                    or code != os.path.splitext(fname)[0].upper()
                    or not snap.get('game', {}).get('phase')):
                continue
            if snap['game'].get('gameVersion') != E.GAME_VERSION:
                os.remove(path)   # snapshot from an older rules version
                continue
            room = Room(code, host_key=snap.get('hostKey'))
            room.game = snap['game']
            room.tokens = snap.get('tokens', {})
            room.created = snap.get('created', now)
            room.last_active = snap.get('lastActive', now)
            if now - room.last_active > room_ttl_ms(room):
                os.remove(path)          # expired while the server was down
                continue
            ROOMS[code] = room
        except (OSError, ValueError) as e:
            print(f'[gm] could not load {path}: {e}', flush=True)
    if ROOMS and not FRESH:
        print(f"[gm] resumed {len(ROOMS)} saved room(s): {', '.join(sorted(ROOMS))} "
              f'— run with --fresh to discard', flush=True)


# ---------------------------------------------------------------- live updates

def broadcast(room):
    """Push a per-audience view of the room's state to every open stream."""
    extras = {'now': now_ms(), 'joinUrl': room.join_url(), 'hostUrl': room.host_url(),
              'roomCode': room.code, 'connections': room.connections()}
    for c in list(room.clients):
        payload = json.dumps(E.view_for(room.game, c.kind, c.pid, extras))
        try:
            c.q.put_nowait(payload)
        except queue.Full:
            pass  # hopelessly slow client; it will resync on its next message


def touched(room):
    """Call under LOCK after any successful mutation of the room."""
    room.last_active = now_ms()
    persist_soon(room)
    rearm(room)
    broadcast(room)


# ---------------------------------------------------------------- phase timers

def rearm(room):
    if room.timer:
        room.timer.cancel()
        room.timer = None
    g = room.game
    deadlines = [d for d in (g.get('deadline'), g.get('eventDeadline')) if d is not None]
    if deadlines:
        delay = max(0.0, (min(deadlines) - now_ms()) / 1000) + 0.03
        room.timer = threading.Timer(delay, fire, args=(room,))
        room.timer.daemon = True
        room.timer.start()


def fire(room):
    with LOCK:
        if not room.alive():
            return
        try:
            did = E.on_deadline(room.game, now_ms(), RNG)
        except Exception as e:  # never let a timer thread die silently
            print(f'[gm] timer error in room {room.code}: {e}', flush=True)
            did = None
        if did:
            touched(room)
        else:
            rearm(room)


# ---------------------------------------------------------------- http plumbing

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
ERROR_CODES = {'taken': 409, 'badkey': 403, 'rate': 429, 'no-room': 404, 'busy': 503}


def no_room_error():
    return E.GameError("That room doesn't exist — it may have expired.", code='no-room')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):  # keep the console clean
        pass

    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get('X-Forwarded-For')
            if xff:
                # The RIGHTMOST entry is the one our own trusted proxy appended;
                # everything left of it is client-supplied and spoofable.
                return xff.split(',')[-1].strip()
        return self.client_address[0]

    # ---- helpers

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def redirect(self, where):
        self.send_response(302)
        self.send_header('Location', where)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n > 16384 or n < 0:
            # the unread body would desync this keep-alive connection — drop it
            self.close_connection = True
            raise E.GameError('Request too large.')
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise E.GameError('Malformed request.')

    def get_room(self, code):
        """Look up a live room or raise. Caller should hold LOCK for mutations."""
        room = ROOMS.get(code)
        if room is None:
            raise no_room_error()
        return room

    @staticmethod
    def require_host(room, params):
        if params.get('key') != room.host_key:
            raise E.GameError('Bad host key.', code='badkey')

    # ---- GET

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip('/') or '/'
        if path == '/':
            return self.serve_static('index.html')
        if path in ('/practice', '/learn'):
            return self.serve_static('practice.html')
        if path in ('/app.js', '/style.css'):
            return self.serve_static(path.lstrip('/'))
        if path == '/healthz':
            with LOCK:
                return self.reply(200, {'ok': True, 'version': VERSION,
                                        'rooms': len(ROOMS), 'clients': total_clients(),
                                        'uptimeSeconds': int(time.time() - STARTED)})
        if path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path in ('/host', '/board'):   # pre-multiroom bookmarks
            return self.redirect('/')

        m = re.match(rf'^/api/rooms/([A-Za-z]{{{CODE_LEN}}})$', path)
        if m:
            with LOCK:
                room = ROOMS.get(m.group(1).upper())
                if room is None:
                    return self.reply(404, {'error': 'No room with that code.',
                                            'code': 'no-room'})
                return self.reply(200, {'ok': True, 'code': room.code,
                                        'phase': room.game['phase'],
                                        'players': len(E.active_players(room.game))})

        m = ROOM_PATH_RE.match(path)
        if m:
            code, sub = m.group(1).upper(), m.group(2) or ''
            if sub in ('', '/host', '/board'):
                # serve the shell even for unknown rooms — the client shows
                # a friendly "room expired" message via the event stream
                return self.serve_static('index.html')
            if sub == '/events':
                return self.serve_events(code, urllib.parse.parse_qs(url.query))
            if sub == '/api/state':
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                try:
                    with LOCK:
                        room = self.get_room(code)
                        self.require_host(room, q)
                        view = E.view_for(room.game, 'host', None,
                                          {'now': now_ms(), 'joinUrl': room.join_url(),
                                           'hostUrl': room.host_url(),
                                           'roomCode': room.code,
                                           'connections': room.connections()})
                    return self.reply(200, view)
                except E.GameError as e:
                    return self.reply(ERROR_CODES.get(e.code, 400), {'error': str(e)})
        self.reply(404, {'error': 'Not found'})

    def serve_static(self, name):
        try:
            with open(os.path.join(PUBLIC_DIR, name), 'rb') as f:
                body = f.read()
        except OSError:
            return self.reply(404, {'error': 'Not found'})
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(os.path.splitext(name)[1], 'application/octet-stream'))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def serve_events(self, code, qs):
        params = {k: v[0] for k, v in qs.items()}
        role = params.get('role', 'player')
        ip = self.client_ip()
        with LOCK:
            room = ROOMS.get(code)
            if room is not None and role == 'host' and params.get('key') != room.host_key:
                return self.reply(403, {'error': 'Bad host key'})
            if room is not None and (total_clients() >= MAX_CLIENTS
                                     or len(room.clients) >= MAX_CLIENTS_PER_ROOM
                                     or clients_from_ip(ip) >= MAX_CLIENTS_PER_IP):
                return self.reply(503, {'error': 'The server is full right now.'})

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        def hang_up(err):
            try:
                self.wfile.write(b'retry: 3000\n\n')
                self.wfile.write(f'data: {json.dumps({"error": err})}\n\n'.encode())
                self.wfile.flush()
            except OSError:
                pass

        if room is None:
            return hang_up('no-room')
        kind = role if role in ('host', 'board') else 'player'
        token = params.get('token') or ''
        pid = room.tokens.get(token) if kind == 'player' else None
        if kind == 'player' and not pid:
            return hang_up('bad-token')   # stale token: tell the client to re-join

        client = Client(kind, pid, token, ip)
        with LOCK:
            if not room.alive():
                return hang_up('no-room')
            room.clients.add(client)
            room.last_active = now_ms()
            broadcast(room)  # seeds this client, refreshes connection dots for all

        def revoked():
            """Seat resumed on another device (or kicked/reset): this stream must end.

            This is also the escape hatch for ghost connections — a dead phone's
            stream can keep absorbing writes for many minutes, but the moment its
            seat is claimed elsewhere the token rotates and we exit here."""
            with LOCK:
                return not room.alive() or (client.kind == 'player'
                                            and room.tokens.get(client.token) != client.pid)
        try:
            self.wfile.write(b'retry: 1500\n\n')
            self.wfile.flush()
            while True:
                try:
                    item = client.q.get(timeout=15)
                    self.wfile.write(f'data: {item}\n\n'.encode())
                except queue.Empty:
                    self.wfile.write(b':hb\n\n')
                self.wfile.flush()
                if revoked():
                    self.wfile.write(f'data: {json.dumps({"error": "superseded"})}\n\n'.encode())
                    self.wfile.flush()
                    break
        except (OSError, ValueError):
            pass
        finally:
            with LOCK:
                room.clients.discard(client)
                if room.alive():
                    room.last_active = now_ms()
                    broadcast(room)

    # ---- POST

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip('/') or '/'
        m = None
        try:
            body = self.read_json()
        except E.GameError as e:
            return self.reply(400, {'error': str(e)})
        try:
            if path == '/api/rooms':
                return self.h_create_room(body)
            m = ROOM_PATH_RE.match(path)
            handler = {
                '/api/join': self.h_join,
                '/api/claim': self.h_claim,
                '/api/quote': self.h_quote,
                '/api/cancel': self.h_cancel,
                '/api/market': self.h_market,
                '/api/host': self.h_host,
            }.get(m.group(2) or '') if m else None
            if not handler:
                return self.reply(404, {'error': 'Not found'})
            handler(m.group(1).upper(), body)
        except E.GameError as e:
            out = {'error': str(e)}
            if e.code:
                out['code'] = e.code
            if e.code in ('taken', 'started'):
                # offer the "resume that seat" flow whenever the name matches a
                # live seat — including mid-game, and even if the seat still looks
                # connected (its old device may be dead without having hung up)
                with LOCK:
                    room = ROOMS.get((m.group(1) if m else '').upper())
                    p = room and E.find_active_by_name(room.game, body.get('name'))
                    out['canClaim'] = bool(p)
            self.reply(ERROR_CODES.get(e.code, 400), out)
        except Exception as e:  # defensive: report instead of dropping the socket
            print(f'[gm] internal error: {e!r}', flush=True)
            self.reply(500, {'error': 'Internal error — check the server console.'})

    def h_create_room(self, body):
        if not allow(self.client_ip(), 'create'):
            raise E.GameError('You are creating rooms too quickly — wait a minute.',
                              code='rate')
        with LOCK:
            room = create_room()
        print(f'[gm] room {room.code} created', flush=True)
        self.reply(200, {'ok': True, 'code': room.code, 'hostKey': room.host_key,
                         'joinUrl': room.join_url(), 'hostUrl': room.host_url()})

    @staticmethod
    def player_from(room, body):
        pid = room.tokens.get(str(body.get('token') or ''))
        if not pid:
            raise E.GameError('Your session is stale — rejoin the game.', code='badtoken')
        return pid

    def h_join(self, code, body):
        if not allow(self.client_ip(), 'join'):
            raise E.GameError('Too many join attempts — wait a minute.', code='rate')
        with LOCK:
            room = self.get_room(code)
            p = E.add_player(room.game, body.get('name'), now_ms())
            token = secrets.token_hex(12)
            room.tokens[token] = p['id']
            touched(room)
        self.reply(200, {'token': token, 'pid': p['id'], 'name': p['name']})

    def h_claim(self, code, body):
        """Resume an existing seat from a new device.

        Deliberately allowed even while the seat looks connected: a phone that
        died without closing its socket can look "connected" for many minutes,
        and this is exactly the situation claiming exists for. Rotating the
        tokens evicts the old stream (see revoked() in serve_events); a live
        old device gets a 'superseded' notice and can claim right back."""
        if not allow(self.client_ip(), 'join'):
            raise E.GameError('Too many join attempts — wait a minute.', code='rate')
        with LOCK:
            room = self.get_room(code)
            p = E.find_active_by_name(room.game, body.get('name'))
            if not p:
                raise E.GameError('No player by that name.')
            for t in [t for t, pid in room.tokens.items() if pid == p['id']]:
                del room.tokens[t]
            token = secrets.token_hex(12)
            room.tokens[token] = p['id']
            E.note(room.game, f"{p['name']} resumed their seat from a new device", now_ms())
            touched(room)
        self.reply(200, {'token': token, 'pid': p['id'], 'name': p['name']})

    def h_quote(self, code, body):
        with LOCK:
            room = self.get_room(code)
            pid = self.player_from(room, body)
            result = E.submit_quote(room.game, pid, body, now_ms())
            touched(room)
        self.reply(200, {'ok': True, **result})

    def h_cancel(self, code, body):
        with LOCK:
            room = self.get_room(code)
            pid = self.player_from(room, body)
            result = E.cancel_quotes(room.game, pid, now_ms())
            touched(room)
        self.reply(200, {'ok': True, **result})

    def h_market(self, code, body):
        with LOCK:
            room = self.get_room(code)
            pid = self.player_from(room, body)
            req_id = str(body.get('reqId') or '')
            cache = room.recent_req.setdefault(pid, OrderedDict())
            if req_id and req_id in cache:
                return self.reply(200, cache[req_id])  # double-tap: same answer
            result = E.market_order(room.game, pid, body.get('side'), body.get('size'),
                                    now_ms())
            names = {p['id']: p['name'] for p in room.game['players'].values()}
            out = {'ok': True, 'requested': result['requested'], 'filled': result['filled'],
                   'fills': [{'name': names.get(f['pid'], '—'), 'price': f['price'],
                              'size': f['size']} for f in result['fills']]}
            if req_id:
                cache[req_id] = out
                while len(cache) > 16:
                    cache.popitem(last=False)
            touched(room)
        self.reply(200, out)

    def h_host(self, code, body):
        with LOCK:
            room = self.get_room(code)
            self.require_host(room, body)
            game = room.game
            action = body.get('action')
            now = now_ms()
            if action == 'settings':
                E.set_settings(game, body.get('settings') or {}, now)
            elif action == 'role':
                E.set_role(game, body.get('pid'), body.get('role'), now)
            elif action == 'start':
                E.start_game(game, now, RNG)
            elif action == 'endDay':
                E.end_day(game, now)
            elif action == 'next':
                E.next_day(game, now, RNG)
            elif action == 'event':
                E.draw_event(game, now, RNG)
            elif action == 'settle':
                E.settle(game, now)
            elif action == 'extend':
                if game['phase'] != 'open':
                    raise E.GameError('Nothing to extend right now.')
                secs = max(5, min(300, int(body.get('seconds') or 30)))
                game['deadline'] = (game['deadline'] or now) + secs * 1000
                E.note(game, f'Host added {secs}s to the clock', now)
            elif action == 'kick':
                pid = body.get('pid')
                E.kick_player(game, pid, now)
                for t in [t for t, p in room.tokens.items() if p == pid]:
                    del room.tokens[t]
            elif action == 'rematch':
                E.rematch(game, now)
            elif action == 'reset':
                room.game = E.create_game()
                room.tokens.clear()
                room.recent_req.clear()
                E.note(room.game, 'Fresh game created', now)
            else:
                raise E.GameError('Unknown host action.')
            touched(room)
        self.reply(200, {'ok': True})


# ---------------------------------------------------------------- startup

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))  # no packets sent; just picks the default route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def main():
    global BASE_URL
    # Windows pipes (CI, redirects) default to cp1252, which can't print the ♠♥ banner.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    load_rooms()

    want = os.environ.get('PORT')
    candidates = [int(want)] if want is not None else list(range(3000, 3011))
    server = None
    for port in candidates:
        try:
            server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
            break
        except OSError:
            print(f'[gm] port {port} busy, trying next…', flush=True)
    if server is None:
        print('[gm] no free port found — set PORT explicitly', flush=True)
        sys.exit(1)
    server.daemon_threads = True

    port = server.server_address[1]
    BASE_URL = (os.environ.get('JOIN_URL') or f'http://{lan_ip()}:{port}').rstrip('/')
    print(f'[gm] listening on http://127.0.0.1:{port}', flush=True)
    line = '─' * 62
    print(f"""
{line}
  ♠♥  Glosten–Milgrom trading game v{VERSION} — multi-room exchange

  Landing page:   {BASE_URL}
     Create a room there — every room gets its own join code and
     host key (saved automatically in the creator's browser).

  Health check:   {BASE_URL}/healthz

  Rooms snapshot to {STATE_DIR}{os.sep}<CODE>.json (restart-safe) and
  expire after ~{ROOM_TTL_MS // 60_000} min idle. Start clean:  python3 server.py --fresh
{line}
""", flush=True)

    if '--open' in sys.argv:  # used by the double-click launchers
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f'http://localhost:{port}/')).start()

    with LOCK:
        for room in ROOMS.values():
            rearm(room)  # resume saved phase timers, if any

    reaper = threading.Thread(target=reaper_loop, daemon=True)
    reaper.start()

    # a plain `kill` should also flush the rooms to disk before exiting
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with LOCK:
            for room in ROOMS.values():
                save_room(room)
        print('\n[gm] state saved — bye', flush=True)


if __name__ == '__main__':
    main()
