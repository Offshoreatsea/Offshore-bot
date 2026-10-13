"""
Одноразовый помощник: получить строковую сессию Telethon (TG_SESSION) для
авто-парсинга вакансий из каналов.

Запусти ЛОКАЛЬНО на своём компьютере (не на Railway):

    pip install telethon
    python tools/make_session.py

Понадобятся TG_API_ID и TG_API_HASH с https://my.telegram.org
(→ API development tools). Скрипт попросит номер телефона и код из Telegram,
затем напечатает длинную строку — это и есть TG_SESSION. Скопируй её в
переменные окружения Railway. Номер/код больше не понадобятся.

Важно: это сессия ТВОЕГО аккаунта. Твой аккаунт должен быть подписан на
каналы-источники (или они должны быть публичными), иначе бот их не прочитает.
"""
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.getenv("TG_API_ID") or input("TG_API_ID: ").strip())
api_hash = os.getenv("TG_API_HASH") or input("TG_API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n==== TG_SESSION (скопируй целиком в Railway) ====\n")
    print(client.session.save())
    print("\n=================================================\n")
