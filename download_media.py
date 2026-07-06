

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.utils import get_extension

# ── Настройки ──────────────────────────────────────────────────────────────────

API_ID = 0                  # Вставить свой app_api_id (число)
API_HASH = ""               # Вставить свой app_api_hash (строка)

CHAT = ""                   # username (@mychat), номер телефона, или числовой ID чата
OUTPUT_DIR = "downloads"    # Папка для сохранения файлов

# Фильтры (True = скачивать)
DOWNLOAD_PHOTOS = True
DOWNLOAD_VIDEOS = True
DOWNLOAD_OTHER_DOCS = False  # gif, аудио, документы и т.д.

# Лимит сообщений (None = все)
MESSAGE_LIMIT = None

# Диапазон дат (None = без ограничений)
DATE_FROM = None  # datetime(2024, 1, 1)
DATE_TO   = None  # datetime(2024, 12, 31)

# ──────────────────────────────────────────────────────────────────────────────


def is_video(document) -> bool:
    if document is None:
        return False
    mime = getattr(document, "mime_type", "") or ""
    return mime.startswith("video/")


def should_download(message) -> bool:
    media = message.media
    if media is None:
        return False
    if isinstance(media, MessageMediaPhoto):
        return DOWNLOAD_PHOTOS
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if is_video(doc):
            return DOWNLOAD_VIDEOS
        return DOWNLOAD_OTHER_DOCS
    return False


def make_filename(message) -> str:
    """Формирует имя файла: <дата>_<id>.<расширение>"""
    date_str = message.date.strftime("%Y%m%d_%H%M%S")
    media = message.media

    if isinstance(media, MessageMediaPhoto):
        ext = ".jpg"
    elif isinstance(media, MessageMediaDocument):
        ext = get_extension(media.document) or ".bin"
    else:
        ext = ".bin"

    return f"{date_str}_{message.id}{ext}"


async def main():
    if API_ID == 0 or not API_HASH:
        print("Ошибка: заполните API_ID и API_HASH в начале скрипта.")
        print("Получить их можно на https://my.telegram.org")
        sys.exit(1)

    if not CHAT:
        print("Ошибка: укажите чат в переменной CHAT.")
        sys.exit(1)

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)

    print(f"Подключение к Telegram...")
    async with TelegramClient("session", API_ID, API_HASH) as client:
        entity = await client.get_entity(CHAT)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", str(CHAT))
        print(f"Чат: {chat_name}")

        total = 0
        skipped = 0
        downloaded = 0

        print("Сканирование сообщений...")
        async for message in client.iter_messages(entity, limit=MESSAGE_LIMIT):
            total += 1

            # Фильтр по дате
            msg_date = message.date.replace(tzinfo=None)
            if DATE_FROM and msg_date < DATE_FROM:
                continue
            if DATE_TO and msg_date > DATE_TO:
                continue

            if not should_download(message):
                skipped += 1
                continue

            filename = make_filename(message)
            dest = output_path / filename

            if dest.exists():
                print(f"  Уже есть: {filename}")
                skipped += 1
                continue

            print(f"  Скачиваю [{message.id}]: {filename} ...", end=" ", flush=True)
            try:
                await client.download_media(message.media, file=str(dest))
                size_kb = dest.stat().st_size // 1024
                print(f"OK ({size_kb} KB)")
                downloaded += 1
            except Exception as e:
                print(f"ОШИБКА: {e}")

        print(f"\nГотово: скачано {downloaded}, пропущено {skipped}, всего сообщений {total}.")
        print(f"Файлы сохранены в: {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
