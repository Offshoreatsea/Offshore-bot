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

Защита: одно письмо на клиента на вакансию, одному работодателю не чаще
раза в MAIL_DEDUP_DAYS дней, не больше MAIL_DAILY_LIMIT писем в день на клиента.
"""
import asyncio
import base64
import hashlib
import html
import io
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet, InvalidToken

import db

router = Router()

MAIL_DAILY_LIMIT = int(os.getenv("MAIL_DAILY_LIMIT", "15"))
MAIL_DEDUP_DAYS = int(os.getenv("MAIL_DEDUP_DAYS", "14"))
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


def recently_sent_to(client_id: int, to_email: str) -> bool:
    since = (datetime.now() - timedelta(days=MAIL_DEDUP_DAYS)).isoformat()
    return bool(_q(
        """SELECT 1 FROM mail_applications WHERE client_id = ? AND lower(to_email) = lower(?)
           AND status = 'sent' AND sent_at > ?""", (client_id, to_email, since), one=True))


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


def _smtp_connect(host: str, port: int, user: str, password: str):
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
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
        maintype, subtype = ("application", "pdf") if filename.lower().endswith(".pdf") \
            else ("application", "octet-stream")
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


async def compose(fields: dict, client) -> tuple[str, str]:
    letter = await asyncio.to_thread(_compose_letter_sync, fields, client)
    return make_subject(fields, client), letter + "\n\n" + signature(client)


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
        if recently_sent_to(client["id"], to_email):
            continue
        if sent_today(client["id"]) >= MAIL_DAILY_LIMIT:
            await _notify_admins(bot, f"⚠️ {html.escape(client['full_name'])}: дневной лимит "
                                      f"{MAIL_DAILY_LIMIT} писем исчерпан, вакансия #{vacancy_id} пропущена.")
            continue
        subject, body = await compose(fields, client)
        app_id = insert_app(client["id"], vacancy_id, to_email, subject, body)
        await _notify_admins(bot, draft_text(get_app(app_id), client), draft_keyboard(app_id))
        created += 1
    return created


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
    if sent_today(client["id"]) >= MAIL_DAILY_LIMIT:
        return await callback.answer(f"Дневной лимит {MAIL_DAILY_LIMIT} писем исчерпан", show_alert=True)
    set_app(app_id, status="sending")
    await callback.answer("Отправляю…")
    try:
        msg = await build_message(callback.bot, client, app["to_email"], app["subject"], app["body"])
        await asyncio.to_thread(_smtp_send, client, msg)
    except Exception as e:
        err = _friendly_smtp_error(e)
        set_app(app_id, status="failed", error=err)
        await callback.message.answer(f"❌ Не отправлено ({html.escape(client['full_name'])}): {html.escape(err)}\n"
                                      f"Можно нажать ✅ Send ещё раз после исправления.")
        return
    set_app(app_id, status="sent", sent_at=datetime.now().isoformat(), error=None)
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


class ClientCV(StatesGroup):
    cv = State()


class ClientPass(StatesGroup):
    password = State()


def _parse_positions(text: str) -> tuple[list[str], list[str]]:
    lookup = {t.lower(): t for t in _valid_tags}
    ok, bad = [], []
    for raw in re.split(r"[,\s]+", text.strip()):
        raw = raw.strip().lstrip("#")
        if not raw:
            continue
        (ok if raw.lower() in lookup else bad).append(lookup.get(raw.lower(), raw))
    return list(dict.fromkeys(ok)), bad


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
        "1/6. Имя и фамилия (латиницей, как в CV):\n/cancel — отмена"
    )


@router.message(StateFilter(AddClient.name), F.text & ~F.text.startswith("/"))
async def ac_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(AddClient.email)
    await message.answer("2/6. Email клиента (с него будут уходить письма):")


@router.message(StateFilter(AddClient.email), F.text & ~F.text.startswith("/"))
async def ac_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if not EMAIL_RE.fullmatch(email):
        return await message.answer("Не похоже на email, пришлите ещё раз.")
    host, port = smtp_for(email)
    await state.update_data(email=email)
    await state.set_state(AddClient.password)
    await message.answer(
        f"3/6. Пароль приложения для этой почты (сервер {host}).\n\n"
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
        "✅ Вход в почту работает.\n\n4/6. Должности через запятую (теги как в канале):\n" + _tags_hint()
    )


@router.message(StateFilter(AddClient.positions), F.text & ~F.text.startswith("/"))
async def ac_positions(message: Message, state: FSMContext):
    ok, bad = _parse_positions(message.text)
    if bad or not ok:
        return await message.answer(
            f"Не узнал: {html.escape(', '.join(bad) or '—')}. Пришлите ещё раз. Доступные:\n" + _tags_hint())
    await state.update_data(positions=ok)
    await state.set_state(AddClient.phone)
    await message.answer("5/6. Телефон/WhatsApp для подписи письма (или «-», если не нужен):")


@router.message(StateFilter(AddClient.phone), F.text & ~F.text.startswith("/"))
async def ac_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone="" if phone == "-" else phone)
    await state.set_state(AddClient.cv)
    await message.answer("6/6. Пришлите CV файлом (лучше PDF):")


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
        f"✅ Клиент #{client_id} {html.escape(data['full_name'])} добавлен.\n"
        f"Должности: {', '.join(data['positions'])}\n\n{tail}\n\n"
        f"Проверить отправку: <code>/testmail {client_id}</code>"
    )


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
        flag = "🟢" if c["active"] else "⏸"
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
        "/clientpos ID теги — сменить должности\n"
        "/clientabout ID текст — выжимка для cover letter\n"
        "/clientphone ID телефон\n"
        "/clientcv ID — заменить CV (потом прислать файл)\n"
        "/clientpass ID — новый пароль приложения\n"
        "/clientsmtp ID host port — свой SMTP-сервер\n"
        "/clientoff ID · /clienton ID — пауза/включить\n"
        "/delclient ID — удалить\n"
        "/testmail ID — тестовое письмо клиенту на его же почту\n"
        "/applyto VACANCY_ID [CLIENT_ID] — сделать черновик для уже опубликованной вакансии\n"
        "/mailsent — последние отправки\n\n"
        f"Лимиты: {MAIL_DAILY_LIMIT} писем в день на клиента, одному адресу не чаще раза в {MAIL_DEDUP_DAYS} дн."
    )


@router.message(Command("clientpos"))
async def cmd_client_pos(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    client, rest = _client_arg(command)
    if not client:
        return await message.answer("Формат: /clientpos ID ChiefOfficer, 2ndOfficer")
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


@router.message(Command("applyto"))
async def cmd_apply_to(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit():
        return await message.answer("Формат: /applyto VACANCY_ID [CLIENT_ID]")
    vacancy_id = int(parts[0])
    only = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    n = await propose_for_vacancy(message.bot, vacancy_id, only_client_id=only)
    if not n:
        await message.answer("Черновиков нет: в вакансии нет email, нет подходящих клиентов, "
                             "либо этому адресу уже писали / лимит исчерпан.")


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
