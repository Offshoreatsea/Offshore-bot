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

import stripe
from aiohttp import web

import db

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_SUBSCRIPTION_DAYS = 30  # держим в одном месте — совпадает с SUBSCRIPTION_DAYS в main.py
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
        "position_tag": row["position_tag"],
        "vessel": row["vessel"],
        "vessel_tag": row["vessel_tag"],
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


def create_app(bot, bot_token: str, on_stripe_payment=None) -> web.Application:
    app = web.Application()

    def get_authenticated_tg_id(init_data: str) -> int | None:
        """Проверяет подпись initData и достаёт id пользователя. Используется
        и профилем, и списком откликов — везде, где нужно точно знать, что
        запрос пришёл от конкретного кандидата, а не подделан снаружи."""
        parsed = validate_init_data(init_data, bot_token)
        if parsed is None:
            return None
        try:
            tg_user = json.loads(parsed.get("user", "{}"))
        except json.JSONDecodeError:
            return None
        return tg_user.get("id")

    async def handle_stripe_webhook(request: web.Request) -> web.Response:
        if not STRIPE_WEBHOOK_SECRET or not STRIPE_SECRET_KEY:
            # ключи ещё не настроены в Variables — честно говорим об этом в
            # логах, а не притворяемся что всё ок
            return web.Response(status=503, text="Stripe not configured")

        payload = await request.read()
        sig_header = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError):
            # подпись не сошлась — это не настоящее событие от Stripe,
            # отклоняем, чтобы кто угодно не мог подделать "я оплатил"
            return web.Response(status=400, text="Invalid signature")

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            tg_id_raw = session.get("client_reference_id")
            if tg_id_raw and tg_id_raw.isdigit() and on_stripe_payment:
                tg_id = int(tg_id_raw)
                amount = (session.get("amount_total") or 0) / 100  # центы → доллары
                currency = (session.get("currency") or "usd").upper()
                charge_id = session.get("payment_intent") or session.get("id")
                try:
                    await on_stripe_payment(bot, tg_id, STRIPE_SUBSCRIPTION_DAYS, amount, currency, charge_id)
                except Exception as e:
                    print(f"[stripe webhook] Ошибка обработки оплаты для tg_id={tg_id}: {e}")

        return web.Response(status=200, text="ok")

    async def handle_get_profile(request: web.Request) -> web.Response:
        init_data = request.query.get("initData", "")
        tg_id = get_authenticated_tg_id(init_data)
        if tg_id is None:
            return web.json_response({"error": "invalid_init_data"}, status=403)
        row = db.get_candidate_profile(tg_id)
        if row is None:
            return web.json_response(None)
        return web.json_response({
            "full_name": row["full_name"],
            "nationality": row["nationality"],
            "current_rank": row["current_rank"],
            "vessel_types": row["vessel_types"],
            "years_experience": row["years_experience"],
            "availability": row["availability"],
            "documents": row["documents"],
        })

    async def handle_post_profile(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        tg_id = get_authenticated_tg_id(body.get("initData", ""))
        if tg_id is None:
            return web.json_response({"error": "invalid_init_data"}, status=403)
        fields = {
            "full_name": (body.get("full_name") or "").strip()[:200],
            "nationality": (body.get("nationality") or "").strip()[:100],
            "current_rank": (body.get("current_rank") or "").strip()[:50],
            "vessel_types": (body.get("vessel_types") or "").strip()[:300],
            "years_experience": (body.get("years_experience") or "").strip()[:20],
            "availability": (body.get("availability") or "").strip()[:50],
            "documents": (body.get("documents") or "").strip()[:300],
        }
        db.upsert_candidate_profile(tg_id, fields)
        return web.json_response({"ok": True})

    async def handle_my_applications(request: web.Request) -> web.Response:
        init_data = request.query.get("initData", "")
        tg_id = get_authenticated_tg_id(init_data)
        if tg_id is None:
            return web.json_response({"error": "invalid_init_data"}, status=403)
        rows = db.get_applications_for_candidate(tg_id)
        return web.json_response([
            {
                "id": r["id"],
                "vacancy_position": r["vacancy_position"],
                "vacancy_vessel": r["vacancy_vessel"],
                "created_at": r["created_at"],
            }
            for r in rows
        ])

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

    async def handle_index(request: web.Request) -> web.Response:
        # без этого голый домен (WEBAPP_URL без /index.html на конце) отдавал
        # 403 Forbidden — статический хендлер ниже не умеет сам подставлять
        # index.html для корневого пути
        return web.FileResponse(STATIC_DIR / "index.html")

    app.router.add_get("/api/filters", handle_filters)
    app.router.add_get("/api/vacancies", handle_vacancies)
    app.router.add_post("/api/apply", handle_apply)
    app.router.add_get("/api/profile", handle_get_profile)
    app.router.add_post("/api/profile", handle_post_profile)
    app.router.add_get("/api/applications", handle_my_applications)
    app.router.add_post("/stripe/webhook", handle_stripe_webhook)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", STATIC_DIR, show_index=False)
    return app


async def run_web_server(bot, bot_token: str, port: int, on_stripe_payment=None):
    app = create_app(bot, bot_token, on_stripe_payment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
