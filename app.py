"""
Boys Art Activation Server
Deploy this on Render.com (free tier) for 24/7 worldwide access.
"""

import os
import sqlite3
import json
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'boysart2024master')
DB_PATH = os.environ.get('DB_PATH', 'boysart.db')


# ──────────────────────────────────────────────
#  DATABASE SETUP
# ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                phone     TEXT    NOT NULL,
                utr_id    TEXT    NOT NULL,
                status    TEXT    NOT NULL DEFAULT "pending",
                created_at TEXT   NOT NULL,
                updated_at TEXT   NOT NULL
            )
        ''')
        conn.commit()


init_db()


def row_to_dict(row):
    return {
        'id':        row['id'],
        'name':      row['name'],
        'phone':     row['phone'],
        'utrId':     row['utr_id'],
        'status':    row['status'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


# ──────────────────────────────────────────────
#  HEALTH CHECK
# ──────────────────────────────────────────────

@app.route('/api/healthz', methods=['GET'])
def healthz():
    return jsonify({'status': 'ok'})


# ──────────────────────────────────────────────
#  CLIENT ENDPOINTS
# ──────────────────────────────────────────────

@app.route('/api/activation/submit', methods=['POST'])
def submit():
    data = request.get_json(silent=True) or {}
    name  = str(data.get('name',  '')).strip()
    phone = str(data.get('phone', '')).strip()
    utr   = str(data.get('utrId', '')).strip()

    if not name or not phone or not utr:
        return jsonify({'ok': False, 'error': 'Please fill all fields: Name, Phone, and UTR.'}), 400

    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM submissions WHERE phone = ?', (phone,)
        ).fetchone()

        if row:
            if row['status'] == 'approved':
                return jsonify({'ok': True, 'status': 'approved', 'message': 'Already approved.', 'name': row['name']})
            return jsonify({'ok': True, 'status': row['status'], 'message': 'Already submitted. Awaiting approval.'})

        ts = now_iso()
        cur = conn.execute(
            'INSERT INTO submissions (name, phone, utr_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?)',
            (name, phone, utr, 'pending', ts, ts)
        )
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid, 'status': 'pending'})


@app.route('/api/activation/status/<path:phone>', methods=['GET'])
def check_status(phone):
    phone = phone.strip()
    if not phone:
        return jsonify({'ok': False, 'status': 'not_found', 'error': 'Phone required.'}), 400

    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM submissions WHERE phone = ?', (phone,)
        ).fetchone()

    if not row:
        return jsonify({'ok': False, 'status': 'not_found'})

    return jsonify({'ok': True, 'status': row['status'], 'name': row['name'], 'phone': row['phone']})


# ──────────────────────────────────────────────
#  MASTER / ADMIN ENDPOINTS
# ──────────────────────────────────────────────

def check_admin():
    secret = request.args.get('secret', '')
    if secret != ADMIN_SECRET:
        return False
    return True


@app.route('/api/admin/submissions', methods=['GET'])
def list_submissions():
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    status_filter = request.args.get('status', '').strip()

    with get_db() as conn:
        if status_filter in ('pending', 'approved', 'rejected'):
            rows = conn.execute(
                'SELECT * FROM submissions WHERE status = ? ORDER BY created_at DESC',
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM submissions ORDER BY created_at DESC'
            ).fetchall()

    return jsonify({'ok': True, 'submissions': [row_to_dict(r) for r in rows]})


@app.route('/api/admin/submissions/<int:sub_id>/approve', methods=['POST'])
def approve(sub_id):
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    with get_db() as conn:
        row = conn.execute('SELECT * FROM submissions WHERE id = ?', (sub_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        conn.execute(
            'UPDATE submissions SET status = ?, updated_at = ? WHERE id = ?',
            ('approved', now_iso(), sub_id)
        )
        conn.commit()

    return jsonify({'ok': True, 'status': 'approved', 'id': sub_id})


@app.route('/api/admin/submissions/<int:sub_id>/reject', methods=['POST'])
def reject(sub_id):
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    with get_db() as conn:
        row = conn.execute('SELECT * FROM submissions WHERE id = ?', (sub_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        conn.execute(
            'UPDATE submissions SET status = ?, updated_at = ? WHERE id = ?',
            ('rejected', now_iso(), sub_id)
        )
        conn.commit()

    return jsonify({'ok': True, 'status': 'rejected', 'id': sub_id})


# ──────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
