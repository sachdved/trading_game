#!/usr/bin/env python3
"""The exchange server for the Glosten-Milgrom classroom trading game.

Python 3.9+ standard library only — run with:  python3 server.py
Options via env: PORT (default 3000, 0 = random), HOST_KEY, JOIN_URL, STATE_FILE.
Flags: --fresh  (ignore any saved game snapshot and start clean)

Views served:
  /            player view (join from phones)
  /board       projector view (public info only)
  /host?key=K  host control panel (key printed below at startup)
"""

import json
import os
import queue
import random
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine as E

VERSION = '1.0.0'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
STATE_FILE = os.environ.get('STATE_FILE', os.path.join(BASE_DIR, 'state.json'))
FRESH = '--fresh' in sys.argv

LOCK = threading.RLock()
RNG = random.Random()
GAME = E.create_game()
TOKENS = {}          # token -> pid
HOST_KEY = os.environ.get('HOST_KEY') or secrets.token_hex(4)
CLIENTS = set()      # live SSE connections
RECENT_REQ = {}      # pid -> OrderedDict(reqId -> cached market-order response)
JOIN_URL = None      # filled in once we know the port
TIMER = None
SAVE_TIMER = None


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- persistence

def load_snapshot():
    global GAME, TOKENS, HOST_KEY
    if FRESH or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            snap = json.load(f)
        if snap.get('game', {}).get('phase'):
            GAME = snap['game']
            TOKENS = snap.get('tokens', {})
            HOST_KEY = os.environ.get('HOST_KEY') or snap.get('hostKey') or HOST_KEY
            print(f"[gm] resumed saved game (phase {GAME['phase']}, round {GAME['round']}) "
                  f"from {STATE_FILE} — run with --fresh to discard", flush=True)
    except (OSError, ValueError) as e:
        print(f'[gm] could not load {STATE_FILE}: {e}', flush=True)


def save_now():
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'game': GAME, 'tokens': TOKENS, 'hostKey': HOST_KEY}, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f'[gm] failed to save state: {e}', flush=True)


def persist_soon():
    global SAVE_TIMER
    if SAVE_TIMER:
        SAVE_TIMER.cancel()

    def run():
        with LOCK:
            save_now()
    SAVE_TIMER = threading.Timer(0.25, run)
    SAVE_TIMER.daemon = True
    SAVE_TIMER.start()


# ---------------------------------------------------------------- live updates

class Client:
    __slots__ = ('q', 'kind', 'pid')

    def __init__(self, kind, pid=None):
        self.q = queue.Queue(maxsize=100)
        self.kind = kind
        self.pid = pid


def connections():
    counts = {}
    for c in CLIENTS:
        if c.kind == 'player' and c.pid:
            counts[c.pid] = counts.get(c.pid, 0) + 1
    return counts


def broadcast():
    """Push a per-audience view of the current state to every open stream."""
    extras = {'now': now_ms(), 'joinUrl': JOIN_URL, 'connections': connections(),
              'hostUrl': f'{JOIN_URL}/host?key={HOST_KEY}' if JOIN_URL else None}
    for c in list(CLIENTS):
        payload = json.dumps(E.view_for(GAME, c.kind, c.pid, extras))
        try:
            c.q.put_nowait(payload)
        except queue.Full:
            pass  # hopelessly slow client; it will resync on its next message


def touched():
    """Call under LOCK after any successful mutation."""
    persist_soon()
    rearm()
    broadcast()


# ---------------------------------------------------------------- phase timer

def rearm():
    global TIMER
    if TIMER:
        TIMER.cancel()
        TIMER = None
    deadline = GAME.get('deadline')
    if deadline is not None:
        delay = max(0.0, (deadline - now_ms()) / 1000) + 0.03
        TIMER = threading.Timer(delay, fire)
        TIMER.daemon = True
        TIMER.start()


def fire():
    with LOCK:
        try:
            did = E.on_deadline(GAME, now_ms(), RNG)
        except Exception as e:  # never let the timer thread die silently
            print(f'[gm] timer error: {e}', flush=True)
            did = None
        if did:
            touched()
        else:
            rearm()


# ---------------------------------------------------------------- http plumbing

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
STATIC = {'/': 'index.html', '/host': 'index.html', '/board': 'index.html',
          '/app.js': 'app.js', '/style.css': 'style.css'}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):  # keep the console clean for the host
        pass

    def local_admin(self):
        """True when the request comes from a browser on the server machine itself.

        Loopback peer alone is not enough: tunnels (cloudflared/ngrok) also connect
        from loopback, so we additionally require a localhost Host header, which
        tunnels do not send. Lets the person who started the server open /host
        without copying the key from the terminal."""
        if self.client_address[0] not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            return False
        host = (self.headers.get('Host') or '').split(':')[0].strip().lower()
        return host in ('localhost', '127.0.0.1')

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

    def read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n > 16384:
            raise E.GameError('Request too large.')
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise E.GameError('Malformed request.')

    def require_host(self, params):
        if params.get('key') != HOST_KEY and not self.local_admin():
            raise E.GameError('Bad host key.', code='badkey')

    # ---- GET

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip('/') or '/'
        if path in STATIC:
            return self.serve_static(STATIC[path])
        if path == '/events':
            return self.serve_events(urllib.parse.parse_qs(url.query))
        if path == '/api/state':
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                self.require_host(q)
            except E.GameError as e:
                return self.reply(403, {'error': str(e)})
            with LOCK:
                view = E.view_for(GAME, 'host', None,
                                  {'now': now_ms(), 'joinUrl': JOIN_URL,
                                   'hostUrl': f'{JOIN_URL}/host?key={HOST_KEY}' if JOIN_URL else None,
                                   'connections': connections()})
            return self.reply(200, view)
        if path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
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

    def serve_events(self, qs):
        params = {k: v[0] for k, v in qs.items()}
        role = params.get('role', 'player')
        pid = None
        if role == 'host':
            if params.get('key') != HOST_KEY and not self.local_admin():
                return self.reply(403, {'error': 'Bad host key'})
            kind = 'host'
        elif role == 'board':
            kind = 'board'
        else:
            kind = 'player'
            pid = TOKENS.get(params.get('token') or '')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        if kind == 'player' and not pid:
            # Unknown/stale token: tell the client to re-join, then hang up.
            try:
                self.wfile.write(b'retry: 3000\n\n')
                self.wfile.write(f'data: {json.dumps({"error": "bad-token"})}\n\n'.encode())
                self.wfile.flush()
            except OSError:
                pass
            return

        client = Client(kind, pid)
        with LOCK:
            CLIENTS.add(client)
            broadcast()  # seeds this client and refreshes connection dots for everyone
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
        except (OSError, ValueError):
            pass
        finally:
            with LOCK:
                CLIENTS.discard(client)
                broadcast()

    # ---- POST

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            body = self.read_json()
        except E.GameError as e:
            return self.reply(400, {'error': str(e)})
        try:
            handler = {
                '/api/join': self.h_join,
                '/api/claim': self.h_claim,
                '/api/quote': self.h_quote,
                '/api/market': self.h_market,
                '/api/host': self.h_host,
            }.get(url.path)
            if not handler:
                return self.reply(404, {'error': 'Not found'})
            handler(body)
        except E.GameError as e:
            code = 409 if e.code in ('taken',) else 403 if e.code == 'badkey' else 400
            out = {'error': str(e)}
            if e.code:
                out['code'] = e.code
            if e.code == 'taken':
                with LOCK:
                    p = E.find_active_by_name(GAME, body.get('name'))
                    out['canClaim'] = bool(p) and connections().get(p['id'], 0) == 0
            self.reply(code, out)
        except Exception as e:  # defensive: report instead of dropping the socket
            print(f'[gm] internal error: {e!r}', flush=True)
            self.reply(500, {'error': 'Internal error — check the server console.'})

    def player_from(self, body):
        pid = TOKENS.get(str(body.get('token') or ''))
        if not pid:
            raise E.GameError('Your session is stale — rejoin the game.', code='badtoken')
        return pid

    def h_join(self, body):
        with LOCK:
            p = E.add_player(GAME, body.get('name'), now_ms())
            token = secrets.token_hex(12)
            TOKENS[token] = p['id']
            touched()
        self.reply(200, {'token': token, 'pid': p['id'], 'name': p['name']})

    def h_claim(self, body):
        """Resume an existing seat from a new device (only while it is disconnected)."""
        with LOCK:
            p = E.find_active_by_name(GAME, body.get('name'))
            if not p:
                raise E.GameError('No player by that name.')
            if connections().get(p['id'], 0) > 0:
                raise E.GameError('That seat is currently connected on another device.')
            for t in [t for t, pid in TOKENS.items() if pid == p['id']]:
                del TOKENS[t]
            token = secrets.token_hex(12)
            TOKENS[token] = p['id']
            E.note(GAME, f"{p['name']} reconnected from a new device", now_ms())
            touched()
        self.reply(200, {'token': token, 'pid': p['id'], 'name': p['name']})

    def h_quote(self, body):
        with LOCK:
            pid = self.player_from(body)
            result = E.submit_quote(GAME, pid, body, now_ms())
            touched()
        self.reply(200, {'ok': True, **result})

    def h_market(self, body):
        with LOCK:
            pid = self.player_from(body)
            req_id = str(body.get('reqId') or '')
            cache = RECENT_REQ.setdefault(pid, OrderedDict())
            if req_id and req_id in cache:
                return self.reply(200, cache[req_id])  # double-tap: same answer
            result = E.market_order(GAME, pid, body.get('side'), body.get('size'), now_ms())
            names = {p['id']: p['name'] for p in GAME['players'].values()}
            out = {'ok': True, 'requested': result['requested'], 'filled': result['filled'],
                   'fills': [{'name': names.get(f['pid'], '—'), 'price': f['price'],
                              'size': f['size']} for f in result['fills']]}
            if req_id:
                cache[req_id] = out
                while len(cache) > 16:
                    cache.popitem(last=False)
            touched()
        self.reply(200, out)

    def h_host(self, body):
        with LOCK:
            self.require_host(body)
            action = body.get('action')
            now = now_ms()
            if action == 'settings':
                E.set_settings(GAME, body.get('settings') or {}, now)
            elif action == 'role':
                E.set_role(GAME, body.get('pid'), body.get('role'), now)
            elif action == 'start':
                E.start_game(GAME, now, RNG)
            elif action == 'reveal':
                E.reveal(GAME, now, RNG)
            elif action == 'endMarket':
                E.end_market(GAME, now)
            elif action == 'next':
                E.next_round(GAME, now)
            elif action == 'settle':
                E.settle(GAME, now)
            elif action == 'extend':
                if GAME['phase'] not in ('quote', 'market'):
                    raise E.GameError('Nothing to extend right now.')
                secs = max(5, min(300, int(body.get('seconds') or 30)))
                GAME['deadline'] = (GAME['deadline'] or now) + secs * 1000
                E.note(GAME, f'Host added {secs}s to the clock', now)
            elif action == 'kick':
                pid = body.get('pid')
                E.kick_player(GAME, pid, now)
                for t in [t for t, p in TOKENS.items() if p == pid]:
                    del TOKENS[t]
            elif action == 'rematch':
                E.rematch(GAME, now)
            elif action == 'reset':
                reset_game()
            else:
                raise E.GameError('Unknown host action.')
            touched()
        self.reply(200, {'ok': True})


def reset_game():
    global GAME, TOKENS
    GAME = E.create_game()
    TOKENS.clear()
    RECENT_REQ.clear()
    E.note(GAME, 'Fresh game created', now_ms())


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
    global JOIN_URL
    load_snapshot()

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
    ip = lan_ip()
    JOIN_URL = os.environ.get('JOIN_URL') or f'http://{ip}:{port}'
    print(f'[gm] listening on http://127.0.0.1:{port}', flush=True)
    print(f'[gm] host key: {HOST_KEY}', flush=True)
    line = '─' * 62
    print(f"""
{line}
  ♠♥  Glosten–Milgrom trading game v{VERSION} — the app is the exchange

  Players join:    {JOIN_URL}
  Projector view:  {JOIN_URL}/board
  Host controls:   http://localhost:{port}/host   (no key needed here)
     from another device:  {JOIN_URL}/host?key={HOST_KEY}

  Game state auto-saves to {os.path.basename(STATE_FILE)} (restart-safe).
  Start over from scratch:  python3 server.py --fresh
{line}
""", flush=True)

    if '--open' in sys.argv:  # used by the double-click launchers
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f'http://localhost:{port}/host')).start()

    with LOCK:
        rearm()  # resume a saved phase timer, if any

    # a plain `kill` should also flush the game to disk before exiting
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with LOCK:
            save_now()
        print('\n[gm] state saved — bye', flush=True)


if __name__ == '__main__':
    main()
