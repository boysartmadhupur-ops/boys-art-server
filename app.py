"""
Boys Art Activation + Update Server
Deploy on Render.com for 24/7 worldwide access.
"""

import os
import sqlite3
import json
import time
import base64
import ast
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import io

app = Flask(__name__)
CORS(app)

ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'boysart2024master')
DB_PATH = os.environ.get('DB_PATH', 'boysart.db')
PURCHASES_FILE = os.environ.get('PURCHASES_FILE', 'purchases.json')

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload


# ──────────────────────────────────────────────
#  DATABASE SETUP
# ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        # Existing: activation submissions
        conn.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                phone      TEXT    NOT NULL,
                utr_id     TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT "pending",
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
        ''')

        # NEW: DXF templates stored as binary blobs
        conn.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path     TEXT    NOT NULL UNIQUE,
                filename     TEXT    NOT NULL,
                file_data    BLOB    NOT NULL,
                file_size    INTEGER NOT NULL,
                uploaded_at  TEXT    NOT NULL
            )
        ''')

        # NEW: Template purchases by clients
        conn.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id  INTEGER NOT NULL,
                device_id    TEXT    NOT NULL,
                name         TEXT    NOT NULL,
                phone        TEXT    NOT NULL,
                utr_id       TEXT    NOT NULL,
                amount       INTEGER NOT NULL DEFAULT 5,
                status       TEXT    NOT NULL DEFAULT "pending",
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                FOREIGN KEY (template_id) REFERENCES templates(id)
            )
        ''')

        conn.commit()


init_db()


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def load_purchases():
    if not os.path.exists(PURCHASES_FILE):
        return {}
    try:
        with open(PURCHASES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        clean = {}
        for device_id, items in data.items():
            if isinstance(items, list):
                clean[str(device_id)] = [str(x) for x in items if str(x)]
        return clean
    except Exception:
        return {}


def save_purchases(purchases):
    safe = {}
    for device_id, items in purchases.items():
        seen = []
        for item in items:
            item = str(item)
            if item and item not in seen:
                seen.append(item)
        safe[str(device_id)] = seen
    tmp_path = PURCHASES_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(safe, f, indent=2)
    os.replace(tmp_path, PURCHASES_FILE)


def add_purchase_record(device_id, template_ids):
    ids = [str(x) for x in template_ids if str(x)]
    if not ids:
        return
    purchases = load_purchases()
    existing = purchases.get(device_id, [])
    for template_id in ids:
        if template_id not in existing:
            existing.append(template_id)
    purchases[device_id] = existing
    save_purchases(purchases)


def extract_template_values(data):
    values = []
    single = data.get('template_id', data.get('templateId', ''))
    if single:
        values.append(single)
    templates = data.get('templates', [])
    if not isinstance(templates, list):
        templates = [templates]
    for item in templates:
        value = ''
        if isinstance(item, dict):
            value = item.get('id') or item.get('template_id') or item.get('templateId') or item.get('rel_path') or item.get('path') or item.get('filename') or ''
        else:
            text = str(item).strip()
            if text.startswith('{') and text.endswith('}'):
                try:
                    parsed = ast.literal_eval(text)
                    if isinstance(parsed, dict):
                        value = parsed.get('id') or parsed.get('template_id') or parsed.get('templateId') or parsed.get('rel_path') or parsed.get('path') or parsed.get('filename') or ''
                    else:
                        value = text
                except Exception:
                    value = text
            else:
                value = text
        if value and value not in values:
            values.append(value)
    return values


def resolve_template_ids(conn, values):
    resolved = []
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        row = None
        if value.isdigit():
            row = conn.execute('SELECT id FROM templates WHERE id = ?', (int(value),)).fetchone()
        if not row:
            row = conn.execute(
                'SELECT id FROM templates WHERE rel_path = ? OR filename = ?',
                (value, os.path.basename(value))
            ).fetchone()
        if not row and not value.startswith('templates/'):
            row = conn.execute(
                'SELECT id FROM templates WHERE rel_path = ?',
                ('templates/' + value.replace('\\', '/'),)
            ).fetchone()
        if not row:
            row = conn.execute(
                'SELECT id FROM templates WHERE rel_path LIKE ?',
                ('%' + value.replace('\\', '/').split('/')[-1],)
            ).fetchone()
        template_id = str(row['id']) if row else value
        if template_id not in resolved:
            resolved.append(template_id)
    return resolved


def check_admin():
    secret = request.args.get('secret', '') or (request.get_json(silent=True) or {}).get('secret', '')
    return secret == ADMIN_SECRET


# ──────────────────────────────────────────────
#  HEALTH CHECK
# ──────────────────────────────────────────────

@app.route('/api/healthz', methods=['GET'])
def healthz():
    return jsonify({'status': 'ok'})


# ──────────────────────────────────────────────
#  ACTIVATION — CLIENT ENDPOINTS (unchanged)
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
#  ACTIVATION — ADMIN ENDPOINTS (unchanged)
# ──────────────────────────────────────────────

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
#  TEMPLATES — MASTER UPLOAD
# ──────────────────────────────────────────────

@app.route('/api/admin/upload-template', methods=['POST'])
def upload_template():
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    # Accept JSON body with base64-encoded file data
    data = request.get_json(silent=True) or {}
    rel_path = str(data.get('path', '') or data.get('rel_path', '')).strip().replace('\\', '/')
    b64_data = data.get('data', '')

    if not rel_path:
        return jsonify({'ok': False, 'error': 'path is required'}), 400

    if not b64_data:
        return jsonify({'ok': False, 'error': 'data (base64) is required'}), 400

    # Decode base64 file content
    try:
        file_data = base64.b64decode(b64_data)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Invalid base64 data: {e}'}), 400

    # Normalize path: ensure it starts with "templates/" (lowercase)
    parts = rel_path.replace('\\', '/').split('/')
    templates_idx = None
    for i, part in enumerate(parts):
        if part.lower() == 'templates':
            templates_idx = i
    if templates_idx is not None:
        rel_parts = parts[templates_idx:]
        rel_parts[0] = 'templates'
        rel_path = '/'.join(rel_parts)
    else:
        rel_path = 'templates/' + rel_path

    filename = os.path.basename(rel_path)
    ts = now_iso()

    with get_db() as conn:
        existing = conn.execute(
            'SELECT id FROM templates WHERE rel_path = ?', (rel_path,)
        ).fetchone()

        if existing:
            conn.execute(
                'UPDATE templates SET file_data = ?, file_size = ?, filename = ?, uploaded_at = ? WHERE rel_path = ?',
                (file_data, len(file_data), filename, ts, rel_path)
            )
            template_id = existing['id']
            conn.commit()
            return jsonify({'ok': True, 'id': template_id, 'updated': True, 'rel_path': rel_path})
        else:
            cur = conn.execute(
                'INSERT INTO templates (rel_path, filename, file_data, file_size, uploaded_at) VALUES (?,?,?,?,?)',
                (rel_path, filename, file_data, len(file_data), ts)
            )
            conn.commit()
            return jsonify({'ok': True, 'id': cur.lastrowid, 'updated': False, 'rel_path': rel_path})


# ──────────────────────────────────────────────
#  TEMPLATES — CLIENT: CHECK FOR UPDATES
# ──────────────────────────────────────────────

@app.route('/api/updates/available', methods=['GET'])
def updates_available():
    device_id = (request.args.get('device_id') or request.args.get('deviceId') or 'default_device').strip() or 'default_device'
    purchases = load_purchases()
    user_purchased = set(str(x) for x in purchases.get(device_id, []))

    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, rel_path, filename, file_size, uploaded_at FROM templates ORDER BY uploaded_at DESC'
        ).fetchall()

    templates = []
    for r in rows:
        if str(r['id']) in user_purchased or r['rel_path'] in user_purchased or r['filename'] in user_purchased:
            continue
        templates.append({
            'id':         r['id'],
            'template_id': r['id'],
            'rel_path':   r['rel_path'],
            'path':       r['rel_path'],
            'filename':   r['filename'],
            'name':       r['filename'],
            'file_size':  r['file_size'],
            'uploaded_at': r['uploaded_at'],
            'price':      5,
        })

    return jsonify({'ok': True, 'templates': templates})


# ──────────────────────────────────────────────
#  TEMPLATES — CLIENT: DOWNLOAD (after approval)
# ──────────────────────────────────────────────

@app.route('/api/templates/download/<int:template_id>', methods=['GET'])
def download_template(template_id):
    device_id = request.args.get('device_id', '').strip()
    phone = request.args.get('phone', '').strip()

    if not device_id or not phone:
        return jsonify({'error': 'device_id and phone are required'}), 400

    with get_db() as conn:
        # Check purchase is approved for this device+phone combo
        purchase = conn.execute(
            '''SELECT p.status FROM purchases p
               WHERE p.template_id = ? AND p.device_id = ? AND p.phone = ? AND p.status = "approved"
               LIMIT 1''',
            (template_id, device_id, phone)
        ).fetchone()

        if not purchase:
            return jsonify({'error': 'Purchase not approved or not found'}), 403

        tmpl = conn.execute(
            'SELECT filename, file_data FROM templates WHERE id = ?', (template_id,)
        ).fetchone()

        if not tmpl:
            return jsonify({'error': 'Template not found'}), 404

    return send_file(
        io.BytesIO(tmpl['file_data']),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=tmpl['filename']
    )


@app.route('/api/templates/download', methods=['GET'])
def download_template_by_path():
    rel_path = request.args.get('path', '').strip().replace('\\', '/')

    if not rel_path:
        return jsonify({'error': 'path is required'}), 400

    with get_db() as conn:
        tmpl = conn.execute(
            'SELECT filename, file_data FROM templates WHERE rel_path = ?',
            (rel_path,)
        ).fetchone()

        if not tmpl and not rel_path.startswith('templates/'):
            tmpl = conn.execute(
                'SELECT filename, file_data FROM templates WHERE rel_path = ?',
                ('templates/' + rel_path,)
            ).fetchone()

        if not tmpl:
            tmpl = conn.execute(
                'SELECT filename, file_data FROM templates WHERE rel_path LIKE ?',
                ('%' + rel_path.split('/')[-1],)
            ).fetchone()

        if not tmpl:
            return jsonify({'error': 'Template not found'}), 404

    return send_file(
        io.BytesIO(tmpl['file_data']),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=tmpl['filename']
    )


# ──────────────────────────────────────────────
#  PURCHASES — CLIENT: SUBMIT PAYMENT
# ──────────────────────────────────────────────

@app.route('/api/purchases/submit', methods=['POST'])
def submit_purchase():
    data = request.get_json(silent=True) or {}
    utr_id      = str(data.get('utr_id') or data.get('utr') or data.get('utrId') or '').strip()
    device_id   = str(data.get('device_id') or data.get('deviceId') or 'default_device').strip() or 'default_device'
    name        = str(data.get('name') or data.get('clientName') or '').strip()
    phone       = str(data.get('phone', '')).strip()
    amount      = data.get('amount', 5)
    template_values = extract_template_values(data)

    if not utr_id:
        return jsonify({'ok': False, 'error': 'UTR / Transaction ID is required'}), 400

    with get_db() as conn:
        template_ids = resolve_template_ids(conn, template_values)
        if template_ids:
            add_purchase_record(device_id, template_ids)
        ts = now_iso()
        created_ids = []
        for template_id in template_ids:
            if not str(template_id).isdigit():
                continue
            existing = conn.execute(
                'SELECT * FROM purchases WHERE template_id = ? AND device_id = ? AND phone = ?',
                (int(template_id), device_id, phone)
            ).fetchone()
            if existing:
                created_ids.append(existing['id'])
                continue
            cur = conn.execute(
                '''INSERT INTO purchases (template_id, device_id, name, phone, utr_id, amount, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (int(template_id), device_id, name, phone, utr_id, amount, 'pending', ts, ts)
            )
            created_ids.append(cur.lastrowid)
        conn.commit()
        return jsonify({'ok': True, 'id': created_ids[0] if created_ids else None, 'ids': created_ids, 'status': 'pending'})


# ──────────────────────────────────────────────
#  PURCHASES — CLIENT: CHECK STATUS
# ──────────────────────────────────────────────

@app.route('/api/purchases/status', methods=['GET'])
def purchase_status():
    template_id = (request.args.get('template_id') or request.args.get('templateId') or '').strip()
    device_id   = (request.args.get('device_id') or request.args.get('deviceId') or 'default_device').strip() or 'default_device'
    phone       = request.args.get('phone', '').strip()

    with get_db() as conn:
        if template_id:
            row = conn.execute(
                'SELECT * FROM purchases WHERE template_id = ? AND device_id = ? AND phone = ?',
                (template_id, device_id, phone)
            ).fetchone()

            if not row:
                return jsonify({'ok': False, 'status': 'not_found'})

            return jsonify({
                'ok':     True,
                'id':     row['id'],
                'status': row['status'],
            })

        rows = conn.execute(
            '''SELECT p.*, t.rel_path, t.filename FROM purchases p
               JOIN templates t ON t.id = p.template_id
               WHERE p.device_id = ? AND p.phone = ? AND p.status = "approved"
               ORDER BY p.updated_at DESC''',
            (device_id, phone)
        ).fetchall()

    approved = []
    for r in rows:
        approved.append({
            'id': r['template_id'],
            'template_id': r['template_id'],
            'path': r['rel_path'],
            'rel_path': r['rel_path'],
            'filename': r['filename'],
            'name': r['filename'],
        })

    return jsonify({'ok': True, 'status': 'approved' if approved else 'not_found', 'approvedTemplates': approved})


# ──────────────────────────────────────────────
#  PURCHASES — ADMIN: LIST & APPROVE/REJECT
# ──────────────────────────────────────────────

@app.route('/api/admin/purchases', methods=['GET'])
def list_purchases():
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    status_filter = request.args.get('status', '').strip()

    with get_db() as conn:
        if status_filter in ('pending', 'approved', 'rejected'):
            rows = conn.execute(
                '''SELECT p.*, t.rel_path, t.filename FROM purchases p
                   JOIN templates t ON t.id = p.template_id
                   WHERE p.status = ? ORDER BY p.created_at DESC''',
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT p.*, t.rel_path, t.filename FROM purchases p
                   JOIN templates t ON t.id = p.template_id
                   ORDER BY p.created_at DESC'''
            ).fetchall()

    result = []
    for r in rows:
        result.append({
            'id':          r['id'],
            'templateId':  r['template_id'],
            'relPath':     r['rel_path'],
            'filename':    r['filename'],
            'templates':   [r['rel_path']],
            'deviceId':    r['device_id'],
            'clientName':  r['name'],
            'name':        r['name'],
            'phone':       r['phone'],
            'utr':         r['utr_id'],
            'utrId':       r['utr_id'],
            'amount':      r['amount'],
            'status':      r['status'],
            'createdAt':   r['created_at'],
            'updatedAt':   r['updated_at'],
        })

    return jsonify({'ok': True, 'purchases': result})


@app.route('/api/admin/purchases/<int:purchase_id>/approve', methods=['POST'])
def approve_purchase(purchase_id):
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    with get_db() as conn:
        row = conn.execute('SELECT * FROM purchases WHERE id = ?', (purchase_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        conn.execute(
            'UPDATE purchases SET status = ?, updated_at = ? WHERE id = ?',
            ('approved', now_iso(), purchase_id)
        )
        conn.commit()

    return jsonify({'ok': True, 'status': 'approved', 'id': purchase_id})


@app.route('/api/admin/purchases/<int:purchase_id>/reject', methods=['POST'])
def reject_purchase(purchase_id):
    if not check_admin():
        return jsonify({'error': 'Forbidden'}), 403

    with get_db() as conn:
        row = conn.execute('SELECT * FROM purchases WHERE id = ?', (purchase_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        conn.execute(
            'UPDATE purchases SET status = ?, updated_at = ? WHERE id = ?',
            ('rejected', now_iso(), purchase_id)
        )
        conn.commit()

    return jsonify({'ok': True, 'status': 'rejected', 'id': purchase_id})


# ──────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
