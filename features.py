"""
Дополнительные функции бота для подписчиков (входят в подписку €1/мес, кроме
платного email-дайджеста):

1. 📄 Проверка контракта перед подписанием — моряк присылает PDF или фото
   контракта, Claude разбирает его по пунктам (зарплата, ротация, расторжение,
   подводные камни) и даёт список вопросов работодателю.
2. 🗂 Документы и напоминания — моряк хранит сроки действия своих документов
   (паспорт, STCW, медкомиссия, визы, сертификаты), бот напоминает заранее,
   когда что заканчивается. Срок можно вписать вручную или прислать фото
   документа — Claude сам найдёт дату окончания.
3. 📰 Новости офшора — свежие новости только по офшору: новые проекты и
   месторождения, новые суда и верфи, компании и контракты. По запросу /news
   и опциональной ежедневной сводкой тем, кто её включил.

Модуль самодостаточный: свой роутер, свои таблицы, свои воркеры. Подключается
в main.py четырьмя строками (init_tables / setup / include_router / create_task).
Логику вакансий, подписок и рассылок не трогает.
"""
import asyncio
import base64
import html
import os
import re
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import db

router = Router()

_admin_ids: list[int] = []
_claude = None
_channel_id = None   # куда админ публикует выбранную новость

CONTRACT_MODEL = os.getenv("CONTRACT_MODEL", "claude-haiku-4-5-20251001")
NEWS_MODEL = os.getenv("NEWS_MODEL", "claude-haiku-4-5-20251001")
MAX_CONTRACT_MB = int(os.getenv("MAX_CONTRACT_MB", "15"))
DOC_REMIND_DAYS = [int(x) for x in os.getenv("DOC_REMIND_DAYS", "60,30,14,7,1").split(",") if x.strip().isdigit()]
NEWS_FETCH_MINUTES = int(os.getenv("NEWS_FETCH_MINUTES", "120"))
NEWS_DIGEST_HOUR = int(os.getenv("NEWS_DIGEST_HOUR", "9"))      # час ежедневной сводки (по NEWS_TZ_OFFSET)
NEWS_TZ_OFFSET = int(os.getenv("NEWS_TZ_OFFSET", "1"))
NEWS_FEEDS = [u.strip() for u in os.getenv(
    "NEWS_FEEDS",
    "https://www.offshore-energy.biz/feed/,"
    "https://www.oedigital.com/rss.xml,"
    "https://splash247.com/feed/,"
    "https://gcaptain.com/feed/,"
    "https://www.rivieramm.com/rss/all"
).split(",") if u.strip()]


def setup(admin_ids, claude, channel_id=None):
    global _admin_ids, _claude, _channel_id
    _admin_ids = list(admin_ids)
    _claude = claude
    _channel_id = channel_id


# ---------------------------------------------------------------- локализация (en/ru/uk)

_TR = {
    "menu_title": {
        "en": "🧰 <b>Your tools</b> — included in your subscription:",
        "ru": "🧰 <b>Ваши инструменты</b> — входят в подписку:",
        "uk": "🧰 <b>Ваші інструменти</b> — входять у підписку:",
    },
    "btn_contract": {"en": "📄 Check my contract", "ru": "📄 Проверить контракт", "uk": "📄 Перевірити контракт"},
    "btn_docs": {"en": "🗂 My documents & reminders", "ru": "🗂 Мои документы и напоминания",
                 "uk": "🗂 Мої документи та нагадування"},
    "btn_news": {"en": "📰 Offshore news", "ru": "📰 Новости офшора", "uk": "📰 Новини офшору"},
    "need_sub": {
        "en": "🔒 This tool is part of the subscription. Open /subscribe to activate.",
        "ru": "🔒 Этот инструмент входит в подписку. Откройте /subscribe, чтобы активировать.",
        "uk": "🔒 Цей інструмент входить у підписку. Відкрийте /subscribe, щоб активувати.",
    },
    "contract_ask": {
        "en": "📄 Send your contract as a PDF or a clear photo. I'll check salary, rotation, "
              "termination terms and red flags, and suggest what to ask the employer.\n\n"
              "⚠️ This is an automatic aid, not legal advice.",
        "ru": "📄 Пришлите контракт файлом PDF или чётким фото. Я проверю зарплату, ротацию, условия "
              "расторжения и подводные камни и подскажу, что уточнить у работодателя.\n\n"
              "⚠️ Это автоматическая помощь, а не юридическая консультация.",
        "uk": "📄 Надішліть контракт файлом PDF або чітким фото. Я перевірю зарплату, ротацію, умови "
              "розірвання та підводні камені й підкажу, що уточнити в роботодавця.\n\n"
              "⚠️ Це автоматична допомога, а не юридична консультація.",
    },
    "contract_reading": {"en": "🔎 Reading the contract…", "ru": "🔎 Читаю контракт…", "uk": "🔎 Читаю контракт…"},
    "contract_fail": {
        "en": "Couldn't read this file. Send a PDF or a sharp photo of every page.",
        "ru": "Не смог прочитать файл. Пришлите PDF или чёткое фото каждой страницы.",
        "uk": "Не вдалося прочитати файл. Надішліть PDF або чітке фото кожної сторінки.",
    },
    "too_big": {
        "en": f"File is too large (limit {MAX_CONTRACT_MB} MB).",
        "ru": f"Файл слишком большой (лимит {MAX_CONTRACT_MB} МБ).",
        "uk": f"Файл завеликий (ліміт {MAX_CONTRACT_MB} МБ).",
    },
    "docs_title": {"en": "🗂 <b>Your documents</b>", "ru": "🗂 <b>Ваши документы</b>", "uk": "🗂 <b>Ваші документи</b>"},
    "docs_empty": {
        "en": "No documents yet. Add one and I'll remind you before it expires.",
        "ru": "Документов пока нет. Добавьте — и я напомню до окончания срока.",
        "uk": "Документів ще немає. Додайте — і я нагадаю до завершення терміну.",
    },
    "docs_add": {"en": "➕ Add document", "ru": "➕ Добавить документ", "uk": "➕ Додати документ"},
    "docs_pick_type": {"en": "Choose document type:", "ru": "Выберите тип документа:", "uk": "Оберіть тип документа:"},
    "docs_ask_expiry": {
        "en": "Send the expiry date as DD.MM.YYYY — or just send a photo of the document and I'll read the date.",
        "ru": "Пришлите дату окончания в виде ДД.ММ.ГГГГ — или просто фото документа, и я сам найду дату.",
        "uk": "Надішліть дату завершення у форматі ДД.ММ.РРРР — або просто фото документа, і я знайду дату.",
    },
    "docs_saved": {"en": "✅ Saved: {name} — valid until {date}.", "ru": "✅ Сохранено: {name} — действует до {date}.",
                   "uk": "✅ Збережено: {name} — дійсний до {date}."},
    "docs_bad_date": {
        "en": "Didn't get the date. Send it as DD.MM.YYYY (e.g. 15.03.2027) or a photo.",
        "ru": "Не понял дату. Пришлите в виде ДД.ММ.ГГГГ (например 15.03.2027) или фото.",
        "uk": "Не зрозумів дату. Надішліть у форматі ДД.ММ.РРРР (напр. 15.03.2027) або фото.",
    },
    "docs_photo_fail": {
        "en": "Couldn't find a date on the photo. Please type it as DD.MM.YYYY.",
        "ru": "Не нашёл дату на фото. Впишите вручную в виде ДД.ММ.ГГГГ.",
        "uk": "Не знайшов дату на фото. Впишіть вручну у форматі ДД.ММ.РРРР.",
    },
    "news_empty": {
        "en": "No fresh offshore news yet — check back a bit later.",
        "ru": "Свежих новостей офшора пока нет — загляните чуть позже.",
        "uk": "Свіжих новин офшору поки немає — зазирніть трохи згодом.",
    },
    "news_on": {"en": "🔔 Daily offshore news: ON", "ru": "🔔 Ежедневные новости офшора: ВКЛ",
                "uk": "🔔 Щоденні новини офшору: УВІМК"},
    "news_off": {"en": "🔕 Daily offshore news: OFF", "ru": "🔕 Ежедневные новости офшора: ВЫКЛ",
                 "uk": "🔕 Щоденні новини офшору: ВИМК"},
    "news_toggle_on": {"en": "Enable daily digest", "ru": "Включить ежедневную сводку",
                       "uk": "Увімкнути щоденну зведення"},
    "news_toggle_off": {"en": "Disable daily digest", "ru": "Выключить ежедневную сводку",
                        "uk": "Вимкнути щоденне зведення"},
    "blocked": {"en": "", "ru": "", "uk": ""},
}


def L(lang, key, **kw):
    lang = lang if lang in ("en", "ru", "uk") else "en"
    s = _TR.get(key, {}).get(lang) or _TR.get(key, {}).get("en", key)
    return s.format(**kw) if kw else s


def _lang(tg_id):
    return db.get_subscriber_language(tg_id) or "en"


def _is_admin(uid):
    return uid in _admin_ids


def _allowed(tg_id):
    """Функции доступны активным подписчикам (как и вакансии-алерты), и всегда админам."""
    if db.is_blocked(tg_id):
        return False
    return _is_admin(tg_id) or db.is_subscription_active(tg_id)


# ---------------------------------------------------------------- таблицы

DOC_TYPES = [
    ("passport", {"en": "Passport", "ru": "Паспорт", "uk": "Паспорт"}),
    ("seaman_book", {"en": "Seaman's Book", "ru": "Паспорт моряка", "uk": "Посвідчення моряка"}),
    ("stcw", {"en": "STCW / BST", "ru": "STCW / BST", "uk": "STCW / BST"}),
    ("medical", {"en": "Medical (ENG1/ILO)", "ru": "Медкомиссия (ENG1/ILO)", "uk": "Медкомісія (ENG1/ILO)"}),
    ("visa", {"en": "Visa", "ru": "Виза", "uk": "Віза"}),
    ("gmdss", {"en": "GMDSS", "ru": "GMDSS", "uk": "GMDSS"}),
    ("dp", {"en": "DP certificate", "ru": "DP сертификат", "uk": "DP сертифікат"}),
    ("coc", {"en": "CoC / Licence", "ru": "Диплом / CoC", "uk": "Диплом / CoC"}),
    ("bosiet", {"en": "BOSIET / HUET", "ru": "BOSIET / HUET", "uk": "BOSIET / HUET"}),
    ("yellow_fever", {"en": "Yellow fever", "ru": "Жёлтая лихорадка", "uk": "Жовта гарячка"}),
    ("other", {"en": "Other", "ru": "Другое", "uk": "Інше"}),
]
_DOC_LABEL = {k: v for k, v in DOC_TYPES}


def doc_label(doc_type, lang):
    return _DOC_LABEL.get(doc_type, {}).get(lang) or _DOC_LABEL.get(doc_type, {}).get("en", doc_type)


def init_tables():
    conn = db.get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seafarer_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            doc_type TEXT,
            title TEXT,
            expiry TEXT,
            file_id TEXT,
            reminded_days TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_tg ON seafarer_docs (tg_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            summary TEXT,
            category TEXT,
            source TEXT,
            published_at TEXT,
            added_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_added ON news_items (added_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_prefs (
            tg_id INTEGER PRIMARY KEY,
            daily INTEGER DEFAULT 0,
            last_sent_id INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def _q(sql, params=(), one=False, commit=False):
    conn = db.get_conn()
    try:
        cur = conn.execute(sql, params)
        if commit:
            conn.commit()
            return cur.lastrowid
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- общее меню инструментов

class ContractFlow(StatesGroup):
    wait_file = State()


class DocFlow(StatesGroup):
    wait_value = State()


def tools_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=L(lang, "btn_contract"), callback_data="feat:contract")],
        [InlineKeyboardButton(text=L(lang, "btn_docs"), callback_data="feat:docs")],
        [InlineKeyboardButton(text=L(lang, "btn_news"), callback_data="feat:news")],
    ])


@router.message(Command("tools"))
async def cmd_tools(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await message.answer(L(lang, "need_sub"))
    await message.answer(L(lang, "menu_title"), reply_markup=tools_keyboard(lang))


@router.callback_query(F.data == "feat:home")
async def cb_tools_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = _lang(callback.from_user.id)
    try:
        await callback.message.edit_text(L(lang, "menu_title"), reply_markup=tools_keyboard(lang))
    except Exception:
        await callback.message.answer(L(lang, "menu_title"), reply_markup=tools_keyboard(lang))
    await callback.answer()


def _back_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️", callback_data="feat:home")]])


# ---------------------------------------------------------------- 1. проверка контракта

CONTRACT_PROMPT = """You are a maritime crewing expert helping a seafarer review a job contract/employment
agreement BEFORE signing. The document is attached. Analyse it and reply in the SAME language as the
seafarer's interface, which is: {lang_name}.

Give a concise, practical review with these sections (use short bullet points, Telegram HTML <b> for headers):
1. <b>Key terms</b> — position, vessel/company if stated, wage (amount + currency + per month/day), rotation
   (weeks on/off), contract length, notice period, currency and date of payment.
2. <b>⚠️ Watch out</b> — anything unfavourable or risky: unpaid training/standby, who pays flights/visa/medical,
   penalties for early termination, automatic renewal, wage deductions, probation, unclear overtime, missing
   or vague clauses a seafarer would expect (e.g. no mention of P&I / insurance, no repatriation, no MLC
   compliance).
3. <b>❓ Ask the employer</b> — 3–6 specific questions the seafarer should clarify before signing.
4. <b>Bottom line</b> — one or two sentences: overall is it a normal contract or are there serious red flags.

Rules: base everything ONLY on what the document actually says; if something important is simply not mentioned,
say so (that itself is useful). Do not invent numbers. Keep it tight — it's read on a phone. End with one line:
"Это автоматическая проверка, не юридическая консультация." translated into {lang_name}.
"""

_LANG_NAME = {"en": "English", "ru": "Russian", "uk": "Ukrainian"}


@router.callback_query(F.data == "feat:contract")
async def cb_contract(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await callback.answer(L(lang, "need_sub"), show_alert=True)
    await state.set_state(ContractFlow.wait_file)
    try:
        await callback.message.edit_text(L(lang, "contract_ask"), reply_markup=_back_kb(lang))
    except Exception:
        await callback.message.answer(L(lang, "contract_ask"), reply_markup=_back_kb(lang))
    await callback.answer()


@router.message(Command("contract"))
async def cmd_contract(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await message.answer(L(lang, "need_sub"))
    await state.set_state(ContractFlow.wait_file)
    await message.answer(L(lang, "contract_ask"), reply_markup=_back_kb(lang))


def _blocks_from(data: bytes, mime: str):
    b64 = base64.standard_b64encode(data).decode()
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}


def _review_contract_sync(data: bytes, mime: str, lang: str) -> str | None:
    if not _claude:
        return None
    prompt = CONTRACT_PROMPT.format(lang_name=_LANG_NAME.get(lang, "English"))
    try:
        resp = _claude.messages.create(
            model=CONTRACT_MODEL, max_tokens=1800,
            messages=[{"role": "user", "content": [_blocks_from(data, mime), {"type": "text", "text": prompt}]}],
        )
        return resp.content[0].text.strip() or None
    except Exception as e:
        print(f"[features] разбор контракта не удался: {type(e).__name__}: {e}")
        return None


async def _handle_contract_file(message: Message, state: FSMContext, data: bytes, mime: str):
    lang = _lang(message.from_user.id)
    status = await message.answer(L(lang, "contract_reading"))
    review = await asyncio.to_thread(_review_contract_sync, data, mime, lang)
    await state.clear()
    if not review:
        return await status.edit_text(L(lang, "contract_fail"), reply_markup=_back_kb(lang))
    # ответ может быть длинным — режем по лимиту Telegram
    for i, chunk in enumerate(_split(review, 3900)):
        if i == 0:
            await status.edit_text(chunk, reply_markup=(None if len(review) > 3900 else _back_kb(lang)))
        else:
            await message.answer(chunk)
    if len(review) > 3900:
        await message.answer("—", reply_markup=_back_kb(lang))


@router.message(StateFilter(ContractFlow.wait_file), F.document)
async def contract_doc(message: Message, state: FSMContext):
    doc = message.document
    lang = _lang(message.from_user.id)
    if (doc.file_size or 0) > MAX_CONTRACT_MB * 1024 * 1024:
        return await message.answer(L(lang, "too_big"))
    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()
    if "pdf" not in mime and not name.endswith(".pdf") and not mime.startswith("image/"):
        return await message.answer(L(lang, "contract_fail"), reply_markup=_back_kb(lang))
    buf = await _download(message.bot, doc.file_id)
    use_mime = "application/pdf" if ("pdf" in mime or name.endswith(".pdf")) else (mime or "image/jpeg")
    await _handle_contract_file(message, state, buf, use_mime)


@router.message(StateFilter(ContractFlow.wait_file), F.photo)
async def contract_photo(message: Message, state: FSMContext):
    buf = await _download(message.bot, message.photo[-1].file_id)
    await _handle_contract_file(message, state, buf, "image/jpeg")


# ---------------------------------------------------------------- 2. документы и напоминания

@router.callback_query(F.data == "feat:docs")
async def cb_docs(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = callback.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await callback.answer(L(lang, "need_sub"), show_alert=True)
    await _show_docs(callback.message, tg_id, lang, edit=True)
    await callback.answer()


@router.message(Command("mydocs"))
async def cmd_mydocs(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await message.answer(L(lang, "need_sub"))
    await _show_docs(message, tg_id, lang, edit=False)


def _fmt_expiry(expiry: str, lang: str) -> str:
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return expiry
    days = (d - datetime.now().date()).days
    ds = d.strftime("%d.%m.%Y")
    if days < 0:
        tag = {"en": "❌ expired", "ru": "❌ просрочен", "uk": "❌ прострочено"}[lang if lang in ("en", "ru", "uk") else "en"]
    elif days <= 30:
        tag = {"en": f"⚠️ {days} d left", "ru": f"⚠️ осталось {days} дн", "uk": f"⚠️ лишилось {days} дн"}[lang if lang in ("en", "ru", "uk") else "en"]
    else:
        tag = {"en": f"{days} d left", "ru": f"ещё {days} дн", "uk": f"ще {days} дн"}[lang if lang in ("en", "ru", "uk") else "en"]
    return f"{ds} ({tag})"


async def _show_docs(target, tg_id, lang, edit):
    rows = _q("SELECT * FROM seafarer_docs WHERE tg_id = ? ORDER BY expiry", (tg_id,))
    lines = [L(lang, "docs_title"), ""]
    kb = [[InlineKeyboardButton(text=L(lang, "docs_add"), callback_data="doc:add")]]
    if not rows:
        lines.append(L(lang, "docs_empty"))
    else:
        for r in rows:
            lines.append(f"• <b>{html.escape(r['title'] or doc_label(r['doc_type'], lang))}</b> — "
                         f"{_fmt_expiry(r['expiry'], lang)}")
            kb.append([InlineKeyboardButton(text=f"🗑 {html.escape((r['title'] or doc_label(r['doc_type'], lang))[:30])}",
                                            callback_data=f"doc:del:{r['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️", callback_data="feat:home")])
    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "doc:add")
async def cb_doc_add(callback: CallbackQuery, state: FSMContext):
    lang = _lang(callback.from_user.id)
    if not _allowed(callback.from_user.id):
        return await callback.answer(L(lang, "need_sub"), show_alert=True)
    kb = []
    row = []
    for key, labels in DOC_TYPES:
        row.append(InlineKeyboardButton(text=labels.get(lang, labels["en"]), callback_data=f"doc:type:{key}"))
        if len(row) == 2:
            kb.append(row); row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton(text="⬅️", callback_data="feat:docs")])
    await callback.message.edit_text(L(lang, "docs_pick_type"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@router.callback_query(F.data.startswith("doc:type:"))
async def cb_doc_type(callback: CallbackQuery, state: FSMContext):
    doc_type = callback.data.split(":", 2)[2]
    lang = _lang(callback.from_user.id)
    await state.set_state(DocFlow.wait_value)
    await state.update_data(doc_type=doc_type)
    await callback.message.edit_text(
        f"<b>{doc_label(doc_type, lang)}</b>\n\n{L(lang, 'docs_ask_expiry')}", reply_markup=_back_kb(lang))
    await callback.answer()


@router.callback_query(F.data.startswith("doc:del:"))
async def cb_doc_del(callback: CallbackQuery, state: FSMContext):
    doc_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id
    _q("DELETE FROM seafarer_docs WHERE id = ? AND tg_id = ?", (doc_id, tg_id), commit=True)
    await _show_docs(callback.message, tg_id, _lang(tg_id), edit=True)
    await callback.answer("🗑")


_DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _parse_date(text: str) -> str | None:
    m = _DATE_RE.search(text or "")
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m = _ISO_RE.search(text or "")
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _extract_expiry_sync(data: bytes, mime: str) -> str | None:
    if not _claude:
        return None
    try:
        resp = _claude.messages.create(
            model=CONTRACT_MODEL, max_tokens=60,
            messages=[{"role": "user", "content": [
                _blocks_from(data, mime),
                {"type": "text", "text": "This is a seafarer document/certificate. Find its EXPIRY / VALID UNTIL "
                                         "date. Reply with ONLY that date as YYYY-MM-DD. If there is no expiry "
                                         "date, reply exactly NONE."},
            ]}],
        )
        out = resp.content[0].text.strip()
        return _parse_date(out)
    except Exception as e:
        print(f"[features] дата из документа не распозналась: {type(e).__name__}: {e}")
        return None


async def _save_doc(message, state, expiry, file_id=None):
    data = await state.get_data()
    doc_type = data.get("doc_type", "other")
    tg_id = message.from_user.id
    lang = _lang(tg_id)
    _q("INSERT INTO seafarer_docs (tg_id, doc_type, title, expiry, file_id, reminded_days, created_at) "
       "VALUES (?, ?, ?, ?, ?, '', ?)",
       (tg_id, doc_type, doc_label(doc_type, lang), expiry, file_id, datetime.now().isoformat()), commit=True)
    await state.clear()
    await message.answer(L(lang, "docs_saved", name=doc_label(doc_type, lang),
                           date=datetime.strptime(expiry, "%Y-%m-%d").strftime("%d.%m.%Y")))
    await _show_docs(message, tg_id, lang, edit=False)


@router.message(StateFilter(DocFlow.wait_value), F.text & ~F.text.startswith("/"))
async def doc_value_text(message: Message, state: FSMContext):
    lang = _lang(message.from_user.id)
    expiry = _parse_date(message.text)
    if not expiry:
        return await message.answer(L(lang, "docs_bad_date"))
    await _save_doc(message, state, expiry)


@router.message(StateFilter(DocFlow.wait_value), F.photo)
async def doc_value_photo(message: Message, state: FSMContext):
    lang = _lang(message.from_user.id)
    buf = await _download(message.bot, message.photo[-1].file_id)
    expiry = await asyncio.to_thread(_extract_expiry_sync, buf, "image/jpeg")
    if not expiry:
        return await message.answer(L(lang, "docs_photo_fail"))
    await _save_doc(message, state, expiry, file_id=message.photo[-1].file_id)


@router.message(StateFilter(DocFlow.wait_value), F.document)
async def doc_value_doc(message: Message, state: FSMContext):
    lang = _lang(message.from_user.id)
    doc = message.document
    mime = doc.mime_type or ""
    use = "application/pdf" if "pdf" in mime or (doc.file_name or "").lower().endswith(".pdf") else mime
    if not use:
        return await message.answer(L(lang, "docs_bad_date"))
    buf = await _download(message.bot, doc.file_id)
    expiry = await asyncio.to_thread(_extract_expiry_sync, buf, use)
    if not expiry:
        return await message.answer(L(lang, "docs_photo_fail"))
    await _save_doc(message, state, expiry, file_id=doc.file_id)


# ---------------------------------------------------------------- 3. новости офшора

@router.callback_query(F.data == "feat:news")
async def cb_news(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = callback.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await callback.answer(L(lang, "need_sub"), show_alert=True)
    await _show_news(callback.message, tg_id, lang, edit=True)
    await callback.answer()


@router.message(Command("news"))
async def cmd_news(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await message.answer(L(lang, "need_sub"))
    await _show_news(message, tg_id, lang, edit=False)


_CAT_EMOJI = {"project": "🛢", "vessel": "🚢", "company": "🏢"}


def _news_prefs(tg_id):
    r = _q("SELECT * FROM news_prefs WHERE tg_id = ?", (tg_id,), one=True)
    return r


def _render_news(rows, lang):
    out = []
    for r in rows:
        emoji = _CAT_EMOJI.get(r["category"], "📰")
        out.append(f"{emoji} <b>{html.escape(r['title'])}</b>\n{html.escape(r['summary'] or '')}\n"
                   f"<a href=\"{html.escape(r['url'])}\">{html.escape(r['source'] or 'link')}</a>")
    return "\n\n".join(out)


async def _show_news(target, tg_id, lang, edit):
    rows = _q("SELECT * FROM news_items ORDER BY id DESC LIMIT 7")
    pref = _news_prefs(tg_id)
    daily_on = bool(pref and pref["daily"])
    if not rows:
        body = L(lang, "news_empty")
    else:
        body = _render_news(rows, lang)
    kb_rows = [[InlineKeyboardButton(text=(L(lang, "news_on") if daily_on else L(lang, "news_off")),
                                     callback_data="news:toggle")]]
    # админам — по кнопке на каждую новость: опубликовать её в канал подробным
    # текстом без ссылок
    if _is_admin(tg_id) and rows:
        for i, r in enumerate(rows, 1):
            kb_rows.append([InlineKeyboardButton(text=f"📢 Опубликовать #{i} в канал",
                                                 callback_data=f"news:pub:{r['id']}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️", callback_data="feat:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    head = "📰 <b>Offshore</b>\n\n" if lang == "en" else "📰 <b>Офшор</b>\n\n"
    text = head + body
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


NEWS_PUBLISH_PROMPT = """You are the editor of an offshore/maritime jobs & industry Telegram channel.
Turn the news item below into a ready-to-publish channel post IN RUSSIAN. Make it a detailed, standalone
post (4–8 sentences): what happened, who is involved (companies, field/vessel/shipyard), where, key figures
and what it means for the offshore industry and crews. Professional tone, a couple of fitting emojis, 2–4
hashtags at the end. IMPORTANT: do NOT include any links or URLs. Do not invent facts beyond the item.

Title: {title}
Summary: {summary}
Source: {source}
"""


def _expand_news_sync(row) -> str | None:
    if not _claude:
        return None
    try:
        resp = _claude.messages.create(
            model=NEWS_MODEL, max_tokens=900,
            messages=[{"role": "user", "content": NEWS_PUBLISH_PROMPT.format(
                title=row["title"], summary=row["summary"] or "", source=row["source"] or "")}],
        )
        txt = resp.content[0].text.strip()
        txt = re.sub(r"https?://\S+", "", txt)  # подстраховка: вырезаем любые ссылки
        return txt or None
    except Exception as e:
        print(f"[features] разворот новости не удался: {type(e).__name__}: {e}")
        return None


@router.callback_query(F.data.startswith("news:pub:"))
async def cb_news_publish(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return await callback.answer()
    if not _channel_id:
        return await callback.answer("Канал не настроен (CHANNEL_ID).", show_alert=True)
    news_id = int(callback.data.split(":")[2])
    row = _q("SELECT * FROM news_items WHERE id = ?", (news_id,), one=True)
    if not row:
        return await callback.answer("Новость не найдена.", show_alert=True)
    await callback.answer("Готовлю пост…")
    post = await asyncio.to_thread(_expand_news_sync, row)
    if not post:
        post = f"📰 <b>{html.escape(row['title'])}</b>\n\n{html.escape(row['summary'] or '')}"
    # предпросмотр админу с кнопкой подтверждения
    _pending_news_posts[callback.from_user.id] = post
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать в канал", callback_data="news:pubok"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="news:pubcancel"),
    ]])
    await callback.message.answer(f"Предпросмотр поста в канал:\n\n{post}", reply_markup=kb,
                                  disable_web_page_preview=True)


_pending_news_posts: dict[int, str] = {}


@router.callback_query(F.data == "news:pubcancel")
async def cb_news_pub_cancel(callback: CallbackQuery):
    _pending_news_posts.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_text("Публикация отменена.")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "news:pubok")
async def cb_news_pub_ok(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return await callback.answer()
    post = _pending_news_posts.pop(callback.from_user.id, None)
    if not post:
        return await callback.answer("Текст не найден, выберите новость заново.", show_alert=True)
    try:
        await callback.bot.send_message(_channel_id, post, disable_web_page_preview=True)
        await callback.message.edit_text("✅ Опубликовано в канал.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Не удалось опубликовать: {html.escape(str(e))}")
    await callback.answer()


@router.callback_query(F.data == "news:toggle")
async def cb_news_toggle(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    lang = _lang(tg_id)
    if not _allowed(tg_id):
        return await callback.answer(L(lang, "need_sub"), show_alert=True)
    pref = _news_prefs(tg_id)
    new = 0 if (pref and pref["daily"]) else 1
    last_id = _q("SELECT MAX(id) m FROM news_items", one=True)["m"] or 0
    _q("INSERT INTO news_prefs (tg_id, daily, last_sent_id, updated_at) VALUES (?, ?, ?, ?) "
       "ON CONFLICT(tg_id) DO UPDATE SET daily = excluded.daily, updated_at = excluded.updated_at",
       (tg_id, new, last_id, datetime.now().isoformat()), commit=True)
    await _show_news(callback.message, tg_id, lang, edit=True)
    await callback.answer(L(lang, "news_on") if new else L(lang, "news_off"))


# ---- загрузка и фильтрация новостей

_OFFSHORE_TERMS = ("offshore", "subsea", "fpso", "fso ", "psv", "ahts", "osv", "ocv", "csov", "sov", "wind farm",
                   "offshore wind", "rig", "drillship", "jack-up", "jackup", "semi-sub", "semisub", "oil field",
                   "oilfield", "gas field", "field development", "fid ", "final investment decision", "platform",
                   "umbilical", "pipelay", "flng", "fsru", "decommission", "mooring", "wellhead", "tie-back",
                   "tieback", "exploration", "appraisal well", "production start", "first oil", "first gas")
_CAT_RULES = [
    ("vessel", ("newbuild", "new build", "shipyard", "delivered", "order for", "orders ", "christened", "launch",
                "keel", "psv", "ahts", "csov", "sov", "osv", "vessel order", "drillship", "jack-up", "jackup",
                "fleet expansion", "charter", "time charter")),
    ("project", ("field development", "fid", "final investment decision", "first oil", "first gas", "exploration",
                 "appraisal", "discovery", "licence", "license round", "block ", "production start", "sanction",
                 "tie-back", "tieback", "wind farm", "offshore wind")),
    ("company", ("awarded", "contract", "agreement", "acquires", "acquisition", "merger", "joint venture",
                 "partnership", "secures", "wins ", "signs ")),
]


def _categorize(text: str) -> str | None:
    t = text.lower()
    if not any(term in t for term in _OFFSHORE_TERMS):
        return None  # не про офшор — пропускаем
    for cat, kws in _CAT_RULES:
        if any(k in t for k in kws):
            return cat
    return "project"  # офшорное, но без явной категории — пусть будет в проектах


def _fetch_feeds_sync() -> list[dict]:
    try:
        import feedparser
    except Exception:
        print("[news] feedparser не установлен — добавьте feedparser в requirements.txt")
        return []
    items = []
    for url in NEWS_FEEDS:
        try:
            d = feedparser.parse(url)
            source = (d.feed.get("title") if getattr(d, "feed", None) else None) or url.split("/")[2]
            for e in d.entries[:25]:
                link = e.get("link")
                title = (e.get("title") or "").strip()
                if not link or not title:
                    continue
                summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))[:400].strip()
                cat = _categorize(title + " " + summary)
                if not cat:
                    continue
                items.append({"url": link, "title": title[:300], "summary": summary, "category": cat,
                              "source": source[:80], "published": e.get("published", "")})
        except Exception as ex:
            print(f"[news] {url}: {type(ex).__name__}: {ex}")
    return items


async def news_fetch_once(bot=None) -> int:
    items = await asyncio.to_thread(_fetch_feeds_sync)
    added = 0
    for it in items:
        try:
            rid = _q("INSERT OR IGNORE INTO news_items (url, title, summary, category, source, published_at, added_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (it["url"], it["title"], it["summary"], it["category"], it["source"], it["published"],
                      datetime.now().isoformat()), commit=True)
            if rid:
                added += 1
        except Exception as e:
            print(f"[news] insert: {e}")
    return added


async def news_worker(bot: Bot):
    await asyncio.sleep(30)
    while True:
        try:
            n = await news_fetch_once(bot)
            if n:
                print(f"[news] добавлено {n} новых материалов")
        except Exception as e:
            print(f"[news_worker] {type(e).__name__}: {e}")
        await asyncio.sleep(NEWS_FETCH_MINUTES * 60)


async def news_digest_worker(bot: Bot):
    """Раз в день шлёт тем, кто включил, новые материалы с прошлой отправки."""
    last_day = None
    while True:
        try:
            now = datetime.utcnow() + timedelta(hours=NEWS_TZ_OFFSET)
            if now.hour == NEWS_DIGEST_HOUR and last_day != now.date():
                last_day = now.date()
                subs = _q("SELECT * FROM news_prefs WHERE daily = 1")
                for pref in subs:
                    tg_id = pref["tg_id"]
                    if not _allowed(tg_id):
                        continue
                    rows = _q("SELECT * FROM news_items WHERE id > ? ORDER BY id DESC LIMIT 6",
                              (pref["last_sent_id"] or 0,))
                    if not rows:
                        continue
                    lang = _lang(tg_id)
                    head = "📰 <b>Offshore — today</b>\n\n" if lang == "en" else "📰 <b>Офшор — сводка дня</b>\n\n"
                    try:
                        await bot.send_message(tg_id, head + _render_news(rows, lang), disable_web_page_preview=True)
                        top = _q("SELECT MAX(id) m FROM news_items", one=True)["m"] or 0
                        _q("UPDATE news_prefs SET last_sent_id = ? WHERE tg_id = ?", (top, tg_id), commit=True)
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[news_digest_worker] {type(e).__name__}: {e}")
        await asyncio.sleep(300)


# ---------------------------------------------------------------- напоминания о документах

async def docs_reminder_worker(bot: Bot):
    await asyncio.sleep(60)
    while True:
        try:
            await _docs_reminder_tick(bot)
        except Exception as e:
            print(f"[docs_reminder_worker] {type(e).__name__}: {e}")
        await asyncio.sleep(6 * 3600)  # 4 раза в сутки достаточно


async def _docs_reminder_tick(bot: Bot):
    today = datetime.now().date()
    rows = _q("SELECT * FROM seafarer_docs")
    for r in rows:
        try:
            exp = datetime.strptime(r["expiry"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_left = (exp - today).days
        reminded = set((r["reminded_days"] or "").split(",")) - {""}
        expired_key = "expired"
        fire = None
        if days_left < 0:
            if expired_key not in reminded:
                fire = expired_key
        else:
            # только БЛИЖАЙШИЙ порог, до которого осталось <= дней; если о нём уже
            # напоминали — молчим (не перескакиваем на больший порог, иначе было бы
            # несколько напоминаний подряд об одном документе)
            candidates = sorted(d for d in DOC_REMIND_DAYS if days_left <= d)
            if candidates and str(candidates[0]) not in reminded:
                fire = candidates[0]
        if fire is None:
            continue
        tg_id = r["tg_id"]
        if not _allowed(tg_id):
            continue
        lang = _lang(tg_id)
        name = html.escape(r["title"] or doc_label(r["doc_type"], lang))
        ds = exp.strftime("%d.%m.%Y")
        if fire == expired_key:
            msg = {"en": f"❌ Your <b>{name}</b> expired on {ds}. Time to renew it.",
                   "ru": f"❌ Ваш документ <b>{name}</b> просрочен с {ds}. Пора продлить.",
                   "uk": f"❌ Ваш документ <b>{name}</b> прострочений з {ds}. Час продовжити."}[lang if lang in ("en","ru","uk") else "en"]
        else:
            msg = {"en": f"⏰ Your <b>{name}</b> expires on {ds} — {days_left} days left. Plan the renewal.",
                   "ru": f"⏰ Ваш документ <b>{name}</b> заканчивается {ds} — осталось {days_left} дн. Планируйте продление.",
                   "uk": f"⏰ Ваш документ <b>{name}</b> завершується {ds} — лишилось {days_left} дн. Плануйте продовження."}[lang if lang in ("en","ru","uk") else "en"]
        try:
            await bot.send_message(tg_id, msg)
            reminded.add(str(fire))
            _q("UPDATE seafarer_docs SET reminded_days = ? WHERE id = ?", (",".join(sorted(reminded)), r["id"]),
               commit=True)
            await asyncio.sleep(0.1)
        except Exception:
            pass


# ---------------------------------------------------------------- вспомогательное

def _split(text: str, limit: int):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


async def _download(bot: Bot, file_id: str) -> bytes:
    import io
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


ADMIN_HINT_COMMANDS = [
    ("tools", "🧰 Инструменты подписчика (контракт, документы, новости)"),
    ("news", "📰 Новости офшора"),
]


@router.startup()
async def _install_commands(bot: Bot):
    # команды для всех: /tools, /contract, /mydocs, /news
    try:
        cmds = [
            BotCommand(command="tools", description="🧰 Tools: contract, documents, news"),
            BotCommand(command="news", description="📰 Offshore news"),
            BotCommand(command="mydocs", description="🗂 My documents & reminders"),
            BotCommand(command="contract", description="📄 Check a contract"),
        ]
        base = await bot.get_my_commands()
        names = {c.command for c in base}
        merged = list(base) + [c for c in cmds if c.command not in names]
        await bot.set_my_commands(merged)
    except Exception as e:
        print(f"[features] не удалось поставить команды: {e}")
