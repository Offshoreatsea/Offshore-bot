import sqlite3
from datetime import datetime, timedelta

DB_PATH = "bot.db"


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


def insert_vacancy(fields: dict, text: str, dedup_key: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO vacancies
           (position, vessel, region, dates, rotation, contact, requirements,
            hashtags, dedup_key, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (
            fields["position"], fields["vessel"], fields["region"],
            fields["dates"], fields["rotation"], fields["contact"],
            "\n".join(fields["requirements"]), fields["hashtags"],
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
