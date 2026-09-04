import asyncio
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # без @, для диплинков счётчика кликов
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "offshoreatsea")
CHANNEL_ID = f"@{CHANNEL_USERNAME}"
APPLY_BOT_LINK = os.getenv("APPLY_BOT_LINK", f"https://t.me/{CHANNEL_USERNAME}")

DIGEST_TIMES = ["09:00", "14:00", "19:00"]  # время сервера — см. примечание в инструкции

router = Router()

# ---------- Справочники ----------

REGIONS = [
    "UK", "United Kingdom", "Angola", "Nigeria", "Saudi Arabia", "ARAMCO",
    "Finland", "USA", "US", "Norway", "Netherlands", "Qatar", "UAE",
    "Worldwide", "Europe", "West Africa", "Ghana", "Egypt", "Brazil",
]
VESSEL_TYPES = [
    "AHTS", "PSV", "OSV", "DP2", "DP3", "Jack Up", "Jackup", "Heavy Lift",
    "Cable Lay", "Rock Installation", "Research Vessel",
    "Diving Support Vessel", "Pipe Lay", "Construction Vessel",
]
BULLET_PREFIXES = ("•", "-", "*", "☑", "✓", "‣", "·")


def slugify_tag(word: str) -> str:
    return "#" + re.sub(r"[^A-Za-z0-9]", "", word)


def find_first(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(rf"\b{re.escape(p)}\b", text, re.IGNORECASE):
            return p
    return None


def extract_email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    return m.group(0) if m else None


def extract_dates(text: str) -> str | None:
    patterns = [
        r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b",
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s*\d{0,4}\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def extract_rotation(text: str) -> str | None:
    m = re.search(
        r"\d+\s*(?:week|weeks|day|days)?\s*(?:on|ON)\s*/\s*\d+\s*(?:week|weeks|day|days)?\s*(?:off|OFF)",
        text,
    )
    if m:
        return m.group(0)
    m = re.search(r"\d+/\d+\s*rotation", text, re.IGNORECASE)
    return m.group(0) if m else None


def extract_requirements(lines: list[str]) -> list[str]:
    reqs = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(BULLET_PREFIXES):
            cleaned = stripped.lstrip("".join(BULLET_PREFIXES)).strip()
            if cleaned:
                reqs.append(cleaned)
    return reqs


def parse_vacancy(raw: str) -> dict:
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    full_text = raw

    position = lines[0] if lines else "Вакансия"
    vessel = find_first(VESSEL_TYPES, full_text)
    region = find_first(REGIONS, full_text)
    dates = extract_dates(full_text)
    rotation = extract_rotation(full_text)
    contact = extract_email(full_text)
    requirements = extract_requirements(lines)

    tags = []
    if region:
        tags.append(slugify_tag(region))
    if vessel:
        tags.append(slugify_tag(vessel))
    hashtags = " ".join(tags)

    return {
        "position": position, "vessel": vessel, "region": region,
        "dates": dates, "rotation": rotation, "contact": contact,
        "requirements": requirements, "hashtags": hashtags,
    }


def render_template(fields: dict) -> str:
    parts = [f"⚓ <b>{fields['position']}</b>", ""]
    parts.append(f"🚢 Судно: {fields['vessel'] or '—'}")
    parts.append(f"🌍 Регион: {fields['region'] or '—'}")
    parts.append(f"📅 Дата: {fields['dates'] or '—'}")
    parts.append(f"🔄 Ротация: {fields['rotation'] or '—'}")
    parts.append("")
    parts.append("✅ Требования:")
    reqs = fields["requirements"] if isinstance(fields["requirements"], list) else \
        [r for r in fields["requirements"].split("\n") if r]
    if reqs:
        for r in reqs:
            parts.append(f"• {r}")
    else:
        parts.append("—")
    parts.append("")
    parts.append(f"📩 Контакт: {fields['contact'] or '—'}")
    if fields.get("hashtags"):
        parts.append("")
        parts.append(fields["hashtags"])
    return "\n".join(parts)


def split_batch(text: str) -> list[str]:
    parts = re.split(r"\n\s*[-=]{3,}\s*\n", text)
    if len(parts) == 1:
        parts = re.split(r"\n{3,}", text)
    return [p.strip() for p in parts if p.strip()]


def dedup_key_for(fields: dict) -> str:
    return f"{fields['position'].strip().lower()}|{(fields['contact'] or '').strip().lower()}"


def apply_button_url(vacancy_id: int) -> str:
    # диплинк на самого бота — так можно посчитать клик перед тем, как отдать реальную ссылку
    return f"https://t.me/{BOT_USERNAME}?start=apply_{vacancy_id}"


def channel_keyboard(vacancy_id: int, post_link: str | None, title: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📩 Откликнуться", url=apply_button_url(vacancy_id))]]
    if post_link:
        share_url = "https://t.me/share/url?url=" + quote(post_link) + "&text=" + quote(title)
        rows.append([InlineKeyboardButton(text="↗️ Поделиться", url=share_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def draft_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать сейчас", callback_data=f"pub:{vacancy_id}"),
            InlineKeyboardButton(text="В очередь (дайджест)", callback_data=f"queue:{vacancy_id}"),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{vacancy_id}")],
    ])


def duplicate_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Всё равно опубликовать", callback_data=f"pub:{vacancy_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{vacancy_id}"),
    ]])


def admin_only(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
    fields["requirements"] = fields["requirements"]  # уже строка из БД, render_template это учитывает
    text = render_template(fields)

    sent = await bot.send_message(
        chat_id=CHANNEL_ID, text=text,
        reply_markup=channel_keyboard(vacancy_id, None, fields["position"]),
    )
    post_link = f"https://t.me/{CHANNEL_USERNAME}/{sent.message_id}"
    await bot.edit_message_reply_markup(
        chat_id=CHANNEL_ID, message_id=sent.message_id,
        reply_markup=channel_keyboard(vacancy_id, post_link, fields["position"]),
    )
    db.set_status(vacancy_id, "published", sent.message_id)


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    # обработка диплинка счётчика кликов: /start apply_<id>
    if command.args and command.args.startswith("apply_"):
        vacancy_id = int(command.args.replace("apply_", ""))
        db.increment_clicks(vacancy_id)
        await message.answer(
            "Переход на форму отклика:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть", url=APPLY_BOT_LINK)
            ]]),
        )
        return

    if not admin_only(message.from_user.id):
        await message.answer("Бот приватный.")
        return
    await message.answer(
        "Пришлите текст вакансии в свободной форме (или пачку через ---).\n"
        "Команды: /stats — сводка за неделю."
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not admin_only(message.from_user.id):
        return
    total, top = db.weekly_stats(7)
    lines = [f"За последние 7 дней опубликовано: {total}", "", "Топ по кликам:"]
    for row in top:
        lines.append(f"• {row['position']} — {row['clicks']} кликов")
    await message.answer("\n".join(lines) or "Пока нет данных.")


@router.message(F.text & ~F.text.startswith("/"))
async def handle_vacancy_text(message: Message):
    if not admin_only(message.from_user.id):
        return

    for raw in split_batch(message.text):
        fields = parse_vacancy(raw)
        key = dedup_key_for(fields)
        dup = db.find_recent_duplicate(key)

        vacancy_id = db.insert_vacancy(fields, render_template(fields), key)
        text = render_template(fields)

        if dup:
            warn = (
                f"⚠️ Похоже, такая вакансия уже публиковалась "
                f"{dup['created_at'][:10]} (id {dup['id']}).\n\n{text}"
            )
            await message.answer(warn, reply_markup=duplicate_keyboard(vacancy_id))
        else:
            await message.answer(
                text + "\n\n<i>Опубликовать сейчас или поставить в очередь дайджеста?</i>",
                reply_markup=draft_keyboard(vacancy_id),
            )


@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    await do_publish(callback.bot, vacancy_id)
    await callback.message.edit_text(callback.message.html_text + "\n\n✅ Опубликовано")
    await callback.answer("Опубликовано в канал")


@router.callback_query(F.data.startswith("queue:"))
async def cb_queue(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    slot = next_digest_slot()
    db.set_schedule(vacancy_id, slot.isoformat())
    await callback.message.edit_text(
        callback.message.html_text + f"\n\n🕒 В очереди, выйдет в {slot.strftime('%H:%M %d.%m')}"
    )
    await callback.answer("Добавлено в очередь")


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    db.set_status(vacancy_id, "cancelled")
    await callback.message.edit_text("Отменено")
    await callback.answer()


async def digest_worker(bot: Bot):
    while True:
        due = db.get_due_queue(datetime.now().isoformat())
        for row in due:
            await do_publish(bot, row["id"])
        await asyncio.sleep(60)


async def main():
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(digest_worker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
