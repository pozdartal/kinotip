"""
Простой Flask-парсер для канала Telegram шоу "Титр".
Запуск: python app.py
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional, cast

from dotenv import load_dotenv
from flask import Flask, jsonify

try:
    # Импортируем Telethon для работы с Telegram API
    from telethon import TelegramClient  # type: ignore
    from telethon.errors import (  # type: ignore
        ChannelInvalidError,
        ChannelPrivateError,
        ChannelPublicGroupNaError,
        RPCError,
    )
except ModuleNotFoundError as exc:
    # Подсказываем, как установить Telethon, если библиотека не найдена
    raise SystemExit(
        "Не найден модуль Telethon. Установите зависимости командой:\n"
        "pip install -r requirements.txt"
    ) from exc

# Загружаем переменные окружения из .env
load_dotenv()

# Создаём отдельный логгер, чтобы видеть, что происходит
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Забираем настройки из окружения
API_ID_RAW = os.getenv("API_ID")
API_HASH_RAW = os.getenv("API_HASH")
PHONE_RAW = os.getenv("PHONE")
CHANNEL_USERNAME_RAW = os.getenv("CHANNEL_USERNAME")

if not API_ID_RAW:
    raise SystemExit("Проверь .env — не указано значение API_ID.")
if not API_HASH_RAW:
    raise SystemExit("Проверь .env — не указано значение API_HASH.")
if not PHONE_RAW:
    raise SystemExit("Проверь .env — не указан PHONE.")
if not CHANNEL_USERNAME_RAW:
    raise SystemExit("Проверь .env — не указан CHANNEL_USERNAME.")

try:
    API_ID_INT = int(API_ID_RAW)
except ValueError as exc:
    raise SystemExit("API_ID должен быть числом. Исправь это в .env.") from exc

API_HASH_VALUE: str = API_HASH_RAW
PHONE_VALUE: str = PHONE_RAW
CHANNEL_USERNAME_VALUE: str = CHANNEL_USERNAME_RAW

# Flask-приложение
app = Flask(__name__)

# Небольшой кэш, который наполняем при старте
cached_posts: List[Dict[str, Any]] = []


def create_client() -> TelegramClient:
    """Создаёт и авторизует Telethon-клиент."""
    # Идентификатор сессии можно назвать как угодно — используем имя канала
    client = TelegramClient("kinotip_parser", API_ID_INT, API_HASH_VALUE)

    # start() внутри себя синхронно запускает авторизацию и запрос кода
    client.start(phone=PHONE_VALUE)
    return client


def ensure_session() -> None:
    """Проверяет, что сессия авторизована (при необходимости запросит код)."""
    client: Optional[TelegramClient] = None
    try:
        client = create_client()
    finally:
        if client:
            try:
                client.disconnect()
            except Exception:
                pass


async def _collect_posts(client: TelegramClient, limit: Optional[int]) -> List[Dict[str, Any]]:
    """Асинхронно собирает посты из канала."""
    results: List[Dict[str, Any]] = []
    async for message in client.iter_messages(  # type: ignore[arg-type]
        CHANNEL_USERNAME_VALUE,
        limit=limit,  # type: ignore[arg-type]
        reverse=False,
    ):
        msg = cast(Any, message)
        if not msg:
            continue

        text_raw: Any = getattr(msg, "message", None) or getattr(msg, "raw_text", "")
        text = str(text_raw or "").strip()
        if not text:
            continue

        if "#showtitrvibe" not in text.lower():
            continue

        link = ""
        try:
            link = str(getattr(msg, "link", "") or "")
        except AttributeError:
            link = ""
        channel_slug = CHANNEL_USERNAME_VALUE.lstrip("@")
        if not link and channel_slug and getattr(msg, "id", None):
            link = f"https://t.me/{channel_slug}/{getattr(msg, 'id')}"

        payload: Dict[str, Any] = {
            "id": str(getattr(msg, "id", "")),
            "message_id": int(getattr(msg, "id", 0)),
            "text": text,
            "caption": text,
            "type": "text",
            "content": text,
        }
        if link:
            payload["link"] = link

        results.append(payload)
    return results


def fetch_posts(limit: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
    """Забирает посты из канала и оставляет только те, что с хештегом #showtitrvibe."""
    client: Optional[TelegramClient] = None
    posts: List[Dict[str, Any]] = []
    try:
        client = create_client()
    except RPCError as error:
        logger.error("Ошибка при подключении к Telegram: %s", error)
        return None
    except Exception as error:
        logger.error("Непредвиденная ошибка при подключении к Telegram: %s", error)
        return None

    try:
        posts = client.loop.run_until_complete(_collect_posts(client, limit))
    except (
        ChannelInvalidError,
        ChannelPrivateError,
        ChannelPublicGroupNaError,
        ValueError,
    ) as error:
        logger.warning("Канал недоступен (%s): %s", CHANNEL_USERNAME_VALUE, error)
        return None
    except RPCError as error:
        logger.error("Ошибка Telethon при чтении сообщений: %s", error)
        return None
    except Exception as error:
        logger.error("Непредвиденная ошибка при чтении сообщений: %s", error)
        return None
    finally:
        try:
            if client:
                client.disconnect()
        except Exception:
            pass

    return posts


def warm_up_cache() -> None:
    """Наполняет кэш последними постами при старте приложения."""
    refresh_cache("старт сервера")


def refresh_cache(reason: str) -> None:
    """Обновляет кэш и логирует причину."""
    global cached_posts

    logger.info("Обновляем кэш (%s)", reason)
    new_posts = fetch_posts(limit=None)
    if new_posts is None:
        logger.warning(
            "Не удалось обновить кэш (%s). Сохраняем предыдущие данные (%d постов)",
            reason,
            len(cached_posts),
        )
        return

    cached_posts = new_posts
    logger.info("В кэше сейчас %s постов", len(cached_posts))


def schedule_cache_updates() -> None:
    """Запускает фоновой поток, обновляющий кэш дважды в сутки."""

    def worker() -> None:
        while True:
            now = datetime.now()
            next_runs: List[datetime] = []
            for target_hour in (0, 12):
                candidate = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(days=1)
                next_runs.append(candidate)

            next_run = min(next_runs)
            sleep_seconds = max((next_run - now).total_seconds(), 0.0)
            logger.info(
                "Следующее плановое обновление кэша в %s (через %.0f секунд)",
                next_run.isoformat(),
                sleep_seconds,
            )

            if sleep_seconds:
                time.sleep(sleep_seconds)

            try:
                refresh_cache(f"плановое обновление {next_run.isoformat()}")
            except Exception as error:
                logger.exception("Не удалось обновить кэш по расписанию: %s", error)

    thread = threading.Thread(target=worker, name="cache-scheduler", daemon=True)
    thread.start()


@app.route("/feed", methods=["GET"])
def feed():
    """Отдаём JSON с постами из кэша."""
    return jsonify({"posts": cached_posts})


@app.route("/", methods=["GET"])
def index():
    """Просто дружелюбное приветствие."""
    return (
        "Привет! Это парсер шоу Титр. Забирай свежие посты на /feed и наслаждайся вайбом ✨",
        200,
    )


def run_feed_server(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Запускает HTTP-сервер с фидом."""
    warm_up_cache()
    schedule_cache_updates()
    app.run(host=host, port=port)


if __name__ == "__main__":
    run_feed_server()

