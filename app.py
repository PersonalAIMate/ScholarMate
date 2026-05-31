"""ScholarMate – Flask web app (SQLite locally, Postgres on Vercel)."""
import json
import logging
import os
import secrets
import time

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from arxiv_client import get_recommendations
from db_adapter import IntegrityError, get_db, init_db, query_one, execute

log = logging.getLogger('scholarmate')

app = Flask(__name__)

# SECRET_KEY signs the session cookie. A known/default value lets anyone forge a
# session and impersonate users, so require a real one in production. Vercel sets
# the VERCEL env var on every deployment; locally we allow an ephemeral dev key.
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if os.environ.get('VERCEL'):
        raise RuntimeError(
            'SECRET_KEY environment variable is required in production. '
            'Set it in the Vercel project settings (Settings → Environment Variables).'
        )
    # Local development only: a random per-process key (sessions reset on restart).
    _secret = os.urandom(32).hex()
    print('[WARN] SECRET_KEY not set — using an ephemeral dev key (local only).')
app.secret_key = _secret


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_user(user_id):
    with get_db() as db:
        return query_one(db, 'SELECT * FROM users WHERE id=?', (user_id,))


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ── CSRF protection ─────────────────────────────────────────────────────────────
# Lightweight per-session token (no extra dependency). Stored in the signed
# session cookie, injected into every POST form, and verified on submission.

def get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['_csrf_token'] = token
    return token


@app.context_processor
def _inject_csrf():
    # Makes csrf_token() callable inside any Jinja template.
    return {'csrf_token': get_csrf_token}


def csrf_ok():
    sent = request.form.get('csrf_token', '')
    real = session.get('_csrf_token', '')
    return bool(real) and secrets.compare_digest(sent, real)


# ── Login rate limiting ─────────────────────────────────────────────────────────
# DB-backed so the limit holds across stateless serverless invocations.

LOGIN_MAX_ATTEMPTS = 8          # failed attempts allowed per window, per IP
LOGIN_WINDOW_SEC   = 15 * 60    # rolling window: 15 minutes


def _client_ip():
    # On Vercel the real client IP is the first entry of X-Forwarded-For.
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _recent_failures(db, ip):
    cutoff = int(time.time()) - LOGIN_WINDOW_SEC
    # opportunistic cleanup of stale rows keeps the table small
    execute(db, 'DELETE FROM login_attempts WHERE attempt_time < ?', (cutoff,))
    row = query_one(db,
        'SELECT COUNT(*) AS c FROM login_attempts WHERE ip=? AND attempt_time > ?',
        (ip, cutoff))
    return row['c'] if row else 0


def _record_failure(db, ip):
    execute(db, 'INSERT INTO login_attempts (ip, attempt_time) VALUES (?, ?)',
            (ip, int(time.time())))


def _clear_failures(db, ip):
    execute(db, 'DELETE FROM login_attempts WHERE ip=?', (ip,))


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if not csrf_ok():
            return render_template('login.html',
                                   error='Your session expired. Please try again.'), 400

        ip       = _client_ip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        with get_db() as db:
            if _recent_failures(db, ip) >= LOGIN_MAX_ATTEMPTS:
                return render_template('login.html',
                    error='Too many failed attempts. Please wait ~15 minutes and try again.'), 429

            user = query_one(db, 'SELECT * FROM users WHERE email=?', (email,))
            if user and check_password_hash(user['password_hash'], password):
                _clear_failures(db, ip)
                session.clear()                 # prevent session fixation
                session['user_id'] = user['id']
                return redirect(url_for('dashboard'))

            _record_failure(db, ip)
        error = 'Invalid email or password.'
    return render_template('login.html', error=error)


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        if not csrf_ok():
            return render_template('register.html',
                                   error='Your session expired. Please try again.'), 400

        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if '@' not in email or '.' not in email.split('@')[-1]:
            error = 'Please enter a valid email address.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        else:
            try:
                with get_db() as db:
                    execute(db,
                        'INSERT INTO users (email, password_hash) VALUES (?, ?)',
                        (email, generate_password_hash(password))
                    )
                return redirect(url_for('login') + '?registered=1')
            except IntegrityError:
                error = 'This email is already registered.'
            except Exception:
                log.exception('registration failed')
                error = 'Something went wrong on our side. Please try again later.'
    return render_template('register.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    user   = get_user(session['user_id'])
    papers = json.loads(user['cached_papers'] or '[]')
    cache_age_min = (
        int((time.time() - user['cache_time']) / 60)
        if user['cache_time'] else None
    )
    return render_template('dashboard.html', user=user, papers=papers, cache_age=cache_age_min)


@app.route('/settings', methods=['POST'])
@login_required
def settings():
    if not csrf_ok():
        abort(400)
    scholar_url = request.form.get('scholar_url', '').strip()
    keywords    = request.form.get('keywords',    '').strip()
    top_k       = max(1, min(50, int(request.form.get('top_k', 10) or 10)))
    with get_db() as db:
        execute(db,
            'UPDATE users SET scholar_url=?, keywords=?, top_k=? WHERE id=?',
            (scholar_url, keywords, top_k, session['user_id'])
        )
    return redirect(url_for('dashboard'))


# ── Paper API ─────────────────────────────────────────────────────────────────

@app.route('/api/papers')
@login_required
def api_papers():
    user = get_user(session['user_id'])
    print(f'[API] scholar_url={user["scholar_url"]!r} keywords={user["keywords"]!r} top_k={user["top_k"]}')
    try:
        papers, used_keywords = get_recommendations(
            user['scholar_url'], user['keywords'], user['top_k']
        )
        if papers:
            with get_db() as db:
                execute(db,
                    'UPDATE users SET cached_papers=?, cache_time=? WHERE id=?',
                    (json.dumps(papers, ensure_ascii=False), int(time.time()), user['id'])
                )
        return jsonify(papers=papers, keywords=used_keywords)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(error=str(e)), 500


# ── Entry point ───────────────────────────────────────────────────────────────

init_db()   # safe to call every cold start

if __name__ == '__main__':
    app.run(debug=True, port=5000)
