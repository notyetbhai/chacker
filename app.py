"""
FlareCloud Web Interface
Wraps the existing FlareCloud.py checker with a Flask web server.
The checking logic is NOT modified — only the I/O layer is replaced.
"""

import os
import sys
import io
import re
import time
import json
import zipfile
import hashlib
import secrets
import string
import threading
import concurrent.futures
from collections import deque
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, send_file, session, redirect, url_for

# ── Ensure this directory is on the path so we can import FlareCloud ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════
#  LIVE LOG CAPTURE (stdout interceptor)
# ═══════════════════════════════════════════

log_buffer = deque(maxlen=500)
log_lock = threading.Lock()


class LogCapture(io.TextIOBase):
    """Wraps stdout to capture checker prints into a buffer for live preview."""

    def __init__(self, original_stdout):
        self.original = original_stdout

    def write(self, text):
        if isinstance(text, bytes):
            try:
                text = text.decode('utf-8', errors='ignore')
            except:
                text = str(text)
        if text and text.strip():
            clean = re.sub(r'\x1b\[[0-9;]*m', '', text).strip()
            if clean:
                timestamp = datetime.now().strftime('%H:%M:%S')
                log_type = 'info'
                if 'Hit:' in clean or 'hit:' in clean:
                    log_type = 'hit'
                elif 'Bad:' in clean or 'bad:' in clean:
                    log_type = 'bad'
                elif '2FA:' in clean or '2fa:' in clean:
                    log_type = 'twofa'
                elif 'Valid Mail:' in clean:
                    log_type = 'valid'
                elif 'Xbox Game Pass Ultimate:' in clean:
                    log_type = 'xgpu'
                elif 'Xbox Game Pass:' in clean:
                    log_type = 'xgp'
                elif 'Other:' in clean:
                    log_type = 'other'
                elif 'Scraped' in clean or 'Loaded' in clean:
                    log_type = 'system'
                elif 'Error' in clean or 'error' in clean:
                    log_type = 'error'
                entry = {'time': timestamp, 'type': log_type, 'text': clean}
                with log_lock:
                    log_buffer.append(entry)
        try:
            if self.original:
                self.original.write(text)
        except:
            pass
        return len(text) if text else 0

    def flush(self):
        try:
            if self.original:
                self.original.flush()
        except:
            pass

    def fileno(self):
        return self.original.fileno()


_original_stdout = sys.stdout
sys.stdout = LogCapture(_original_stdout)

# ── Import the checker module (untouched) ──
import FlareCloud as fc
from werkzeug.utils import secure_filename

# ── Flask app ──
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keys.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ═══════════════════════════════════════════
#  KEY MANAGEMENT SYSTEM
# ═══════════════════════════════════════════

ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN-FLARECLOUD-MASTER")

def load_keys():
    """Load keys from JSON file."""
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    # Default: include the original permanent key
    default = {
        "permanent": ["KEY-5545TS-G56OL5Y"],
        "temporary": []  # { "key": "...", "expires": "ISO timestamp", "label": "..." }
    }
    save_keys(default)
    return default


def save_keys(data):
    """Save keys to JSON file."""
    with open(KEYS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def generate_temp_key():
    """Generate a random key like KEY-XXXXXX-XXXXXXX."""
    part1 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    part2 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
    return f"KEY-{part1}-{part2}"


def validate_key(key):
    """Check if a key is valid (permanent or non-expired temp)."""
    keys_data = load_keys()
    # Check permanent keys
    if key in keys_data.get('permanent', []):
        return True
    # Check temp keys
    now = datetime.now(timezone.utc)
    for tk in keys_data.get('temporary', []):
        if tk['key'] == key:
            expires = datetime.fromisoformat(tk['expires'])
            if now < expires:
                return True
            else:
                return False  # Expired
    return False


# Rate limiting
login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


def check_rate_limit(ip):
    now = time.time()
    if ip in login_attempts:
        record = login_attempts[ip]
        if now - record['last_attempt'] > LOCKOUT_SECONDS:
            login_attempts[ip] = {'count': 0, 'last_attempt': now}
            return True
        if record['count'] >= MAX_LOGIN_ATTEMPTS:
            return False
    return True


def record_attempt(ip, success):
    now = time.time()
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'last_attempt': now}
    if success:
        login_attempts[ip] = {'count': 0, 'last_attempt': now}
    else:
        login_attempts[ip]['count'] += 1
        login_attempts[ip]['last_attempt'] = now


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function


# ── State tracking ──
checker_state = {
    'running': False,
    'finished': False,
    'combo_file': None,
    'proxy_file': None,
    'combo_count': 0,
    'proxy_count': 0,
    'session_name': None,
}


# ═══════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════

@app.route('/login')
def login_page():
    if session.get('authenticated'):
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def login():
    ip = request.remote_addr
    if not check_rate_limit(ip):
        remaining = int(LOCKOUT_SECONDS - (time.time() - login_attempts[ip]['last_attempt']))
        return jsonify({'error': f'Too many attempts. Try again in {remaining}s'}), 429

    data = request.get_json()
    key = (data.get('key') or '').strip()
    if not key:
        record_attempt(ip, False)
        return jsonify({'error': 'No key provided'}), 400

    # Check admin key
    if key == ADMIN_KEY:
        record_attempt(ip, True)
        session['authenticated'] = True
        session['is_admin'] = True
        session.permanent = False
        return jsonify({'success': True, 'admin': True})

    # Check user keys
    if validate_key(key):
        record_attempt(ip, True)
        session['authenticated'] = True
        session['is_admin'] = False
        session.permanent = False
        return jsonify({'success': True, 'admin': False})
    else:
        record_attempt(ip, False)
        attempts_left = MAX_LOGIN_ATTEMPTS - login_attempts.get(ip, {}).get('count', 0)
        return jsonify({'error': f'Invalid key. {max(0, attempts_left)} attempts remaining.'}), 401


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# ═══════════════════════════════════════════
#  ADMIN — KEY MANAGEMENT
# ═══════════════════════════════════════════

@app.route('/admin')
@login_required
@admin_required
def admin_page():
    return render_template('admin.html')


@app.route('/api/admin/keys', methods=['GET'])
@login_required
@admin_required
def get_keys():
    """Get all keys (permanent + temp)."""
    keys_data = load_keys()
    # Clean expired temp keys
    now = datetime.now(timezone.utc)
    keys_data['temporary'] = [
        tk for tk in keys_data.get('temporary', [])
        if datetime.fromisoformat(tk['expires']) > now
    ]
    save_keys(keys_data)
    return jsonify(keys_data)


@app.route('/api/admin/keys/create', methods=['POST'])
@login_required
@admin_required
def create_temp_key():
    """Create a new temporary key."""
    data = request.get_json()
    duration_hours = int(data.get('hours', 24))
    label = data.get('label', 'Temporary Key')

    new_key = generate_temp_key()
    expires = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()

    keys_data = load_keys()
    keys_data['temporary'].append({
        'key': new_key,
        'expires': expires,
        'label': label,
        'created': datetime.now(timezone.utc).isoformat()
    })
    save_keys(keys_data)

    return jsonify({
        'success': True,
        'key': new_key,
        'expires': expires,
        'label': label
    })


@app.route('/api/admin/keys/delete', methods=['POST'])
@login_required
@admin_required
def delete_key():
    """Delete a key."""
    data = request.get_json()
    key_to_delete = data.get('key', '')

    keys_data = load_keys()
    keys_data['permanent'] = [k for k in keys_data['permanent'] if k != key_to_delete]
    keys_data['temporary'] = [tk for tk in keys_data['temporary'] if tk['key'] != key_to_delete]
    save_keys(keys_data)

    return jsonify({'success': True})


@app.route('/api/admin/keys/add-permanent', methods=['POST'])
@login_required
@admin_required
def add_permanent_key():
    """Add a permanent key."""
    data = request.get_json()
    key = data.get('key', '').strip()
    if not key:
        key = generate_temp_key()

    keys_data = load_keys()
    if key not in keys_data['permanent']:
        keys_data['permanent'].append(key)
        save_keys(keys_data)

    return jsonify({'success': True, 'key': key})


# ═══════════════════════════════════════════
#  DASHBOARD ROUTES (Protected)
# ═══════════════════════════════════════════

@app.route('/')
@login_required
def index():
    return render_template('index.html', is_admin=session.get('is_admin', False))


@app.route('/api/upload/combos', methods=['POST'])
@login_required
def upload_combos():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_FOLDER, 'combos_' + filename)
    f.save(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
            lines = fh.readlines()
            unique = list(set(lines))
            dupes = len(lines) - len(unique)
            checker_state['combo_file'] = filepath
            checker_state['combo_count'] = len(unique)
            return jsonify({'success': True, 'filename': filename, 'total': len(lines), 'unique': len(unique), 'dupes': dupes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/proxies', methods=['POST'])
@login_required
def upload_proxies():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_FOLDER, 'proxies_' + filename)
    f.save(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
            lines = [l.strip() for l in fh.readlines() if l.strip()]
            checker_state['proxy_file'] = filepath
            checker_state['proxy_count'] = len(lines)
            return jsonify({'success': True, 'filename': filename, 'count': len(lines)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/start', methods=['POST'])
@login_required
def start_check():
    if checker_state['running']:
        return jsonify({'error': 'A check is already running'}), 409

    data = request.get_json()
    threads = int(data.get('threads', 5))
    proxy_type = str(data.get('proxyType', '4'))
    webhook = data.get('webhook', '').strip()
    banned_webhook = data.get('bannedWebhook', '').strip()
    unbanned_webhook = data.get('unbannedWebhook', '').strip()

    if not checker_state['combo_file']:
        return jsonify({'error': 'No combo file uploaded'}), 400

    try: fc.loadconfig()
    except: pass

    if webhook: fc.config.set('webhook', webhook)
    if banned_webhook: fc.config.set('bannedwebhook', banned_webhook)
    if unbanned_webhook: fc.config.set('unbannedwebhook', unbanned_webhook)

    # Reset counters
    for attr in ['hits','bad','twofa','cpm','cpm1','errors','retries','checked','vm','sfa','mfa','xgp','xgpu','other']:
        setattr(fc, attr, 0)

    fc.proxytype = f"'{proxy_type}'"
    fc.screen = "'2'"

    # Clear log buffer
    with log_lock:
        log_buffer.clear()

    try:
        with open(checker_state['combo_file'], 'r', encoding='utf-8', errors='ignore') as fh:
            fc.Combos = list(set(fh.readlines()))
    except Exception as e:
        return jsonify({'error': f'Failed to load combos: {str(e)}'}), 500

    fc.fname = os.path.splitext(os.path.basename(checker_state['combo_file']))[0]
    if fc.fname.startswith('combos_'):
        fc.fname = fc.fname[7:]
    checker_state['session_name'] = fc.fname

    if proxy_type in ('1', '2', '3') and checker_state['proxy_file']:
        fc.proxylist.clear()
        try:
            with open(checker_state['proxy_file'], 'r', encoding='utf-8', errors='ignore') as fh:
                for line in fh.readlines():
                    try: fc.proxylist.append(line.split()[0].replace('\n', ''))
                    except: pass
        except: pass
    elif proxy_type == '4':
        fc.proxylist.clear()

    os.makedirs('results', exist_ok=True)
    os.makedirs(f'results/{fc.fname}', exist_ok=True)

    checker_state['running'] = True
    checker_state['finished'] = False

    def run_checker():
        try:
            if proxy_type == '5':
                threading.Thread(target=fc.get_proxies, daemon=True).start()
                while len(fc.proxylist) == 0:
                    time.sleep(1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [executor.submit(fc.Checker, combo) for combo in fc.Combos]
                concurrent.futures.wait(futures)
        except Exception as e:
            print(f"Checker error: {e}")
        finally:
            checker_state['running'] = False
            checker_state['finished'] = True
            try:
                webhook_url = fc.config.get('webhook')
                if webhook_url and webhook_url != 'paste your discord webhook here':
                    import requests as req
                    req.post(webhook_url, json={
                        "username": "Flare Cloud",
                        "embeds": [{"title": "FlareCloud Checking Summary", "color": 0x00FF00,
                            "fields": [
                                {"name": "Total", "value": str(len(fc.Combos)), "inline": True},
                                {"name": "Hits", "value": str(fc.hits), "inline": True},
                                {"name": "Bad", "value": str(fc.bad), "inline": True},
                                {"name": "SFA", "value": str(fc.sfa), "inline": True},
                                {"name": "MFA", "value": str(fc.mfa), "inline": True},
                                {"name": "2FA", "value": str(fc.twofa), "inline": True},
                                {"name": "XGP", "value": str(fc.xgp), "inline": True},
                                {"name": "XGPU", "value": str(fc.xgpu), "inline": True},
                                {"name": "Other", "value": str(fc.other), "inline": True},
                            ],
                            "footer": {"text": "FlareCloud"},
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }]
                    }, headers={"Content-Type": "application/json"})
            except: pass

    threading.Thread(target=run_checker, daemon=True).start()
    return jsonify({'success': True, 'total': len(fc.Combos), 'session': fc.fname})


@app.route('/api/status')
@login_required
def status_stream():
    def generate():
        last_cpm_time = time.time()
        last_checked = 0
        while True:
            now = time.time()
            elapsed = now - last_cpm_time
            if elapsed >= 1.0:
                cpm = int(((fc.checked - last_checked) / elapsed) * 60)
                last_checked = fc.checked
                last_cpm_time = now
            else:
                cpm = 0
            total = len(fc.Combos) if fc.Combos else 0
            data = {
                'hits': fc.hits, 'bad': fc.bad, 'twofa': fc.twofa,
                'sfa': fc.sfa, 'mfa': fc.mfa, 'xgp': fc.xgp,
                'xgpu': fc.xgpu, 'other': fc.other, 'vm': fc.vm,
                'checked': fc.checked, 'total': total, 'cpm': cpm,
                'retries': fc.retries, 'errors': fc.errors,
                'running': checker_state['running'], 'finished': checker_state['finished'],
            }
            yield f"data: {json.dumps(data)}\n\n"
            if checker_state['finished'] and not checker_state['running']:
                yield f"data: {json.dumps({**data, 'finished': True})}\n\n"
                break
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/logs')
@login_required
def log_stream():
    def generate():
        sent_index = 0
        while True:
            with log_lock:
                current_len = len(log_buffer)
                buf_list = list(log_buffer)
            if sent_index < current_len:
                for entry in buf_list[sent_index:]:
                    yield f"data: {json.dumps(entry)}\n\n"
                sent_index = current_len
            elif sent_index > current_len:
                sent_index = 0
            if checker_state['finished'] and not checker_state['running']:
                yield f"data: {json.dumps({'type': 'system', 'text': 'Checking complete!', 'time': datetime.now().strftime('%H:%M:%S')})}\n\n"
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/download')
@login_required
def download_results():
    session_name = checker_state.get('session_name')
    if not session_name:
        return jsonify({'error': 'No results available'}), 404
    result_dir = os.path.join('results', session_name)
    if not os.path.exists(result_dir):
        return jsonify({'error': 'Results directory not found'}), 404
    zip_path = os.path.join(UPLOAD_FOLDER, f'{session_name}_results.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(result_dir):
            for file in files:
                fp = os.path.join(root, file)
                zf.write(fp, os.path.relpath(fp, result_dir))
    return send_file(zip_path, mimetype='application/zip', as_attachment=True, download_name=f'{session_name}_results.zip')


@app.route('/api/stop', methods=['POST'])
@login_required
def stop_check():
    if checker_state['running']:
        fc.Combos.clear()
        checker_state['running'] = False
        checker_state['finished'] = True
        return jsonify({'success': True, 'message': 'Stopping...'})
    return jsonify({'error': 'No check is running'}), 400


# ═══════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════

if __name__ == '__main__':
    # Initialize keys file
    load_keys()
    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║       FlareCloud Web Interface               ║")
    print("  ║       Open:  http://localhost:5000            ║")
    print("  ║       User:  KEY-5545TS-G56OL5Y              ║")
    print("  ║       Admin: ADMIN-FLARECLOUD-MASTER         ║")
    print("  ╚══════════════════════════════════════════════╝\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
