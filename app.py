"""ScholarMate – Flask web app (SQLite locally, Postgres on Vercel)."""
import json
import os
import time

from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from arxiv_client import get_recommendations
from db_adapter import get_db, init_db, query_one, execute

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'scholarmate-dev-key-change-in-prod')


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


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        with get_db() as db:
            user = query_one(db, 'SELECT * FROM users WHERE email=?', (email,))
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            return redirect(url_for('dashboard'))
        error = 'Invalid email or password.'
    return render_template('login.html', error=error)


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        if len(password) < 6:
            error = 'Password must be at least 6 characters.'
        else:
            try:
                with get_db() as db:
                    execute(db,
                        'INSERT INTO users (email, password_hash) VALUES (?, ?)',
                        (email, generate_password_hash(password))
                    )
                return redirect(url_for('login') + '?registered=1')
            except Exception:
                error = 'This email is already registered.'
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


@app.route('/api/debug')
@login_required
def api_debug():
    user = get_user(session['user_id'])
    return jsonify({
        'email':        user['email'],
        'scholar_url':  user['scholar_url'],
        'keywords':     user['keywords'],
        'top_k':        user['top_k'],
        'cached_count': len(json.loads(user['cached_papers'] or '[]')),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

init_db()   # safe to call every cold start

if __name__ == '__main__':
    app.run(debug=True, port=5000)
