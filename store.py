"""SQLite-backed analysis persistence.

Stores the raw parsed emails from an analysis run so:
  - The user can re-open the app hours or days later and skip the Gmail refetch.
  - Preset changes rescore in-place against the persisted rows.
  - Deletions are recorded so the next load excludes them automatically.

One active run per user (keyed by Gmail address). Re-running analysis replaces
the previous run. Runs auto-expire after config.ANALYSIS_TTL_DAYS.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config


_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    user_email       TEXT PRIMARY KEY,
    query            TEXT DEFAULT '',
    preset           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    total_emails     INTEGER DEFAULT 0,
    total_size_bytes INTEGER DEFAULT 0,
    history_id       TEXT,
    last_synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS analysis_emails (
    user_email      TEXT NOT NULL,
    id              TEXT NOT NULL,
    thread_id       TEXT,
    sender_email    TEXT,
    sender_name     TEXT,
    sender_domain   TEXT,
    subject         TEXT DEFAULT '',
    date_iso        TEXT,
    size_bytes      INTEGER DEFAULT 0,
    labels          TEXT DEFAULT '[]',   -- JSON array of label strings
    snippet         TEXT DEFAULT '',
    is_unread       INTEGER DEFAULT 0,
    is_starred      INTEGER DEFAULT 0,
    is_important    INTEGER DEFAULT 0,
    has_unsubscribe INTEGER DEFAULT 0,
    is_deleted      INTEGER DEFAULT 0,
    PRIMARY KEY (user_email, id),
    FOREIGN KEY (user_email) REFERENCES analysis_runs(user_email) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ae_sender  ON analysis_emails(user_email, sender_email);
CREATE INDEX IF NOT EXISTS idx_ae_domain  ON analysis_emails(user_email, sender_domain);
CREATE INDEX IF NOT EXISTS idx_ae_deleted ON analysis_emails(user_email, is_deleted);
"""


def _db_path() -> Path:
    return Path(config.ANALYSIS_DB_FILE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@contextmanager
def _conn():
    """A short-lived connection that commits on clean exit and closes always.

    SQLite is single-writer, and we take _LOCK around writes; reads can happen
    concurrently. WAL mode reduces write contention for the small volumes we deal
    with.
    """
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Create tables and indexes if missing, then apply idempotent migrations."""
    with _LOCK:
        with _conn() as c:
            c.executescript(SCHEMA)
            _migrate(c)


def _migrate(conn):
    """Idempotent column-additions for older DBs. Each try/except adds a column
    that's now part of SCHEMA; sqlite raises OperationalError when it already
    exists, which we swallow. Cheap enough to run every startup."""
    for column, ddl in (
        ('history_id',      'ALTER TABLE analysis_runs ADD COLUMN history_id TEXT'),
        ('last_synced_at',  'ALTER TABLE analysis_runs ADD COLUMN last_synced_at TEXT'),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists


def _email_row(user_email: str, e: dict):
    """Flatten a parsed email dict into the row tuple for analysis_emails."""
    date_val = e.get('date')
    if isinstance(date_val, datetime):
        date_iso = _iso(date_val)
    elif date_val:
        date_iso = str(date_val)
    else:
        date_iso = None
    return (
        user_email, e['id'], e.get('thread_id'),
        (e.get('sender_email') or '').lower(),
        e.get('sender_name') or '',
        (e.get('sender_domain') or '').lower(),
        e.get('subject') or '',
        date_iso,
        int(e.get('size_bytes') or 0),
        json.dumps(e.get('labels') or []),
        (e.get('snippet') or '')[:1000],  # cap snippet
        int(bool(e.get('is_unread'))),
        int(bool(e.get('is_starred'))),
        int(bool(e.get('is_important'))),
        int(bool(e.get('has_unsubscribe'))),
    )


def save_run(user_email: str, query: str, preset: str, emails: list,
             history_id: str = None) -> None:
    """Replace any existing run for this user with a new one containing the
    given emails. Sets a 7-day expiry (config.ANALYSIS_TTL_DAYS).

    history_id is the Gmail historyId as of the fetch; used later as the cursor
    for /api/analyze/incremental. Pass None if unknown.
    """
    if not user_email:
        raise ValueError('user_email required')
    with _LOCK:
        with _conn() as c:
            # ON DELETE CASCADE drops the old emails.
            c.execute('DELETE FROM analysis_runs WHERE user_email = ?', (user_email,))
            now = _now()
            expires = now + timedelta(days=config.ANALYSIS_TTL_DAYS)
            c.execute(
                'INSERT INTO analysis_runs '
                '(user_email, query, preset, created_at, expires_at, total_emails, '
                ' total_size_bytes, history_id, last_synced_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_email, query or '', preset, _iso(now), _iso(expires),
                 len(emails), sum(int(e.get('size_bytes') or 0) for e in emails),
                 str(history_id) if history_id else None, _iso(now)),
            )
            if emails:
                rows = [_email_row(user_email, e) for e in emails]
                c.executemany(
                    'INSERT OR REPLACE INTO analysis_emails '
                    '(user_email, id, thread_id, sender_email, sender_name, sender_domain, '
                    ' subject, date_iso, size_bytes, labels, snippet, is_unread, is_starred, '
                    ' is_important, has_unsubscribe) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    rows,
                )


def load_run(user_email: str):
    """Return (metadata_dict, list_of_email_dicts).

    On miss or expiry, returns (None, []).
    age_days is recomputed on load so the "unread and old" signal stays fresh.
    """
    if not user_email:
        return None, []
    with _conn() as c:
        run = c.execute(
            'SELECT * FROM analysis_runs WHERE user_email = ?', (user_email,)
        ).fetchone()
        if not run:
            return None, []
        try:
            expires = datetime.fromisoformat(run['expires_at'])
        except Exception:
            return None, []
        if expires < _now():
            return None, []

        rows = c.execute(
            'SELECT * FROM analysis_emails '
            'WHERE user_email = ? AND is_deleted = 0',
            (user_email,),
        ).fetchall()

        now = _now()
        emails = []
        for r in rows:
            dt = now
            if r['date_iso']:
                try:
                    dt = datetime.fromisoformat(r['date_iso'])
                except Exception:
                    dt = now
            emails.append({
                'id': r['id'],
                'thread_id': r['thread_id'],
                'sender_email': r['sender_email'] or '',
                'sender_name': r['sender_name'] or '',
                'sender_domain': r['sender_domain'] or '',
                'subject': r['subject'] or '',
                'subject_lower': (r['subject'] or '').lower(),
                'date': dt,
                'age_days': (now - dt).days,
                'size_bytes': r['size_bytes'] or 0,
                'labels': json.loads(r['labels'] or '[]'),
                'snippet': r['snippet'] or '',
                'is_unread': bool(r['is_unread']),
                'is_starred': bool(r['is_starred']),
                'is_important': bool(r['is_important']),
                'has_unsubscribe': bool(r['has_unsubscribe']),
            })

        metadata = {
            'user_email': run['user_email'],
            'query': run['query'],
            'preset': run['preset'],
            'created_at': run['created_at'],
            'expires_at': run['expires_at'],
            'total_emails': run['total_emails'],
            'total_size_bytes': run['total_size_bytes'],
            'history_id': run['history_id'] if 'history_id' in run.keys() else None,
            'last_synced_at': run['last_synced_at'] if 'last_synced_at' in run.keys() else None,
        }
        return metadata, emails


def mark_deleted(user_email: str, ids) -> int:
    """Soft-delete emails so they no longer contribute to load_run() results.

    Returns the number of rows updated."""
    if not user_email or not ids:
        return 0
    ids = list(ids)
    with _LOCK:
        with _conn() as c:
            total = 0
            # Chunk to keep the SQL statement size sane on huge deletes.
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ','.join('?' * len(chunk))
                cur = c.execute(
                    f'UPDATE analysis_emails SET is_deleted = 1 '
                    f'WHERE user_email = ? AND id IN ({placeholders}) AND is_deleted = 0',
                    (user_email, *chunk),
                )
                total += cur.rowcount
            return total


def clear(user_email: str) -> None:
    """Drop the run and all its emails. Idempotent."""
    if not user_email:
        return
    with _LOCK:
        with _conn() as c:
            c.execute('DELETE FROM analysis_runs WHERE user_email = ?', (user_email,))


def status(user_email: str):
    """Return a status dict (or None if no run). Also reports counts of active
    vs deleted emails."""
    if not user_email:
        return None
    with _conn() as c:
        run = c.execute(
            'SELECT * FROM analysis_runs WHERE user_email = ?', (user_email,)
        ).fetchone()
        if not run:
            return None
        try:
            expires = datetime.fromisoformat(run['expires_at'])
        except Exception:
            expires = _now()
        active = c.execute(
            'SELECT COUNT(*) FROM analysis_emails '
            'WHERE user_email = ? AND is_deleted = 0',
            (user_email,),
        ).fetchone()[0]
        deleted = c.execute(
            'SELECT COUNT(*) FROM analysis_emails '
            'WHERE user_email = ? AND is_deleted = 1',
            (user_email,),
        ).fetchone()[0]
        now = _now()
        return {
            'user_email': run['user_email'],
            'query': run['query'],
            'preset': run['preset'],
            'created_at': run['created_at'],
            'expires_at': run['expires_at'],
            'expired': expires < now,
            'seconds_remaining': max(0, int((expires - now).total_seconds())),
            'active_email_count': active,
            'deleted_email_count': deleted,
            'ttl_days': config.ANALYSIS_TTL_DAYS,
            'history_id': run['history_id'] if 'history_id' in run.keys() else None,
            'last_synced_at': run['last_synced_at'] if 'last_synced_at' in run.keys() else None,
        }


def prune_expired() -> int:
    """Delete rows past expiry. Runs at startup and can be called any time.

    Returns the number of runs removed."""
    with _LOCK:
        with _conn() as c:
            cur = c.execute(
                'DELETE FROM analysis_runs WHERE expires_at < ?', (_iso(_now()),)
            )
            return cur.rowcount


def touch_preset(user_email: str, preset: str) -> None:
    """Persist a preset change on the current run without rewriting the emails."""
    if not user_email:
        return
    with _LOCK:
        with _conn() as c:
            c.execute(
                'UPDATE analysis_runs SET preset = ? WHERE user_email = ?',
                (preset, user_email),
            )


# ---------- Incremental sync helpers ----------

def upsert_emails(user_email: str, emails: list) -> int:
    """Add or replace individual email rows. Returns number of rows written.

    Used by /api/analyze/incremental for messages added since the last sync
    and for messages whose labels changed (we always refetch metadata so the
    stored flags stay accurate)."""
    if not user_email or not emails:
        return 0
    with _LOCK:
        with _conn() as c:
            rows = [_email_row(user_email, e) for e in emails]
            c.executemany(
                'INSERT OR REPLACE INTO analysis_emails '
                '(user_email, id, thread_id, sender_email, sender_name, sender_domain, '
                ' subject, date_iso, size_bytes, labels, snippet, is_unread, is_starred, '
                ' is_important, has_unsubscribe) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                rows,
            )
            return len(rows)


def hard_delete_emails(user_email: str, ids) -> int:
    """Physically remove rows. Use for Gmail-side deletions surfaced by the
    history API (mark_deleted keeps the row around; hard_delete does not)."""
    if not user_email or not ids:
        return 0
    ids = list(ids)
    with _LOCK:
        with _conn() as c:
            total = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ','.join('?' * len(chunk))
                cur = c.execute(
                    f'DELETE FROM analysis_emails '
                    f'WHERE user_email = ? AND id IN ({placeholders})',
                    (user_email, *chunk),
                )
                total += cur.rowcount
            return total


def update_history_cursor(user_email: str, history_id: str,
                          extend_ttl: bool = True) -> None:
    """Advance the cursor after a successful incremental sync. Optionally
    resets expires_at so an actively-synced cache stays alive."""
    if not user_email or not history_id:
        return
    with _LOCK:
        with _conn() as c:
            now = _now()
            if extend_ttl:
                new_expiry = now + timedelta(days=config.ANALYSIS_TTL_DAYS)
                c.execute(
                    'UPDATE analysis_runs '
                    'SET history_id = ?, last_synced_at = ?, expires_at = ? '
                    'WHERE user_email = ?',
                    (str(history_id), _iso(now), _iso(new_expiry), user_email),
                )
            else:
                c.execute(
                    'UPDATE analysis_runs '
                    'SET history_id = ?, last_synced_at = ? '
                    'WHERE user_email = ?',
                    (str(history_id), _iso(now), user_email),
                )


def recount(user_email: str) -> dict:
    """Refresh total_emails and total_size_bytes on the run from the current
    non-deleted rows. Returns the fresh counts."""
    if not user_email:
        return {'total_emails': 0, 'total_size_bytes': 0}
    with _LOCK:
        with _conn() as c:
            row = c.execute(
                'SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes '
                'FROM analysis_emails WHERE user_email = ? AND is_deleted = 0',
                (user_email,),
            ).fetchone()
            total = int(row['n'] or 0)
            size = int(row['bytes'] or 0)
            c.execute(
                'UPDATE analysis_runs SET total_emails = ?, total_size_bytes = ? '
                'WHERE user_email = ?',
                (total, size, user_email),
            )
            return {'total_emails': total, 'total_size_bytes': size}
