import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"


def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    _ensure_dir()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT
            )
            """
        )
        # Lightweight migration for existing DBs (SQLite doesn't support IF NOT EXISTS on ADD COLUMN).
        try:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(users)").fetchall()
                if len(r) > 1
            }
            if "first_name" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
            if "last_name" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
        except Exception:
            # If anything goes wrong here, we keep running; worst-case names won't be persisted.
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                output_filename TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user_created ON translation_history(user_id, created_at DESC)"
        )
        conn.commit()


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
