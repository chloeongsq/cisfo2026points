import sqlite3
import json
import uuid
import os
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock

app = Flask(__name__, static_folder='static')
sock = Sock(app)

DB = 'points.db'
clients = set()

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            emoji TEXT NOT NULL,
            total_points INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            team_count INTEGER NOT NULL,
            category TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            game_id TEXT,
            game_name TEXT NOT NULL,
            team1_id TEXT NOT NULL,
            team2_id TEXT,
            points1 INTEGER NOT NULL DEFAULT 0,
            points2 INTEGER DEFAULT 0,
            note TEXT,
            event_type TEXT NOT NULL,
            day INTEGER NOT NULL DEFAULT 1,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        ''')
        # Migrate: add day column if missing
        try:
            conn.execute('ALTER TABLE events ADD COLUMN day INTEGER NOT NULL DEFAULT 1')
        except Exception:
            pass
        # Default settings
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('password', 'minion2026')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('sabotage_win', '50')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('sabotage_lose', '-50')")

        # Seed / update teams
        universes = [
            ('team1', 'Mystery-verse', '#F59E0B', '💛'),
            ('team2', 'Sky-verse',     '#38BDF8', '🩵'),
            ('team3', 'Crimson-verse', '#F87171', '❤️'),
            ('team4', 'Shadow-verse',  '#94A3B8', '🌑'),
            ('team5', 'Tech-verse',    '#22D3EE', '⚡'),
            ('team6', 'Nova-verse',    '#F472B6', '💗'),
            ('team7', 'Chaos-verse',   '#C084FC', '💜'),
        ]
        for tid, name, color, emoji in universes:
            exists = conn.execute('SELECT id FROM teams WHERE id=?', (tid,)).fetchone()
            if exists:
                conn.execute('UPDATE teams SET name=?, color=?, emoji=? WHERE id=?', (name, color, emoji, tid))
            else:
                conn.execute('INSERT INTO teams VALUES (?,?,?,?,0)', (tid, name, color, emoji))

        # Seed games if empty
        if conn.execute('SELECT COUNT(*) FROM games').fetchone()[0] == 0:
            games = [
                (str(uuid.uuid4()), 'Amazing Race',    2, 'game'),
                (str(uuid.uuid4()), 'Quiz Bee',        1, 'game'),
                (str(uuid.uuid4()), 'Tug of War',      2, 'game'),
                (str(uuid.uuid4()), 'Scavenger Hunt',  1, 'game'),
                (str(uuid.uuid4()), 'Relay Race',      2, 'game'),
                (str(uuid.uuid4()), 'Trivia Night',    1, 'game'),
                (str(uuid.uuid4()), 'Obstacle Course', 1, 'game'),
                (str(uuid.uuid4()), 'Penalty',         1, 'misc'),
                (str(uuid.uuid4()), 'Wild Card',       1, 'misc'),
                (str(uuid.uuid4()), 'Bonus Points',    1, 'misc'),
                (str(uuid.uuid4()), 'Sabotage',        2, 'sabotage'),
            ]
            conn.executemany('INSERT INTO games VALUES (?,?,?,?)', games)

def broadcast(data):
    dead = set()
    for ws in clients:
        try:
            ws.send(json.dumps(data))
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

def recalc_totals(conn, day=None):
    conn.execute('UPDATE teams SET total_points = 0')
    clause = 'WHERE day=?' if day else ''
    params = (day,) if day else ()
    rows = conn.execute(f'SELECT team1_id, SUM(points1) as p FROM events {clause} GROUP BY team1_id', params).fetchall()
    for r in rows:
        conn.execute('UPDATE teams SET total_points = total_points + ? WHERE id = ?', (r['p'], r['team1_id']))
    rows = conn.execute(f'SELECT team2_id, SUM(points2) as p FROM events {clause} WHERE team2_id IS NOT NULL GROUP BY team2_id', params).fetchall()
    for r in rows:
        conn.execute('UPDATE teams SET total_points = total_points + ? WHERE id = ?', (r['p'], r['team2_id']))

def get_setting(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None

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

# Auth
@app.route('/api/auth', methods=['POST'])
def auth():
    data = request.json
    with get_db() as conn:
        pw = get_setting(conn, 'password')
    if data.get('password') == pw:
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Wrong password'}), 401

@app.route('/api/settings', methods=['GET'])
def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE key != 'password'").fetchall()
        return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    current_pw = data.get('current_password')
    with get_db() as conn:
        pw = get_setting(conn, 'password')
        if current_pw != pw:
            return jsonify({'ok': False, 'error': 'Wrong password'}), 401
        for key, value in data.items():
            if key == 'current_password':
                continue
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
    return jsonify({'ok': True})

@app.route('/api/teams')
def get_teams():
    day = request.args.get('day', type=int)
    with get_db() as conn:
        if day:
            recalc_totals(conn, day)
            conn.commit()
        rows = conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()
        # Restore all-time totals after temp recalc
        if day:
            recalc_totals(conn)
            conn.commit()
        return jsonify([dict(r) for r in rows])

@app.route('/api/teams-live')
def get_teams_live():
    """Returns live standings for given day filter without side effects."""
    day = request.args.get('day', type=int)
    with get_db() as conn:
        teams = conn.execute('SELECT id, name, color, emoji FROM teams').fetchall()
        result = []
        for t in teams:
            tid = t['id']
            clause = 'AND day=?' if day else ''
            params1 = (tid, day) if day else (tid,)
            params2 = (tid, day) if day else (tid,)
            p1 = conn.execute(f'SELECT COALESCE(SUM(points1),0) as p FROM events WHERE team1_id=? {clause}', params1).fetchone()['p']
            p2 = conn.execute(f'SELECT COALESCE(SUM(points2),0) as p FROM events WHERE team2_id=? {clause}', params2).fetchone()['p']
            result.append({**dict(t), 'total_points': p1 + p2})
        result.sort(key=lambda x: x['total_points'], reverse=True)
        return jsonify(result)

@app.route('/api/games')
def get_games():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM games ORDER BY category, name').fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/events')
def get_events():
    team_id = request.args.get('team_id')
    day = request.args.get('day', type=int)
    with get_db() as conn:
        conditions = []
        params = []
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
            ORDER BY e.timestamp DESC LIMIT 100
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
            INSERT INTO events (id, game_id, game_name, team1_id, team2_id,
                points1, points2, note, event_type, day, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ''', (event_id, data.get('game_id'), data['game_name'],
              data['team1_id'], data.get('team2_id'),
              int(data['points1']), int(data.get('points2', 0)),
              data.get('note', ''), data.get('event_type', 'game'), day, now))
        recalc_totals(conn)
        teams = conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()
        ev = conn.execute('''
            SELECT e.*, t1.name as team1_name, t1.emoji as team1_emoji,
                t2.name as team2_name, t2.emoji as team2_emoji
            FROM events e JOIN teams t1 ON e.team1_id=t1.id
            LEFT JOIN teams t2 ON e.team2_id=t2.id
            WHERE e.id=?
        ''', (event_id,)).fetchone()
    broadcast({'type': 'update', 'teams': [dict(t) for t in teams], 'latest_event': dict(ev)})
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
        teams = conn.execute('SELECT * FROM teams ORDER BY total_points DESC').fetchall()
    broadcast({'type': 'update', 'teams': [dict(t) for t in teams], 'latest_event': None})
    return jsonify({'ok': True})

@app.route('/api/games', methods=['POST'])
def add_game():
    data = request.json
    game_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute('INSERT INTO games VALUES (?,?,?,?)',
                     (game_id, data['name'], data['team_count'], data.get('category', 'game')))
    return jsonify({'ok': True, 'id': game_id})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    print('🍌 Operation inCISive — Multiverse of Minions')
    print(f'   Game Master: http://localhost:{port}/gamemaster')
    print(f'   OC Tracker:  http://localhost:{port}/')
    print('   Default OC password: minion2026')
    app.run(debug=False, host='0.0.0.0', port=port)
