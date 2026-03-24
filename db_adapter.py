"""
Database adapter: SQLite locally, Neon Postgres on Vercel.
Vercel Neon integration injects: POSTGRES_URL (pooled) or POSTGRES_URL_NON_POOLING
"""
import os

# Vercel Neon uses POSTGRES_URL; fallback chain covers other providers
_url = (
    os.environ.get('POSTGRES_URL') or
    os.environ.get('DATABASE_URL') or
    os.environ.get('POSTGRES_URL_NON_POOLING') or
    ''
)

USE_POSTGRES = _url.startswith('postgres')

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

    # Neon requires SSL; add sslmode=require if not already in the URL
    CONN_URL = _url if 'sslmode' in _url else _url + '?sslmode=require'
    print(f'[DB] Using Postgres (Neon)')

    def get_db():
        conn = psycopg2.connect(CONN_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn

    def init_db():
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id            SERIAL  PRIMARY KEY,
                    email         TEXT    UNIQUE NOT NULL,
                    password_hash TEXT    NOT NULL,
                    scholar_url   TEXT    DEFAULT '',
                    keywords      TEXT    DEFAULT '',
                    top_k         INT     DEFAULT 10,
                    cached_papers TEXT    DEFAULT '[]',
                    cache_time    BIGINT  DEFAULT 0
                )
            ''')
        conn.commit()
        conn.close()
        print('[DB] Postgres tables ready')

    def query_one(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql.replace('?', '%s'), params)
            row = cur.fetchone()
        return dict(row) if row else None

    def execute(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql.replace('?', '%s'), params)
        conn.commit()

else:
    import sqlite3

    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scholarmate.db')
    print(f'[DB] Using SQLite: {DB_PATH}')

    def get_db():
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        return db

    def init_db():
        with get_db() as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         TEXT    UNIQUE NOT NULL,
                    password_hash TEXT    NOT NULL,
                    scholar_url   TEXT    DEFAULT '',
                    keywords      TEXT    DEFAULT '',
                    top_k         INTEGER DEFAULT 10,
                    cached_papers TEXT    DEFAULT '[]',
                    cache_time    INTEGER DEFAULT 0
                )
            ''')
        print(f'[DB] SQLite tables ready')

    def query_one(conn, sql, params=()):
        return conn.execute(sql, params).fetchone()

    def execute(conn, sql, params=()):
        conn.execute(sql, params)
