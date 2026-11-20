"""Сбор вакансий из LinkedIn: поиск через Apify, оценка поста целиком,
чтение вакансий с картинок. Здесь только чистая логика без Telegram —
публикация, модерация и админ-раздел живут в main.py."""

import base64
import json
import os
import re

import httpx

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
# Актор Apify для поиска постов LinkedIn. Формат id: "автор~название".
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "harvestapi~linkedin-post-search")
# Если у выбранного актора другие имена входных полей — задайте JSON в этой
# переменной, он будет добавлен к входным данным (например {"sortBy":"date"}).
APIFY_INPUT_EXTRA = os.getenv("APIFY_INPUT_EXTRA", "")
LINKEDIN_INTERVAL_HOURS = float(os.getenv("LINKEDIN_INTERVAL_HOURS", "4"))
LINKEDIN_PHRASES_PER_RUN = int(os.getenv("LINKEDIN_PHRASES_PER_RUN", "10"))
LINKEDIN_POSTS_PER_PHRASE = int(os.getenv("LINKEDIN_POSTS_PER_PHRASE", "5"))

# --- Поисковые фразы: должности + то, как крюинги реально пишут -----------
POSITION_PHRASES = [
    "hiring Master offshore", "hiring Chief Officer offshore", "hiring 2nd Officer offshore",
    "hiring 3rd Officer offshore", "hiring Chief Engineer offshore", "hiring 2nd Engineer offshore",
    "hiring 3rd Engineer offshore", "hiring ETO offshore", "hiring Electrician offshore vessel",
    "hiring Able Seaman AB offshore", "hiring Bosun offshore", "hiring Deck Cadet offshore",
    "hiring Crane Operator offshore", "hiring Rigger offshore", "hiring Motorman offshore",
    "hiring Oiler offshore", "hiring Cook offshore vessel", "hiring Steward offshore vessel",
    "hiring DPO dynamic positioning operator", "hiring ROV pilot technician",
    "hiring Chief Cook offshore", "hiring Fitter offshore vessel", "hiring OS ordinary seaman",
    "vacancy AHTS crew", "vacancy PSV crew", "vacancy jack-up rig crew", "vacancy DSV crew",
    "vacancy SOV CSOV crew", "vacancy FPSO crew", "vacancy cable layer crew",
    "vacancy pipelay vessel crew", "vacancy tug crew", "vacancy dredger crew",
    "vacancy wind farm vessel crew", "vacancy DP2 vessel", "vacancy DP3 vessel",
    "vacancy offshore construction vessel", "vacancy survey vessel crew",
    "vacancy anchor handling tug", "vacancy platform supply vessel",
    "vacancy accommodation vessel crew",
]
CREWING_PHRASES = [
    "we are hiring C/O 2/O offshore", "we are hiring 2/E 3/E offshore", "we are hiring offshore crew",
    "join our crew offshore", "rotation 28/28 offshore", "crewing agency hiring offshore",
    "urgent offshore vacancy joining", "send CV offshore vessel crew", "CE 2E 3E needed offshore",
    "seafarers wanted offshore vessel",
]
ALL_PHRASES = POSITION_PHRASES + CREWING_PHRASES  # 51
HASHTAG_PHRASES = ["#offshorejobs", "#seafarerjobs"]  # входят в общий список, но их 2 из 53
ALL_PHRASES = ALL_PHRASES + HASHTAG_PHRASES

# --- Признаки найма (8 групп) ----------------------------------------------
HIRING_GROUPS = {
    "ищем": r"\b(we(?:'re| are)\s+(?:hiring|looking|recruiting|seeking)|(?:now\s+)?hiring|looking for|"
            r"is\s+required|are\s+required|wanted|vacanc(?:y|ies)|recruiting|seeking|in need of|"
            r"ищем|набираем|требуется|требуются|вакансия|вакансии)\b",
    "срочно": r"\b(urgent(?:ly)?|asap|immediate(?:ly)?|срочно)\b",
    "дата": r"\b(joining|join date|sign[- ]?on|start date|embark(?:ation)?|crew change|"
            r"посадка|на судно)\b|\bETD\b",
    "ротация": r"\b\d{1,2}\s?/\s?\d{1,2}\s*(?:rotation|weeks|days|on/off)?\b(?=.{0,30}(?:rotation|on|off|days|weeks))|"
              r"\brotation\b|\bday ?rate\b|\$\s?\d{2,4}\s?/\s?day|\busd\s?\d+\s?/\s?day\b|\bставка\b",
    "резюме": r"\b(send (?:your |me |us )?(?:cv|resume)|dm me|pm me|inbox me|apply (?:via|at|to|now)|"
              r"cv to|resume to|пришлите (?:cv|резюме)|в лс|в личку)\b",
    "позиция": r"\b(open position|open positions|positions? available|openings?|vacancies)\b",
    "почта": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "экипаж": r"\b(join our crew|join our team|crewing agency|manning agency|crew manager)\b",
}
HIRING_RE = {k: re.compile(v, re.I | re.S) for k, v in HIRING_GROUPS.items()}

# --- Должности (слова — без учёта регистра, сокращения — с учётом) ---------
POSITION_WORDS = [
    r"master", r"captain", r"chief officer", r"first officer", r"(?:second|2nd) officer",
    r"(?:third|3rd) officer", r"chief engineer", r"(?:second|2nd) engineer", r"(?:third|3rd) engineer",
    r"(?:fourth|4th) engineer", r"electro[- ]?technical officer", r"electrician", r"able seaman",
    r"ordinary seaman", r"bosun", r"boatswain", r"deck cadet", r"engine cadet", r"crane operator",
    r"rigger", r"motorman", r"oiler", r"fitter", r"cook", r"steward", r"dynamic positioning operator",
    r"ROV (?:pilot|technician|supervisor)", r"able[- ]bodied", r"deck officer", r"mate",
    r"капитан", r"старпом", r"второй помощник", r"третий помощник", r"старший механик",
    r"второй механик", r"третий механик", r"электромеханик", r"боцман", r"матрос", r"моторист", r"повар",
]
POSITION_ABBR = [
    r"C/O", r"1/O", r"2/O", r"3/O", r"C/E", r"2/E", r"3/E", r"4/E", r"CE", r"2E", r"3E",
    r"AB", r"OS", r"ETO", r"DPO", r"OIM", r"2O", r"3O", r"CO",
]
POS_WORD_RE = re.compile(r"(?<![\w/])(" + "|".join(POSITION_WORDS) + r")(?![\w/])", re.I)
POS_ABBR_RE = re.compile(r"(?<![\w/])(" + "|".join(POSITION_ABBR) + r")(?![\w/])")
# «опыт работы 2/O», «min 2 years as Master» — это требование, а не позиция
POS_EXCLUDE_BEFORE = re.compile(
    r"(experience|exp\.?|worked|working|served|serving|years?|yrs|as a|previous|former|"
    r"опыт(?: работы)?|стаж)\s*(?:as|in|на|в)?\s*(?:an?\s+)?$", re.I)
POS_LABEL_RE = re.compile(r"(position|rank|role|vacancy|post|должность)\s*[:\-–]", re.I)

# --- Оффшор ---------------------------------------------------------------
OFFSHORE_RE = re.compile(
    r"\b(AHTS|AHT|PSV|OSV|DP[123]?|jack[- ]?up|rig|SOV|CSOV|WTIV|FPSO|FSO|FLNG|subsea|ROV|"
    r"offshore|wind ?farm|platform supply|supply vessel|anchor handling|pipe ?lay(?:er|ing)?|"
    r"cable ?lay(?:er|ing)?|DSV|MPSV|MPV|CSV|dredger|accommodation vessel|drill(?:ing)? (?:ship|rig)|"
    r"standby vessel|ERRV|survey vessel|seismic|tug)\b", re.I)

# --- Шум -------------------------------------------------------------------
SEEKER_RE = re.compile(
    r"(#opentowork|open to work|looking for (?:a )?(?:new )?(?:job|contract|opportunit|position|vessel)|"
    r"seeking (?:a )?(?:new )?(?:job|contract|opportunit|position)|available for (?:a )?contract|"
    r"i am (?:a )?(?:seafarer|master|officer|engineer)\b.{0,60}(?:looking|seeking|available)|"
    r"ищу (?:работу|контракт|судно)|ищу работу)", re.I | re.S)
NEWS_RE = re.compile(
    r"(proud to announce|pleased to announce|congratulations|congrats|happy birthday|happy new year|"
    r"merry christmas|delivered|delivery of|christening|naming ceremony|keel laying|launched|"
    r"awarded (?:a )?contract|wins contract|new build|press release|поздравля)", re.I)


def find_positions(text: str) -> list[str]:
    """Должности, на которые именно набирают: рядом есть признак найма или
    метка «Position:», и перед упоминанием нет «опыт работы …»."""
    found: list[str] = []
    for rx in (POS_WORD_RE, POS_ABBR_RE):
        for m in rx.finditer(text):
            before = text[max(0, m.start() - 40):m.start()]
            if POS_EXCLUDE_BEFORE.search(before):
                continue
            window = text[max(0, m.start() - 120):m.end() + 120]
            labelled = bool(POS_LABEL_RE.search(text[max(0, m.start() - 25):m.start()]))
            hiring_near = any(r.search(window) for k, r in HIRING_RE.items() if k != "почта")
            if labelled or hiring_near:
                name = m.group(1)
                if name not in found:
                    found.append(name)
    return found


# --- Почта: только корпоративная, без бесплатных ящиков и без ссылок ------
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FREE_MAIL_RE = re.compile(
    r"^(?:gmail|googlemail|yahoo|ymail|rocketmail|hotmail|outlook|live|msn|icloud|me|mac|aol|"
    r"mail|bk|list|inbox|yandex|ya|rambler|gmx|web|proton|protonmail|pm|tutanota|tuta|"
    r"ukr|i|meta|bigmir|qq|163|126|sina|wp|o2|seznam|t-online|libero|orange|free|"
    r"yahoo\.co|btinternet|sky|virginmedia|comcast|verizon|att|sbcglobal|zoho)"
    r"(?:\.[a-z]{2,})+$", re.I)


def corporate_emails(text: str) -> list[str]:
    out: list[str] = []
    for m in EMAIL_RE.findall(text or ""):
        email = m.strip(".,;:()<>[]").lower()
        domain = email.rsplit("@", 1)[-1]
        if FREE_MAIL_RE.match(domain) or re.search(r"\.(png|jpe?g|gif|webp)$", domain):
            continue
        if email not in out:
            out.append(email)
    return out


URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|co|me|ly|link|app)/\S*", re.I)


def strip_urls(value):
    """Убирает ссылки из текстового поля (или из каждой строки списка)."""
    if isinstance(value, str):
        return re.sub(r"\s{2,}", " ", URL_RE.sub("", value)).strip()
    if isinstance(value, list):
        return [x for x in (strip_urls(v) for v in value) if x]
    return value


def hiring_signals(text: str) -> list[str]:
    return [k for k, r in HIRING_RE.items() if r.search(text)]


def evaluate_post(text: str, has_image: bool = False) -> tuple[bool, str, dict]:
    """(пропускаем?, причина, признаки). Причины отсева:
    seeker — моряк ищет работу, news — новость/поздравление,
    not_offshore — нет ни оффшора, ни подходящей связки, weak — мало признаков;
    need_image — текст почти пустой, но есть картинка: нужно читать картинку."""
    text = text or ""
    signals = hiring_signals(text)
    positions = find_positions(text)
    offshore = bool(OFFSHORE_RE.search(text))
    info = {"signals": signals, "positions": positions, "offshore": offshore}

    if SEEKER_RE.search(text) and not (len(signals) >= 3 and positions):
        return False, "seeker", info
    if NEWS_RE.search(text) and len(signals) < 2:
        return False, "news", info

    if positions and signals:
        return True, "ok", info
    if positions and offshore:
        return True, "ok", info
    if offshore and len(signals) >= 2:
        return True, "ok", info

    if has_image and signals and len(text) < 400:
        return False, "need_image", info
    if positions or signals:
        return False, ("weak" if offshore or len(signals) else "not_offshore"), info
    return False, "not_offshore", info


# --- Apify -----------------------------------------------------------------
def _pick(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def normalize_post(item: dict) -> dict | None:
    """Приводит ответ актора к единому виду. Названия полей у разных
    акторов отличаются, поэтому перебираем популярные варианты."""
    url = _pick(item, "linkedinUrl", "postUrl", "url", "link")
    text = _pick(item, "content", "text", "postText", "commentary", default="") or ""
    if isinstance(text, dict):
        text = text.get("text", "")
    if not url:
        return None
    images: list[str] = []
    raw_imgs = _pick(item, "postImages", "images", "imageUrls", "image", default=[]) or []
    if isinstance(raw_imgs, (str, dict)):
        raw_imgs = [raw_imgs]
    for im in raw_imgs:
        if isinstance(im, dict):
            im = _pick(im, "url", "src", "imageUrl")
        if isinstance(im, str) and im.startswith("http"):
            images.append(im)
    author = item.get("author") or {}
    author_name = _pick(author, "name", "fullName", default="") if isinstance(author, dict) else str(author)
    return {"url": url.split("?")[0], "text": text.strip(), "images": images, "author": author_name}


APIFY_TASK_ID = os.getenv("APIFY_TASK_ID")  # если задан — бот запускает сохранённую задачу Apify
LINKEDIN_POSTED_LIMIT = os.getenv("LINKEDIN_POSTED_LIMIT", "24h")


async def apify_search(phrases: list[str]) -> list[dict]:
    """Запускает актор (или сохранённую задачу, если задан APIFY_TASK_ID),
    ждёт окончания и забирает посты. Без ограничения в 5 минут: запуск
    асинхронный, статус проверяется раз в 10 секунд до 30 минут."""
    import asyncio
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN не задан в Variables")
    if APIFY_TASK_ID:
        start_url = f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK_ID}/runs"
        payload = {"postedLimit": LINKEDIN_POSTED_LIMIT}  # остальное берётся из задачи
    else:
        start_url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs"
        payload = {
            "searchQueries": phrases,
            "maxPosts": LINKEDIN_POSTS_PER_PHRASE,
            "postedLimit": LINKEDIN_POSTED_LIMIT,
        }
        if APIFY_INPUT_EXTRA:
            try:
                payload.update(json.loads(APIFY_INPUT_EXTRA))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"APIFY_INPUT_EXTRA — некорректный JSON: {e}")
    params = {"token": APIFY_TOKEN}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(start_url, params=params, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Apify вернул {resp.status_code}: {resp.text[:300]}")
        run = resp.json()["data"]
        run_id, dataset_id = run["id"], run["defaultDatasetId"]
        status = run.get("status")
        for _ in range(180):  # до 30 минут
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break
            await asyncio.sleep(10)
            st = await client.get(f"https://api.apify.com/v2/actor-runs/{run_id}", params=params)
            status = st.json()["data"]["status"]
        if status != "SUCCEEDED":
            raise RuntimeError(f"Запуск Apify завершился со статусом {status}")
        items_resp = await client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={**params, "clean": "true", "limit": 5000},
        )
    if items_resp.status_code >= 300:
        raise RuntimeError(f"Не удалось получить результаты: {items_resp.status_code}")
    items = items_resp.json()
    posts = []
    for it in items if isinstance(items, list) else []:
        p = normalize_post(it)
        if p:
            posts.append(p)
    return posts


# --- Картинки --------------------------------------------------------------
_SKIP_IMG = re.compile(r"(profile-displayphoto|company-logo|profile-framedphoto|logo)", re.I)

IMAGE_PROMPT = (
    "This image is attached to a LinkedIn post. If it is a job vacancy / crew requirement "
    "(positions, vessel, dates, salary, contacts), transcribe ALL its text faithfully as plain text. "
    "If it is a logo, avatar, portrait, photo of a ship or anything that is not a vacancy, reply with exactly NONE."
)


async def read_vacancy_image(claude_client, image_urls: list[str]) -> str | None:
    """Скачивает первую подходящую картинку и просит Claude прочитать вакансию.
    Аватарки и логотипы пропускаем. None — если вакансии на картинке нет."""
    import asyncio
    for url in image_urls[:2]:
        if _SKIP_IMG.search(url):
            continue
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code != 200 or len(r.content) < 8000 or len(r.content) > 4_500_000:
                continue
            ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
            if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                ctype = "image/jpeg"
            b64 = base64.b64encode(r.content).decode()

            def _call():
                return claude_client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=1500,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": ctype, "data": b64}},
                        {"type": "text", "text": IMAGE_PROMPT},
                    ]}],
                )
            resp = await asyncio.to_thread(_call)
            out = resp.content[0].text.strip()
            if out and out.upper() != "NONE":
                return out
        except Exception as e:
            print(f"[linkedin] чтение картинки не удалось: {type(e).__name__}: {e}")
    return None
