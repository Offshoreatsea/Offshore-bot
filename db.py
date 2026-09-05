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
    # миграция для уже существующих баз (добавились salary/documents/nationality/duration/notes/raw_text)
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(vacancies)")}
    for col in ("salary", "documents", "nationality", "duration", "notes", "raw_text",
                "position_tag", "vessel_tag"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_text TEXT,
            corrected_fields TEXT,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id INTEGER,
            candidate_tg_id INTEGER,
            candidate_name TEXT,
            candidate_username TEXT,
            contact TEXT,
            message TEXT,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            tg_id INTEGER PRIMARY KEY,
            position_tag TEXT,
            username TEXT,
            subscribed_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS click_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id INTEGER,
            created_at TEXT
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


def insert_vacancy(fields: dict, dedup_key: str, raw_text: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO vacancies
           (position, vessel, region, nationality, dates, duration, rotation, salary,
            documents, contact, requirements, notes, hashtags, position_tag, vessel_tag,
            dedup_key, raw_text, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (
            fields.get("position"), fields.get("vessel"), fields.get("region"),
            fields.get("nationality"), fields.get("date"), fields.get("duration"),
            fields.get("rotation"), fields.get("salary"),
            "\n".join(fields.get("documents") or []),
            fields.get("contact"),
            "\n".join(fields.get("requirements") or []),
            fields.get("notes"),
            fields.get("hashtags"),
            fields.get("position_tag"), fields.get("vessel_tag"),
            dedup_key, raw_text, datetime.now().isoformat(),
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
    conn.execute(
        "INSERT INTO click_events (vacancy_id, created_at) VALUES (?, ?)",
        (vacancy_id, datetime.now().isoformat()),
    )
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


def daily_stats(days: int = 1):
    """Сводка за последние `days` суток: сколько опубликовано, разбивка по
    должностям (position_tag), час с наибольшим числом кликов по Apply
    (лучшая доступная боту метрика активности — просмотры поста Telegram
    боту не отдаёт, это видно только во встроенной статистике канала),
    и самый кликабельный пост за период."""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    total = conn.execute(
        "SELECT COUNT(*) c FROM vacancies WHERE status = 'published' AND created_at > ?",
        (cutoff,),
    ).fetchone()["c"]

    by_position = conn.execute(
        """SELECT COALESCE(position_tag, 'Other') AS tag, COUNT(*) c
           FROM vacancies WHERE status = 'published' AND created_at > ?
           GROUP BY tag ORDER BY c DESC""",
        (cutoff,),
    ).fetchall()

    peak_hour_row = conn.execute(
        """SELECT strftime('%H', created_at) AS hour, COUNT(*) c
           FROM click_events WHERE created_at > ?
           GROUP BY hour ORDER BY c DESC LIMIT 1""",
        (cutoff,),
    ).fetchone()

    top_post = conn.execute(
        """SELECT position, clicks FROM vacancies
           WHERE status = 'published' AND created_at > ?
           ORDER BY clicks DESC LIMIT 1""",
        (cutoff,),
    ).fetchone()

    conn.close()
    return {
        "total": total,
        "by_position": by_position,
        "peak_hour": peak_hour_row["hour"] if peak_hour_row else None,
        "top_post": top_post,
    }


def upsert_subscriber(tg_id: int, position_tag: str, username: str | None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO subscribers (tg_id, position_tag, username, subscribed_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(tg_id) DO UPDATE SET
               position_tag = excluded.position_tag,
               username = excluded.username,
               subscribed_at = excluded.subscribed_at""",
        (tg_id, position_tag, username, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_subscribers_for_tag(position_tag: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT tg_id FROM subscribers WHERE position_tag = ?", (position_tag,)
    ).fetchall()
    conn.close()
    return [r["tg_id"] for r in rows]


def get_recent_published_by_tag(position_tag: str, days: int = 3):
    """Бэкфилл для новых подписчиков — опубликованные вакансии этой должности
    за последние `days` суток, от старых к новым."""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM vacancies
           WHERE status = 'published' AND position_tag = ? AND created_at > ?
           ORDER BY created_at ASC""",
        (position_tag, cutoff),
    ).fetchall()
    conn.close()
    return rows


def insert_correction(original_text: str, corrected_fields_json: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO corrections (original_text, corrected_fields, created_at) VALUES (?, ?, ?)",
        (original_text, corrected_fields_json, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_recent_corrections(limit: int = 3):
    conn = get_conn()
    rows = conn.execute(
        "SELECT original_text, corrected_fields FROM corrections ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def distinct_regions():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT region FROM vacancies WHERE status = 'published' "
        "AND region IS NOT NULL AND region != '' ORDER BY region"
    ).fetchall()
    conn.close()
    return [r["region"] for r in rows]


def search_published_vacancies(region: str = "", q: str = "", limit: int = 50):
    conn = get_conn()
    query = "SELECT * FROM vacancies WHERE status = 'published'"
    params = []
    if region:
        query += " AND region = ?"
        params.append(region)
    if q:
        like = f"%{q}%"
        query += " AND (position LIKE ? OR vessel LIKE ? OR requirements LIKE ?)"
        params += [like, like, like]
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def insert_application(vacancy_id: int, candidate_tg_id: int, candidate_name: str,
                        candidate_username: str, contact: str, message: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO applications
           (vacancy_id, candidate_tg_id, candidate_name, candidate_username,
            contact, message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (vacancy_id, candidate_tg_id, candidate_name, candidate_username,
         contact, message, datetime.now().isoformat()),
    )
    conn.commit()
    app_id = cur.lastrowid
    conn.close()
    return app_id


def list_recent_applications(limit: int = 20):
    conn = get_conn()
    rows = conn.execute(
        """SELECT applications.*, vacancies.position AS vacancy_position
           FROM applications
           LEFT JOIN vacancies ON vacancies.id = applications.vacancy_id
           ORDER BY applications.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


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
