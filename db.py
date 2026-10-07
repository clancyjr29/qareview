"""SQLite storage layer. No ORM, no migrations - runs anywhere Python runs."""
import json
import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get("QAREVIEW_DB", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "qareview.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    rubric_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    rubric_id TEXT,
    stats TEXT NOT NULL,
    escalations TEXT NOT NULL,
    feedback TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
);
CREATE TABLE IF NOT EXISTS rows (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    row_num INTEGER NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    issues TEXT NOT NULL,
    FOREIGN KEY (review_id) REFERENCES reviews(id)
);
CREATE INDEX IF NOT EXISTS idx_rows_review ON rows(review_id);
CREATE INDEX IF NOT EXISTS idx_rows_status ON rows(review_id, status);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    # lightweight migration for pre-1.1 databases
    try:
        conn.execute("ALTER TABLE rows ADD COLUMN ai TEXT DEFAULT '{}'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.close()


def new_id():
    return uuid.uuid4().hex[:12]


def now():
    return time.time()
