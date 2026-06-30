import sqlite3
import json
import uuid
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock

app = Flask(__name__, static_folder='static')
sock = Sock(app)

DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_PG = DATABASE_URL.startswith('postgres')

if USE_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _pg_pool = None

    def _get_pool():
        global _pg_pool
        if _pg_pool is None:
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        return _pg_pool

clients = set()

# ── DB wrapper ────────────────────────────────────────────────────────────────

class _Conn:
    """Unified wrapper: makes psycopg2 behave like sqlite3 for our query patterns."""
    def __init__(self):
        if USE_PG:
            self._c = _get_pool().getconn()
            self._pooled = True
        else:
            self._c = sqlite3.connect('points.db')
            self._c.row_factory = sqlite3.Row
            self._pooled = False

    def execute(self, sql, params=()):
        if USE_PG:
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace('?', '%s'), params or None)
            return cur
        return self._c.execute(sql, params)

    def executemany(self, sql, params_list):
        if USE_PG:
            cur = self._c.cursor()
            cur.executemany(sql.replace('?', '%s'), params_list)
            return cur
        return self._c.executemany(sql, params_list)

    def savepoint(self, name='sp'):
        if USE_PG:
            self._c.cursor().execute(f'SAVEPOINT {name}')

    def release(self, name='sp'):
        if USE_PG:
            self._c.cursor().execute(f'RELEASE SAVEPOINT {name}')

    def rollback_to(self, name='sp'):
        if USE_PG:
            self._c.cursor().execute(f'ROLLBACK TO SAVEPOINT {name}')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._c.commit()
        else:
            self._c.rollback()
        if self._pooled:
            _get_pool().putconn(self._c)  # return to pool, not close
        else:
            self._c.close()

def get_db():
    return _Conn()

# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        for stmt in [
            '''CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
                emoji TEXT NOT NULL, total_points INTEGER DEFAULT 0)''',
            '''CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, team_count INTEGER NOT NULL,
                category TEXT NOT NULL, day_tag INTEGER DEFAULT 0, location TEXT DEFAULT '',
                ui_type TEXT DEFAULT 'standard_1', config TEXT DEFAULT '{}')''',
            '''CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, game_id TEXT, game_name TEXT NOT NULL,
                team1_id TEXT NOT NULL, team2_id TEXT,
                points1 INTEGER NOT NULL DEFAULT 0, points2 INTEGER DEFAULT 0,
                note TEXT, event_type TEXT NOT NULL,
                day INTEGER NOT NULL DEFAULT 1, timestamp TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)''',
        ]:
            conn.execute(stmt)

        # Migrations: add missing columns (safe to re-run)
        for table, col, typedef in [
            ('events', 'day', 'INTEGER NOT NULL DEFAULT 1'),
            ('games', 'day_tag', 'INTEGER DEFAULT 0'),
            ('games', 'location', "TEXT DEFAULT ''"),
            ('games', 'ui_type', "TEXT DEFAULT 'standard_1'"),
            ('games', 'config', "TEXT DEFAULT '{}'"),
        ]:
            try:
                conn.savepoint('mig')
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typedef}')
                conn.release('mig')
            except Exception:
                conn.rollback_to('mig')

        # Default settings
        if USE_PG:
            conn.execute("INSERT INTO settings(key,value) VALUES ('password','minion2026') ON CONFLICT DO NOTHING")
        else:
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('password', 'minion2026')")

        # Seed teams
        for tid, name, color, emoji in [
            ('team1', 'Mystery-verse', '#F59E0B', '💛'),
            ('team2', 'Sky-verse',     '#38BDF8', '🩵'),
            ('team3', 'Crimson-verse', '#F87171', '❤️'),
            ('team4', 'Shadow-verse',  '#94A3B8', '🌑'),
            ('team5', 'Tech-verse',    '#22D3EE', '⚡'),
            ('team6', 'Nova-verse',    '#F472B6', '💗'),
            ('team7', 'Chaos-verse',   '#C084FC', '💜'),
        ]:
            exists = conn.execute('SELECT id FROM teams WHERE id=?', (tid,)).fetchone()
            if exists:
                conn.execute('UPDATE teams SET name=?,color=?,emoji=? WHERE id=?', (name, color, emoji, tid))
            else:
                conn.execute('INSERT INTO teams VALUES (?,?,?,?,0)', (tid, name, color, emoji))

        # Re-seed games (always refresh)
        conn.execute('DELETE FROM games')
        conn.executemany('INSERT INTO games VALUES (?,?,?,?,?,?,?,?)', [
            # Day 1 — Vivo Rooftop
            ('g_spot',    'Spot the Difference',    2, 'game', 1, 'Vivo Rooftop',  'win_lose_2',   '{"win":150,"lose":50}'),
            ('g_jigsaw',  'Jigsaw Puzzle',           2, 'game', 1, 'Vivo Rooftop',  'win_lose_2',   '{"win":150,"lose":50}'),
            ('g_song',    'Guess the Song',          2, 'game', 1, 'Vivo Rooftop',  'win_lose_2',   '{"win":150,"lose":50}'),
            ('g_mrt',     'MRT Line Game',           1, 'game', 1, 'Vivo Rooftop',  'multiplier_1', '{"multiplier":2,"label":"Score"}'),
            # Day 1 — Sensory Scape
            ('g_mafia',   'Mafia',                   1, 'game', 1, 'Sensory Scape', 'preset_1',     '{"presets":[250],"labels":["Winner — 250 pts"]}'),
            ('g_imposter','Imposter Game',            1, 'game', 1, 'Sensory Scape', 'preset_1',     '{"presets":[250],"labels":["Winner — 250 pts"]}'),
            ('g_guesswho','Guess Who',               1, 'game', 1, 'Sensory Scape', 'preset_1',     '{"presets":[250],"labels":["Winner — 250 pts"]}'),
            ('g_cards',   'Writing of Cards',        1, 'game', 1, 'Sensory Scape', 'preset_1',     '{"presets":[250],"labels":["Winner — 250 pts"]}'),
            # Day 1 — Beach
            ('g_captball',"Captain's Ball",          2, 'game', 1, 'Beach',         'captains_ball','{"win":250}'),
            ('g_splash',  'Splash Ball Race',        2, 'game', 1, 'Beach',         'captains_ball','{"win":250}'),
            ('g_bandana', 'Bandana Pull',            2, 'game', 1, 'Beach',         'win_lose_2',   '{"win":250,"lose":0}'),
            ('g_charades','Handicap Charades',       1, 'game', 1, 'Beach',         'multiplier_1', '{"multiplier":20,"label":"Correct Guesses"}'),
            ('g_relay',   'Relay Race',              7, 'game', 1, 'Beach',         'relay',        '{"places":[400,300,200,100]}'),
            # Day 2 — School
            ('g_police',  'Police Sketch Pictionary',2, 'game', 2, 'School',        'standard_2',   '{}'),
            ('g_back',    "Don't Show Your Back",    2, 'game', 2, 'School',        'standard_2',   '{}'),
            ('g_movie',   'Movie Jeopardy',          2, 'game', 2, 'School',        'standard_2',   '{}'),
            ('g_coney',   'Coney',                   1, 'game', 2, 'School',        'standard_1',   '{}'),
            # Day 2 — Scavenger Hunt
            ('g_scav',    'Scavenger Hunt',          1, 'game', 2, 'Scavenger Hunt','standard_1',   '{}'),
        ])

# ── Helpers ───────────────────────────────────────────────────────────────────

def broadcast(data):
    dead = set()
    for ws in clients:
        try:
            ws.send(json.dumps(data))
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

def recalc_totals(conn):
    conn.execute('UPDATE teams SET total_points = 0')
    for r in conn.execute('SELECT team1_id, SUM(points1) as p FROM events GROUP BY team1_id').fetchall():
        conn.execute('UPDATE teams SET total_points = total_points + ? WHERE id = ?', (r['p'], r['team1_id']))
    for r in conn.execute('SELECT team2_id, SUM(points2) as p FROM events WHERE team2_id IS NOT NULL GROUP BY team2_id').fetchall():
        conn.execute('UPDATE teams SET total_points = total_points + ? WHERE id = ?', (r['p'], r['team2_id']))

def get_teams_data(conn, day=None):
    result = []
    for t in conn.execute('SELECT id, name, color, emoji FROM teams').fetchall():
        t = dict(t)
        tid = t['id']
        clause = 'AND day=?' if day else ''
        p1 = conn.execute(f'SELECT COALESCE(SUM(points1),0) as p FROM events WHERE team1_id=? {clause}',
                          (tid, day) if day else (tid,)).fetchone()
        p2 = conn.execute(f'SELECT COALESCE(SUM(points2),0) as p FROM events WHERE team2_id=? {clause}',
                          (tid, day) if day else (tid,)).fetchone()
        result.append({**t, 'total_points': p1['p'] + p2['p']})
    result.sort(key=lambda x: x['total_points'], reverse=True)
    return result

def get_setting(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None

def notify_sheets(url, rows):
    """Fire-and-forget POST. url is pre-fetched so no extra DB call needed."""
    if not url or not url.startswith('http'):
        return
    def _send():
        try:
            payload = json.dumps({'events': rows}).encode()
            headers = {'Content-Type': 'application/json'}
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            try:
                urllib.request.urlopen(req, timeout=8)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    req2 = urllib.request.Request(
                        e.headers.get('Location', url), data=payload, headers=headers, method='POST')
                    urllib.request.urlopen(req2, timeout=8)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

@sock.route('/ws')
def websocket(ws):
    clients.add(ws)
    try:
        while True:
            ws.receive()
    except Exception:
        pass
    finally:
        clients.discard(ws)

@app.route('/')
def index():
    return send_from_directory('static', 'tracker.html')

@app.route('/gamemaster')
def gamemaster():
    return send_from_directory('static', 'gamemaster.html')

@app.route('/api/auth', methods=['POST'])
def auth():
    data = request.json
    with get_db() as conn:
        pw = get_setting(conn, 'password')
    if data.get('password') == pw:
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/settings', methods=['GET'])
def get_settings_api():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE key != 'password'").fetchall()
        return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    with get_db() as conn:
        pw = get_setting(conn, 'password')
        if data.get('current_password') != pw:
            return jsonify({'ok': False}), 401
        for key, value in data.items():
            if key == 'current_password':
                continue
            if USE_PG:
                conn.execute(
                    'INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value',
                    (key, str(value)))
            else:
                conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
    return jsonify({'ok': True})

@app.route('/api/teams')
def get_teams():
    day = request.args.get('day', type=int)
    with get_db() as conn:
        return jsonify(get_teams_data(conn, day))

@app.route('/api/games')
def get_games():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM games ORDER BY day_tag, location, name').fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/events')
def get_events():
    team_id = request.args.get('team_id')
    day = request.args.get('day', type=int)
    with get_db() as conn:
        conditions, params = [], []
        if team_id:
            conditions.append('(e.team1_id=? OR e.team2_id=?)')
            params.extend([team_id, team_id])
        if day:
            conditions.append('e.day=?')
            params.append(day)
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        rows = conn.execute(f'''
            SELECT e.*,
                t1.name as team1_name, t1.emoji as team1_emoji, t1.color as team1_color,
                t2.name as team2_name, t2.emoji as team2_emoji
            FROM events e
            JOIN teams t1 ON e.team1_id = t1.id
            LEFT JOIN teams t2 ON e.team2_id = t2.id
            {where}
            ORDER BY e.timestamp DESC LIMIT 200
        ''', params).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/submit', methods=['POST'])
def submit_points():
    data = request.json
    event_id = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec='seconds')
    day = int(data.get('day', 1))
    with get_db() as conn:
        conn.execute('''
            INSERT INTO events (id,game_id,game_name,team1_id,team2_id,
                points1,points2,note,event_type,day,timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ''', (event_id, data.get('game_id'), data['game_name'],
              data['team1_id'], data.get('team2_id'),
              int(data['points1']), int(data.get('points2', 0)),
              data.get('note', ''), data.get('event_type', 'game'), day, now))
        recalc_totals(conn)
        teams = [dict(t) for t in conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()]
        ev = dict(conn.execute('''
            SELECT e.*, t1.name as team1_name, t1.emoji as team1_emoji,
                t2.name as team2_name, t2.emoji as team2_emoji
            FROM events e JOIN teams t1 ON e.team1_id=t1.id
            LEFT JOIN teams t2 ON e.team2_id=t2.id WHERE e.id=?
        ''', (event_id,)).fetchone())
        sheets_url = get_setting(conn, 'sheets_webhook_url') or os.environ.get('SHEETS_WEBHOOK_URL', '')
    broadcast({'type': 'update', 'teams': teams, 'latest_event': ev})
    notify_sheets(sheets_url, [{
        'timestamp': now, 'game_name': data['game_name'],
        'team1_id': data['team1_id'], 'points1': int(data['points1']),
        'team2_id': data.get('team2_id', ''), 'points2': int(data.get('points2', 0)),
        'day': day, 'note': data.get('note', ''), 'event_type': data.get('event_type', 'game')
    }])
    return jsonify({'ok': True})

@app.route('/api/submit-batch', methods=['POST'])
def submit_batch():
    events = request.json.get('events', [])
    now = datetime.now().isoformat(timespec='seconds')
    with get_db() as conn:
        for data in events:
            if int(data.get('points1', 0)) == 0 and int(data.get('points2', 0)) == 0:
                continue
            event_id = str(uuid.uuid4())
            conn.execute('''
                INSERT INTO events (id,game_id,game_name,team1_id,team2_id,
                    points1,points2,note,event_type,day,timestamp)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ''', (event_id, data.get('game_id'), data['game_name'],
                  data['team1_id'], data.get('team2_id'),
                  int(data['points1']), int(data.get('points2', 0)),
                  data.get('note', ''), data.get('event_type', 'game'),
                  int(data.get('day', 1)), now))
        recalc_totals(conn)
        teams = [dict(t) for t in conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()]
        sheets_url = get_setting(conn, 'sheets_webhook_url') or os.environ.get('SHEETS_WEBHOOK_URL', '')
    broadcast({'type': 'update', 'teams': teams, 'latest_event': None})
    notify_sheets(sheets_url, [{
        'timestamp': now, 'game_name': d['game_name'],
        'team1_id': d['team1_id'], 'points1': int(d['points1']),
        'team2_id': d.get('team2_id', ''), 'points2': int(d.get('points2', 0)),
        'day': int(d.get('day', 1)), 'note': d.get('note', ''), 'event_type': d.get('event_type', 'game')
    } for d in events if int(d.get('points1', 0)) != 0 or int(d.get('points2', 0)) != 0])
    return jsonify({'ok': True})

@app.route('/api/teams/<team_id>', methods=['PATCH'])
def update_team(team_id):
    data = request.json
    with get_db() as conn:
        for field in ('name', 'emoji', 'color'):
            if field in data:
                conn.execute(f'UPDATE teams SET {field}=? WHERE id=?', (data[field], team_id))
    return jsonify({'ok': True})

@app.route('/api/events/<event_id>', methods=['DELETE'])
def delete_event(event_id):
    with get_db() as conn:
        conn.execute('DELETE FROM events WHERE id=?', (event_id,))
        recalc_totals(conn)
        teams = [dict(t) for t in conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()]
    broadcast({'type': 'update', 'teams': teams, 'latest_event': None})
    return jsonify({'ok': True})

@app.route('/api/reset', methods=['POST'])
def reset_leaderboard():
    data = request.json
    with get_db() as conn:
        pw = get_setting(conn, 'password')
        if data.get('password') != pw:
            return jsonify({'ok': False}), 401
        conn.execute('DELETE FROM events')
        conn.execute('UPDATE teams SET total_points = 0')
        teams = [dict(t) for t in conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()]
    broadcast({'type': 'reset', 'teams': teams})
    return jsonify({'ok': True})

@app.route('/api/games', methods=['POST'])
def add_game():
    data = request.json
    game_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute('INSERT INTO games (id,name,team_count,category,day_tag,location,ui_type,config) VALUES (?,?,?,?,?,?,?,?)',
                     (game_id, data['name'], data['team_count'], data.get('category', 'game'),
                      data.get('day_tag', 0), data.get('location', ''),
                      data.get('ui_type', 'standard_1'), data.get('config', '{}')))
    return jsonify({'ok': True, 'id': game_id})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    print('🍌 Operation inCISive — Multiverse of Minions')
    print(f'   Game Master: http://localhost:{port}/gamemaster')
    print(f'   OC Tracker:  http://localhost:{port}/')
    print('   Default OC password: minion2026')
    app.run(debug=False, host='0.0.0.0', port=port)
