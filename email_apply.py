"""
Отклик на вакансии по email с почты клиентов-моряков (только для админа).

Как работает:
  1. Админ заводит клиента: /addclient → имя, почта, пароль приложения,
     должности, телефон, CV (PDF). Пароль хранится в БД в зашифрованном виде.
  2. Когда вакансия публикуется в канал и в ней есть email, бот находит
     клиентов с подходящей должностью и присылает админу черновик письма:
     кому, тема, cover letter, CV во вложении.
  3. Админ жмёт ✅ Send — письмо уходит с почты самого клиента (SMTP).
     Ответ работодателя придёт прямо клиенту в его ящик.

Защита: одно письмо на клиента на вакансию, одному работодателю не больше
MAIL_PER_EMPLOYER_DAY (4) писем в день, не больше MAIL_DAILY_LIMIT (100) писем в день на клиента.
"""
import asyncio
import base64
import hashlib
import html
import io
import mimetypes
import os
import re
import smtplib
import socket
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from cryptography.fernet import Fernet, InvalidToken

import db

router = Router()

MAIL_DAILY_LIMIT = int(os.getenv("MAIL_DAILY_LIMIT", "100"))          # писем в день с одного клиента
MAIL_PER_EMPLOYER_DAY = int(os.getenv("MAIL_PER_EMPLOYER_DAY", "4"))  # писем в день одному работодателю от клиента
MAIL_BACKFILL_DAYS = int(os.getenv("MAIL_BACKFILL_DAYS", "4"))        # за сколько дней откликаться при заведении клиента
COVER_MODEL = os.getenv("COVER_MODEL", "claude-haiku-4-5-20251001")

# заполняется из main.py через setup(), чтобы не было циклического импорта
_admin_ids: list[int] = []
_claude = None
_valid_tags: list[str] = []
_tag_labels: dict[str, str] = {}

SMTP_PRESETS = {
    "gmail.com": ("smtp.gmail.com", 465),
    "googlemail.com": ("smtp.gmail.com", 465),
    "yandex.ru": ("smtp.yandex.ru", 465),
    "yandex.com": ("smtp.yandex.ru", 465),
    "ya.ru": ("smtp.yandex.ru", 465),
    "mail.ru": ("smtp.mail.ru", 465),
    "bk.ru": ("smtp.mail.ru", 465),
    "inbox.ru": ("smtp.mail.ru", 465),
    "list.ru": ("smtp.mail.ru", 465),
    "internet.ru": ("smtp.mail.ru", 465),
    "ukr.net": ("smtp.ukr.net", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
    "me.com": ("smtp.mail.me.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
}
DEFAULT_SMTP = ("smtp.gmail.com", 465)  # свой домен на Google Workspace

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def setup(admin_ids, claude, valid_tags, tag_labels):
    global _admin_ids, _claude, _valid_tags, _tag_labels
    _admin_ids = list(admin_ids)
    _claude = claude
    _valid_tags = sorted(valid_tags)
    _tag_labels = dict(tag_labels)


def is_admin(user_id: int) -> bool:
    return user_id in _admin_ids


ADMIN_MAIL_COMMANDS = [
    ("mailhelp", "📧 Отклики по email — все команды"),
    ("clients", "📧 Клиенты для откликов"),
    ("addclient", "📧 Добавить клиента"),
    ("mailsent", "📧 Последние отправки"),
]


@router.startup()
async def _install_admin_commands(bot: Bot):
    # команды откликов видны в меню только админам; обычное меню для остальных не меняется
    try:
        base = await bot.get_my_commands()
    except Exception:
        base = []
    names = {c.command for c in base}
    cmds = list(base) + [BotCommand(command=c, description=d) for c, d in ADMIN_MAIL_COMMANDS if c not in names]
    for admin_id in _admin_ids:
        try:
            await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            print(f"[email_apply] не удалось поставить меню команд админу {admin_id}: {e}")


# ---------------------------------------------------------------- шифрование

def _fernet() -> Fernet:
    # MAIL_SECRET_KEY можно задать в Railway отдельно; если не задан — ключ
    # выводится из BOT_TOKEN (при смене токена пароли клиентов придётся ввести заново)
    secret = os.getenv("MAIL_SECRET_KEY") or ("mail:" + (os.getenv("BOT_TOKEN") or ""))
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ---------------------------------------------------------------- база

def init_tables():
    conn = db.get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT,
            email TEXT,
            enc_password TEXT,
            smtp_host TEXT,
            smtp_port INTEGER,
            positions TEXT,
            phone TEXT,
            about TEXT,
            cv_file_id TEXT,
            cv_filename TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            vacancy_id INTEGER,
            to_email TEXT,
            subject TEXT,
            body TEXT,
            status TEXT DEFAULT 'draft',
            error TEXT,
            created_at TEXT,
            sent_at TEXT,
            UNIQUE(client_id, vacancy_id)
        )
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(mail_clients)")}
    for col, ddl in (("subject_tpl", "TEXT"), ("letter_tpl", "TEXT"), ("auto_send", "INTEGER DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE mail_clients ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()


def _q(sql, params=(), one=False, commit=False):
    conn = db.get_conn()
    cur = conn.execute(sql, params)
    if commit:
        conn.commit()
        res = cur.lastrowid
    else:
        res = cur.fetchone() if one else cur.fetchall()
    conn.close()
    return res


def add_client(d: dict) -> int:
    host, port = smtp_for(d["email"])
    return _q(
        """INSERT INTO mail_clients (full_name, email, enc_password, smtp_host, smtp_port,
               positions, phone, cv_file_id, cv_filename, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (d["full_name"], d["email"], encrypt(d["password"]), host, port,
         ",".join(d["positions"]), d.get("phone") or "", d["cv_file_id"],
         d["cv_filename"], datetime.now().isoformat()),
        commit=True,
    )


def get_client(client_id: int):
    return _q("SELECT * FROM mail_clients WHERE id = ?", (client_id,), one=True)


def list_clients():
    return _q("SELECT * FROM mail_clients ORDER BY id")


def update_client(client_id: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    _q(f"UPDATE mail_clients SET {cols} WHERE id = ?", (*fields.values(), client_id), commit=True)


def delete_client(client_id: int):
    _q("DELETE FROM mail_clients WHERE id = ?", (client_id,), commit=True)


def clients_for_tag(tag: str):
    rows = _q("SELECT * FROM mail_clients WHERE active = 1")
    return [r for r in rows if tag in (r["positions"] or "").split(",")]


def get_app(app_id: int):
    return _q("SELECT * FROM mail_applications WHERE id = ?", (app_id,), one=True)


def set_app(app_id: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    _q(f"UPDATE mail_applications SET {cols} WHERE id = ?", (*fields.values(), app_id), commit=True)


def app_exists(client_id: int, vacancy_id: int) -> bool:
    return bool(_q("SELECT 1 FROM mail_applications WHERE client_id = ? AND vacancy_id = ?",
                   (client_id, vacancy_id), one=True))


def employer_limit_reached(client_id: int, to_email: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    row = _q("""SELECT COUNT(*) AS n FROM mail_applications WHERE client_id = ?
                AND lower(to_email) = lower(?) AND status = 'sent' AND sent_at LIKE ?""",
             (client_id, to_email, today + "%"), one=True)
    return row["n"] >= MAIL_PER_EMPLOYER_DAY


def sent_today(client_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    row = _q("""SELECT COUNT(*) AS n FROM mail_applications
                WHERE client_id = ? AND status = 'sent' AND sent_at LIKE ?""",
             (client_id, today + "%"), one=True)
    return row["n"]


def insert_app(client_id, vacancy_id, to_email, subject, body) -> int:
    return _q(
        """INSERT INTO mail_applications (client_id, vacancy_id, to_email, subject, body,
               status, created_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)""",
        (client_id, vacancy_id, to_email, subject, body, datetime.now().isoformat()),
        commit=True,
    )


# ---------------------------------------------------------------- SMTP

def smtp_for(email: str) -> tuple[str, int]:
    domain = email.split("@")[-1].lower()
    return SMTP_PRESETS.get(domain, DEFAULT_SMTP)


def _ipv4_socket(host: str, port: int, timeout, source_address=None):
    # Railway по умолчанию не выпускает IPv6 наружу, а smtp.gmail.com отдаёт
    # IPv6-адрес первым -> "Network is unreachable". Подключаемся строго по IPv4.
    last_err = None
    for *_, sockaddr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        try:
            return socket.create_connection(sockaddr[:2], timeout, source_address)
        except OSError as e:
            last_err = e
    raise last_err or OSError(f"нет IPv4-адреса для {host}")


class _SMTP4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _ipv4_socket(host, port, timeout, self.source_address)


class _SMTP4_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _ipv4_socket(host, port, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def _smtp_connect(host: str, port: int, user: str, password: str):
    ctx = ssl.create_default_context()
    if port == 465:
        s = _SMTP4_SSL(host, port, context=ctx, timeout=30)
    else:
        s = _SMTP4(host, port, timeout=30)
        s.starttls(context=ctx)
    s.login(user, password)
    return s


def _smtp_send(client, msg: EmailMessage):
    password = decrypt(client["enc_password"])
    if not password:
        raise RuntimeError("не удалось расшифровать пароль — задайте заново через /clientpass")
    s = _smtp_connect(client["smtp_host"], client["smtp_port"], client["email"], password)
    try:
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def _smtp_check(host, port, user, password):
    s = _smtp_connect(host, port, user, password)
    s.quit()


def _friendly_smtp_error(e: Exception) -> str:
    text = str(e)
    if isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException):
        return ("нет соединения с почтовым сервером — похоже, Railway всё ещё закрывает "
                "почтовые порты (после перехода на Pro нужен Redeploy). "
                f"Техническая ошибка: {type(e).__name__}: {text[:150]}")
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("почта не приняла логин/пароль. Для Gmail нужен именно пароль приложения "
                "(16 символов), а у аккаунта должна быть включена двухэтапная проверка. "
                f"Ответ сервера: {text[:200]}")
    return f"{type(e).__name__}: {text[:300]}"


async def build_message(bot: Bot, client, to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((client["full_name"], client["email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=client["email"].split("@")[-1])
    msg.set_content(body)
    if client["cv_file_id"]:
        buf = io.BytesIO()
        await bot.download(client["cv_file_id"], destination=buf)
        filename = client["cv_filename"] or "CV.pdf"
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(buf.getvalue(), maintype=maintype, subtype=subtype, filename=filename)
    return msg


# ---------------------------------------------------------------- тексты письма

def signature(client) -> str:
    lines = ["Kind regards,", client["full_name"]]
    if client["phone"]:
        lines.append(client["phone"])
    lines.append(client["email"])
    return "\n".join(lines)


def make_subject(fields: dict, client) -> str:
    position = (fields.get("position") or "").strip() or _tag_labels.get(fields.get("position_tag"), "")
    position = re.sub(r"\s+", " ", position)[:70]
    vessel = (fields.get("vessel") or "").strip()
    parts = [f"Application for {position}" if position else "Job application"]
    if vessel and len(vessel) <= 35:
        parts.append(vessel)
    parts.append(client["full_name"])
    return " – ".join(parts)


def _fallback_letter(fields: dict, client) -> str:
    position = fields.get("position") or "the advertised position"
    return (
        "Dear Sir/Madam,\n\n"
        f"I would like to apply for the position of {position}"
        + (f" on {fields['vessel']}" if fields.get("vessel") else "")
        + " advertised recently. Please find my CV attached.\n\n"
        "I would be glad to provide any further documents or references on request.\n\n"
        "Thank you for your time and consideration."
    )


def _compose_letter_sync(fields: dict, client) -> str:
    if not _claude:
        return _fallback_letter(fields, client)
    vacancy = "\n".join(
        f"{k}: {fields.get(k)}" for k in
        ("position", "vessel", "region", "date", "dates", "duration", "rotation",
         "salary", "requirements", "documents", "notes")
        if fields.get(k)
    )
    prompt = f"""Write the body of a short job-application email from a seafarer to a crewing
agency / employer. English only. 80-140 words. Professional, specific, no clichés.

Rules:
- Start with "Dear Sir/Madam," (or "Dear Hiring Team,").
- Mention the exact position (and vessel if given) from the vacancy.
- Use ONLY facts from the candidate summary below; never invent certificates,
  years, vessels or companies. If the summary is empty, stay general.
- Say the CV is attached.
- Do NOT add a subject line, signature, name, phone or placeholders like [Name].
- Plain text, no markdown.

VACANCY:
{vacancy}

CANDIDATE SUMMARY:
{client['about'] or '(no summary)'}
"""
    try:
        resp = _claude.messages.create(
            model=COVER_MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return text or _fallback_letter(fields, client)
    except Exception as e:
        print(f"[email_apply] cover letter через Claude не получился: {e}")
        return _fallback_letter(fields, client)


PLACEHOLDERS_HELP = ("Можно вставлять метки, бот подставит их из вакансии: "
                     "<code>{position}</code> — должность, <code>{vessel}</code> — судно, "
                     "<code>{region}</code> — регион, <code>{name}</code> — имя клиента.")


def fill_tpl(text: str, fields: dict, client) -> str:
    position = re.sub(r"\s+", " ", (fields.get("position") or "")).strip() \
        or _tag_labels.get(fields.get("position_tag"), "the advertised position")
    values = {
        "{position}": position[:70],
        "{vessel}": (fields.get("vessel") or "").strip(),
        "{region}": (fields.get("region") or "").strip(),
        "{name}": client["full_name"],
    }
    for k, v in values.items():
        text = text.replace(k, v)
    if "\n" not in text:
        # тема: если метка пустая, убираем лишний разделитель " –  – "
        text = " – ".join(p.strip() for p in re.split(r"\s[–-](?=\s|$)", text) if p.strip())
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def with_signature(body: str, client) -> str:
    if client["full_name"].lower() in body.lower():
        return body
    has_closing = re.search(r"(regards|sincerely|faithfully|respectfully|thank you)[\s,.!]*$", body, re.I)
    sig = signature(client)
    if has_closing:
        sig = sig.split("\n", 1)[1]  # без "Kind regards," — прощание уже есть в тексте
        return body.rstrip() + ",\n" + sig if not body.rstrip().endswith(",") else body.rstrip() + "\n" + sig
    return body + "\n\n" + sig


async def compose(fields: dict, client) -> tuple[str, str]:
    subject = fill_tpl(client["subject_tpl"], fields, client) if client["subject_tpl"] \
        else make_subject(fields, client)
    if client["letter_tpl"]:
        return subject, with_signature(fill_tpl(client["letter_tpl"], fields, client), client)
    letter = await asyncio.to_thread(_compose_letter_sync, fields, client)
    return subject, letter + "\n\n" + signature(client)


def _summarize_cv_sync(pdf_bytes: bytes) -> str | None:
    if not _claude:
        return None
    try:
        resp = _claude.messages.create(
            model=COVER_MODEL, max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": (
                    "This is a seafarer's CV. Summarize in English in max 6 short lines, facts only: "
                    "rank, total sea experience, vessel types, key certificates (e.g. DP, BOSIET, "
                    "STCW, CoC grade), nationality, availability. No invented facts.")},
            ]}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[email_apply] не удалось прочитать CV через Claude: {e}")
        return None


async def summarize_cv(bot: Bot, file_id: str, filename: str) -> str | None:
    if not filename.lower().endswith(".pdf"):
        return None
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return await asyncio.to_thread(_summarize_cv_sync, buf.getvalue())


# ---------------------------------------------------------------- черновики для админа

def draft_keyboard(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Send", callback_data=f"ea_send:{app_id}"),
         InlineKeyboardButton(text="❌ Skip", callback_data=f"ea_skip:{app_id}")],
        [InlineKeyboardButton(text="✏️ Edit letter", callback_data=f"ea_edit:{app_id}"),
         InlineKeyboardButton(text="🔄 Rewrite", callback_data=f"ea_regen:{app_id}")],
    ])


def draft_text(app, client) -> str:
    e = html.escape
    return (
        f"📧 <b>Отклик по email</b> — вакансия #{app['vacancy_id']}\n\n"
        f"<b>От:</b> {e(client['full_name'])} &lt;{e(client['email'])}&gt;\n"
        f"<b>Кому:</b> {e(app['to_email'])}\n"
        f"<b>Тема:</b> {e(app['subject'])}\n"
        f"<b>Вложение:</b> {e(client['cv_filename'] or '— нет CV —')}\n\n"
        f"<pre>{e(app['body'][:3000])}</pre>"
    )


async def _notify_admins(bot: Bot, text: str, markup=None):
    for admin_id in _admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            print(f"[email_apply] не удалось написать админу {admin_id}: {e}")


async def propose_for_vacancy(bot: Bot, vacancy_id: int, only_client_id: int | None = None) -> int:
    """Создаёт черновики писем для всех подходящих клиентов. Возвращает их число."""
    row = db.get_vacancy(vacancy_id)
    if not row:
        return 0
    fields = dict(row)
    m = EMAIL_RE.search(fields.get("contact") or "")
    if not m:
        return 0
    to_email = m.group(0).rstrip(".")
    tag = fields.get("position_tag")
    if only_client_id:
        c = get_client(only_client_id)
        candidates = [c] if c else []
    else:
        candidates = clients_for_tag(tag) if tag else []

    created = 0
    for client in candidates:
        if app_exists(client["id"], vacancy_id):
            continue
        if employer_limit_reached(client["id"], to_email):
            await _notify_admins(bot, f"⚠️ {html.escape(client['full_name'])}: на {html.escape(to_email)} уже "
                                      f"{MAIL_PER_EMPLOYER_DAY} письма сегодня, вакансия #{vacancy_id} пропущена.")
            continue
        if sent_today(client["id"]) >= MAIL_DAILY_LIMIT:
            await _notify_admins(bot, f"⚠️ {html.escape(client['full_name'])}: дневной лимит "
                                      f"{MAIL_DAILY_LIMIT} писем исчерпан, вакансия #{vacancy_id} пропущена.")
            continue
        subject, body = await compose(fields, client)
        app_id = insert_app(client["id"], vacancy_id, to_email, subject, body)
        if client["auto_send"]:
            ok, err = await send_app(bot, app_id)
            if ok:
                await _notify_admins(bot, draft_text(get_app(app_id), client)
                                     + "\n\n✅ <b>Отправлено автоматически</b>")
            else:
                await _notify_admins(bot, draft_text(get_app(app_id), client)
                                     + f"\n\n❌ Автоотправка не удалась: {html.escape(err)}",
                                     draft_keyboard(app_id))
        else:
            await _notify_admins(bot, draft_text(get_app(app_id), client), draft_keyboard(app_id))
        created += 1
    return created


async def send_app(bot: Bot, app_id: int) -> tuple[bool, str]:
    app = get_app(app_id)
    client = get_client(app["client_id"]) if app else None
    if not client:
        return False, "клиент удалён"
    if sent_today(client["id"]) >= MAIL_DAILY_LIMIT:
        set_app(app_id, status="failed", error="дневной лимит")
        return False, f"дневной лимит {MAIL_DAILY_LIMIT} писем исчерпан"
    if employer_limit_reached(client["id"], app["to_email"]):
        set_app(app_id, status="failed", error="лимит на работодателя")
        return False, f"на {app['to_email']} уже {MAIL_PER_EMPLOYER_DAY} письма сегодня"
    set_app(app_id, status="sending")
    try:
        msg = await build_message(bot, client, app["to_email"], app["subject"], app["body"])
        await asyncio.to_thread(_smtp_send, client, msg)
    except Exception as e:
        err = _friendly_smtp_error(e)
        set_app(app_id, status="failed", error=err)
        return False, err
    set_app(app_id, status="sent", sent_at=datetime.now().isoformat(), error=None)
    return True, ""


async def backfill_client(bot: Bot, client_id: int, days: int = MAIL_BACKFILL_DAYS):
    """Откликается от клиента на опубликованные за последние `days` дней вакансии по его должностям."""
    client = get_client(client_id)
    if not client or not client["active"]:
        return
    tags = [t for t in (client["positions"] or "").split(",") if t]
    if not tags:
        return
    since = (datetime.now() - timedelta(days=days)).isoformat()
    marks = ",".join("?" * len(tags))
    rows = _q(f"""SELECT id FROM vacancies WHERE status = 'published' AND created_at > ?
                  AND position_tag IN ({marks}) ORDER BY id""", (since, *tags))
    before_sent = sent_today(client_id)
    total = 0
    for r in rows:
        try:
            total += await propose_for_vacancy(bot, r["id"], only_client_id=client_id)
        except Exception as e:
            print(f"[email_apply] backfill вакансия {r['id']}: {e}")
        await asyncio.sleep(1.5)  # не частим — и Gmail, и Telegram не любят пачки подряд
    sent = sent_today(client_id) - before_sent
    mode = "отправлено" if client["auto_send"] else "черновиков создано"
    await _notify_admins(bot, f"📬 {html.escape(client['full_name'])}: вакансий за {days} дн. по его должностям — "
                              f"{len(rows)}, новых откликов: {total} ({mode}: "
                              f"{sent if client['auto_send'] else total}).")


def start_backfill(bot: Bot, client_id: int, days: int = MAIL_BACKFILL_DAYS):
    asyncio.create_task(backfill_client(bot, client_id, days))


@router.callback_query(F.data.startswith("ea_send:"))
async def cb_send(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    app_id = int(callback.data.split(":")[1])
    app = get_app(app_id)
    if not app or app["status"] not in ("draft", "failed"):
        return await callback.answer(f"Уже обработано: {app['status'] if app else '—'}", show_alert=True)
    client = get_client(app["client_id"])
    if not client:
        return await callback.answer("Клиент удалён", show_alert=True)
    await callback.answer("Отправляю…")
    ok, err = await send_app(callback.bot, app_id)
    if not ok:
        await callback.message.answer(f"❌ Не отправлено ({html.escape(client['full_name'])}): {html.escape(err)}\n"
                                      f"Можно нажать ✅ Send ещё раз после исправления.")
        return
    try:
        await callback.message.edit_text(draft_text(get_app(app_id), client) + "\n\n✅ <b>Отправлено</b>")
    except Exception:
        await callback.message.answer("✅ Отправлено")


@router.callback_query(F.data.startswith("ea_skip:"))
async def cb_skip(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    app_id = int(callback.data.split(":")[1])
    set_app(app_id, status="skipped")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("ea_regen:"))
async def cb_regen(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    app_id = int(callback.data.split(":")[1])
    app = get_app(app_id)
    client = get_client(app["client_id"]) if app else None
    row = db.get_vacancy(app["vacancy_id"]) if app else None
    if not (app and client and row) or app["status"] not in ("draft", "failed"):
        return await callback.answer("Нельзя переписать", show_alert=True)
    await callback.answer("Переписываю…")
    subject, body = await compose(dict(row), client)
    set_app(app_id, subject=subject, body=body)
    await callback.message.edit_text(draft_text(get_app(app_id), client), reply_markup=draft_keyboard(app_id))


class EditLetter(StatesGroup):
    body = State()


@router.callback_query(F.data.startswith("ea_edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    app_id = int(callback.data.split(":")[1])
    await state.set_state(EditLetter.body)
    await state.update_data(app_id=app_id)
    await callback.message.answer(
        "Пришлите новый текст письма целиком (подпись тоже). "
        "Первой строкой можно указать новую тему так: <code>Subject: ...</code>\n/cancel — отмена"
    )
    await callback.answer()


@router.message(StateFilter(EditLetter.body), F.text & ~F.text.startswith("/"))
async def on_edit_body(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    app_id = data["app_id"]
    text = message.text.strip()
    fields = {"body": text}
    first, _, rest = text.partition("\n")
    if first.lower().startswith("subject:"):
        fields = {"subject": first.split(":", 1)[1].strip(), "body": rest.strip()}
    set_app(app_id, **fields)
    app = get_app(app_id)
    await message.answer(draft_text(app, get_client(app["client_id"])), reply_markup=draft_keyboard(app_id))


# ---------------------------------------------------------------- заведение клиента

class AddClient(StatesGroup):
    name = State()
    email = State()
    password = State()
    positions = State()
    phone = State()
    cv = State()


class ClientPosEdit(StatesGroup):
    pick = State()


class ClientSetup(StatesGroup):
    subject = State()
    letter = State()
    auto = State()


class ClientLetter(StatesGroup):
    letter = State()


class ClientCV(StatesGroup):
    cv = State()


class ClientPass(StatesGroup):
    password = State()


def _norm_tag(raw: str) -> str:
    raw = raw.strip().lstrip("#").lower().replace(".", "").replace("/", "").replace(" ", "")
    return raw.replace("2nd", "second").replace("3rd", "third")


def _parse_positions(text: str) -> tuple[list[str], list[str]]:
    lookup = {t.lower(): t for t in _valid_tags}
    ok, bad = [], []
    for raw in re.split(r"[,;\n]+", text.strip()):
        if not raw.strip():
            continue
        key = _norm_tag(raw)
        (ok if key in lookup else bad).append(lookup.get(key, raw.strip()))
    return list(dict.fromkeys(ok)), bad


# порядок и группы кнопок — как задал владелец; один тег может стоять в двух группах
# (HLO, Fitter/Welder) — отметка ставится/снимается сразу в обеих
POSITION_GROUPS = [
    ("Bridge Officers", [
        ("MasterSDPO", "Master / SDPO"), ("Master", "Master"),
        ("ChiefOfficerSDPO", "Chief Officer / SDPO"), ("ChiefOfficer", "Chief Officer"),
        ("SecondOfficerDPO", "2nd Officer / DPO"), ("SecondOfficerJDPO", "2nd Officer / JDPO"),
        ("SecondOfficer", "2nd Officer"), ("ThirdOfficerJDPO", "3rd Officer / JDPO"),
        ("ThirdOfficer", "3rd Officer"), ("SafetyOfficer", "Safety Officer"), ("HLO", "HLO"),
    ]),
    ("Engine Officers", [
        ("ChiefEngineer", "Chief Engineer"), ("SecondEngineer", "2nd Engineer"),
        ("ThirdEngineer", "3rd Engineer"), ("JuniorEngineer", "Junior Engineer"),
        ("ETO", "ETO"), ("Electrician", "Electrician"), ("ElectricianAssistant", "Electrician Assistant"),
    ]),
    ("Deck Ratings", [
        ("AB", "AB"), ("OS", "OS"), ("Bosun", "Bosun"), ("Roustabout", "Roustabout"),
        ("CraneOperator", "Crane Operator"), ("GangwayOperator", "Gangway Operator"), ("HLO", "HLO"),
        ("Rigger", "Rigger"), ("FitterWelder", "Fitter / Welder"), ("DeckCadet", "Deck Cadet"),
    ]),
    ("Engine Ratings", [
        ("Motorman", "Motorman"), ("Oiler", "Oiler"), ("Wiper", "Wiper"),
        ("FitterWelder", "Fitter / Welder"), ("EngineCadet", "Engine Cadet"),
    ]),
    ("Catering", [
        ("Cook", "Cook"), ("NightCook", "Night Cook"), ("CampBoss", "Camp Boss"),
        ("Steward", "Steward"), ("ChiefSteward", "Chief Steward"), ("Messman", "Messman"),
    ]),
]


def _position_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    valid = set(_valid_tags)
    groups = [(name, [(t, l) for t, l in items if t in valid]) for name, items in POSITION_GROUPS]
    listed = {t for _, items in groups for t, _ in items}
    rest = [(t, _tag_labels.get(t, t)) for t in _valid_tags if t not in listed]
    if rest:  # теги, которые есть в канале, но не вошли в группы (Diver, ROV, …)
        groups.append(("Other", rest))
    return [(n, items) for n, items in groups if items]


def positions_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for name, items in _position_groups():
        rows.append([InlineKeyboardButton(text=f"— {name} —", callback_data="eahdr")])
        buttons = [
            InlineKeyboardButton(text=("✅ " if tag in selected else "") + label, callback_data=f"eapos:{tag}")
            for tag, label in items
        ]
        rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text=f"Готово ✔️ ({len(selected)})", callback_data="eapos_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pos_label(tag: str) -> str:
    for _, items in POSITION_GROUPS:
        for t, l in items:
            if t == tag:
                return l
    return _tag_labels.get(tag, tag)


POSITIONS_PROMPT = "Выберите должности кнопками (можно несколько), затем нажмите «Готово»:"


def _tags_hint() -> str:
    return ", ".join(f"<code>{t}</code>" for t in _valid_tags)


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("addclient"))
async def cmd_add_client(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AddClient.name)
    await message.answer(
        "Новый клиент для отклика по email.\n"
        "⚠️ Заводите только тех моряков, кто сам дал доступ к почте и согласен на отклики от своего имени.\n\n"
        "1/9. Имя и фамилия (латиницей, как в CV):\n/cancel — отмена"
    )


@router.message(StateFilter(AddClient.name), F.text & ~F.text.startswith("/"))
async def ac_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(AddClient.email)
    await message.answer("2/9. Email клиента (с него будут уходить письма):")


@router.message(StateFilter(AddClient.email), F.text & ~F.text.startswith("/"))
async def ac_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if not EMAIL_RE.fullmatch(email):
        return await message.answer("Не похоже на email, пришлите ещё раз.")
    host, port = smtp_for(email)
    await state.update_data(email=email)
    await state.set_state(AddClient.password)
    await message.answer(
        f"3/9. Пароль приложения для этой почты (сервер {host}).\n\n"
        "Gmail: myaccount.google.com → Безопасность → Двухэтапная проверка (включить) → "
        "Пароли приложений → создать → 16 символов.\n"
        "Сообщение с паролем я сразу удалю из чата, в базе он хранится зашифрованным."
    )


@router.message(StateFilter(AddClient.password), F.text & ~F.text.startswith("/"))
async def ac_password(message: Message, state: FSMContext):
    password = message.text.strip().replace(" ", "")
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    host, port = smtp_for(data["email"])
    status = await message.answer("Проверяю вход в почту…")
    try:
        await asyncio.to_thread(_smtp_check, host, port, data["email"], password)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(_friendly_smtp_error(e))}\n\nПришлите пароль ещё раз или /cancel.")
        return
    await state.update_data(password=password)
    await state.set_state(AddClient.positions)
    await status.edit_text(
        "✅ Вход в почту работает.\n\n4/9. " + POSITIONS_PROMPT, reply_markup=positions_keyboard([])
    )


@router.message(StateFilter(AddClient.positions), F.text & ~F.text.startswith("/"))
async def ac_positions(message: Message, state: FSMContext):
    ok, bad = _parse_positions(message.text)
    if bad or not ok:
        return await message.answer(
            f"Не узнал: {html.escape(', '.join(bad) or '—')}. Пришлите ещё раз. Доступные:\n" + _tags_hint())
    await state.update_data(positions=ok)
    await state.set_state(AddClient.phone)
    await message.answer("5/9. Телефон/WhatsApp для подписи письма (или «-», если не нужен):")


@router.callback_query(StateFilter(AddClient.positions, ClientPosEdit.pick), F.data.startswith("eapos:"))
async def cb_pos_toggle(callback: CallbackQuery, state: FSMContext):
    tag = callback.data.split(":", 1)[1]
    data = await state.get_data()
    sel = list(data.get("sel") or [])
    sel.remove(tag) if tag in sel else sel.append(tag)
    await state.update_data(sel=sel)
    await callback.message.edit_reply_markup(reply_markup=positions_keyboard(sel))
    await callback.answer()


@router.callback_query(StateFilter(AddClient.positions, ClientPosEdit.pick), F.data == "eapos_done")
async def cb_pos_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = [t for t in _valid_tags if t in (data.get("sel") or [])]
    if not sel:
        return await callback.answer("Выберите хотя бы одну должность", show_alert=True)
    labels = ", ".join(_pos_label(t) for t in sel)
    await callback.message.edit_text(f"Должности: {html.escape(labels)}")
    await callback.answer()
    if await state.get_state() == ClientPosEdit.pick.state:
        update_client(data["client_id"], positions=",".join(sel))
        await state.clear()
        await callback.message.answer(f"✅ #{data['client_id']}: должности сохранены. Проверяю вакансии "
                                      f"за {MAIL_BACKFILL_DAYS} дн. по новым должностям…")
        start_backfill(callback.bot, data["client_id"])
        return
    await state.update_data(positions=sel)
    await state.set_state(AddClient.phone)
    await callback.message.answer("5/9. Телефон/WhatsApp для подписи письма (или «-», если не нужен):")


@router.callback_query(F.data == "eahdr")
async def cb_pos_header(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("eapos"))
async def cb_pos_stale(callback: CallbackQuery):
    await callback.answer("Этот выбор уже закрыт. Для смены должностей: /clientpos ID", show_alert=True)


@router.message(StateFilter(AddClient.phone), F.text & ~F.text.startswith("/"))
async def ac_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone="" if phone == "-" else phone)
    await state.set_state(AddClient.cv)
    await message.answer("6/9. Пришлите CV файлом (лучше PDF):")


@router.message(StateFilter(AddClient.cv), F.document)
async def ac_cv(message: Message, state: FSMContext):
    doc = message.document
    data = await state.get_data()
    await state.clear()
    data["cv_file_id"] = doc.file_id
    data["cv_filename"] = doc.file_name or "CV.pdf"
    client_id = add_client(data)
    status = await message.answer("Сохранил. Читаю CV для cover letter…")
    about = await summarize_cv(message.bot, doc.file_id, data["cv_filename"])
    if about:
        update_client(client_id, about=about)
        tail = f"Краткая выжимка из CV (по ней пишется cover letter):\n<pre>{html.escape(about)}</pre>\n" \
               f"Поправить: <code>/clientabout {client_id} текст</code>"
    else:
        tail = (f"CV прочитать не удалось — напишите пару строк о клиенте: "
                f"<code>/clientabout {client_id} Chief Officer, 8 years DP2 PSV, DPO unlimited, BOSIET…</code>")
    await status.edit_text(
        f"✅ Клиент #{client_id} {html.escape(data['full_name'])} сохранён.\n"
        f"Должности: {', '.join(data['positions'])}\n\n{tail}"
    )
    await state.set_state(ClientSetup.subject)
    await state.update_data(client_id=client_id)
    await message.answer(
        "7/9. <b>Тема письма</b> — один раз, дальше бот берёт её для каждой вакансии.\n"
        + PLACEHOLDERS_HELP +
        "\n\nПример: <code>Application for {position} – {name}</code>\n"
        "Отправьте «-», чтобы бот составлял тему сам."
    )


@router.message(StateFilter(ClientSetup.subject), F.text & ~F.text.startswith("/"))
async def cs_subject(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    update_client(data["client_id"], subject_tpl=None if text == "-" else text)
    await state.set_state(ClientSetup.letter)
    await message.answer(
        "8/9. <b>Cover letter</b> — пришлите текст письма целиком одним сообщением.\n"
        + PLACEHOLDERS_HELP +
        "\nЕсли в тексте нет имени клиента, бот сам добавит подпись (имя, телефон, email).\n\n"
        "Отправьте «-», чтобы Claude писал письмо под каждую вакансию сам."
    )


@router.message(StateFilter(ClientSetup.letter), F.text & ~F.text.startswith("/"))
async def cs_letter(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    update_client(data["client_id"], letter_tpl=None if text == "-" else text)
    await state.set_state(ClientSetup.auto)
    await message.answer(AUTO_PROMPT_TEXT.replace("{n}", "9/9. "), reply_markup=auto_keyboard(data["client_id"]))


AUTO_PROMPT_TEXT = (
    "{n}<b>Как подавать отклики?</b>\n\n"
    "🤖 <b>Автоматически</b> — письмо уходит само, как только выходит подходящая вакансия с email; "
    "вам приходит уведомление, что отправлено.\n"
    "✋ <b>Через кнопку</b> — сначала приходит черновик, письмо уходит после ✅ Send."
)


def auto_keyboard(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Автоматически", callback_data=f"eaauto:{client_id}:1"),
        InlineKeyboardButton(text="✋ Через кнопку", callback_data=f"eaauto:{client_id}:0"),
    ]])


@router.callback_query(F.data.startswith("eaauto:"))
async def cb_auto(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    _, cid, flag = callback.data.split(":")
    cid, auto = int(cid), flag == "1"
    if not get_client(cid):
        return await callback.answer("Клиент не найден", show_alert=True)
    update_client(cid, auto_send=1 if auto else 0)
    in_setup = await state.get_state() == ClientSetup.auto.state
    mode = "автоматически 🤖" if auto else "через кнопку ✋"
    await callback.message.edit_text(f"Клиент #{cid}: подача {mode}.")
    await callback.answer()
    if in_setup:
        await state.clear()
        await callback.message.answer(
            f"✅ Клиент #{cid} готов.\n"
            f"Сейчас откликнусь на подходящие вакансии за последние {MAIL_BACKFILL_DAYS} дн. — "
            f"итог пришлю отдельным сообщением.\n\n"
            f"Настройки: <code>/clientshow {cid}</code> · тест: <code>/testmail {cid}</code>"
        )
        start_backfill(callback.bot, cid)


@router.message(StateFilter(ClientSetup.auto), F.text & ~F.text.startswith("/"))
async def cs_auto_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Выберите кнопкой 👇", reply_markup=auto_keyboard(data["client_id"]))


@router.message(StateFilter(AddClient.cv, ClientCV.cv), ~F.document)
async def ac_cv_wrong(message: Message):
    await message.answer("Нужен файл (документ), а не текст/фото. Или /cancel.")


# ---------------------------------------------------------------- управление

def _client_arg(command: CommandObject):
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return None, None
    return get_client(int(parts[0])), (parts[1] if len(parts) > 1 else "")


@router.message(Command("clients"))
async def cmd_clients(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = list_clients()
    if not rows:
        return await message.answer("Клиентов пока нет. /addclient — добавить.")
    lines = ["<b>Клиенты (отклик по email):</b>"]
    for c in rows:
        flag = ("🟢" if c["active"] else "⏸") + ("🤖" if c["auto_send"] else "✋")
        lines.append(f"{flag} #{c['id']} {html.escape(c['full_name'])} — {html.escape(c['email'])}\n"
                     f"    {c['positions']} · сегодня {sent_today(c['id'])}/{MAIL_DAILY_LIMIT}")
    lines.append("\n/mailhelp — все команды")
    await message.answer("\n".join(lines))


@router.message(Command("mailhelp"))
async def cmd_mail_help(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Отклик по email с почты клиента</b>\n\n"
        "/addclient — добавить клиента\n"
        "/clients — список\n"
        "/clientshow ID — посмотреть тему, письмо и режим\n"
        "/clientsubject ID текст — тема письма (шаблон)\n"
        "/clientletter ID — cover letter (шаблон, потом прислать текст)\n"
        "/clientauto ID — подача автоматически или через кнопку\n"
        f"/backfill ID [дней] — откликнуться на вакансии за последние дни (по умолчанию {MAIL_BACKFILL_DAYS})\n"
        "/clientpos ID — сменить должности (кнопками)\n"
        "/clientabout ID текст — выжимка для cover letter\n"
        "/clientphone ID телефон\n"
        "/clientcv ID — заменить CV (потом прислать файл)\n"
        "/clientpass ID — новый пароль приложения\n"
        "/clientsmtp ID host port — свой SMTP-сервер\n"
        "/clientoff ID · /clienton ID — пауза/включить\n"
        "/delclient ID — удалить\n"
        "/testmail ID — тестовое письмо клиенту на его же почту\n"
        "/applyto VACANCY_ID [CLIENT_ID] — отклик на уже опубликованную вакансию\n"
        "/applyto last [CLIENT_ID] — на последнюю опубликованную\n"
        "/mailsent — последние отправки\n\n"
        f"Лимиты: {MAIL_DAILY_LIMIT} писем в день на клиента, одному работодателю до {MAIL_PER_EMPLOYER_DAY} в день, "
        "на одну вакансию — одно письмо."
    )


@router.message(Command("clientshow"))
async def cmd_client_show(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientshow ID")
    e = html.escape
    demo = {"position": "Chief Officer", "vessel": "DP2 PSV", "region": "North Sea", "position_tag": ""}
    subject, body = (fill_tpl(client["subject_tpl"], demo, client) if client["subject_tpl"]
                     else "(бот составляет сам)"), None
    if client["letter_tpl"]:
        body = with_signature(fill_tpl(client["letter_tpl"], demo, client), client)
    await message.answer(
        f"<b>#{client['id']} {e(client['full_name'])}</b> — {e(client['email'])}\n"
        f"Должности: {e(client['positions'] or '')}\n"
        f"Режим: {'автоотправка 🤖' if client['auto_send'] else 'через подтверждение ✋'} · "
        f"{'включён 🟢' if client['active'] else 'на паузе ⏸'}\n"
        f"CV: {e(client['cv_filename'] or '—')}\n\n"
        f"<b>Тема</b> (пример для Chief Officer, DP2 PSV):\n{e(subject)}\n\n"
        f"<b>Письмо:</b>\n" + (f"<pre>{e(body[:3000])}</pre>" if body else "(Claude пишет под каждую вакансию)")
    )


@router.message(Command("clientsubject"))
async def cmd_client_subject(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    if not client or not rest.strip():
        return await message.answer("Формат: /clientsubject ID Application for {position} – {name}\n"
                                    "«-» вместо текста — бот составляет тему сам.\n" + PLACEHOLDERS_HELP)
    update_client(client["id"], subject_tpl=None if rest.strip() == "-" else rest.strip())
    await message.answer("✅ Тема сохранена. Проверить: /clientshow " + str(client["id"]))


@router.message(Command("clientletter"))
async def cmd_client_letter(message: Message, command: CommandObject, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientletter ID — потом пришлите текст письма")
    await state.set_state(ClientLetter.letter)
    await state.update_data(client_id=client["id"])
    await message.answer("Пришлите текст cover letter одним сообщением. «-» — пусть пишет Claude.\n"
                         + PLACEHOLDERS_HELP + "\n/cancel — отмена")


@router.message(StateFilter(ClientLetter.letter), F.text & ~F.text.startswith("/"))
async def on_client_letter(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    text = message.text.strip()
    update_client(data["client_id"], letter_tpl=None if text == "-" else text)
    await message.answer("✅ Письмо сохранено. Проверить: /clientshow " + str(data["client_id"]))


@router.message(Command("clientauto"))
async def cmd_client_auto(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    mode = rest.strip().lower()
    if not client:
        return await message.answer("Формат: /clientauto ID")
    if mode not in ("on", "off"):
        now = "автоматически 🤖" if client["auto_send"] else "через кнопку ✋"
        return await message.answer(AUTO_PROMPT_TEXT.replace("{n}", f"#{client['id']} {html.escape(client['full_name'])} "
                                                                    f"(сейчас: {now}).\n"),
                                     reply_markup=auto_keyboard(client["id"]))
    update_client(client["id"], auto_send=1 if mode == "on" else 0)
    await message.answer(f"#{client['id']}: {'автоотправка 🤖' if mode == 'on' else 'через подтверждение ✋'}")


@router.message(Command("clientpos"))
async def cmd_client_pos(message: Message, command: CommandObject, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientpos ID")
    if not rest.strip():
        current = [t for t in (client["positions"] or "").split(",") if t]
        await state.set_state(ClientPosEdit.pick)
        await state.update_data(client_id=client["id"], sel=current)
        return await message.answer(f"#{client['id']} {html.escape(client['full_name'])}. " + POSITIONS_PROMPT,
                                    reply_markup=positions_keyboard(current))
    ok, bad = _parse_positions(rest)
    if bad or not ok:
        return await message.answer(f"Не узнал: {html.escape(', '.join(bad) or '—')}. Доступные:\n" + _tags_hint())
    update_client(client["id"], positions=",".join(ok))
    await message.answer(f"✅ #{client['id']}: {', '.join(ok)}")


@router.message(Command("clientabout"))
async def cmd_client_about(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    if not client or not rest.strip():
        return await message.answer("Формат: /clientabout ID текст о клиенте")
    update_client(client["id"], about=rest.strip())
    await message.answer("✅ Сохранено")


@router.message(Command("clientphone"))
async def cmd_client_phone(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientphone ID +380...")
    update_client(client["id"], phone=rest.strip())
    await message.answer("✅ Сохранено")


@router.message(Command("clientsmtp"))
async def cmd_client_smtp(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    parts = rest.split()
    if not client or len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Формат: /clientsmtp ID smtp.example.com 465")
    update_client(client["id"], smtp_host=parts[0], smtp_port=int(parts[1]))
    await message.answer("✅ Сохранено. Проверьте: /testmail " + str(client["id"]))


@router.message(Command("clientoff", "clienton"))
async def cmd_client_toggle(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Укажите ID клиента")
    active = 1 if command.command == "clienton" else 0
    update_client(client["id"], active=active)
    await message.answer(f"#{client['id']} {'включён 🟢' if active else 'на паузе ⏸'}")


@router.message(Command("delclient"))
async def cmd_del_client(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /delclient ID")
    delete_client(client["id"])
    await message.answer(f"🗑 Клиент #{client['id']} удалён (вместе с паролем).")


@router.message(Command("clientcv"))
async def cmd_client_cv(message: Message, command: CommandObject, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientcv ID")
    await state.set_state(ClientCV.cv)
    await state.update_data(client_id=client["id"])
    await message.answer("Пришлите новый CV файлом. /cancel — отмена")


@router.message(StateFilter(ClientCV.cv), F.document)
async def on_client_cv(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    doc = message.document
    filename = doc.file_name or "CV.pdf"
    update_client(data["client_id"], cv_file_id=doc.file_id, cv_filename=filename)
    about = await summarize_cv(message.bot, doc.file_id, filename)
    if about:
        update_client(data["client_id"], about=about)
    await message.answer("✅ CV обновлён" + (f"\n<pre>{html.escape(about)}</pre>" if about else ""))


@router.message(Command("clientpass"))
async def cmd_client_pass(message: Message, command: CommandObject, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientpass ID")
    await state.set_state(ClientPass.password)
    await state.update_data(client_id=client["id"])
    await message.answer("Пришлите новый пароль приложения. Сообщение удалю сразу. /cancel — отмена")


@router.message(StateFilter(ClientPass.password), F.text & ~F.text.startswith("/"))
async def on_client_pass(message: Message, state: FSMContext):
    data = await state.get_data()
    password = message.text.strip().replace(" ", "")
    try:
        await message.delete()
    except Exception:
        pass
    client = get_client(data["client_id"])
    try:
        await asyncio.to_thread(_smtp_check, client["smtp_host"], client["smtp_port"], client["email"], password)
    except Exception as e:
        return await message.answer(f"❌ {html.escape(_friendly_smtp_error(e))}\nПришлите ещё раз или /cancel.")
    await state.clear()
    update_client(client["id"], enc_password=encrypt(password))
    await message.answer("✅ Пароль обновлён, вход в почту работает.")


@router.message(Command("testmail"))
async def cmd_test_mail(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, _ = _client_arg(command)
    if not client:
        return await message.answer("Формат: /testmail ID")
    status = await message.answer("Отправляю тест…")
    try:
        msg = await build_message(
            message.bot, client, client["email"], "Test – OffshoreAtSea job applications",
            "This is a test message. If you see it with the CV attached, sending works.\n\n" + signature(client),
        )
        await asyncio.to_thread(_smtp_send, client, msg)
    except Exception as e:
        return await status.edit_text(f"❌ {html.escape(_friendly_smtp_error(e))}")
    await status.edit_text(f"✅ Тестовое письмо ушло на {html.escape(client['email'])} — проверьте ящик клиента.")


@router.message(Command("backfill"))
async def cmd_backfill(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit() or not get_client(int(parts[0])):
        return await message.answer(f"Формат: /backfill ID [дней] (по умолчанию {MAIL_BACKFILL_DAYS})")
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else MAIL_BACKFILL_DAYS
    await message.answer(f"Проверяю вакансии за {days} дн.…")
    start_backfill(message.bot, int(parts[0]), days)


@router.message(Command("applyto"))
async def cmd_apply_to(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if parts and parts[0].lower() == "last":
        row = _q("SELECT id FROM vacancies WHERE status = 'published' ORDER BY id DESC LIMIT 1", one=True)
        if not row:
            return await message.answer("Опубликованных вакансий нет.")
        parts[0] = str(row["id"])
    if not parts or not parts[0].isdigit():
        return await message.answer("Формат: /applyto VACANCY_ID [CLIENT_ID] или /applyto last [CLIENT_ID]")
    vacancy_id = int(parts[0])
    vac = db.get_vacancy(vacancy_id)
    if vac:
        await message.answer(f"Вакансия #{vacancy_id}: тег {html.escape(vac['position_tag'] or '—')}, "
                             f"контакт {html.escape((vac['contact'] or '—')[:80])}")
    only = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    n = await propose_for_vacancy(message.bot, vacancy_id, only_client_id=only)
    if not n:
        await message.answer("Черновиков нет: в вакансии нет email, нет подходящих клиентов, "
                             "либо лимит на сегодня исчерпан.")


@router.message(Command("mailsent"))
async def cmd_mail_sent(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = _q("""SELECT a.*, c.full_name FROM mail_applications a
                 LEFT JOIN mail_clients c ON c.id = a.client_id
                 ORDER BY a.id DESC LIMIT 20""")
    if not rows:
        return await message.answer("Писем пока не было.")
    icons = {"sent": "✅", "failed": "❌", "skipped": "⏭", "draft": "📝", "sending": "⏳"}
    lines = [f"{icons.get(r['status'], '•')} {(r['sent_at'] or r['created_at'])[:16].replace('T', ' ')} "
             f"{html.escape(r['full_name'] or '?')} → {html.escape(r['to_email'])} (вак. #{r['vacancy_id']})"
             for r in rows]
    await message.answer("\n".join(lines))
