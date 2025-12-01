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


def create_client() -> Optional[TelegramClient]:
    """Создаёт и авторизует Telethon-клиент. Возвращает None при ошибке авторизации."""
    import os
    import asyncio
    session_name = "kinotip_parser"
    session_file = f"{session_name}.session"
    
    # ПРОВЕРЯЕМ: есть ли файл сессии?
    has_session = os.path.exists(session_file)
    
    # Создаём event loop для текущего потока
    # В новом потоке может не быть event loop, поэтому создаём новый
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Создаём клиент с явным указанием loop
    client = TelegramClient(session_name, API_ID_INT, API_HASH_VALUE, loop=loop)
    
    try:
        # Подключаемся
        loop.run_until_complete(client.connect())
        
        # Если файл сессии ЕСТЬ - проверяем авторизацию
        if has_session:
            logger.info("Файл сессии найден: %s", session_file)
            is_authorized = loop.run_until_complete(client.is_user_authorized())
            if is_authorized:
                logger.info("✅ Сессия авторизована")
                return client
            else:
                logger.warning("⚠️ Файл сессии есть, но авторизация не прошла!")
                logger.warning("Возможно, сессия истекла или повреждена.")
                # Отключаемся и возвращаем None
                try:
                    loop.run_until_complete(client.disconnect())
                except Exception:
                    pass  # Игнорируем ошибки отключения
                logger.error("❌ Требуется переавторизация. Удалите файл %s и создайте новую сессию", session_file)
                return None
        
        # Если файла НЕТ - пытаемся авторизоваться
        logger.info("Файл сессии НЕ найден, требуется авторизация")
        try:
            client.start(phone=PHONE_VALUE)
            logger.info("✅ Авторизация успешна, файл сессии создан")
            return client
        except EOFError:
            logger.error("❌ Нет интерактивного ввода и нет файла сессии!")
            logger.error("Запустите 'python app.py' локально один раз для создания файла сессии")
            try:
                loop.run_until_complete(client.disconnect())
            except:
                pass
            return None
    except Exception as e:
        logger.error("Ошибка при создании клиента: %s", e)
        try:
            if client:
                loop.run_until_complete(client.disconnect())
        except:
            pass
        return None


def ensure_session() -> None:
    """Проверяет, что сессия авторизована (при необходимости запросит код)."""
    client: Optional[TelegramClient] = None
    try:
        client = create_client()
        if client is None:
            logger.error("Не удалось создать клиент. Проверьте сессию.")
            return
        # Проверяем, авторизован ли клиент
        if client.loop.run_until_complete(client.is_user_authorized()):
            logger.info("Telethon сессия авторизована успешно")
        else:
            logger.warning("Telethon сессия не авторизована")
    except Exception as error:
        logger.warning("Ошибка при проверке сессии: %s", error)
    finally:
        if client:
            try:
                client.loop.run_until_complete(client.disconnect())
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

        # Получаем текст сообщения
        text_raw: Any = getattr(msg, "message", None) or getattr(msg, "raw_text", "")
        text = str(text_raw or "").strip()
        
        # Если текста нет, проверяем подпись к медиа
        if not text:
            try:
                text = str(getattr(msg, "raw_text", "") or "")
            except:
                pass
        
        # Пропускаем, если вообще нет текста
        if not text:
            continue
        
        # Проверяем хештег
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

        # Определяем тип медиа
        post_type = "text"
        
        # Проверяем фото
        if hasattr(msg, "photo") and msg.photo:
            post_type = "photo"
        # Проверяем документ
        elif hasattr(msg, "document") and msg.document:
            post_type = "document"
        # Проверяем видео
        elif hasattr(msg, "video") and msg.video:
            post_type = "video"
        # Проверяем стикер
        elif hasattr(msg, "sticker") and msg.sticker:
            post_type = "sticker"

        # Используем текст (уже получен выше)
        display_text = text

        payload: Dict[str, Any] = {
            "id": str(getattr(msg, "id", "")),
            "message_id": int(getattr(msg, "id", 0)),
            "text": display_text,
            "caption": display_text,
            "type": post_type,
            "content": display_text,
        }
        if link:
            payload["link"] = link

        results.append(payload)
    
    logger.info("Собрано %d постов с #showtitrvibe из канала", len(results))
    return results


def fetch_posts(limit: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
    """Забирает посты из канала и оставляет только те, что с хештегом #showtitrvibe."""
    client: Optional[TelegramClient] = None
    posts: List[Dict[str, Any]] = []
    try:
        client = create_client()
        
        # Если клиент не создан (ошибка авторизации), возвращаем None
        if client is None:
            logger.error("Не удалось создать клиент. Невозможно получить посты.")
            return None
        
        # Проверяем авторизацию ещё раз (на всякий случай)
        if not client.loop.run_until_complete(client.is_user_authorized()):
            logger.error("Telethon сессия не авторизована. Невозможно получить посты.")
            return None
    except RPCError as error:
        logger.error("Ошибка при подключении к Telegram: %s", error)
        return None
    except Exception as error:
        logger.error("Непредвиденная ошибка при подключении к Telegram: %s", error)
        return None

    if client is None:
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
                client.loop.run_until_complete(client.disconnect())
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
        import asyncio
        # Создаём event loop для этого потока
        # Это необходимо, так как Telethon требует asyncio event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
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
    # Проверяем сессию перед запуском
    ensure_session()
    # Наполняем кэш
    warm_up_cache()
    schedule_cache_updates()
    
    # Запускаем Flask в отдельном потоке
    import threading
    flask_thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    logger.info("Flask сервер запущен в фоновом потоке")
    
    # Запускаем бота в отдельном процессе
    try:
        import subprocess
        import sys
        logger.info("Запускаем бота в отдельном процессе...")
        bot_process = subprocess.Popen(
            [sys.executable, "bot.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        logger.info("Бот запущен, PID: %d", bot_process.pid)
        
        # Ждём, пока процессы работают
        try:
            while True:
                time.sleep(1)
                # Проверяем, что бот ещё работает
                if bot_process.poll() is not None:
                    logger.warning("Процесс бота завершился с кодом: %s", bot_process.returncode)
                    break
        except KeyboardInterrupt:
            logger.info("Остановка...")
            bot_process.terminate()
            bot_process.wait()
    except Exception as e:
        logger.error("Ошибка при запуске бота: %s", e)
        logger.info("Парсер будет работать без бота")
        # Если бот не запустился, просто ждём
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Остановка сервера...")


if __name__ == "__main__":
    run_feed_server()

