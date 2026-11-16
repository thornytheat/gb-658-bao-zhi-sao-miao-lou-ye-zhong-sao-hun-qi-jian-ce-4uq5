"""M4 重扫采信的持久化层。

原件（rescan_scans）与历次决定（rescan_decisions）分表留存：
- 扫描行只插入不更新，重复导入同一扫描不会产生第二条原件。
- 决定行只追加，发布只允许把状态从 PENDING 迁移到 PUBLISHED/SUPERSEDED。
- 批次快照（rescan_batches.snapshot）在导入事务内冻结，后续裁决不再改写。
"""
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import app.store as store

TZ = timezone(timedelta(hours=8))
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rescan_batches (
  batch_id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  input_count INTEGER NOT NULL,
  accepted INTEGER NOT NULL,
  quarantined INTEGER NOT NULL,
  snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescan_scans (
  scan_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  business_key TEXT NOT NULL,
  issue TEXT NOT NULL,
  edition TEXT NOT NULL,
  page TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  file_name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  phash TEXT,
  clarity INTEGER NOT NULL,
  header_issue TEXT,
  has_header INTEGER NOT NULL,
  header_verified INTEGER NOT NULL,
  is_original INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rescan_scans_key ON rescan_scans(business_key);
CREATE INDEX IF NOT EXISTS idx_rescan_scans_batch ON rescan_scans(batch_id);
CREATE TABLE IF NOT EXISTS rescan_groups (
  business_key TEXT PRIMARY KEY,
  issue TEXT NOT NULL,
  edition TEXT NOT NULL,
  page TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  adopted_scan_id TEXT,
  original_scan_id TEXT,
  origin_batch_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescan_decisions (
  decision_id TEXT PRIMARY KEY,
  request_id TEXT,
  business_key TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'ADOPT',
  revision INTEGER NOT NULL,
  previous_revision INTEGER NOT NULL,
  scan_id TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  reason_text TEXT,
  actor TEXT,
  decided_at TEXT NOT NULL,
  publish_state TEXT NOT NULL,
  published_at TEXT,
  publish_attempts INTEGER NOT NULL DEFAULT 0,
  batch_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rescan_decisions_request
  ON rescan_decisions(request_id) WHERE request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_rescan_decisions_revision
  ON rescan_decisions(business_key, revision);
CREATE INDEX IF NOT EXISTS idx_rescan_decisions_key ON rescan_decisions(business_key, revision);
CREATE TABLE IF NOT EXISTS rescan_scopes (
  scope_key TEXT PRIMARY KEY,
  issue TEXT NOT NULL,
  edition TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  origin_batch_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescan_evidence (
  evidence_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  business_key TEXT,
  line_no INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  raw_value TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rescan_evidence_batch ON rescan_evidence(batch_id);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect():
    db = store.connect()
    ensure_schema(db)
    return db


def ensure_schema(db=None):
    path = str(store.DATABASE_PATH)
    if path in _SCHEMA_READY:
        return
    own = db is None
    if own:
        db = store.connect()
    try:
        with _SCHEMA_LOCK:
            if path not in _SCHEMA_READY:
                db.executescript(SCHEMA)
                _SCHEMA_READY.add(path)
    finally:
        if own:
            db.close()


@contextmanager
def write(db):
    """写事务：BEGIN IMMEDIATE 让并发裁决在数据库层串行化。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _rows(cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _row(cursor) -> dict | None:
    row = cursor.fetchone()
    return dict(row) if row else None


def get_batch(db, batch_id: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_batches WHERE batch_id = ?", (batch_id,)))


def list_batches(db) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_batches ORDER BY created_at, batch_id"))


def insert_batch(db, batch: dict):
    db.execute(
        "INSERT INTO rescan_batches(batch_id, manifest_sha256, created_at, input_count, accepted, quarantined, snapshot) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch["batch_id"], batch["manifest_sha256"], batch["created_at"], batch["input_count"], batch["accepted"], batch["quarantined"], batch["snapshot"]),
    )


def insert_scan(db, scan: dict) -> bool:
    cursor = db.execute(
        "INSERT OR IGNORE INTO rescan_scans(scan_id, batch_id, business_key, issue, edition, page, line_no, file_name, sha256, phash, clarity, header_issue, has_header, header_verified, is_original, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (scan["scan_id"], scan["batch_id"], scan["business_key"], scan["issue"], scan["edition"], scan["page"], scan["line_no"], scan["file_name"], scan["sha256"], scan.get("phash"), scan["clarity"], scan.get("header_issue"), scan["has_header"], scan["header_verified"], scan["created_at"]),
    )
    return cursor.rowcount == 1


def mark_original(db, scan_id: str):
    db.execute("UPDATE rescan_scans SET is_original = 1 WHERE scan_id = ?", (scan_id,))


def get_scan(db, scan_id: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_scans WHERE scan_id = ?", (scan_id,)))


def list_scans(db) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_scans ORDER BY issue, edition, page, line_no, file_name"))


def scans_for_key(db, business_key: str) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_scans WHERE business_key = ? ORDER BY line_no, file_name", (business_key,)))


def get_group(db, business_key: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_groups WHERE business_key = ?", (business_key,)))


def list_groups(db) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_groups ORDER BY issue, edition, page"))


def insert_group(db, group: dict):
    db.execute(
        "INSERT INTO rescan_groups(business_key, issue, edition, page, revision, adopted_scan_id, original_scan_id, origin_batch_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (group["business_key"], group["issue"], group["edition"], group["page"], group["revision"], group.get("adopted_scan_id"), group.get("original_scan_id"), group.get("origin_batch_id"), group["updated_at"]),
    )


def update_adoption(db, business_key: str, expected_revision: int, scan_id: str | None, new_revision: int, updated_at: str) -> bool:
    cursor = db.execute(
        "UPDATE rescan_groups SET adopted_scan_id = ?, revision = ?, updated_at = ? WHERE business_key = ? AND revision = ?",
        (scan_id, new_revision, updated_at, business_key, expected_revision),
    )
    return cursor.rowcount == 1


def get_scope(db, scope_key: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_scopes WHERE scope_key = ?", (scope_key,)))


def list_scopes(db) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_scopes ORDER BY issue, edition"))


def insert_scope(db, scope: dict):
    db.execute(
        "INSERT OR IGNORE INTO rescan_scopes(scope_key, issue, edition, expected_count, origin_batch_id, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (scope["scope_key"], scope["issue"], scope["edition"], scope["expected_count"], scope.get("origin_batch_id"), scope["updated_at"]),
    )


def expand_scope(db, scope_key: str, expected_count: int, updated_at: str) -> bool:
    cursor = db.execute(
        "UPDATE rescan_scopes SET expected_count = ?, updated_at = ? WHERE scope_key = ? AND expected_count < ?",
        (expected_count, updated_at, scope_key, expected_count),
    )
    return cursor.rowcount == 1


def insert_decision(db, decision: dict):
    db.execute(
        "INSERT INTO rescan_decisions(decision_id, request_id, business_key, action, revision, previous_revision, scan_id, reason_code, reason_text, actor, decided_at, publish_state, published_at, publish_attempts, batch_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision["decision_id"], decision.get("request_id"), decision["business_key"], decision["action"], decision["revision"], decision["previous_revision"], decision["scan_id"], decision["reason_code"], decision.get("reason_text"), decision.get("actor"), decision["decided_at"], decision["publish_state"], decision.get("published_at"), decision.get("publish_attempts", 0), decision.get("batch_id")),
    )


def get_decision(db, decision_id: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_decisions WHERE decision_id = ?", (decision_id,)))


def get_decision_by_request(db, request_id: str) -> dict | None:
    return _row(db.execute("SELECT * FROM rescan_decisions WHERE request_id = ?", (request_id,)))


def pending_decision(db, business_key: str) -> dict | None:
    return _row(db.execute(
        "SELECT * FROM rescan_decisions WHERE business_key = ? AND publish_state = 'PENDING' ORDER BY revision LIMIT 1",
        (business_key,),
    ))


def list_decisions(db, business_key: str | None = None) -> list[dict]:
    if business_key:
        return _rows(db.execute("SELECT * FROM rescan_decisions WHERE business_key = ? ORDER BY revision, decided_at", (business_key,)))
    return _rows(db.execute("SELECT * FROM rescan_decisions ORDER BY decided_at, revision"))


def mark_decision_published(db, decision_id: str, published_at: str):
    db.execute(
        "UPDATE rescan_decisions SET publish_state = 'PUBLISHED', published_at = ?, publish_attempts = publish_attempts + 1 WHERE decision_id = ? AND publish_state = 'PENDING'",
        (published_at, decision_id),
    )


def mark_decision_superseded(db, decision_id: str, resolved_at: str):
    db.execute(
        "UPDATE rescan_decisions SET publish_state = 'SUPERSEDED', published_at = ? WHERE decision_id = ? AND publish_state = 'PENDING'",
        (resolved_at, decision_id),
    )


def note_publish_attempt(db, decision_id: str):
    db.execute(
        "UPDATE rescan_decisions SET publish_attempts = publish_attempts + 1 WHERE decision_id = ? AND publish_state = 'PENDING'",
        (decision_id,),
    )


def insert_evidence(db, evidence: dict):
    db.execute(
        "INSERT OR IGNORE INTO rescan_evidence(evidence_id, batch_id, business_key, line_no, reason_code, raw_value, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence["evidence_id"], evidence["batch_id"], evidence.get("business_key"), evidence["line_no"], evidence["reason_code"], evidence.get("raw_value"), evidence.get("detail"), evidence["created_at"]),
    )


def list_evidence(db) -> list[dict]:
    return _rows(db.execute("SELECT * FROM rescan_evidence ORDER BY created_at, batch_id, line_no"))
