"""
Авто-парсинг вакансий из чужих Telegram-каналов/групп.

Работает как «userbot»: читает новые сообщения в указанных каналах ОТ ИМЕНИ
ТВОЕГО аккаунта (Telethon), прогоняет текст через тот же разбор ИИ + дедуп +
публикацию, что и вакансии, присланные админом вручную. Бот-аккаунт читать
чужие каналы не может — поэтому именно userbot, как в merchant-боте.

Модуль НЕОБЯЗАТЕЛЬНЫЙ: если переменные окружения не заданы, он просто молчит и
ничего не запускает (бот работает как раньше).

Переменные окружения:
  TG_API_ID, TG_API_HASH   — с https://my.telegram.org → API development tools
  TG_SESSION               — строковая сессия Telethon (см. tools/make_session.py)
  SOURCE_CHANNELS          — каналы-источники через запятую:
                             @username, t.me/username или числовой id (-100…)
  INGEST_MIN_LEN           — короче стольких символов текст игнорируем (по умолч. 40)
  INGEST_KEYWORDS          — (опц.) слать в разбор только если есть одно из слов
                             через запятую (напр. "vacancy,crew,AB,master,DP")
"""

import asyncio
import os

import db

SOURCE_CHANNELS_RAW = os.getenv("SOURCE_CHANNELS", "")
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "")
INGEST_MIN_LEN = int(os.getenv("INGEST_MIN_LEN", "40"))
INGEST_KEYWORDS = [w.strip().lower() for w in os.getenv("INGEST_KEYWORDS", "").split(",") if w.strip()]


def _parse_channels(raw: str):
    out = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        p = p.replace("https://", "").replace("http://", "").replace("t.me/", "").replace("@", "")
        if p.lstrip("-").isdigit():
            out.append(int(p))
        else:
            out.append(p)
    return out


SOURCE_CHANNELS = _parse_channels(SOURCE_CHANNELS_RAW)


def enabled() -> bool:
    return bool(TG_API_ID and TG_API_HASH and TG_SESSION and SOURCE_CHANNELS)


def _ensure_table():
    conn = db.get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ingest_seen (msg_key TEXT PRIMARY KEY, seen_at TEXT)")
        conn.commit()
    finally:
        conn.close()


def _seen(msg_key: str) -> bool:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT 1 FROM ingest_seen WHERE msg_key = ?", (msg_key,)).fetchone()
        if row:
            return True
        from datetime import datetime
        conn.execute("INSERT OR IGNORE INTO ingest_seen (msg_key, seen_at) VALUES (?, ?)",
                     (msg_key, datetime.now().isoformat()))
        conn.commit()
        return False
    finally:
        conn.close()


def _looks_relevant(text: str) -> bool:
    if len(text.strip()) < INGEST_MIN_LEN:
        return False
    if INGEST_KEYWORDS:
        low = text.lower()
        return any(k in low for k in INGEST_KEYWORDS)
    return True


async def run_ingest(on_vacancy, log=print):
    """Запускает userbot и на каждое новое сообщение в SOURCE_CHANNELS вызывает
    await on_vacancy(raw_text, source_name). Переподключается сам при обрыве."""
    if not enabled():
        log("[ingest] выключен (нет TG_API_ID/TG_API_HASH/TG_SESSION/SOURCE_CHANNELS)")
        return
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except Exception as e:
        log(f"[ingest] Telethon не установлен ({e}). Добавьте 'telethon' в requirements.txt")
        return

    _ensure_table()

    while True:
        client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)

        @client.on(events.NewMessage(chats=SOURCE_CHANNELS))
        async def handler(event):
            try:
                text = event.message.message or ""
                if not _looks_relevant(text):
                    return
                chat = await event.get_chat()
                source = getattr(chat, "username", None) or getattr(chat, "title", None) or str(event.chat_id)
                msg_key = f"{event.chat_id}:{event.message.id}"
                if _seen(msg_key):
                    return
                await on_vacancy(text, source)
            except Exception as e:
                log(f"[ingest] ошибка обработки сообщения: {type(e).__name__}: {e}")

        try:
            await client.start()
            me = await client.get_me()
            uname = getattr(me, "username", None) or getattr(me, "first_name", "?")
            log(f"[ingest] userbot запущен как @{uname}; слушаю {len(SOURCE_CHANNELS)} канал(ов)")
            await client.run_until_disconnected()
        except Exception as e:
            log(f"[ingest] соединение оборвалось ({type(e).__name__}: {e}); переподключусь через 30с")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(30)
