import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH") or "bot.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position TEXT,
            vessel TEXT,
            region TEXT,
            dates TEXT,
            rotation TEXT,
            salary TEXT,
            documents TEXT,
            contact TEXT,
            requirements TEXT,
            hashtags TEXT,
            dedup_key TEXT,
            status TEXT DEFAULT 'draft',
            scheduled_time TEXT,
            channel_message_id INTEGER,
            clicks INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    # миграция для уже существующих баз (добавились salary/documents/nationality/duration/notes)
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(vacancies)")}
    for col in ("salary", "documents", "nationality", "duration", "notes"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_setting(key: str, default: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def find_recent_duplicate(dedup_key: str, days: int = 3):
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT * FROM vacancies WHERE dedup_key = ? AND created_at > ? "
        "AND status != 'draft' ORDER BY created_at DESC LIMIT 1",
        (dedup_key, cutoff),
    ).fetchone()
    conn.close()
    return row


def insert_vacancy(fields: dict, dedup_key: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO vacancies
           (position, vessel, region, nationality, dates, duration, rotation, salary,
            documents, contact, requirements, notes, hashtags, dedup_key, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (
            fields.get("position"), fields.get("vessel"), fields.get("region"),
            fields.get("nationality"), fields.get("date"), fields.get("duration"),
            fields.get("rotation"), fields.get("salary"),
            "\n".join(fields.get("documents") or []),
            fields.get("contact"),
            "\n".join(fields.get("requirements") or []),
            fields.get("notes"),
            fields.get("hashtags"),
            dedup_key, datetime.now().isoformat(),
        ),
    )
    conn.commit()
    vacancy_id = cur.lastrowid
    conn.close()
    return vacancy_id


def get_vacancy(vacancy_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    conn.close()
    return row


def set_status(vacancy_id: int, status: str, channel_message_id: int | None = None):
    conn = get_conn()
    if channel_message_id is not None:
        conn.execute(
            "UPDATE vacancies SET status = ?, channel_message_id = ? WHERE id = ?",
            (status, channel_message_id, vacancy_id),
        )
    else:
        conn.execute("UPDATE vacancies SET status = ? WHERE id = ?", (status, vacancy_id))
    conn.commit()
    conn.close()


def set_schedule(vacancy_id: int, scheduled_time: str):
    conn = get_conn()
    conn.execute(
        "UPDATE vacancies SET status = 'queued', scheduled_time = ? WHERE id = ?",
        (scheduled_time, vacancy_id),
    )
    conn.commit()
    conn.close()


def get_due_queue(now_iso: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM vacancies WHERE status = 'queued' AND scheduled_time <= ?",
        (now_iso,),
    ).fetchall()
    conn.close()
    return rows


def increment_clicks(vacancy_id: int):
    conn = get_conn()
    conn.execute("UPDATE vacancies SET clicks = clicks + 1 WHERE id = ?", (vacancy_id,))
    conn.commit()
    conn.close()


def weekly_stats(days: int = 7):
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    total = conn.execute(
        "SELECT COUNT(*) c FROM vacancies WHERE status = 'published' AND created_at > ?",
        (cutoff,),
    ).fetchone()["c"]
    top = conn.execute(
        "SELECT position, clicks FROM vacancies WHERE status = 'published' AND created_at > ? "
        "ORDER BY clicks DESC LIMIT 5",
        (cutoff,),
    ).fetchall()
    conn.close()
    return total, top


def list_contacts():
    """Уникальные контакты (email/агентства) из всех сохранённых вакансий,
    с числом вакансий и датой последней публикации по каждому."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT contact,
               COUNT(*) as vacancy_count,
               MAX(created_at) as last_seen
        FROM vacancies
        WHERE contact IS NOT NULL AND contact != ''
        GROUP BY LOWER(contact)
        ORDER BY last_seen DESC
    """).fetchall()
    conn.close()
    return rows
