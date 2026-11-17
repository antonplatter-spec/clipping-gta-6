"""SQLite connection, schema and migrations. Only store.py talks to this module (no SQL anywhere else).

Concurrency model: the server has several threads (scheduler, live service, workers, request handlers) on one DB
file. Each thread gets its own connection (`conn()`, thread-local, reopened when paths.db_path() changes, which is
what lets tests switch AUTOMATE_DATA_DIR per case). Connections run in autocommit mode with WAL, foreign keys and a
busy timeout; writes go through `transaction()`, which takes the write lock up front (BEGIN IMMEDIATE) so a
read-then-write sequence is atomic and never fails half-way with "database is locked" on lock upgrade. Nested
`transaction()` blocks join the outer one.

Migrations: `meta.schema_version` holds the applied version. MIGRATIONS is an append-only list of
(version, [statements]); each pending step runs in its own IMMEDIATE transaction on connect, after re-reading the
version inside the lock, so two processes starting at once can't apply a step twice. Add a release's schema change
as a new numbered step (e.g. ALTER TABLE ... ADD COLUMN); never edit an applied one.

Public: SCHEMA_VERSION, MIGRATIONS, connect(path=None), conn(), transaction(), schema_version(c), close().
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import paths

BUSY_TIMEOUT_S = 15.0

_V1 = [
    """CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0, totp_last_counter INTEGER NOT NULL DEFAULT 0,
        recovery_codes TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL)""",
    """CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen REAL, user_agent TEXT)""",
    """CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, key TEXT NOT NULL, ok INTEGER NOT NULL)""",
    """CREATE INDEX login_attempts_key_ts ON login_attempts(key, ts)""",
    """CREATE TABLE profiles (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
        overrides TEXT NOT NULL DEFAULT '{}', updated_at REAL)""",
    """CREATE TABLE creators (
        id INTEGER PRIMARY KEY, profile TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
        match TEXT NOT NULL DEFAULT 'any', paused INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,
        created_at REAL NOT NULL, UNIQUE(profile, url))""",
    """CREATE TABLE streams (
        id INTEGER PRIMARY KEY, profile TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
        topic_only INTEGER NOT NULL DEFAULT 1, paused INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 100, created_at REAL NOT NULL, UNIQUE(profile, name))""",
    """CREATE TABLE runs (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, profile TEXT, trigger TEXT, started_at REAL NOT NULL,
        finished_at REAL, ok INTEGER, summary TEXT)""",
    """CREATE INDEX runs_kind ON runs(kind, started_at)""",
    """CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, profile TEXT NOT NULL,
        run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
        parent_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
        clipper_job TEXT, source_url TEXT, title TEXT, channel TEXT, section TEXT,
        status TEXT NOT NULL DEFAULT 'queued', stage TEXT, progress TEXT, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,
        params TEXT NOT NULL DEFAULT '{}', result TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, finished_at REAL,
        not_before REAL NOT NULL DEFAULT 0, locked_by TEXT, locked_at REAL)""",
    """CREATE INDEX jobs_queue ON jobs(status, priority, created_at)""",
    """CREATE INDEX jobs_clipper_job ON jobs(clipper_job)""",
    """CREATE INDEX jobs_parent ON jobs(parent_id)""",
    """CREATE TABLE job_events (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        ts REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL)""",
    """CREATE INDEX job_events_job ON job_events(job_id, id)""",
    """CREATE TABLE picks (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
        profile TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', reason TEXT,
        click_trigger TEXT, check_report TEXT, simulate_report TEXT, error TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, decided_at REAL, UNIQUE(job_id, idx))""",
    """CREATE INDEX picks_status ON picks(status, job_id)""",
    """CREATE TABLE videos (
        id INTEGER PRIMARY KEY, pick_id INTEGER REFERENCES picks(id) ON DELETE SET NULL,
        job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, profile TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'en', day TEXT, number INTEGER, folder TEXT NOT NULL UNIQUE, title TEXT,
        description TEXT, sources TEXT NOT NULL DEFAULT '[]', archived INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL)""",
    """CREATE INDEX videos_day ON videos(day, number)""",
    """CREATE TABLE variants (
        id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        file TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0, hook TEXT, hook_type TEXT, arm TEXT, title TEXT,
        description TEXT, hashtags TEXT NOT NULL DEFAULT '[]', predicted REAL, duration REAL,
        created_at REAL NOT NULL, UNIQUE(video_id, file))""",
    """CREATE TABLE social_accounts (
        id INTEGER PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL, handle TEXT, profile TEXT,
        connected INTEGER NOT NULL DEFAULT 0, external_id TEXT, credentials TEXT, notes TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE posts (
        id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
        account_id INTEGER REFERENCES social_accounts(id) ON DELETE SET NULL, platform TEXT NOT NULL,
        status TEXT NOT NULL, external_id TEXT, url TEXT, privacy TEXT, note TEXT, error TEXT,
        created_at REAL NOT NULL, posted_at REAL)""",
    """CREATE INDEX posts_variant ON posts(variant_id)""",
    """CREATE TABLE usage (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL,
        job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0, cost_eur REAL NOT NULL DEFAULT 0, request_id TEXT)""",
    """CREATE INDEX usage_day ON usage(day)""",
    """CREATE TABLE alerts (
        id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, level TEXT NOT NULL, message TEXT NOT NULL, details TEXT,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, resolved_at REAL,
        acknowledged INTEGER NOT NULL DEFAULT 0)""",
]

MIGRATIONS = [(1, _V1)]
SCHEMA_VERSION = MIGRATIONS[-1][0]

_local = threading.local()


def schema_version(c):
    row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def _migrate(c):
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    if schema_version(c) > SCHEMA_VERSION:
        raise RuntimeError(f"Die Datenbank ({paths.db_path()}) stammt von einer neueren Version (Schema "
                           f"{schema_version(c)}, diese Version kennt {SCHEMA_VERSION}). "
                           f"Bitte das Programm aktualisieren.")
    for version, statements in MIGRATIONS:
        if schema_version(c) >= version:
            continue
        c.execute("BEGIN IMMEDIATE")
        try:
            if schema_version(c) < version:  # re-read under the write lock: another process may have done it
                for sql in statements:
                    c.execute(sql)
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(version),))
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


def connect(path=None):
    """A new connection to `path` (default paths.db_path()): data dir 0700, DB file 0600 (SQLite gives the -wal/-shm
    files the DB file's mode), WAL, foreign keys, busy timeout, rows as sqlite3.Row, schema migrated."""
    path = Path(path) if path else paths.db_path()
    if path == paths.db_path():
        paths.ensure_data_dir()
    else:
        paths.ensure_private_dir(path.parent)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(path, 0o600)
    # check_same_thread off only so a finished thread's connection can be closed by whoever collects it; each
    # connection is still used by its own thread alone (conn() is thread-local)
    c = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False)
    try:
        c.row_factory = sqlite3.Row
        c.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA foreign_keys = ON")
        _migrate(c)
    except BaseException:
        c.close()
        raise
    return c


class _Held:
    """This thread's connection; closed when the thread's locals go away (a worker thread that ended)."""

    def __init__(self, c, path):
        self.c, self.path = c, path

    def __del__(self):
        try:
            self.c.close()
        except Exception:
            pass


def conn():
    """This thread's connection to the current paths.db_path() (opened on first use)."""
    path = paths.db_path()
    held = getattr(_local, "held", None)
    if held is None or held.path != path:
        close()
        _local.held = _Held(connect(path), path)
    return _local.held.c


def close():
    """Close this thread's connection (tests; a worker thread before it exits)."""
    held = getattr(_local, "held", None)
    _local.held = None
    if held is not None:
        held.c.close()


@contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT on this thread's connection (ROLLBACK on any exception); joins an outer one."""
    c = conn()
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")
