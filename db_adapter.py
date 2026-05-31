"""
Database adapter: SQLite locally, Neon Postgres on Vercel.
Vercel Neon integration injects: POSTGRES_URL (pooled) or POSTGRES_URL_NON_POOLING

get_db() is a context manager that ALWAYS closes the connection on exit, so
serverless invocations never leak Postgres connections (Neon caps them).
"""
import logging
import os
from contextlib import contextmanager

log = logging.getLogger('scholarmate.db')

# Vercel Neon uses POSTGRES_URL (pooled); fallback chain covers other providers.
# Prefer the pooled URL so many short-lived serverless calls share the pool.
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
    log.info('Using Postgres (Neon)')

    @contextmanager
    def get_db():
        """Yield a Postgres connection; commit on success, rollback on error,
        and ALWAYS close so the connection is returned to Neon."""
        conn = psycopg2.connect(CONN_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db():
        with get_db() as conn:
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
        log.info('Postgres tables ready')

    def query_one(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql.replace('?', '%s'), params)
            row = cur.fetchone()
        return dict(row) if row else None

    def execute(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql.replace('?', '%s'), params)
        # commit handled by get_db() context manager on clean exit

else:
    import sqlite3

    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scholarmate.db')
    log.info('Using SQLite: %s', DB_PATH)

    @contextmanager
    def get_db():
        """Yield a SQLite connection; commit on success, rollback on error,
        and ALWAYS close it."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db():
        with get_db() as conn:
            conn.execute('''
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
        log.info('SQLite tables ready')

    def query_one(conn, sql, params=()):
        return conn.execute(sql, params).fetchone()

    def execute(conn, sql, params=()):
        conn.execute(sql, params)
        # commit handled by get_db() context manager on clean exit
