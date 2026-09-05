import asyncio
import io
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

import db
import webapp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "offshoreatsea")
CHANNEL_ID = f"@{CHANNEL_USERNAME}"
CHANNEL_LINK = os.getenv("CHANNEL_LINK", f"https://t.me/{CHANNEL_USERNAME}")
APPLY_BOT_LINK = os.getenv("APPLY_BOT_LINK", f"https://t.me/{CHANNEL_USERNAME}")
CONSULT_LINK = os.getenv("CONSULT_LINK", "https://t.me/Offshore_atsea")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL")  # публичный https-адрес мини-приложения, см. README
PORT = int(os.getenv("PORT", "8080"))

DIGEST_TIMES = ["09:00", "14:00", "19:00"]

# Фиксированный список должностей — единственный источник правды для тегов
# #Position и для матчинга в /subscribe. Если встретится реально новая
# должность, которой тут нет, — просто допишите строку в список, ничего
# больше менять не нужно.
RANK_TAGS = [
    "Master", "ChiefOfficer", "SecondOfficer", "ThirdOfficer", "DeckCadet",
    "ChiefEngineer", "SecondEngineer", "ThirdEngineer", "FourthEngineer", "EngineCadet",
    "ETO", "Electrician", "Bosun", "AB", "OS", "Motorman", "Oiler", "Fitter", "Cook", "Steward",
    "CraneOperator", "DPOperator", "ROVPilot", "Rigger", "Welder", "Scaffolder",
    "ClientRepresentative", "SafetyOfficer", "Surveyor",
]

# Фиксированный список типов судов — тоже единый источник правды для тегов
# и матчинга.
VESSEL_TAGS = [
    "Tanker", "Container", "Bulk", "LNG", "LPG", "Chemical",
    "Offshore", "OSV", "CSV", "DSV", "MPV", "MPSV", "AHTS", "PSV", "SOV",
    "CableLayer", "Dredger", "Cruise", "RoRo", "Ferry", "Yacht", "FPSO", "JackUp",
]

FALLBACK_TAG = "Other"

router = Router()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# admin_id -> vacancy_id, чья вакансия сейчас ожидает исправленный текст от админа
pending_corrections: dict[int, int] = {}
# admin_id -> True, если админ сейчас в процессе публикации рекламного поста
pending_ads: dict[int, bool] = {}
# временное хранилище черновиков рекламы: ad_id -> текст
ad_drafts: dict[int, str] = {}
_ad_counter = 0


def parse_template_text_to_fields(text: str) -> dict:
    """Разбирает текст, который админ прислал как исправленный вариант вакансии
    (в том же формате, что показывает сам бот), обратно в поля — без ИИ, просто
    по известным эмодзи-меткам. Используется для сохранения примеров обучения."""
    label_map = {
        "🚢 Vessel": "vessel", "🌍 Region": "region", "🛂 Nationality": "nationality",
        "📅 Date": "date", "⏱️ Duration": "duration", "🔄 Rotation": "rotation",
        "💰 Salary": "salary", "📩 Contact": "contact",
    }
    lines = [l.strip() for l in text.split("\n")]
    fields = {v: None for v in label_map.values()}
    fields["documents"] = []
    fields["requirements"] = []
    fields["notes"] = None
    fields["position"] = None

    section = None
    for raw_line in lines:
        line = re.sub(r"^⚓\s*<b>|</b>$", "", raw_line).strip()
        if not line:
            continue
        matched_label = False
        for label, key in label_map.items():
            if line.startswith(label):
                fields[key] = line.split(":", 1)[1].strip() if ":" in line else None
                matched_label = True
                section = None
                break
        if matched_label:
            continue
        if line.startswith("📄"):
            section = "documents"
            continue
        if line.startswith("✅"):
            section = "requirements"
            continue
        if line.startswith("ℹ️"):
            fields["notes"] = line.lstrip("ℹ️").strip()
            section = None
            continue
        if line.startswith("🔗") or line.startswith("#"):
            section = None
            continue
        if line.startswith("•") and section in ("documents", "requirements"):
            fields[section].append(line.lstrip("•").strip())
            continue
        if fields["position"] is None:
            fields["position"] = line

    return fields

BATCH_EXTRACT_PROMPT = """You will receive a raw block of text pasted by a recruiter for an
offshore/maritime job board. It may contain ONE or MULTIPLE separate job vacancy postings
mashed together, in any language and any format — with explicit labels (Position:, Location:...),
free-flowing prose, bulleted lists, or a mix. There is often NO reliable separator between
postings: a new vacancy can start right after the previous one's contact email with just a
period and a couple of spaces, or after a blank line, or after "---". Identify where each
distinct vacancy starts and ends by meaning (a new job title / new "send your CV to" contact
usually signals a new posting), then extract fields for each one separately.

For each vacancy, extract:
- position: short job title, as written/implied in the source (human-readable, keep natural
  wording, e.g. "Chief Engineer", "2nd Officer")
- position_tag: map the position to EXACTLY ONE tag from this fixed list (pick the closest
  match, e.g. "2/O" or "Second Mate" -> "SecondOfficer"; "Electro-Technical Officer" -> "ETO"):
  {rank_tags}
  If truly nothing in the list fits, use "Other".
- vessel: vessel/rig type or name, as written/implied in the source, or null
- vessel_tag: map the vessel type to EXACTLY ONE tag from this fixed list (e.g. "OSV",
  "Offshore Support Vessel", "supply vessel" -> "OSV"; "product tanker" -> "Tanker"):
  {vessel_tags}
  If vessel type isn't stated or nothing fits, use "Other".
- region: country/region/location, or null
- nationality: nationality/citizenship requirement if stated, or null
- date: joining/start date, or null
- duration: overall contract length if stated separately from rotation (e.g. "28 days, one hitch",
  "until end of 2026"), or null
- rotation: rotation schedule (e.g. "12 weeks on / 12 weeks off"), or null
- salary: salary or rate if mentioned, or null
- documents: list of required certificates/documents/qualifications explicitly mentioned
  (e.g. STCW, COC, BOSIET, medical certificate, visa, passport) — empty list if none stated
- requirements: list of other requirements (experience, skills) — empty list if none stated
- contact: how to apply — an email address, a URL to apply through, or a text instruction,
  or null. If multiple positions in the text share one contact given once (e.g. at the
  end, or in a shared header), use that same contact for every one of those positions —
  do not leave it null just because it wasn't repeated next to each individual position.
  If instead each position has its OWN distinct application link (e.g. a list of roles
  each followed by a different URL), use that position's own specific link as its contact,
  not a shared/generic one found elsewhere in the text.
- notes: any OTHER important information stated in the posting that doesn't fit the fields
  above — e.g. urgency ("urgent, immediate mobilization"), scope of work description, contract
  type, number of positions, shift pattern details, anything a candidate would want to know.
  Keep it short (1-3 sentences, or a few short bullet-style fragments joined with "; "). Do not
  repeat information already captured in the other fields. Null if there's nothing extra to add.

Translate every value into English regardless of source language. position_tag and vessel_tag
are ALWAYS one of the exact English strings from the lists above, regardless of the source
post's language — never translate or invent a new tag string. Never invent data — use null
(or an empty list) for anything not stated.

Return ONLY a JSON array, one object per distinct vacancy found, in the order they appear.
If the text contains just one vacancy, return an array with a single object. No markdown
fences, no commentary — just the JSON array.
{examples}
Text:
---
{text}
---
"""


def build_few_shot_examples() -> str:
    """Подтягивает последние исправления, сделанные админом через кнопку «Исправить»,
    и превращает их в примеры для промпта — так модель со временем повторяет реже
    те же ошибки на похожих вакансиях, без переобучения самой модели."""
    rows = db.get_recent_corrections(3)
    if not rows:
        return ""
    blocks = ["\nHere are recent examples of corrections made by the channel admin — pay attention "
              "to how fields were filled in these, they reflect this specific channel's conventions:\n"]
    for i, row in enumerate(rows, 1):
        blocks.append(
            f"\nExample {i}:\nInput text:\n{row['original_text'][:800]}\n"
            f"Correct extraction:\n{row['corrected_fields']}\n"
        )
    return "\n".join(blocks) + "\n"


def slugify_tag(word: str) -> str:
    return "#" + re.sub(r"[^A-Za-z0-9]", "", word)


def ai_parse_batch(raw: str) -> list[dict]:
    fallback = [{
        "position": raw.strip().split("\n")[0][:120] or "Vacancy",
        "vessel": None, "region": None, "nationality": None, "date": None,
        "duration": None, "rotation": None, "salary": None,
        "documents": [], "requirements": [], "contact": None, "notes": None,
    }]
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{
                "role": "user",
                "content": BATCH_EXTRACT_PROMPT.format(
                    text=raw, examples=build_few_shot_examples(),
                    rank_tags=", ".join(RANK_TAGS), vessel_tags=", ".join(VESSEL_TAGS),
                ),
            }],
        )
        content = resp.content[0].text.strip()
        content = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        if resp.stop_reason == "max_tokens":
            print(
                "[ai_parse_batch] Ответ обрезан по max_tokens — вакансия слишком большая "
                "для одного запроса, поле разбора будет неполным"
            )
        data = json.loads(content)
        if not isinstance(data, list):
            data = [data]
        if not data:
            return fallback
    except json.JSONDecodeError as e:
        print(f"[ai_parse_batch] Не удалось разобрать JSON от Claude: {e}")
        print(f"[ai_parse_batch] Сырой ответ (первые 500 символов): {content[:500]!r}")
        return fallback
    except Exception as e:
        print(f"[ai_parse_batch] Ошибка вызова Claude API: {type(e).__name__}: {e}")
        return fallback

    for item in data:
        # модель иногда всё равно может вернуть что-то не из списка (опечатка,
        # синоним) — подстраховываемся: если тега нет в фиксированном списке,
        # откатываемся на Other, а не тащим в канал произвольный текст как тег
        position_tag = item.get("position_tag") or FALLBACK_TAG
        if position_tag not in RANK_TAGS:
            position_tag = FALLBACK_TAG
        vessel_tag = item.get("vessel_tag") or FALLBACK_TAG
        if vessel_tag not in VESSEL_TAGS:
            vessel_tag = FALLBACK_TAG
        item["position_tag"] = position_tag
        item["vessel_tag"] = vessel_tag
        item["hashtags"] = f"#{position_tag} #{vessel_tag}"
    return data


def render_template(fields: dict) -> str:
    def val(key):
        v = fields.get(key)
        return v if v else None

    def as_list(key):
        v = fields.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip():
            return [l for l in v.split("\n") if l.strip()]
        return []

    date_val = val("date") or val("dates")

    parts = [f"⚓ <b>{val('position') or 'Vacancy'}</b>", ""]
    # каждое поле выводится, только если для него реально есть данные —
    # никакого "—" на пустых полях, чтобы пост не раздувался лишними строками
    for label, value in [
        ("🚢 Vessel", val("vessel")),
        ("🌍 Region", val("region")),
        ("🛂 Nationality", val("nationality")),
        ("📅 Date", date_val),
        ("⏱️ Duration", val("duration")),
        ("🔄 Rotation", val("rotation")),
        ("💰 Salary", val("salary")),
    ]:
        if value:
            parts.append(f"{label}: {value}")

    docs = as_list("documents")
    if docs:
        parts.append("")
        parts.append("📄 Documents/Certificates:")
        parts += [f"• {d}" for d in docs]

    reqs = as_list("requirements")
    if reqs:
        parts.append("")
        parts.append("✅ Requirements:")
        parts += [f"• {r}" for r in reqs]

    if fields.get("notes"):
        parts.append("")
        parts.append(f"ℹ️ {fields['notes']}")

    if val("contact"):
        parts.append("")
        parts.append(f"📩 Contact: {val('contact')}")

    if fields.get("hashtags"):
        parts.append("")
        parts.append(fields["hashtags"])

    parts.append("")
    parts.append(f"🔗 {CHANNEL_LINK}")

    # схлопываем случайные двойные пустые строки (когда почти все поля пустые
    # и подряд идёт несколько условных блоков с "" в начале)
    cleaned: list[str] = []
    for line in parts:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def dedup_key_for(fields: dict) -> str:
    position = (fields.get("position") or "").strip().lower()
    contact = (fields.get("contact") or "").strip().lower()
    return f"{position}|{contact}"



def extract_email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text or "")
    return m.group(0).rstrip(".") if m else None


def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s)]+", text or "")
    return m.group(0).rstrip(".,;") if m else None


def apply_button_url(vacancy_id: int) -> str:
    # Telegram допускает в кнопках только http(s):// и tg:// ссылки — mailto: там
    # не работает и вызывает BUTTON_URL_INVALID, поэтому Apply всегда идёт через
    # диплинк на самого бота; сам email (если есть) бот покажет текстом в /start
    return f"https://t.me/{BOT_USERNAME}?start=apply_{vacancy_id}"


def channel_keyboard(fields: dict, vacancy_id: int, post_link: str | None, title: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📩 Apply", url=apply_button_url(vacancy_id))]]
    if post_link:
        share_url = "https://t.me/share/url?url=" + quote(post_link) + "&text=" + quote(title)
        rows.append([InlineKeyboardButton(text="↗️ Share", url=share_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def draft_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать сейчас", callback_data=f"pub:{vacancy_id}"),
            InlineKeyboardButton(text="В очередь", callback_data=f"queue:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Исправить", callback_data=f"fix:{vacancy_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{vacancy_id}"),
        ],
    ])


def queue_delay_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    # выбор задержки показывается только ПОСЛЕ нажатия «В очередь» —
    # ничего не публикуется и не планируется до этого второго нажатия
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1ч", callback_data=f"queuedelay:{vacancy_id}:1"),
        InlineKeyboardButton(text="2ч", callback_data=f"queuedelay:{vacancy_id}:2"),
        InlineKeyboardButton(text="6ч", callback_data=f"queuedelay:{vacancy_id}:6"),
        InlineKeyboardButton(text="12ч", callback_data=f"queuedelay:{vacancy_id}:12"),
    ]])


def duplicate_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Всё равно опубликовать", callback_data=f"pub:{vacancy_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{vacancy_id}"),
    ]])


def admin_only(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_auto_publish() -> bool:
    return db.get_setting("auto_publish", "off") == "on"


def next_digest_slot() -> datetime:
    now = datetime.now()
    for t in DIGEST_TIMES:
        h, m = map(int, t.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot > now:
            return slot
    h, m = map(int, DIGEST_TIMES[0].split(":"))
    return (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)


async def do_publish(bot: Bot, vacancy_id: int):
    row = db.get_vacancy(vacancy_id)
    fields = dict(row)
    text = render_template(fields)

    sent = await bot.send_message(
        chat_id=CHANNEL_ID, text=text,
        reply_markup=channel_keyboard(fields, vacancy_id, None, fields["position"] or "Vacancy"),
    )
    post_link = f"https://t.me/{CHANNEL_USERNAME}/{sent.message_id}"
    await bot.edit_message_reply_markup(
        chat_id=CHANNEL_ID, message_id=sent.message_id,
        reply_markup=channel_keyboard(fields, vacancy_id, post_link, fields["position"] or "Vacancy"),
    )
    db.set_status(vacancy_id, "published", sent.message_id)

    # публикация в канал уже состоялась и подтверждена выше — рассылка
    # подписчикам оборачивается отдельно, чтобы её сбой ни в коем случае
    # не выглядел как ошибка самой публикации
    try:
        await notify_subscribers(bot, vacancy_id, fields)
    except Exception as e:
        print(f"[do_publish] Рассылка подписчикам не удалась (публикация в канал прошла успешно): {e}")


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    if command.args and command.args.startswith("apply_"):
        vacancy_id = int(command.args.replace("apply_", ""))
        db.increment_clicks(vacancy_id)
        row = db.get_vacancy(vacancy_id)
        contact = (row["contact"] if row else None) or ""
        email = extract_email(contact)
        url = extract_url(contact) if not email else None
        if email:
            # обычный текст с email — Telegram сам делает его кликабельным
            # (открывает почтовый клиент), в отличие от кнопки с mailto:
            await message.answer(f"Send your CV to: {email}")
        elif url:
            # у вакансии своя собственная ссылка для отклика (например разные
            # ссылки на каждую позицию в одном посте) — https, кнопка разрешена
            await message.answer(
                "Open the application form:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Open", url=url)
                ]]),
            )
        elif contact.strip():
            # контакт есть, но это не email и не ссылка — например текстовая
            # инструкция ("напишите в личку @agency"). Показываем как есть,
            # вместо кнопки в никуда.
            await message.answer(f"How to apply: {contact.strip()}")
        else:
            # у вакансии вообще нет контакта в тексте — честно говорим об
            # этом, а не показываем кнопку "Open", ведущую в общий канал
            await message.answer(
                "No direct contact is listed for this vacancy. "
                "Please contact the channel admin.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Contact admin", url=CONSULT_LINK)
                ]]),
            )
        return

    if not admin_only(message.from_user.id):
        await message.answer("Бот приватный.")
        return
    mode = "включена" if is_auto_publish() else "выключена"
    await message.answer(
        "Пришлите текст вакансии в любом формате (или пачку через ---).\n"
        "Команды:\n"
        "/stats — сводка за сегодня (/stats 7 — за 7 дней)\n"
        "/contacts — список email/агентств из сохранённых вакансий\n"
        "/testchannel — проверить доступ бота к каналу\n"
        "/ad — опубликовать рекламный пост с кнопкой «Консультация»\n"
        "/search — открыть поиск вакансий с фильтрами (мини-приложение)\n"
        "/applications — последние отклики через мини-приложение\n"
        "/subscribe — команда для кандидатов: подписка на вакансии по должности "
        "(доступна любому, не только вам)\n"
        f"/autopublish on|off — автопубликация без подтверждения (сейчас {mode})"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    # /stats — сводка за сегодня (по умолчанию); /stats 7 — за последние 7 дней
    arg = (command.args or "").strip()
    days = int(arg) if arg.isdigit() else 1
    period_label = "сегодня" if days == 1 else f"последние {days} дней"

    s = db.daily_stats(days)
    lines = [f"📊 Статистика за {period_label}", "", f"Опубликовано вакансий: {s['total']}"]

    if s["by_position"]:
        lines.append("")
        lines.append("По должностям:")
        lines += [f"• {row['tag']} — {row['c']}" for row in s["by_position"]]

    lines.append("")
    if s["peak_hour"]:
        lines.append(f"Пик активности по откликам: {s['peak_hour']}:00 UTC")
    else:
        lines.append("Пик активности по откликам: пока нет данных за период")

    if s["top_post"]:
        lines.append(f"Самый кликабельный пост: «{s['top_post']['position']}» — {s['top_post']['clicks']} кликов")

    lines.append("")
    lines.append("Точное число просмотров поста Telegram боту не отдаёт — это видно "
                  "только во встроенной статистике канала (иконка 👁 под постом).")

    await message.answer("\n".join(lines))


@router.message(Command("contacts"))
async def cmd_contacts(message: Message):
    if not admin_only(message.from_user.id):
        return
    rows = db.list_contacts()
    if not rows:
        await message.answer("Пока нет сохранённых контактов.")
        return

    lines = [f"Уникальных контактов: {len(rows)}", ""]
    for row in rows:
        last_seen = (row["last_seen"] or "")[:10]
        lines.append(f"{row['contact']} — вакансий: {row['vacancy_count']}, последняя: {last_seen}")
    text = "\n".join(lines)

    if len(text) <= 3500:
        await message.answer(text)
    else:
        # слишком длинный список для одного сообщения — отдаём файлом
        buf = io.BytesIO(text.encode("utf-8"))
        buf.name = "contacts.txt"
        await message.answer_document(BufferedInputFile(buf.read(), filename="contacts.txt"))


@router.message(Command("autopublish"))
async def cmd_autopublish(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        mode = "включена" if is_auto_publish() else "выключена"
        await message.answer(
            f"Автопубликация сейчас {mode}.\nЧтобы переключить: /autopublish on или /autopublish off"
        )
        return
    db.set_setting("auto_publish", arg)
    if arg == "on":
        await message.answer(
            "✅ Автопубликация включена.\n"
            "Вакансии без обнаруженных дублей будут публиковаться сразу, без превью.\n"
            "Подозрение на дубликат всё равно потребует вашего подтверждения."
        )
    else:
        await message.answer("Автопубликация выключена — снова буду спрашивать подтверждение.")


def ad_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Опубликовать", callback_data=f"adpub:{ad_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"adcancel:{ad_id}"),
    ]])


def subscribe_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tag in RANK_TAGS:
        row.append(InlineKeyboardButton(text=tag, callback_data=f"subpos:{tag}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def consult_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤝 Consultation", url=CONSULT_LINK)
    ]])


@router.message(F.text & ~F.text.startswith("/"))
async def handle_vacancy_text(message: Message):
    if not admin_only(message.from_user.id):
        return

    user_id = message.from_user.id

    # режим исправления — админ прислал текст с правильными полями
    # взамен того, что бот разобрал неверно
    if user_id in pending_corrections:
        vacancy_id = pending_corrections.pop(user_id)
        row = db.get_vacancy(vacancy_id)
        corrected_fields = parse_template_text_to_fields(message.text)
        db.insert_correction(row["raw_text"] or "", json.dumps(corrected_fields, ensure_ascii=False))
        await message.answer(
            "Спасибо, запомнил. На похожих вакансиях в следующий раз буду разбирать точнее."
        )
        return

    # режим рекламы — следующий текст публикуется как есть, без разбора ИИ
    if pending_ads.get(user_id):
        pending_ads[user_id] = False
        global _ad_counter
        _ad_counter += 1
        ad_id = _ad_counter
        ad_drafts[ad_id] = message.text
        await message.answer(
            message.text + "\n\n<i>Опубликовать этот рекламный пост?</i>",
            reply_markup=ad_keyboard(ad_id),
        )
        return

    status_msg = await message.answer("Разбираю...")
    auto = is_auto_publish()
    for fields in ai_parse_batch(message.text):
        key = dedup_key_for(fields)
        dup = db.find_recent_duplicate(key)

        vacancy_id = db.insert_vacancy(fields, key, raw_text=message.text)
        text = render_template(fields)

        if dup:
            # дубликат всегда требует ручного подтверждения, даже в режиме автопубликации
            warn = (
                f"⚠️ Похоже, такая вакансия уже публиковалась "
                f"{dup['created_at'][:10]} (id {dup['id']}).\n\n{text}"
            )
            await message.answer(warn, reply_markup=duplicate_keyboard(vacancy_id))
            continue

        if auto:
            try:
                await do_publish(message.bot, vacancy_id)
                await message.answer(text + "\n\n✅ Опубликовано автоматически")
            except TelegramAPIError as e:
                await message.answer(
                    f"❌ Не удалось опубликовать автоматически: {e}\n\n{text}",
                    reply_markup=draft_keyboard(vacancy_id),
                )
        else:
            await message.answer(
                text + "\n\n<i>Опубликовать сейчас или поставить в очередь дайджеста?</i>",
                reply_markup=draft_keyboard(vacancy_id),
            )
    await status_msg.delete()


@router.message(Command("ad"))
async def cmd_ad(message: Message):
    if not admin_only(message.from_user.id):
        return
    pending_ads[message.from_user.id] = True
    await message.answer(
        "Пришлите текст рекламного поста — опубликую как есть, с одной кнопкой «Консультация»."
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    # доступно всем, не только админу — это функция для кандидатов, не для
    # управления каналом
    await message.answer(
        "Choose your position — I'll send matching vacancies from the last 3 days, "
        "then new ones as they're posted:",
        reply_markup=subscribe_keyboard(),
    )


@router.callback_query(F.data.startswith("subpos:"))
async def cb_subscribe_position(callback: CallbackQuery):
    position_tag = callback.data.split(":", 1)[1]
    tg_id = callback.from_user.id
    db.upsert_subscriber(tg_id, position_tag, callback.from_user.username)
    await callback.message.edit_text(f"✅ Subscribed to {position_tag}. Sending recent vacancies...")
    await callback.answer()

    backfill = db.get_recent_published_by_tag(position_tag, days=3)
    if not backfill:
        await callback.bot.send_message(
            tg_id, f"No {position_tag} vacancies in the last 3 days yet — "
                   "you'll get the next one as soon as it's posted."
        )
        return
    for row in backfill:
        fields = dict(row)
        try:
            await callback.bot.send_message(
                tg_id, render_template(fields),
                reply_markup=channel_keyboard(fields, row["id"], None, fields["position"] or "Vacancy"),
            )
            await asyncio.sleep(0.3)  # не спамим Telegram API пачкой без пауз
        except TelegramAPIError:
            pass


async def notify_subscribers(bot: Bot, vacancy_id: int, fields: dict):
    """Дублирует свежеопубликованную вакансию в личку подписчикам с таким же
    position_tag. Вызывается ПОСЛЕ успешной публикации в канал и полностью
    изолирована try/except-ом на уровне вызова — сбой рассылки никак не
    должен влиять на основную публикацию, которая на этот момент уже прошла."""
    position_tag = fields.get("position_tag")
    if not position_tag or position_tag == FALLBACK_TAG:
        return
    for tg_id in db.get_subscribers_for_tag(position_tag):
        try:
            await bot.send_message(
                tg_id, render_template(fields),
                reply_markup=channel_keyboard(fields, vacancy_id, None, fields.get("position") or "Vacancy"),
            )
            await asyncio.sleep(0.1)
        except TelegramAPIError:
            pass


@router.message(Command("search"))
async def cmd_search(message: Message):
    if not WEBAPP_URL:
        await message.answer(
            "Job search isn't set up yet (WEBAPP_URL missing). "
            "Check the channel directly for vacancies."
        )
        return
    await message.answer(
        "Search vacancies by position, vessel or region:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔍 Open search", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]),
    )


@router.message(Command("applications"))
async def cmd_applications(message: Message):
    if not admin_only(message.from_user.id):
        return
    rows = db.list_recent_applications(20)
    if not rows:
        await message.answer("Пока нет откликов через мини-приложение.")
        return
    lines = ["Последние отклики:", ""]
    for row in rows:
        when = (row["created_at"] or "")[:16].replace("T", " ")
        username = f" (@{row['candidate_username']})" if row["candidate_username"] else ""
        lines.append(
            f"• {row['vacancy_position'] or 'вакансия'} — {row['candidate_name'] or '—'}{username}\n"
            f"  {row['contact']} · {when}"
        )
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("adpub:"))
async def cb_ad_publish(callback: CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    text = ad_drafts.pop(ad_id, None)
    if not text:
        await callback.answer("Черновик не найден, пришлите текст заново.", show_alert=True)
        return
    await callback.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=consult_keyboard())
    await callback.message.edit_text(text + "\n\n✅ Опубликовано")
    await callback.answer("Опубликовано в канал")


@router.callback_query(F.data.startswith("adcancel:"))
async def cb_ad_cancel(callback: CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    ad_drafts.pop(ad_id, None)
    await callback.message.edit_text("Отменено")
    await callback.answer()


@router.message(Command("testchannel"))
async def cmd_testchannel(message: Message):
    if not admin_only(message.from_user.id):
        return
    try:
        chat = await message.bot.get_chat(CHANNEL_ID)
        member = await message.bot.get_chat_member(CHANNEL_ID, message.bot.id)
        can_post = getattr(member, "can_post_messages", None)
        status_line = f"Статус бота в канале: {member.status}"
        if can_post is not None:
            status_line += f", право «Публикация сообщений»: {'да' if can_post else 'НЕТ'}"
        await message.answer(
            f"✅ Вижу канал: {chat.title} ({CHANNEL_ID})\n{status_line}"
        )
    except TelegramAPIError as e:
        await message.answer(
            f"❌ Не могу получить доступ к {CHANNEL_ID}: {e}\n\n"
            f"Проверьте CHANNEL_USERNAME в .env и что бот добавлен админом канала."
        )


@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    try:
        await do_publish(callback.bot, vacancy_id)
    except TelegramAPIError as e:
        await callback.message.answer(
            f"❌ Не удалось опубликовать: {e}\n\n"
            f"Частая причина — бот не админ канала {CHANNEL_ID} "
            f"или у него нет права «Публикация сообщений»."
        )
        await callback.answer("Ошибка публикации")
        return
    await callback.message.edit_text(callback.message.html_text + "\n\n✅ Опубликовано")
    await callback.answer("Опубликовано в канал")


@router.callback_query(F.data.startswith("queue:"))
async def cb_queue(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=queue_delay_keyboard(vacancy_id))
    await callback.answer()


@router.callback_query(F.data.startswith("queuedelay:"))
async def cb_queue_delay(callback: CallbackQuery):
    _, vacancy_id_str, hours_str = callback.data.split(":")
    vacancy_id, hours = int(vacancy_id_str), int(hours_str)
    slot = datetime.now() + timedelta(hours=hours)
    db.set_schedule(vacancy_id, slot.isoformat())
    await callback.message.edit_text(
        callback.message.html_text + f"\n\n🕒 В очереди, выйдет в {slot.strftime('%H:%M %d.%m')} (через {hours}ч)"
    )
    await callback.answer("Добавлено в очередь")


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    db.set_status(vacancy_id, "cancelled")
    await callback.message.edit_text("Отменено")
    await callback.answer()


@router.callback_query(F.data.startswith("fix:"))
async def cb_fix(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    pending_corrections[callback.from_user.id] = vacancy_id
    await callback.message.answer(
        "Пришлите вакансию в исправленном виде — скопируйте пост выше и поправьте "
        "неверные строки, оставив те же эмодзи-метки (🚢 Vessel:, 🌍 Region: и т.д.)."
    )
    await callback.answer()


async def digest_worker(bot: Bot):
    while True:
        due = db.get_due_queue(datetime.now().isoformat())
        for row in due:
            try:
                await do_publish(bot, row["id"])
            except TelegramAPIError:
                pass
        await asyncio.sleep(60)


async def main():
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(digest_worker(bot))

    if WEBAPP_URL:
        asyncio.create_task(webapp.run_web_server(bot, BOT_TOKEN, PORT))
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Jobs", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except TelegramAPIError as e:
            print(f"[main] Не удалось установить кнопку меню мини-приложения: {e}")
    else:
        print("[main] WEBAPP_URL не задан — мини-приложение (поиск вакансий) не запущено")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
