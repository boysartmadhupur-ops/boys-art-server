"""
Boys Art Server — Flask Backend
Deployed on Render at https://boys-art-server.onrender.com

Endpoints
---------
POST /api/activate                  — Client requests activation
GET  /api/activation/status         — Client checks activation status
POST /api/submit-purchase           — Client submits template purchase
GET  /api/templates                 — Client lists available templates
GET  /api/templates/<path>          — Client downloads a template

GET  /api/admin/submissions         — Master lists activation requests
POST /api/admin/approve             — Master approves activation
POST /api/admin/reject              — Master rejects activation
GET  /api/admin/purchases           — Master lists purchase requests
POST /api/admin/approve-purchase    — Master approves purchase
POST /api/admin/reject-purchase     — Master rejects purchase
POST /api/admin/upload-template     — Master uploads a template

POST /api/convert/cdr               — Convert CDR → DXF (server-side, in-memory)
GET  /api/health                    — Health check
"""

import os
import io
import re
import base64
import json
import math
import zipfile
import sqlite3
import datetime

from flask import Flask, request, jsonify, g
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'boysart2024master')
DB_PATH      = os.environ.get('DB_PATH', '/tmp/boysart.db')

ALLOWED_UPLOAD_EXTS = {'.dxf', '.cdr', '.ai', '.plt', '.eps'}
BLOCKED_UPLOAD_EXTS = {'.pdf'}

# ─────────────────────────────────────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('''CREATE TABLE IF NOT EXISTS activations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id   TEXT NOT NULL,
        name        TEXT,
        phone       TEXT,
        utr         TEXT,
        status      TEXT DEFAULT 'pending',
        created_at  TEXT DEFAULT (datetime('now'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS purchases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id   TEXT NOT NULL,
        client_name TEXT,
        templates   TEXT,
        amount      REAL,
        utr         TEXT,
        status      TEXT DEFAULT 'pending',
        created_at  TEXT DEFAULT (datetime('now'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS templates (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT UNIQUE NOT NULL,
        ext         TEXT,
        data        BLOB NOT NULL,
        uploaded_at TEXT DEFAULT (datetime('now'))
    )''')
    db.commit()
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Auth helper
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(data=None):
    """
    Accept secret from query string, JSON body, or form.
    """
    candidates = []
    if data is not None:
        try:
            candidates.append(data.get('secret'))
        except Exception:
            pass
    candidates.append(request.args.get('secret'))
    try:
        body = request.get_json(force=True, silent=True) or {}
        candidates.append(body.get('secret'))
    except Exception:
        pass
    try:
        candidates.append(request.form.get('secret'))
    except Exception:
        pass
    for s in candidates:
        if s and s == ADMIN_SECRET:
            return None
    return jsonify({'error': 'Forbidden'}), 403


def _row_to_submission(r):
    return {
        'id': r['id'],
        'deviceId': r['device_id'] or '',
        'device_id': r['device_id'] or '',
        'name': r['name'] or '',
        'phone': r['phone'] or '',
        'utr': r['utr'] or '',
        'utrId': r['utr'] or '',
        'status': r['status'] or 'pending',
        'createdAt': r['created_at'] or '',
        'created_at': r['created_at'] or '',
        'amount': 999,
    }


def _row_to_purchase(r):
    return {
        'id': r['id'],
        'deviceId': r['device_id'] or '',
        'device_id': r['device_id'] or '',
        'clientName': r['client_name'] or '',
        'name': r['client_name'] or '',
        'templates': r['templates'] or '',
        'amount': r['amount'] or 0,
        'utr': r['utr'] or '',
        'utrId': r['utr'] or '',
        'status': r['status'] or 'pending',
        'createdAt': r['created_at'] or '',
        'created_at': r['created_at'] or '',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Health
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'Boys Art Server'})


# ─────────────────────────────────────────────────────────────────────────────
#  Activation
# ─────────────────────────────────────────────────────────────────────────────

def _lookup_activation(db, device_id='', phone=''):
    """
    Lookup an activation row by device_id (preferred) or phone.
    Returns the row or None.
    """
    if device_id:
        row = db.execute(
            'SELECT id, device_id, name, phone, utr, status, created_at '
            'FROM activations WHERE device_id = ? '
            'ORDER BY id DESC LIMIT 1',
            (device_id,)
        ).fetchone()
        if row:
            return row
    if phone:
        row = db.execute(
            'SELECT id, device_id, name, phone, utr, status, created_at '
            'FROM activations WHERE phone = ? '
            'ORDER BY (status="approved") DESC, id DESC LIMIT 1',
            (phone,)
        ).fetchone()
        if row:
            return row
    return None


@app.route('/api/activate', methods=['POST'])
def activate():
    data = request.get_json(force=True, silent=True) or {}
    device_id = (data.get('device_id') or data.get('deviceId') or '').strip()
    phone     = (data.get('phone') or '').strip()
    name      = (data.get('name')  or '').strip()
    utr       = (data.get('utr') or data.get('utrId') or '').strip()

    if not device_id and not phone:
        return jsonify({'ok': False, 'error': 'device_id required'}), 400

    db = get_db()
    existing = _lookup_activation(db, device_id, phone)

    if existing:
        status = existing['status']
        if status == 'approved':
            # Recognised device / phone — no payment needed.
            return jsonify({
                'ok': True, 'status': 'approved',
                'name': existing['name'] or name,
                'id': existing['id'],
            })
        if status == 'pending':
            # If the same device re-submits, just acknowledge.
            if device_id and existing['device_id'] == device_id:
                return jsonify({
                    'ok': True, 'status': 'pending',
                    'id': existing['id'],
                    'message': 'Awaiting approval.',
                })
        # rejected, or a different device on same phone -> fall through to insert

    db.execute(
        'INSERT INTO activations (device_id, name, phone, utr) VALUES (?, ?, ?, ?)',
        (device_id, name, phone, utr)
    )
    db.commit()
    new_id = db.execute('SELECT last_insert_rowid() AS i').fetchone()['i']
    return jsonify({
        'ok': True, 'status': 'pending', 'id': new_id,
        'message': 'Activation request submitted. Awaiting approval.'
    })


# ── Alias used by client: POST /api/activation/submit ──────────────
@app.route('/api/activation/submit', methods=['POST'])
def activation_submit():
    return activate()


@app.route('/api/activation/status')
def activation_status():
    device_id = request.args.get('device_id', '').strip()
    phone     = request.args.get('phone', '').strip()
    db = get_db()
    row = _lookup_activation(db, device_id, phone)
    if not row:
        return jsonify({'status': 'not_found'})
    return jsonify({
        'status': row['status'],
        'name': row['name'] or '',
        'id': row['id'],
    })


# ── Alias used by client: GET /api/activation/status/<phone> ───────
@app.route('/api/activation/status/<path:phone>')
def activation_status_by_phone(phone):
    device_id = request.args.get('device_id', '').strip()
    db = get_db()
    row = _lookup_activation(db, device_id, phone.strip())
    if not row:
        return jsonify({'status': 'not_found'})
    return jsonify({
        'status': row['status'],
        'name': row['name'] or '',
        'id': row['id'],
    })


# ── Server-side device memory check ────────────────────────────────
@app.route('/api/activation/check-device')
def activation_check_device():
    device_id = request.args.get('device_id', '').strip()
    if not device_id:
        return jsonify({'status': 'not_found'})
    db = get_db()
    row = _lookup_activation(db, device_id, '')
    if not row:
        return jsonify({'status': 'not_found'})
    return jsonify({
        'status': row['status'],
        'name': row['name'] or '',
        'phone': row['phone'] or '',
        'id': row['id'],
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Template purchase
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/submit-purchase', methods=['POST'])
def submit_purchase():
    data      = request.get_json(force=True, silent=True) or {}
    device_id = data.get('device_id', '').strip()
    templates = data.get('templates', '')
    amount    = data.get('amount', 0)
    utr       = data.get('utr', '').strip()
    name      = data.get('name', '')

    if not device_id or not utr:
        return jsonify({'error': 'device_id and utr required'}), 400

    db = get_db()
    db.execute(
        'INSERT INTO purchases (device_id, client_name, templates, amount, utr) '
        'VALUES (?, ?, ?, ?, ?)',
        (device_id, name, templates, amount, utr)
    )
    db.commit()
    return jsonify({'ok': True, 'message': 'Purchase request submitted.'})


# ─────────────────────────────────────────────────────────────────────────────
#  Template library
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/templates')
def list_templates():
    db   = get_db()
    rows = db.execute('SELECT path, ext, uploaded_at FROM templates ORDER BY path').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/templates/<path:tmpl_path>')
def download_template(tmpl_path):
    db  = get_db()
    row = db.execute('SELECT data, ext FROM templates WHERE path = ?',
                     (tmpl_path,)).fetchone()
    if not row:
        return jsonify({'error': 'Template not found'}), 404

    b64 = base64.b64encode(row['data']).decode('utf-8')
    return jsonify({'ok': True, 'path': tmpl_path, 'ext': row['ext'], 'data': b64})


# ─────────────────────────────────────────────────────────────────────────────
#  Admin — activations
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/admin/submissions')
def admin_submissions():
    err = _require_admin()
    if err:
        return err
    status_filter = request.args.get('status', '').strip().lower()
    db   = get_db()
    if status_filter and status_filter != 'all':
        rows = db.execute(
            'SELECT id, device_id, name, phone, utr, status, created_at '
            'FROM activations WHERE status = ? ORDER BY created_at DESC',
            (status_filter,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT id, device_id, name, phone, utr, status, created_at '
            'FROM activations ORDER BY created_at DESC'
        ).fetchall()
    items = [_row_to_submission(r) for r in rows]
    # Master expects {"submissions": [...]}, keep raw list as fallback under a key.
    return jsonify({'submissions': items, 'count': len(items)})


def _set_submission_status(sub_id, new_status):
    db = get_db()
    db.execute('UPDATE activations SET status = ? WHERE id = ?', (new_status, sub_id))
    db.commit()
    return jsonify({'ok': True, 'id': sub_id, 'status': new_status})


@app.route('/api/admin/approve', methods=['POST'])
def admin_approve():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    sub_id = data.get('id') or request.args.get('id')
    if not sub_id:
        return jsonify({'error': 'id required'}), 400
    return _set_submission_status(sub_id, 'approved')


@app.route('/api/admin/reject', methods=['POST'])
def admin_reject():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    sub_id = data.get('id') or request.args.get('id')
    if not sub_id:
        return jsonify({'error': 'id required'}), 400
    return _set_submission_status(sub_id, 'rejected')


# ── Aliases used by master: /api/admin/submissions/<id>/approve|reject
@app.route('/api/admin/submissions/<int:sub_id>/approve', methods=['POST'])
def admin_submissions_approve(sub_id):
    err = _require_admin()
    if err:
        return err
    return _set_submission_status(sub_id, 'approved')


@app.route('/api/admin/submissions/<int:sub_id>/reject', methods=['POST'])
def admin_submissions_reject(sub_id):
    err = _require_admin()
    if err:
        return err
    return _set_submission_status(sub_id, 'rejected')


# ─────────────────────────────────────────────────────────────────────────────
#  Admin — purchases
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/admin/purchases')
def admin_purchases():
    err = _require_admin()
    if err:
        return err
    status_filter = request.args.get('status', '').strip().lower()
    db   = get_db()
    if status_filter and status_filter != 'all':
        rows = db.execute(
            'SELECT id, device_id, client_name, templates, amount, utr, status, created_at '
            'FROM purchases WHERE status = ? ORDER BY created_at DESC',
            (status_filter,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT id, device_id, client_name, templates, amount, utr, status, created_at '
            'FROM purchases ORDER BY created_at DESC'
        ).fetchall()
    items = [_row_to_purchase(r) for r in rows]
    return jsonify({'purchases': items, 'count': len(items)})


def _set_purchase_status(pid, new_status):
    db = get_db()
    db.execute('UPDATE purchases SET status = ? WHERE id = ?', (new_status, pid))
    db.commit()
    return jsonify({'ok': True, 'id': pid, 'status': new_status})


@app.route('/api/admin/approve-purchase', methods=['POST'])
def admin_approve_purchase():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    pid = data.get('id') or request.args.get('id')
    if not pid:
        return jsonify({'error': 'id required'}), 400
    return _set_purchase_status(pid, 'approved')


@app.route('/api/admin/purchases/<int:pid>/approve', methods=['POST'])
def admin_purchases_approve(pid):
    err = _require_admin()
    if err:
        return err
    return _set_purchase_status(pid, 'approved')


@app.route('/api/admin/purchases/<int:pid>/reject', methods=['POST'])
def admin_purchases_reject(pid):
    err = _require_admin()
    if err:
        return err
    return _set_purchase_status(pid, 'rejected')


@app.route('/api/admin/reject-purchase', methods=['POST'])
def admin_reject_purchase():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    pid = data.get('id') or request.args.get('id')
    if not pid:
        return jsonify({'error': 'id required'}), 400
    return _set_purchase_status(pid, 'rejected')


# ─────────────────────────────────────────────────────────────────────────────
#  Admin — template upload (Master → Server)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/admin/upload-template', methods=['POST'])
def admin_upload_template():
    data = request.get_json(force=True, silent=True) or {}
    err  = _require_admin(data)
    if err:
        return err

    path_str  = data.get('path', '').strip().replace('\\', '/')
    b64_data  = data.get('data', '')

    if not path_str or not b64_data:
        return jsonify({'error': 'path and data required'}), 400

    ext = os.path.splitext(path_str)[1].lower()

    # ── Block PDF ─────────────────────────────────────────────────────
    if ext == '.pdf':
        return jsonify({'error': 'PDF files are not allowed'}), 400

    # ── Validate extension ────────────────────────────────────────────
    if ext not in ALLOWED_UPLOAD_EXTS:
        return jsonify({'error': f'Unsupported file type: {ext}'}), 400

    try:
        file_bytes = base64.b64decode(b64_data)
    except Exception:
        return jsonify({'error': 'Invalid base64 data'}), 400

    db = get_db()
    db.execute(
        'INSERT INTO templates (path, ext, data) VALUES (?, ?, ?) '
        'ON CONFLICT(path) DO UPDATE SET data=excluded.data, ext=excluded.ext, '
        'uploaded_at=datetime("now")',
        (path_str, ext, file_bytes)
    )
    db.commit()
    return jsonify({'ok': True, 'path': path_str, 'ext': ext,
                    'size': len(file_bytes)})


# ─────────────────────────────────────────────────────────────────────────────
#  CDR Conversion — in-memory, no disk writes
# ─────────────────────────────────────────────────────────────────────────────

def _cdr_to_dxf(cdr_bytes: bytes) -> str:
    """
    Convert CDR bytes → DXF string entirely in RAM.

    Strategy (tried in order until paths are found):
      1. ZIP extraction: CDR X4+ files are ZIP archives with XML content
         containing SVG-like path data.
      2. XML pattern scan on raw bytes for coordinates.
      3. Binary float32 scan as a last-resort fallback that produces a
         reasonable bounding frame from any geometry found.

    Returns a DXF string (ASCII) ready to be parsed by dxf_parser.parse_dxf.
    """
    paths_mm = []   # list of {'pts': [(x,y),...], 'closed': bool}

    PT_TO_MM = 25.4 / 72.0   # PostScript points → mm

    # ── Attempt 1: ZIP + XML (CDR X4 / X5 / X6 / 2019-2023) ─────────
    if _try_zip_xml(cdr_bytes, paths_mm, PT_TO_MM):
        pass   # populated
    # ── Attempt 2: Raw XML scan ───────────────────────────────────────
    elif _try_raw_xml(cdr_bytes, paths_mm, PT_TO_MM):
        pass
    # ── Attempt 3: Binary coordinate extraction ───────────────────────
    else:
        _try_binary(cdr_bytes, paths_mm)
        paths_mm[:] = _filter_plausible_paths(paths_mm)

    if not paths_mm:
        return ''

    return _build_dxf(paths_mm)


def _filter_plausible_paths(paths_mm: list) -> list:
    cleaned = []
    for path in paths_mm:
        pts = path.get('pts', [])
        if len(pts) < 6:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if w < 1.0 or h < 1.0 or w > 350.0 or h > 350.0:
            continue
        unique = {(round(x, 2), round(y, 2)) for x, y in pts}
        if len(unique) < 6:
            continue
        cleaned.append(path)
    if len(cleaned) == 1:
        pts = cleaned[0].get('pts', [])
        if len(pts) <= 8:
            xs = sorted({round(p[0], 3) for p in pts})
            ys = sorted({round(p[1], 3) for p in pts})
            if len(xs) <= 2 and len(ys) <= 2:
                return []
    return cleaned


def _try_zip_xml(data: bytes, out: list, pt2mm: float) -> bool:
    """Try to unzip CDR and scan its XML files for path data."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            # Look for content files — preference order
            content_names = sorted(
                [n for n in names if n.lower().endswith(('.xml', '.cdr', '.svg'))],
                key=lambda n: (0 if 'content' in n.lower() else 1)
            )
            for name in content_names:
                xml_bytes = zf.read(name)
                try:
                    xml_str = xml_bytes.decode('utf-8', errors='replace')
                except Exception:
                    continue
                if _parse_svg_paths(xml_str, out, pt2mm):
                    return True
                if _parse_cdr_xml_paths(xml_str, out, pt2mm):
                    return True
    except zipfile.BadZipFile:
        pass
    except Exception as e:
        print(f'CDR zip error: {e}')
    return bool(out)


def _try_raw_xml(data: bytes, out: list, pt2mm: float) -> bool:
    """Try to decode CDR bytes as text and scan for SVG path data."""
    try:
        text = data.decode('utf-8', errors='replace')
        return _parse_svg_paths(text, out, pt2mm) or _parse_cdr_xml_paths(text, out, pt2mm)
    except Exception:
        return False


def _parse_svg_paths(text: str, out: list, pt2mm: float) -> bool:
    """Extract SVG <path d="..."> from XML/SVG content."""
    found = False
    for m in re.finditer(r'[dD]\s*=\s*["\']([^"\']{4,})["\']', text):
        pts, closed = _svg_d_to_pts(m.group(1), pt2mm)
        if pts:
            out.append({'pts': pts, 'closed': closed})
            found = True
    return found


def _svg_d_to_pts(d: str, pt2mm: float) -> tuple:
    """Parse SVG path d attribute into list of mm points."""
    pts     = []
    closed  = False
    cx, cy  = 0.0, 0.0
    sx, sy  = 0.0, 0.0   # subpath start

    nums_re = re.compile(r'-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')

    def _nums(s):
        return [float(x) for x in nums_re.findall(s)]

    # Split by command letters, keeping the letter
    parts = re.findall(r'[MmZzLlHhVvCcSsQqTtAa][^MmZzLlHhVvCcSsQqTtAa]*', d)
    last_ctrl = None

    for part in parts:
        cmd   = part[0]
        ns    = _nums(part[1:])
        rel   = cmd.islower()

        if cmd in ('M', 'm'):
            for i in range(0, len(ns) - 1, 2):
                x = (cx + ns[i] if rel and pts else ns[i]) * pt2mm
                y = (cy + ns[i+1] if rel and pts else ns[i+1]) * pt2mm
                cx, cy = x / pt2mm, y / pt2mm
                sx, sy = cx, cy
                pts.append((x, y))
            last_ctrl = None

        elif cmd in ('L', 'l'):
            for i in range(0, len(ns) - 1, 2):
                x = ((cx + ns[i]) if rel else ns[i]) * pt2mm
                y = ((cy + ns[i+1]) if rel else ns[i+1]) * pt2mm
                cx, cy = x / pt2mm, y / pt2mm
                pts.append((x, y))
            last_ctrl = None

        elif cmd in ('H', 'h'):
            for n in ns:
                x = ((cx + n) if rel else n) * pt2mm
                cx = x / pt2mm
                pts.append((x, cy * pt2mm))
            last_ctrl = None

        elif cmd in ('V', 'v'):
            for n in ns:
                y = ((cy + n) if rel else n) * pt2mm
                cy = y / pt2mm
                pts.append((cx * pt2mm, y))
            last_ctrl = None

        elif cmd in ('C', 'c'):
            for i in range(0, len(ns) - 5, 6):
                if rel:
                    x1, y1 = (cx + ns[i])   * pt2mm, (cy + ns[i+1]) * pt2mm
                    x2, y2 = (cx + ns[i+2]) * pt2mm, (cy + ns[i+3]) * pt2mm
                    x3, y3 = (cx + ns[i+4]) * pt2mm, (cy + ns[i+5]) * pt2mm
                else:
                    x1, y1 = ns[i]*pt2mm, ns[i+1]*pt2mm
                    x2, y2 = ns[i+2]*pt2mm, ns[i+3]*pt2mm
                    x3, y3 = ns[i+4]*pt2mm, ns[i+5]*pt2mm
                bpts = _bez3(pts[-1] if pts else (0,0),
                              (x1, y1), (x2, y2), (x3, y3))
                pts.extend(bpts[1:])
                cx, cy = x3 / pt2mm, y3 / pt2mm
                last_ctrl = (x2 / pt2mm, y2 / pt2mm)

        elif cmd in ('Q', 'q'):
            for i in range(0, len(ns) - 3, 4):
                if rel:
                    x1, y1 = (cx + ns[i])*pt2mm,   (cy + ns[i+1])*pt2mm
                    x2, y2 = (cx + ns[i+2])*pt2mm, (cy + ns[i+3])*pt2mm
                else:
                    x1, y1 = ns[i]*pt2mm, ns[i+1]*pt2mm
                    x2, y2 = ns[i+2]*pt2mm, ns[i+3]*pt2mm
                bpts = _bez2(pts[-1] if pts else (0,0), (x1,y1), (x2,y2))
                pts.extend(bpts[1:])
                cx, cy = x2/pt2mm, y2/pt2mm
                last_ctrl = (x1/pt2mm, y1/pt2mm)

        elif cmd in ('Z', 'z'):
            if pts:
                pts.append((sx * pt2mm, sy * pt2mm))
            closed = True

    return pts, closed


def _parse_cdr_xml_paths(text: str, out: list, pt2mm: float) -> bool:
    """Scan CDR XML for coordinate tags."""
    found = False
    # CDR XML stores paths in polyline-like tags with x/y attributes
    for m in re.finditer(r'<(?:poly|path|shape)[^>]*>', text, re.I):
        tag = m.group(0)
        xs  = re.findall(r'[xX]="(-?[\d.]+)"', tag)
        ys  = re.findall(r'[yY]="(-?[\d.]+)"', tag)
        if xs and ys and len(xs) == len(ys):
            try:
                pts = [(float(x) * pt2mm, float(y) * pt2mm)
                       for x, y in zip(xs, ys)]
                if len(pts) >= 2:
                    out.append({'pts': pts, 'closed': False})
                    found = True
            except ValueError:
                pass
    return found


def _try_binary(data: bytes, out: list) -> bool:
    """
    Last-resort: scan binary CDR for float32 coordinate pairs.
    Looks for runs of plausible (x, y) values in the range typical for
    mobile skin templates (0–500 mm).
    """
    import struct
    MIN_MM, MAX_MM = -5.0, 600.0
    pts = []
    i   = 0
    while i + 8 <= len(data):
        try:
            x = struct.unpack_from('<f', data, i)[0]
            y = struct.unpack_from('<f', data, i + 4)[0]
            if MIN_MM <= x <= MAX_MM and MIN_MM <= y <= MAX_MM:
                pts.append((float(x), float(y)))
            else:
                if len(pts) >= 4:
                    out.append({'pts': pts[:], 'closed': False})
                pts = []
        except struct.error:
            pass
        i += 4
    if len(pts) >= 4:
        out.append({'pts': pts, 'closed': False})
    return bool(out)


def _bez3(p0, p1, p2, p3, steps=24):
    """Cubic Bezier → point list."""
    pts = []
    for i in range(steps + 1):
        t  = i / steps; mt = 1 - t
        x  = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
        y  = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
        pts.append((x, y))
    return pts


def _bez2(p0, p1, p2, steps=16):
    """Quadratic Bezier → point list."""
    pts = []
    for i in range(steps + 1):
        t  = i / steps; mt = 1 - t
        x  = mt**2*p0[0] + 2*mt*t*p1[0] + t**2*p2[0]
        y  = mt**2*p0[1] + 2*mt*t*p1[1] + t**2*p2[1]
        pts.append((x, y))
    return pts


def _build_dxf(paths_mm: list) -> str:
    """Convert list of path dicts to a minimal DXF string."""
    lines = [
        '  0', 'SECTION', '  2', 'HEADER',
        '  9', '$INSUNITS', ' 70', '4',   # 4 = mm
        '  0', 'ENDSEC',
        '  0', 'SECTION', '  2', 'ENTITIES',
    ]
    for path in paths_mm:
        pts    = path['pts']
        closed = path.get('closed', False)
        if len(pts) < 2:
            continue
        # Emit as LWPOLYLINE
        lines += [
            '  0', 'LWPOLYLINE',
            '  8', '0',
            ' 70', '1' if closed else '0',
            ' 90', str(len(pts)),
        ]
        for x, y in pts:
            lines += [' 10', f'{x:.6f}', ' 20', f'{y:.6f}']
    lines += ['  0', 'ENDSEC', '  0', 'EOF']
    return '\n'.join(lines)


@app.route('/api/convert/cdr', methods=['POST'])
def convert_cdr():
    """
    POST /api/convert/cdr
    Body: { "filename": "xxx.cdr", "data": "<base64>" }
    Returns: { "ok": true, "dxf": "<base64 DXF>" }
         or: { "ok": false, "error": "..." }

    Security: CDR data is never written to disk.
    Processing is entirely in RAM.
    """
    body = request.get_json(force=True, silent=True) or {}
    b64  = body.get('data', '')
    if not b64:
        return jsonify({'ok': False, 'error': 'No data provided'}), 400

    try:
        cdr_bytes = base64.b64decode(b64)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid base64 data'}), 400

    if len(cdr_bytes) < 4:
        return jsonify({'ok': False, 'error': 'File too small'}), 400

    try:
        dxf_str = _cdr_to_dxf(cdr_bytes)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Conversion error: {e}'}), 500

    if not dxf_str:
        return jsonify({'ok': False,
                        'error': 'No vector geometry found in CDR file. '
                                 'Only CDR X4 and newer (2008+) are supported.'}), 422

    dxf_b64 = base64.b64encode(dxf_str.encode('utf-8')).decode('utf-8')
    return jsonify({'ok': True, 'dxf': dxf_b64})


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
else:
    init_db()
