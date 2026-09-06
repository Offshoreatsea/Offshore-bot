import asyncio
import io
import json
import os
import re
import time
from datetime import datetime, timedelta

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
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
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
SUBSCRIPTION_PRICE_STARS = int(os.getenv("SUBSCRIPTION_PRICE_STARS", "250"))
SUBSCRIPTION_DAYS = 7
MAX_POSITIONS = 2
SUBSCRIPTION_DAYS_MONTH = 30
SUBSCRIPTION_PRICE_STARS_MONTH = int(os.getenv("SUBSCRIPTION_PRICE_STARS_MONTH", "800"))  # вместо 4×250=1000 — скидка за месяц
REFERRAL_BONUS_DAYS = 3

DIGEST_TIMES = ["09:00", "14:00", "19:00"]

# Фиксированный список должностей — единственный источник правды для тегов
# #Position и для матчинга в /subscribe. Если встретится реально новая
# должность, которой тут нет, — просто допишите строку в список, ничего
# больше менять не нужно.
RANK_TAGS = [
    "Master", "ChiefOfficer", "SecondOfficer", "ThirdOfficer", "DeckCadet",
    "ChiefEngineer", "SecondEngineer", "ThirdEngineer", "FourthEngineer", "EngineCadet",
    "ETO", "Electrician", "Bosun", "AB", "OS", "Motorman", "Oiler", "Fitter",
    "Cook", "Steward", "Campboss", "ChiefSteward",
    "CraneOperator", "DPOperator", "ROVPilot", "Rigger", "Welder", "Scaffolder",
    "ClientRepresentative", "SafetyOfficer", "Surveyor",
]

# Группировка тегов по департаментам — чисто вопрос навигации в /subscribe,
# на матчинг вакансий и хранение подписок не влияет (там как был, так и
# остался плоский position_tag). Допишите сюда любую новую должность из
# RANK_TAGS в подходящий департамент, иначе она не попадёт ни в один экран.
DEPARTMENTS = {
    "Bridge": ["Master", "ChiefOfficer", "SecondOfficer", "ThirdOfficer", "DeckCadet"],
    "Engine": ["ChiefEngineer", "SecondEngineer", "ThirdEngineer", "FourthEngineer",
               "EngineCadet", "ETO", "Electrician"],
    "Ratings": ["Bosun", "AB", "OS", "Motorman", "Oiler", "Fitter"],
    "Catering": ["Cook", "Steward", "Campboss", "ChiefSteward"],
    "Offshore & Specialist": ["CraneOperator", "DPOperator", "ROVPilot", "Rigger",
                              "Welder", "Scaffolder", "ClientRepresentative",
                              "SafetyOfficer", "Surveyor"],
}

# Фиксированный список типов судов — тоже единый источник правды для тегов
# и матчинга.
VESSEL_TAGS = [
    "Tanker", "Container", "Bulk", "LNG", "LPG", "Chemical",
    "Offshore", "OSV", "CSV", "DSV", "MPV", "MPSV", "AHTS", "PSV", "SOV",
    "CableLayer", "Dredger", "Cruise", "RoRo", "Ferry", "Yacht", "FPSO", "JackUp",
]

FALLBACK_TAG = "Other"

# Переводы для кандидата — единственное место, где текст локализуется под язык,
# выбранный при первом /start. Сами теги должностей (RANK_TAGS) остаются
# английскими в любом языке — это канонические идентификаторы, а не текст.
TR = {
    "en": {
        "intro": "This bot sends you offshore & maritime job vacancies for the "
                  "position you choose — no need to scroll the channel.\n\n"
                  "⚠️ OffshoreAtSea is a vacancy aggregator only — we are not "
                  "the employer and are not responsible for working conditions "
                  "at the companies listed.",
        "choose_department": "Choose a department to see its positions:",
        "choose_position": "Choose one or more positions — tap to select, tap "
                            "again to remove. I'll send matching vacancies from "
                            "the last 7 days for each, then new ones as they're posted:",
        "subscribed": "✅ Added {tag}. Sending recent vacancies...",
        "unsubscribed": "Removed {tag} from your alerts.",
        "backfill_empty": "No {tag} vacancies in the last 7 days yet — "
                           "you'll get the next one as soon as it's posted.",
        "done": "✅ Done",
        "back_to_departments": "⬅ Departments",
        "no_selection": "You haven't picked any position yet — tap one above.",
        "subscribed_summary": "Your alerts are set up for: {tags}",
        "contact_admin": "🆘 Contact admin",
        "pay_intro": "Job alerts by position are a paid feature: {price} Stars "
                     "gets you 7 days of instant notifications for the positions "
                     "you choose, plus everything already posted in the last 7 days.",
        "pay_button": "⭐ Pay {price} Stars for 7 days",
        "pay_button_month": "⭐ Pay {price} Stars for 30 days (save vs weekly)",
        "pay_contact_admin": "💬 Can't pay with Stars? Message admin",
        "referral_bonus": "🎁 A friend you invited just paid — you got +{days} days, now active until {until}!",
        "invite_friend": "🎁 Invite a friend, get 3 free days",
        "referral_share_text": "Get job alerts by position on OffshoreAtSea 👇",
        "expiry_reminder": "⏳ Your job alerts subscription ends in less than 24 hours. Renew to keep getting instant notifications:",
        "pay_active_until": "✅ Your subscription is active until {until}.",
        "payment_thanks": "✅ Payment received — active until {until}. Now pick your positions:",
        "max_positions": "You can pick up to {max} positions. Remove one first to add another.",
        "positions_locked_notice": "Your positions are locked in for this period: {tags}. "
                                    "Contact admin if you need to change them.",
        "send_cv": "Send your CV to: {v}",
        "open_form": "Open the application form:",
        "how_to_apply": "How to apply: {v}",
        "no_contact": "No direct contact is listed for this vacancy. "
                       "Please contact the channel admin.",
    },
    "ru": {
        "intro": "Этот бот присылает вакансии в офшоре и морской индустрии по "
                 "выбранной должности — не нужно листать канал.\n\n"
                 "⚠️ OffshoreAtSea — только агрегатор вакансий, мы не являемся "
                 "работодателем и не несём ответственности за условия труда "
                 "у указанных компаний.",
        "choose_department": "Выберите департамент, чтобы увидеть должности:",
        "choose_position": "Выберите одну или несколько должностей — нажмите, "
                            "чтобы добавить, ещё раз — чтобы убрать. Пришлю вакансии "
                            "за последние 7 дней по каждой, и дальше — все новые:",
        "subscribed": "✅ Добавлено: {tag}. Отправляю вакансии...",
        "unsubscribed": "Убрано из подписки: {tag}.",
        "backfill_empty": "Вакансий по {tag} за последние 7 дней пока нет — "
                           "пришлю, как только появится подходящая.",
        "done": "✅ Готово",
        "back_to_departments": "⬅ Департаменты",
        "no_selection": "Вы ещё не выбрали ни одной должности — нажмите на любую выше.",
        "subscribed_summary": "Ваши подписки: {tags}",
        "contact_admin": "🆘 Написать администратору",
        "pay_intro": "Уведомления по должности — платная функция: {price} ⭐ "
                     "дают 7 дней мгновенных уведомлений по выбранным должностям, "
                     "плюс всё, что уже публиковалось за последние 7 дней.",
        "pay_button": "⭐ Оплатить {price} Stars за 7 дней",
        "pay_button_month": "⭐ Оплатить {price} Stars за 30 дней (выгоднее понедельной)",
        "pay_contact_admin": "💬 Не можете оплатить Stars? Написать администратору",
        "referral_bonus": "🎁 Приглашённый вами друг оплатил — вам +{days} дня, теперь активно до {until}!",
        "invite_friend": "🎁 Пригласить друга, получить 3 дня бесплатно",
        "referral_share_text": "Уведомления о вакансиях по должности в OffshoreAtSea 👇",
        "expiry_reminder": "⏳ Ваша подписка на уведомления заканчивается меньше чем через 24 часа. Продлите, чтобы не пропускать вакансии:",
        "pay_active_until": "✅ Подписка активна до {until}.",
        "payment_thanks": "✅ Оплата прошла — активно до {until}. Теперь выберите должности:",
        "max_positions": "Можно выбрать не больше {max} должностей. Сначала уберите одну, чтобы добавить другую.",
        "positions_locked_notice": "Ваши должности зафиксированы на этот период: {tags}. "
                                    "Если нужно изменить — напишите администратору.",
        "send_cv": "Отправьте резюме на: {v}",
        "open_form": "Откройте форму отклика:",
        "how_to_apply": "Как откликнуться: {v}",
        "no_contact": "Для этой вакансии не указан прямой контакт. "
                       "Напишите администратору канала.",
    },
    "uk": {
        "intro": "Цей бот надсилає вакансії в офшорі та морській галузі за "
                 "обраною посадою — не потрібно гортати канал.\n\n"
                 "⚠️ OffshoreAtSea — лише агрегатор вакансій, ми не є "
                 "роботодавцем і не несемо відповідальності за умови праці "
                 "у зазначених компаніях.",
        "choose_department": "Оберіть департамент, щоб побачити посади:",
        "choose_position": "Оберіть одну або кілька посад — натисніть, щоб додати, "
                            "ще раз — щоб прибрати. Надішлю вакансії за останні 7 днів "
                            "по кожній, а далі — всі нові:",
        "subscribed": "✅ Додано: {tag}. Надсилаю вакансії...",
        "unsubscribed": "Прибрано з підписки: {tag}.",
        "backfill_empty": "Вакансій по {tag} за останні 7 днів поки немає — "
                           "надішлю, щойно з'явиться відповідна.",
        "done": "✅ Готово",
        "back_to_departments": "⬅ Департаменти",
        "no_selection": "Ви ще не обрали жодної посади — натисніть на будь-яку вище.",
        "subscribed_summary": "Ваші підписки: {tags}",
        "contact_admin": "🆘 Написати адміністратору",
        "pay_intro": "Сповіщення за посадою — платна функція: {price} ⭐ дають "
                     "7 днів миттєвих сповіщень за обраними посадами, плюс усе, "
                     "що вже публікувалося за останні 7 днів.",
        "pay_button": "⭐ Оплатити {price} Stars за 7 днів",
        "pay_button_month": "⭐ Оплатити {price} Stars за 30 днів (вигідніше за тижневу)",
        "pay_contact_admin": "💬 Не можете оплатити Stars? Напишіть адміністратору",
        "referral_bonus": "🎁 Запрошений вами друг оплатив — вам +{days} дні, тепер активно до {until}!",
        "invite_friend": "🎁 Запросити друга, отримати 3 дні безкоштовно",
        "referral_share_text": "Сповіщення про вакансії за посадою в OffshoreAtSea 👇",
        "expiry_reminder": "⏳ Ваша підписка на сповіщення закінчується менш ніж за 24 години. Продовжте, щоб не пропускати вакансії:",
        "pay_active_until": "✅ Підписку активовано до {until}.",
        "payment_thanks": "✅ Оплату отримано — активно до {until}. Тепер оберіть посади:",
        "max_positions": "Можна обрати не більше {max} посад. Спочатку приберіть одну, щоб додати іншу.",
        "positions_locked_notice": "Ваші посади зафіксовано на цей період: {tags}. "
                                    "Якщо потрібно змінити — напишіть адміністратору.",
        "send_cv": "Надішліть резюме на: {v}",
        "open_form": "Відкрийте форму відгуку:",
        "how_to_apply": "Як відгукнутися: {v}",
        "no_contact": "Для цієї вакансії не вказано прямий контакт. "
                       "Напишіть адміністратору каналу.",
    },
}


def t(lang: str | None, key: str, **kwargs) -> str:
    lang = lang if lang in TR else "en"
    return TR[lang][key].format(**kwargs)

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
  match — treat abbreviations, informal titles, and near-synonyms as the same rank):
  {rank_tags}
  Common mappings to use as a guide (not exhaustive — apply the same logic to anything
  similar that isn't listed here):
    "Master", "Captain", "Skipper" -> Master
    "C/O", "Chief Officer", "Chief Mate", "First Mate", "1/O" -> ChiefOfficer
    "2/O", "2nd Officer", "Second Officer", "Second Mate", "SDPO" -> SecondOfficer
    "3/O", "3rd Officer", "Third Officer", "Third Mate" -> ThirdOfficer
    "Deck Cadet", "Deck Trainee", "Navigation Cadet" -> DeckCadet
    "C/E", "Chief Engineer" -> ChiefEngineer
    "2/E", "Second Engineer", "First Assistant Engineer" -> SecondEngineer
    "3/E", "Third Engineer", "Second Assistant Engineer" -> ThirdEngineer
    "4/E", "Fourth Engineer", "Third Assistant Engineer" -> FourthEngineer
    "Engine Cadet", "Engine Trainee", "Motor Cadet" -> EngineCadet
    "Junior ETO", "Electro-Technical Officer", "Electrical Officer", "Electrical"
    (as a job title, not a requirement) -> ETO
    "Ship's Electrician", "Electrical Rating" -> Electrician
    "Boatswain", "Bosun's Mate" -> Bosun
    "AB", "Able Seaman", "Able Bodied Seaman", "Deck Hand", "Deckhand" -> AB
    "OS", "Ordinary Seaman" -> OS
    "Motorman", "Engine Rating" -> Motorman
    "Oiler", "Wiper" -> Oiler
    "Fitter", "Engine Fitter" -> Fitter
    "Cook", "Ship's Cook", "Chief Cook", "Galley Cook" -> Cook
    "Steward", "Mess Man", "Messman" -> Steward
    "Camp Boss", "Campboss", "Catering Manager" -> Campboss
    "Chief Steward", "Chief Steward/ess" -> ChiefSteward
    "OOW" (Officer of the Watch) or a bare "Mate" with no rank number given is ambiguous
    between SecondOfficer and ThirdOfficer — infer from context (years of experience
    required, COC class, whether it's described as senior/junior watch); if there is truly
    no way to tell, default to SecondOfficer rather than Other.
  If truly nothing in the list or the guidance above fits, use "Other".
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


def channel_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎯 Get Matched Jobs", url=f"https://t.me/{BOT_USERNAME}?start=join"
        )
    ]])



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


# антиспам для кандидатских кнопок (subpos/lang/showpos/showlang/subdone) — не даёт
# накрутить рассылку себе или другим частыми повторными тапами. Парсинг вакансий
# через Claude API тут ни при чём — он и так admin_only (см. handle_vacancy_text),
# случайный человек не может обратиться к платному AI-парсингу вообще никак.
_last_candidate_action: dict[int, float] = {}


def throttled(tg_id: int, seconds: float = 0.6) -> bool:
    now = time.monotonic()
    last = _last_candidate_action.get(tg_id, 0)
    if now - last < seconds:
        return True
    _last_candidate_action[tg_id] = now
    return False


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
        reply_markup=channel_keyboard(vacancy_id),
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
        lang = db.get_subscriber_language(message.from_user.id)
        if email:
            # обычный текст с email — Telegram сам делает его кликабельным
            # (открывает почтовый клиент), в отличие от кнопки с mailto:
            await message.answer(t(lang, "send_cv", v=email))
        elif url:
            # у вакансии своя собственная ссылка для отклика (например разные
            # ссылки на каждую позицию в одном посте) — https, кнопка разрешена
            await message.answer(
                t(lang, "open_form"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Open", url=url)
                ]]),
            )
        elif contact.strip():
            # контакт есть, но это не email и не ссылка — например текстовая
            # инструкция ("напишите в личку @agency"). Показываем как есть,
            # вместо кнопки в никуда.
            await message.answer(t(lang, "how_to_apply", v=contact.strip()))
        else:
            # у вакансии вообще нет контакта в тексте — честно говорим об
            # этом, а не показываем кнопку "Open", ведущую в общий канал
            await message.answer(
                t(lang, "no_contact"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=t(lang, "contact_admin"), url=CONSULT_LINK)
                ]]),
            )
        return

    if admin_only(message.from_user.id):
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
            "/subscribers — сколько людей подписалось и разбивка по должностям\n"
            "/subscriberslist — полный список подписчиков (ник, должности, статус оплаты)\n"
            "/grant <@ник или id> [дней] — выдать доступ вручную, если оплатили не через Stars\n"
            "/refund <@ник или id> — вернуть последний неоплаченный возвратом платёж\n"
            "/revenue [дней] — доход в Stars за период (по умолчанию 7 дней)\n"
            "/blockuser <@ник или id> — заблокировать (бот перестанет отвечать)\n"
            "/unblockuser <@ник или id> — снять блокировку\n"
            f"/autopublish on|off — автопубликация без подтверждения (сейчас {mode})"
        )
        # email-дайджест за неделю — только тебе, никто другой это не увидит
        contacts = db.list_contacts_since(7)
        emails = sorted({extract_email(c) for c in contacts if extract_email(c)})
        if emails:
            await message.answer(
                f"📧 Email за последние 7 дней ({len(emails)}):\n\n" + "\n".join(emails)
            )
        return

    # любой другой человек (не админ, без apply_-диплинка) — это кандидат,
    # который либо перешёл по кнопке «🎯 Get Matched Jobs» из канала, либо
    # написал боту сам.
    tg_id = message.from_user.id
    if db.is_blocked(tg_id):
        return  # заблокированные админом (спам/злоупотребление) — бот просто молчит

    # диплинк-приглашение вида ?start=ref_123456789 — запоминаем, кто кого
    # привёл, чтобы начислить бонус пригласившему при первой оплате друга
    if command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args.replace("ref_", ""))
            if referrer_id != tg_id:
                db.set_referred_by(tg_id, referrer_id)
        except ValueError:
            pass

    # Онбординг начинается с выбора языка.
    await message.answer(
        "🇬🇧 English / 🇷🇺 Русский / 🇺🇦 Українська",
        reply_markup=language_keyboard(),
    )


def payment_keyboard(lang: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t(lang, "pay_button", price=SUBSCRIPTION_PRICE_STARS),
            callback_data=f"pay_sub:{SUBSCRIPTION_DAYS}:{SUBSCRIPTION_PRICE_STARS}",
        )],
        [InlineKeyboardButton(
            text=t(lang, "pay_button_month", price=SUBSCRIPTION_PRICE_STARS_MONTH),
            callback_data=f"pay_sub:{SUBSCRIPTION_DAYS_MONTH}:{SUBSCRIPTION_PRICE_STARS_MONTH}",
        )],
        [InlineKeyboardButton(text="🌐 Change language", callback_data="showlang")],
        [InlineKeyboardButton(text=t(lang, "pay_contact_admin"), url=CONSULT_LINK)],
    ])


async def show_department_or_paywall(target, tg_id: int, lang: str | None, edit: bool):
    """target — либо Message (обычный ответ), либо CallbackQuery.message (для
    edit_text). Показывает: экран оплаты (нет активной подписки), сводку
    без редактирования (должности уже зафиксированы на этот период), либо
    список департаментов для выбора (оплачено, но ещё не выбрано/разблокировано)."""
    if db.is_blocked(tg_id):
        return  # заблокированный — просто молчим, не даём вообще никакого экрана
    if not db.is_subscription_active(tg_id):
        text = t(lang, "pay_intro", price=SUBSCRIPTION_PRICE_STARS)
        markup = payment_keyboard(lang)
    elif db.is_positions_locked(tg_id):
        selected = db.get_subscriber_positions(tg_id)
        text = t(lang, "positions_locked_notice", tags=", ".join(selected))
        markup = after_subscribe_keyboard(lang, tg_id)
    else:
        selected = set(db.get_subscriber_positions(tg_id))
        text, markup = t(lang, "choose_department"), department_keyboard(lang, selected)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("pay_sub:"))
async def cb_pay_subscription(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    if db.is_blocked(tg_id):
        await callback.answer()
        return
    _, days_str, price_str = callback.data.split(":")
    days, price = int(days_str), int(price_str)
    await callback.bot.send_invoice(
        chat_id=tg_id,
        title=f"OffshoreAtSea — Job Alerts ({days} days)",
        description=f"Instant vacancy alerts for the positions you choose, "
                     f"{days} days of access.",
        payload=f"subscription_{tg_id}_{days}_{price}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Job Alerts — {days} days", amount=price)],
        provider_token="",  # для Stars (XTR) provider_token не нужен
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    # обязательно ответить в течение ~10 секунд, иначе Telegram отменит платёж
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    tg_id = message.from_user.id
    sp = message.successful_payment
    # payload несёт реальные дни/цену конкретного тарифа — не полагаемся на
    # константы по умолчанию, человек мог оплатить недельный или месячный
    try:
        _, _, days_str, price_str = sp.invoice_payload.split("_")
        days, price = int(days_str), int(price_str)
    except (ValueError, AttributeError):
        days, price = SUBSCRIPTION_DAYS, SUBSCRIPTION_PRICE_STARS

    is_first_payment = db.count_payments(tg_id) == 0
    db.insert_payment(tg_id, price, days, sp.telegram_payment_charge_id)
    new_until = db.extend_subscription(tg_id, days)
    db.unlock_positions(tg_id)  # новый оплаченный период — можно скорректировать выбор
    lang = db.get_subscriber_language(tg_id)
    until_str = datetime.fromisoformat(new_until).strftime("%d.%m.%Y")
    selected = set(db.get_subscriber_positions(tg_id))
    await message.answer(
        t(lang, "payment_thanks", until=until_str),
        reply_markup=department_keyboard(lang, selected),
    )

    # реферальный бонус — только за самую первую оплату приглашённого,
    # чтобы не начислять его повторно за каждое продление
    if is_first_payment:
        referrer_id = db.get_referrer(tg_id)
        if referrer_id:
            ref_until = db.extend_subscription(referrer_id, REFERRAL_BONUS_DAYS)
            ref_lang = db.get_subscriber_language(referrer_id)
            ref_until_str = datetime.fromisoformat(ref_until).strftime("%d.%m.%Y")
            try:
                await message.bot.send_message(
                    referrer_id, t(ref_lang, "referral_bonus", days=REFERRAL_BONUS_DAYS, until=ref_until_str)
                )
            except TelegramAPIError:
                pass

    # уведомление тебе в реальном времени о каждой оплате — без захода в
    # /subscriberslist руками
    username = f"@{message.from_user.username}" if message.from_user.username else f"id{tg_id}"
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id,
                f"💰 Оплата: {username} — {price}⭐ за {days} дней, активно до {until_str}",
            )
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "showlang")
async def cb_show_language(callback: CallbackQuery):
    if throttled(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "🇬🇧 English / 🇷🇺 Русский / 🇺🇦 Українська",
        reply_markup=language_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "showpos")
async def cb_show_positions(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    lang = db.get_subscriber_language(tg_id)
    await show_department_or_paywall(callback.message, tg_id, lang, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("subdept:"))
async def cb_show_department(callback: CallbackQuery):
    dept = callback.data.split(":", 1)[1]
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    lang = db.get_subscriber_language(tg_id)
    if db.is_positions_locked(tg_id) or not db.is_subscription_active(tg_id):
        await show_department_or_paywall(callback.message, tg_id, lang, edit=True)
        await callback.answer()
        return
    selected = set(db.get_subscriber_positions(tg_id))
    await callback.message.edit_text(
        t(lang, "choose_position"), reply_markup=subscribe_keyboard(dept, lang, selected)
    )
    await callback.answer()


@router.callback_query(F.data == "subdeptback")
async def cb_department_back(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    lang = db.get_subscriber_language(tg_id)
    selected = set(db.get_subscriber_positions(tg_id))
    await callback.message.edit_text(
        t(lang, "choose_department"), reply_markup=department_keyboard(lang, selected)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_language(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    lang = callback.data.split(":", 1)[1]
    db.upsert_subscriber(tg_id, callback.from_user.username, language=lang)
    await callback.message.edit_text(t(lang, "intro"))
    await show_department_or_paywall(callback.message, tg_id, lang, edit=False)
    await callback.answer()


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


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang:uk"),
    ]])


def department_keyboard(lang: str | None = None, selected: set[str] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    rows = []
    row = []
    for dept, tags in DEPARTMENTS.items():
        count = len(selected & set(tags))
        label = f"{dept} ({count})" if count else dept
        row.append(InlineKeyboardButton(text=label, callback_data=f"subdept:{dept}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(lang, "done"), callback_data="subdone")])
    rows.append([InlineKeyboardButton(text="🌐 Change language", callback_data="showlang")])
    rows.append([InlineKeyboardButton(text=t(lang, "contact_admin"), url=CONSULT_LINK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscribe_keyboard(dept: str, lang: str | None = None, selected: set[str] | None = None) -> InlineKeyboardMarkup:
    # клавиатура должностей ВНУТРИ одного департамента — dept закодирован в
    # callback_data (subpos:<dept>:<tag>), чтобы toggle-хендлер знал, какой
    # именно экран перерисовывать после нажатия
    selected = selected or set()
    rows = []
    row = []
    for tag in DEPARTMENTS[dept]:
        label = f"✅ {tag}" if tag in selected else tag
        row.append(InlineKeyboardButton(text=label, callback_data=f"subpos:{dept}:{tag}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(lang, "back_to_departments"), callback_data="subdeptback")])
    rows.append([InlineKeyboardButton(text=t(lang, "done"), callback_data="subdone")])
    rows.append([InlineKeyboardButton(text="🌐 Change language", callback_data="showlang")])
    rows.append([InlineKeyboardButton(text=t(lang, "contact_admin"), url=CONSULT_LINK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_subscribe_keyboard(lang: str | None = None, tg_id: int | None = None) -> InlineKeyboardMarkup:
    # должности зафиксированы после "Готово" — кнопки на их смену больше нет,
    # это осознанное решение (см. cb_subscribe_done); язык менять можно всегда
    rows = []
    if tg_id:
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{tg_id}"
        share_url = f"https://t.me/share/url?url={ref_link}&text=" + t(lang, "referral_share_text")
        rows.append([InlineKeyboardButton(text=t(lang, "invite_friend"), url=share_url)])
    rows.append([InlineKeyboardButton(text="🌐 Change language", callback_data="showlang")])
    rows.append([InlineKeyboardButton(text=t(lang, "contact_admin"), url=CONSULT_LINK)])
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


@router.message(Command("subscribers"))
async def cmd_subscribers(message: Message):
    if not admin_only(message.from_user.id):
        return
    total, by_tag = db.subscriber_stats()
    lines = [f"👥 Подписчиков на /subscribe: {total}"]
    if by_tag:
        lines.append("")
        lines.append("По должностям:")
        lines += [f"• {row['tag']} — {row['c']}" for row in by_tag]
    await message.answer("\n".join(lines))


@router.message(Command("subscriberslist"))
async def cmd_subscribers_list(message: Message):
    if not admin_only(message.from_user.id):
        return
    people = db.get_subscribers_list()
    if not people:
        await message.answer("Пока никто не подписался.")
        return
    now = datetime.now()
    lines = [f"👥 Подписчики ({len(people)}):\n"]
    for p in people:
        handle = f"@{p['username']}" if p['username'] else f"id{p['tg_id']}"
        positions = ", ".join(p["positions"]) if p["positions"] else "—"
        if p["subscription_until"] and datetime.fromisoformat(p["subscription_until"]) > now:
            until = datetime.fromisoformat(p["subscription_until"]).strftime("%d.%m.%Y")
            status = f"✅ до {until}"
        else:
            status = "❌ не оплачена"
        lines.append(f"{handle} — {positions} — {status}")
    # телеграм режет сообщения длиннее ~4096 символов — режем на части сами,
    # чтобы длинный список не падал с ошибкой на большой базе подписчиков
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: /grant <@username или id> [дней, по умолчанию 7]")
        return
    handle = args[0]
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else SUBSCRIPTION_DAYS
    row = db.find_subscriber_by_handle(handle)
    if not row:
        await message.answer(
            f"Не нашёл {handle} в базе — человек должен хотя бы раз написать боту /start, "
            f"прежде чем выдать ему доступ вручную."
        )
        return
    tg_id = row["tg_id"]
    new_until = db.extend_subscription(tg_id, days)
    db.unlock_positions(tg_id)
    until_str = datetime.fromisoformat(new_until).strftime("%d.%m.%Y")
    await message.answer(f"✅ Выдал доступ на {days} дней. Активно до {until_str}.")
    lang = db.get_subscriber_language(tg_id)
    try:
        await message.bot.send_message(
            tg_id, t(lang, "payment_thanks", until=until_str),
            reply_markup=department_keyboard(lang, set(db.get_subscriber_positions(tg_id))),
        )
    except TelegramAPIError:
        pass


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    handle = (command.args or "").strip()
    if not handle:
        await message.answer("Использование: /refund <@username или id>")
        return
    row = db.find_subscriber_by_handle(handle)
    if not row:
        await message.answer(f"Не нашёл {handle} в базе подписчиков.")
        return
    payment = db.get_last_unrefunded_payment(row["tg_id"])
    if not payment:
        await message.answer(f"У {handle} нет неоплаченных возвратом платежей.")
        return
    try:
        await message.bot.refund_star_payment(
            user_id=row["tg_id"], telegram_payment_charge_id=payment["charge_id"]
        )
    except TelegramAPIError as e:
        await message.answer(f"❌ Не удалось вернуть: {e}")
        return
    db.mark_payment_refunded(payment["id"])
    await message.answer(f"✅ Возвращено {payment['amount_stars']}⭐ пользователю {handle}.")


@router.message(Command("revenue"))
async def cmd_revenue(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    arg = (command.args or "").strip()
    days = int(arg) if arg.isdigit() else 7
    total, count = db.revenue_since(days)
    label = "неделю" if days == 7 else f"{days} дней"
    await message.answer(f"💰 Доход за последние {label}: {total}⭐ ({count} оплат)")


@router.message(Command("blockuser"))
async def cmd_block_user(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    handle = (command.args or "").strip()
    if not handle:
        await message.answer("Использование: /blockuser <@username или id>")
        return
    row = db.find_subscriber_by_handle(handle)
    if not row:
        await message.answer(f"Не нашёл {handle} в базе подписчиков.")
        return
    db.set_blocked(row["tg_id"], True)
    await message.answer(f"🚫 {handle} заблокирован — бот больше не будет ему отвечать.")


@router.message(Command("unblockuser"))
async def cmd_unblock_user(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    handle = (command.args or "").strip()
    if not handle:
        await message.answer("Использование: /unblockuser <@username или id>")
        return
    row = db.find_subscriber_by_handle(handle)
    if not row:
        await message.answer(f"Не нашёл {handle} в базе подписчиков.")
        return
    db.set_blocked(row["tg_id"], False)
    await message.answer(f"✅ {handle} разблокирован.")


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    # доступно всем, не только админу — это функция для кандидатов, не для
    # управления каналом
    tg_id = message.from_user.id
    lang = db.get_subscriber_language(tg_id)
    await show_department_or_paywall(message, tg_id, lang, edit=False)


@router.callback_query(F.data.startswith("subpos:"))
async def cb_subscribe_position(callback: CallbackQuery):
    _, dept, position_tag = callback.data.split(":", 2)
    tg_id = callback.from_user.id
    if throttled(tg_id, seconds=1.0):
        await callback.answer()
        return
    lang = db.get_subscriber_language(tg_id)
    if not db.is_subscription_active(tg_id):
        await show_department_or_paywall(callback.message, tg_id, lang, edit=True)
        await callback.answer()
        return
    if db.is_positions_locked(tg_id):
        await show_department_or_paywall(callback.message, tg_id, lang, edit=True)
        await callback.answer()
        return

    current = db.get_subscriber_positions(tg_id)
    if position_tag not in current and len(current) >= MAX_POSITIONS:
        await callback.answer(t(lang, "max_positions", max=MAX_POSITIONS), show_alert=True)
        return

    added = db.toggle_subscription(tg_id, position_tag)

    # обновляем только галочки на клавиатуре этого же департамента — текст-
    # приглашение ("выберите должности...") остаётся тем же на протяжении
    # всего мульти-выбора
    selected = set(db.get_subscriber_positions(tg_id))
    try:
        await callback.message.edit_reply_markup(reply_markup=subscribe_keyboard(dept, lang, selected))
    except TelegramAPIError:
        pass  # клавиатура уже в нужном состоянии — Telegram иногда так отвечает, это не ошибка

    if not added:
        await callback.answer(t(lang, "unsubscribed", tag=position_tag))
        return

    await callback.answer(t(lang, "subscribed", tag=position_tag))
    backfill = db.get_recent_published_by_tag(position_tag, days=7)
    if not backfill:
        await callback.bot.send_message(tg_id, t(lang, "backfill_empty", tag=position_tag))
        return
    for row in backfill:
        fields = dict(row)
        try:
            await callback.bot.send_message(
                tg_id, render_template(fields),
                reply_markup=channel_keyboard(row["id"]),
            )
            await asyncio.sleep(0.3)  # не спамим Telegram API пачкой без пауз
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "subdone")
async def cb_subscribe_done(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if throttled(tg_id):
        await callback.answer()
        return
    lang = db.get_subscriber_language(tg_id)
    selected = db.get_subscriber_positions(tg_id)
    if not selected:
        await callback.answer(t(lang, "no_selection"), show_alert=True)
        return
    db.lock_positions(tg_id)  # с этого момента выбор нельзя изменить до следующей оплаты
    await callback.message.edit_text(
        t(lang, "subscribed_summary", tags=", ".join(selected)),
        reply_markup=after_subscribe_keyboard(lang, tg_id),
    )
    await callback.answer()


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
                reply_markup=channel_keyboard(vacancy_id),
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


async def subscription_reminder_worker(bot: Bot):
    # проверяем раз в час — часто чаще и не нужно, окно напоминания 24ч
    while True:
        expiring = db.get_expiring_subscribers(within_hours=24)
        for row in expiring:
            lang = row["language"]
            try:
                await bot.send_message(
                    row["tg_id"],
                    t(lang, "expiry_reminder"),
                    reply_markup=payment_keyboard(lang),
                )
                db.mark_reminder_sent(row["tg_id"], row["subscription_until"])
            except TelegramAPIError:
                pass
        await asyncio.sleep(3600)


async def main():
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(digest_worker(bot))
    asyncio.create_task(subscription_reminder_worker(bot))

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
