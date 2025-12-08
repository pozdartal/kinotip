"""
Простой Flask-парсер для канала Telegram шоу "Титр".
Запуск:  python app.py
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

# Блокировка для синхронизации доступа к сессии Telethon между процессами
import sys

# Файловая блокировка для работы между процессами
_lock_file_path = "kinotip_parser.session.lock"

def _acquire_session_lock():
    """Приобретает файловую блокировку для доступа к сессии."""
    try:
        if sys.platform != 'win32':
            import fcntl
            lock_file = open(_lock_file_path, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return lock_file
        else:
            # На Windows используем простую проверку файла с таймаутом
            import time
            max_wait = 10  # Максимальное время ожидания в секундах
            start_time = time.time()
            while time.time() - start_time < max_wait:
                try:
                    # Пытаемся создать файл блокировки
                    lock_file = open(_lock_file_path, 'x')
                    return lock_file
                except FileExistsError:
                    # Файл существует, ждём немного
                    time.sleep(0.5)
                    continue
            # Если не удалось получить блокировку, возвращаем None
            return None
    except Exception as e:
        logger.debug("Ошибка при получении блокировки: %s", e)
        return None

def _release_session_lock(lock_file):
    """Освобождает файловую блокировку."""
    if lock_file is None:
        return
    try:
        lock_file.close()
        if sys.platform == 'win32':
            # На Windows удаляем файл блокировки
            import os
            try:
                os.remove(_lock_file_path)
            except:
                pass
    except Exception:
        pass


def create_client(max_retries: int = 3, retry_delay: float = 1.0) -> Optional[TelegramClient]:
    """Создаёт и авторизует Telethon-клиент. Возвращает None при ошибке авторизации.
    
    Args:
        max_retries: Максимальное количество попыток при ошибке "database is locked"
        retry_delay: Задержка между попытками в секундах
    """
    import os
    import asyncio
    session_name = "kinotip_parser"
    session_file = f"{session_name}.session"
    
    # ПРОВЕРЯЕМ: есть ли файл сессии?
    has_session = os.path.exists(session_file)
    
    # Используем файловую блокировку для синхронизации доступа к сессии между процессами
    lock_file = None
    try:
        lock_file = _acquire_session_lock()
        if lock_file is None:
            logger.warning("Не удалось получить блокировку сессии, продолжаем без блокировки")
        # Создаём event loop для текущего потока
        # В новом потоке может не быть event loop, поэтому создаём новый
        # В Python 3.10+ get_event_loop() устарел, используем get_running_loop() или new_event_loop()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Нет запущенного loop, создаём новый
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Создаём клиент с явным указанием loop
        client = TelegramClient(session_name, API_ID_INT, API_HASH_VALUE, loop=loop)
        
        # Повторяем попытки при ошибке "database is locked"
        for attempt in range(max_retries):
            try:
                # Подключаемся
                loop.run_until_complete(client.connect())
                break  # Успешно подключились, выходим из цикла
            except Exception as e:
                error_str = str(e).lower()
                if "database is locked" in error_str or "locked" in error_str:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)  # Экспоненциальная задержка
                        logger.warning(
                            "Файл сессии заблокирован (попытка %d/%d). Ждём %.1f сек...",
                            attempt + 1, max_retries, wait_time
                        )
                        time.sleep(wait_time)
                        # Закрываем неудачное подключение
                        try:
                            if client.is_connected():
                                loop.run_until_complete(client.disconnect())
                                # Даём время на завершение задач
                                loop.run_until_complete(asyncio.sleep(0.5))
                        except:
                            pass
                        continue
                    else:
                        logger.error("Файл сессии заблокирован после %d попыток. Возможно, другой процесс использует сессию.", max_retries)
                        try:
                            if client.is_connected():
                                loop.run_until_complete(client.disconnect())
                                # Даём время на завершение задач
                                loop.run_until_complete(asyncio.sleep(0.5))
                        except:
                            pass
                        return None
                else:
                    # Другая ошибка, пробрасываем дальше
                    raise
        
            try:
                # Если файл сессии ЕСТЬ - проверяем авторизацию
                if has_session:
                    logger.info("Файл сессии найден: %s", session_file)
                    is_authorized = loop.run_until_complete(client.is_user_authorized())
                    if is_authorized:
                        logger.info("✅ Сессия авторизована")
                        # Освобождаем блокировку после успешного подключения
                        if lock_file:
                            _release_session_lock(lock_file)
                            lock_file = None
                        return client
                    else:
                        logger.warning("⚠️ Файл сессии есть, но авторизация не прошла!")
                        logger.warning("Возможно, сессия истекла или повреждена.")
                        # Отключаемся и возвращаем None
                        try:
                            loop.run_until_complete(client.disconnect())
                            # Даём время на завершение задач
                            loop.run_until_complete(asyncio.sleep(0.5))
                        except Exception:
                            pass  # Игнорируем ошибки отключения
                        logger.error("❌ Требуется переавторизация. Удалите файл %s и создайте новую сессию", session_file)
                        return None
                
                # Если файла НЕТ - пытаемся авторизоваться
                logger.info("Файл сессии НЕ найден, требуется авторизация")
                try:
                    client.start(phone=PHONE_VALUE)
                    logger.info("✅ Авторизация успешна, файл сессии создан")
                    # Освобождаем блокировку после успешной авторизации
                    if lock_file:
                        _release_session_lock(lock_file)
                        lock_file = None
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
                    if client and client.is_connected():
                        loop.run_until_complete(client.disconnect())
                except:
                    pass
                return None
    finally:
        # Освобождаем блокировку в любом случае
        if lock_file:
            _release_session_lock(lock_file)


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
                # Отключаем клиент и даём время на завершение фоновых задач
                loop = client.loop
                if client.is_connected():
                    loop.run_until_complete(client.disconnect())
                    # Даём небольшое время на завершение фоновых задач Telethon
                    import asyncio
                    try:
                        # Ждём немного, чтобы фоновые задачи успели завершиться
                        loop.run_until_complete(asyncio.sleep(0.5))
                    except Exception:
                        pass
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
                # Отключаем клиент и даём время на завершение фоновых задач
                loop = client.loop
                if client.is_connected():
                    loop.run_until_complete(client.disconnect())
                    # Даём небольшое время на завершение фоновых задач Telethon
                    import asyncio
                    try:
                        # Ждём немного, чтобы фоновые задачи успели завершиться
                        loop.run_until_complete(asyncio.sleep(0.5))
                    except Exception:
                        pass
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
    import subprocess
    import sys
    import threading
    
    def read_bot_output(pipe, prefix):
        """Читает вывод из процесса бота и логирует его"""
        try:
            for line in iter(pipe.readline, ''):
                if line:
                    logger.info("[BOT] %s", line.rstrip())
        except Exception as e:
            logger.error("Ошибка при чтении вывода бота: %s", e)
        finally:
            pipe.close()
    
    bot_process = None
    restart_count = 0
    max_restarts = 10
    
    try:
        while restart_count < max_restarts:
            try:
                logger.info("Запускаем бота в отдельном процессе... (попытка %d/%d)", restart_count + 1, max_restarts)
                bot_process = subprocess.Popen(
                    [sys.executable, "bot.py"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # Объединяем stderr в stdout
                    text=True,
                    bufsize=1  # Строковая буферизация
                )
                logger.info("Бот запущен, PID: %d", bot_process.pid)
                
                # Запускаем поток для чтения логов
                output_thread = threading.Thread(
                    target=read_bot_output,
                    args=(bot_process.stdout, "BOT"),
                    daemon=True
                )
                output_thread.start()
                
                # Ждём, пока процессы работают
                try:
                    while True:
                        time.sleep(1)
                        # Проверяем, что бот ещё работает
                        if bot_process.poll() is not None:
                            exit_code = bot_process.returncode
                            logger.warning("Процесс бота завершился с кодом: %s", exit_code)
                            
                            # Если это не нормальное завершение (0) и не SIGKILL (-9), ждём немного перед перезапуском
                            if exit_code != 0:
                                if exit_code == -9:
                                    logger.warning("Бот был принудительно завершён (SIGKILL). Возможно, нехватка памяти.")
                                else:
                                    logger.warning("Бот завершился с ошибкой. Перезапускаем...")
                                
                                restart_count += 1
                                if restart_count < max_restarts:
                                    logger.info("Перезапуск через 5 секунд...")
                                    time.sleep(5)
                                    break  # Выходим из внутреннего цикла, чтобы перезапустить
                                else:
                                    logger.error("Достигнут лимит перезапусков (%d). Останавливаем попытки.", max_restarts)
                                    return
                            else:
                                logger.info("Бот завершился нормально")
                                return
                except KeyboardInterrupt:
                    logger.info("Остановка...")
                    if bot_process:
                        bot_process.terminate()
                        bot_process.wait()
                    return
            except Exception as e:
                logger.error("Ошибка при запуске бота: %s", e)
                restart_count += 1
                if restart_count < max_restarts:
                    logger.info("Перезапуск через 5 секунд...")
                    time.sleep(5)
                else:
                    logger.error("Достигнут лимит перезапусков. Парсер будет работать без бота")
                    break
        
        # Если бот не запустился, просто ждём
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Остановка сервера...")
    except Exception as e:
        logger.error("Критическая ошибка при работе с ботом: %s", e)
        logger.info("Парсер будет работать без бота")
        # Если бот не запустился, просто ждём
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Остановка сервера...")


if __name__ == "__main__":
    run_feed_server()

