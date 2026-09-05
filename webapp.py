"""
Веб-сервер для Telegram Mini App: отдаёт статическую страницу поиска вакансий
и API для неё (список вакансий с фильтрами, приём откликов). Работает в том же
процессе, что и бот, параллельно с обычным long-polling — см. run_web_server()
в main.py.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import db

STATIC_DIR = Path(__file__).parent / "static"


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData (см. документацию Telegram:
    Validating data received via the Mini App). Возвращает распарсенные поля
    (включая 'user' с данными кандидата), либо None, если подпись не сошлась —
    значит запрос пришёл не из настоящего Telegram-клиента."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    return parsed


def vacancy_to_dict(row) -> dict:
    def as_list(text):
        return [l for l in (text or "").split("\n") if l.strip()]

    return {
        "id": row["id"],
        "position": row["position"],
        "vessel": row["vessel"],
        "region": row["region"],
        "nationality": row["nationality"],
        "date": row["dates"],
        "duration": row["duration"],
        "rotation": row["rotation"],
        "salary": row["salary"],
        "documents": as_list(row["documents"]),
        "requirements": as_list(row["requirements"]),
        "contact": row["contact"],
        "hashtags": row["hashtags"],
    }


async def handle_filters(request: web.Request) -> web.Response:
    return web.json_response({"regions": db.distinct_regions()})


async def handle_vacancies(request: web.Request) -> web.Response:
    region = request.query.get("region", "").strip()
    q = request.query.get("q", "").strip()
    rows = db.search_published_vacancies(region=region, q=q, limit=100)
    return web.json_response([vacancy_to_dict(r) for r in rows])


def create_app(bot, bot_token: str) -> web.Application:
    app = web.Application()

    async def handle_apply(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        init_data = body.get("initData", "")
        parsed = validate_init_data(init_data, bot_token)
        if parsed is None:
            return web.json_response({"error": "invalid_init_data"}, status=403)

        try:
            tg_user = json.loads(parsed.get("user", "{}"))
        except json.JSONDecodeError:
            tg_user = {}

        vacancy_id = body.get("vacancy_id")
        contact = (body.get("contact") or "").strip()
        message = (body.get("message") or "").strip()
        name = (body.get("name") or tg_user.get("first_name") or "").strip()

        if not vacancy_id or not contact:
            return web.json_response({"error": "missing_fields"}, status=400)

        db.insert_application(
            vacancy_id=int(vacancy_id),
            candidate_tg_id=tg_user.get("id"),
            candidate_name=name,
            candidate_username=tg_user.get("username"),
            contact=contact,
            message=message,
        )

        vacancy = db.get_vacancy(int(vacancy_id))
        position = vacancy["position"] if vacancy else "вакансия"
        admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

        notify_lines = [f"📥 Новый отклик на «{position}»", "", f"Имя: {name or '—'}", f"Контакт: {contact}"]
        if tg_user.get("username"):
            notify_lines.append(f"Telegram: @{tg_user['username']}")
        if message:
            notify_lines.append(f"\nСообщение: {message}")
        notify_text = "\n".join(notify_lines)

        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=notify_text)
            except Exception:
                pass

        return web.json_response({"ok": True})

    app.router.add_get("/api/filters", handle_filters)
    app.router.add_get("/api/vacancies", handle_vacancies)
    app.router.add_post("/api/apply", handle_apply)
    app.router.add_static("/", STATIC_DIR, show_index=False)
    return app


async def run_web_server(bot, bot_token: str, port: int):
    app = create_app(bot, bot_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
