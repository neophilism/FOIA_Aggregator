"""SQLite-backed crawl receipts and an exclusive, same-host writer lock.

The lock is intentionally not a distributed lease. Independent runners must
still use the shared GitHub Actions concurrency group and verified restoration.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from .storage import get_connection


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


_held = threading.local()


@contextmanager
def exclusive_writer(db_path, *, allow_nested=False):
    """Fail immediately on contention; a killed process releases the OS lock."""
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    holders = getattr(_held, 'paths', {})
    _held.paths = holders
    key = str(target)
    if allow_nested and holders.get(key) == os.getpid():
        yield
        return
    fd = os.open(str(target) + ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another ingestion writer holds this database") from exc
        acquired = True
        holders[key] = os.getpid()
        yield
    finally:
        if acquired:
            holders.pop(key, None)
        # Never unlink: a second inode would allow two apparent lock owners.
        os.close(fd)


def create_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ingestion_runs (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('RUNNING','COMPLETED','DEGRADED','FAILED','INTERRUPTED')),
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        attempt INTEGER NOT NULL DEFAULT 1,
        source_ids TEXT NOT NULL,
        metrics_before TEXT NOT NULL,
        metrics_after TEXT,
        metadata_ok INTEGER,
        checkpoint_state TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
        checkpoint_key TEXT,
        stop_reason TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ingestion_source_receipts (
        run_id TEXT NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('RUNNING','COMPLETED','FAILED')),
        attempted_at TEXT NOT NULL,
        completed_at TEXT,
        error_class TEXT,
        PRIMARY KEY(run_id, source_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ingestion_runs_updated_idx ON ingestion_runs(updated_at)")


def metrics(conn):
    """Counters are local evidence; external usage/costs stay explicitly unknown."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'documents' not in tables:
        return None
    row = conn.execute("""SELECT COUNT(*) AS discovered,
        COALESCE(SUM(CASE WHEN download_status='downloaded' AND
        (NULLIF(storage_key,'') IS NOT NULL OR NULLIF(local_path,'') IS NOT NULL)
        THEN 1 ELSE 0 END),0) AS archived,
        COALESCE(SUM(CASE WHEN download_status='downloaded' THEN COALESCE(file_size,0) ELSE 0 END),0) AS cataloged_file_bytes
        FROM documents""").fetchone()
    result = dict(row)
    result['indexed'] = conn.execute("SELECT COUNT(*) FROM document_text WHERE extraction_status IN ('indexed','indexed_truncated')").fetchone()[0] if 'document_text' in tables else 0
    result['actual_b2_versioned_bytes'] = None
    result['cost_cents'] = None
    return result


def policy_fingerprint(cfg) -> str:
    # Whitelist nonsecret policy values, never serialize API keys or config wholesale.
    policy = {key: cfg.crawler.get(key) for key in (
        'dry_run', 'max_docs_per_source', 'source_limit', 'max_pages_per_source',
        'max_depth', 'max_discovered_docs_per_source')}
    policy['backend'] = cfg.storage.get('backend', 'local')
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


class RunJournal:
    """Use only while holding exclusive_writer for the entire crawl/checkpoint."""
    def __init__(self, db_path, fingerprint, source_ids):
        self.db_path = db_path
        conn = get_connection(db_path)
        try:
            with conn:
                previous = conn.execute("SELECT * FROM ingestion_runs WHERE state IN ('RUNNING','INTERRUPTED') ORDER BY updated_at DESC LIMIT 1").fetchone()
                stamp = now()
                if previous and previous['fingerprint'] == fingerprint:
                    self.id = previous['id']
                    self.source_ids = json.loads(previous['source_ids'])
                    conn.execute("UPDATE ingestion_runs SET state='RUNNING', attempt=attempt+1, updated_at=?, finished_at=NULL WHERE id=?", (stamp, self.id))
                else:
                    conn.execute("UPDATE ingestion_runs SET state='INTERRUPTED', updated_at=?, stop_reason='policy_changed_after_interruption' WHERE state='RUNNING'", (stamp,))
                    self.id = str(uuid4())
                    self.source_ids = list(source_ids)
                    conn.execute("""INSERT INTO ingestion_runs(id,fingerprint,state,started_at,updated_at,source_ids,metrics_before)
                        VALUES(?,?,'RUNNING',?,?,?,?)""", (self.id, fingerprint, stamp, stamp, json.dumps(self.source_ids), json.dumps(metrics(conn))))
        finally:
            conn.close()

    def completed_sources(self):
        conn = get_connection(self.db_path)
        try:
            return {row[0] for row in conn.execute("SELECT source_id FROM ingestion_source_receipts WHERE run_id=? AND state='COMPLETED'", (self.id,))}
        finally:
            conn.close()

    def source_started(self, source_id):
        self._execute("""INSERT INTO ingestion_source_receipts(run_id,source_id,state,attempted_at) VALUES(?,?,'RUNNING',?)
            ON CONFLICT(run_id,source_id) DO UPDATE SET state='RUNNING', attempted_at=excluded.attempted_at, completed_at=NULL, error_class=NULL""", (self.id, source_id, now()))

    def source_finished(self, source_id, error_class=None):
        self._execute("UPDATE ingestion_source_receipts SET state=?,completed_at=?,error_class=? WHERE run_id=? AND source_id=?", ('FAILED' if error_class else 'COMPLETED', now(), error_class, self.id, source_id))

    def checkpoint(self, state, key=None):
        self._execute("UPDATE ingestion_runs SET checkpoint_state=?,checkpoint_key=?,updated_at=? WHERE id=?", (state, key, now(), self.id))

    def finish(self, *, metadata_ok, state, reason):
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute("""UPDATE ingestion_runs SET state=?,metadata_ok=?,stop_reason=?,metrics_after=?,updated_at=?,finished_at=? WHERE id=?""", (state, int(metadata_ok), reason, json.dumps(metrics(conn)), now(), now(), self.id))
        finally:
            conn.close()

    def source_failed(self, source_id):
        conn = get_connection(self.db_path)
        try:
            row = conn.execute("SELECT last_error_at FROM reading_rooms WHERE id=?", (source_id,)).fetchone()
            # Missing source is a real failure; do not claim it was crawled.
            return row is None or row['last_error_at'] is not None
        finally:
            conn.close()

    def _execute(self, sql, params):
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()


def latest_receipt(db_path):
    """Read-only receipt projection, suitable for operator status commands."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM ingestion_runs ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ('source_ids', 'metrics_before', 'metrics_after'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result['sources'] = [dict(r) for r in conn.execute("SELECT source_id,state,error_class,attempted_at,completed_at FROM ingestion_source_receipts WHERE run_id=? ORDER BY source_id", (row['id'],))]
        result['source_counts'] = {
            state.lower(): sum(item['state'] == state for item in result['sources'])
            for state in ('RUNNING', 'COMPLETED', 'FAILED')
        }
        result['source_counts']['planned'] = len(result['source_ids'])
        result['source_counts']['not_attempted'] = len(result['source_ids']) - len(result['sources'])
        before, after = result['metrics_before'], result['metrics_after']
        result['deltas'] = {key: after[key] - before[key] for key in ('discovered','archived','indexed','cataloged_file_bytes')} if before is not None and after is not None else None
        result['escalation_required'] = result['state'] in ('FAILED','DEGRADED','INTERRUPTED')
        return result
    finally:
        conn.close()
