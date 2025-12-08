import os
import random
import logging
import time
import asyncio
import atexit
import requests
from multiprocessing import Process
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import List, Literal, Optional, Any, Dict
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineQueryResult,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedSticker,
    InputTextMessageContent
)
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получаем токен бота из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
POSTS_FEED_URL = os.getenv('POSTS_FEED_URL')

@dataclass
class PostItem:
    message_id: int
    type: Literal['photo', 'document', 'video', 'sticker', 'text'] = 'text'
    caption: str = ''
    content: str = ''
    file_id: Optional[str] = None
    link: Optional[str] = None


DEFAULT_TITLE = "Рекомендация фильма"


# Кэш для хранения постов с хештегом #showtitrvibe
remote_posts: List[PostItem] = []
manual_posts: List[PostItem] = []
posts_cache: List[PostItem] = []
cache_timestamp: float = 0.0
CACHE_TTL_SECONDS = 60 * 5  # 5 минут
feed_process: Optional[Process] = None
FEED_STARTUP_TIMEOUT = 60  # Увеличено до 60 секунд для сервера


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    logger.info("Получена команда /start")
    message = update.effective_message
    if message is None:
        logger.warning("message is None в start")
        return

    welcome_message = (
        "Привет! Я бот-рекомендатель фильмов из шоу 'Титр'.\n\n"
        "Просто начни вводить мой username в любом чате и выбери фильм из списка.\n"
        "Я буду показывать случайные посты из канала шоу 'Титр', "
        "отмеченные хештегом #showtitrvibe.\n\n"
        "Использование: @ваш_username_бота в любом чате\n\n"
        "Команды: /help, /stats, /test_feed"
    )
    logger.info("Отправляем приветственное сообщение")
    await message.reply_text(welcome_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    message = update.effective_message
    if message is None:
        return

    help_message = (
        "🎬 Kinotip - Бот-рекомендатель фильмов из шоу 'Титр'\n\n"
        "📖 Как использовать:\n"
        "1. Откройте любой чат в Telegram\n"
        "2. Введите @ваш_username_бота\n"
        "3. Выберите фильм из предложенных вариантов\n"
        "4. Пост будет отправлен в чат\n\n"
        "🔧 Команды:\n"
        "• /start - Начать работу с ботом\n"
        "• /help - Показать эту справку\n"
        "• /stats - Показать статистику\n"
        "• /add_post - Добавить пост (ответьте на сообщение с #showtitrvibe)\n\n"
        "Хештег: #showtitrvibe"
    )
    await message.reply_text(help_message)


async def test_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестовая команда для проверки фида"""
    logger.info("Получена команда /test_feed")
    message = update.effective_message
    if message is None:
        logger.warning("message is None в test_feed_command")
        return
    
    logger.info("Отвечаем на команду /test_feed")
    
    if not POSTS_FEED_URL:
        await message.reply_text("❌ POSTS_FEED_URL не указан в .env")
        return
    
    try:
        logger.info("Проверяем фид: %s", POSTS_FEED_URL)
        response = requests.get(POSTS_FEED_URL, timeout=5)
        response.raise_for_status()
        payload = response.json()
        
        items: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = payload.get('posts', payload.get('items', payload.get('data', [])))
        elif isinstance(payload, list):
            items = payload
        
        result_text = (
            f"✅ Фид доступен\n"
            f"📊 Всего элементов: {len(items)}\n"
            f"📝 С #showtitrvibe: {sum(1 for item in items if '#showtitrvibe' in str(item.get('text', '') + ' ' + str(item.get('caption', ''))).lower())}\n"
            f"💾 В кэше бота: {len(posts_cache)}"
        )
        logger.info("Отправляем результат: %s", result_text)
        await message.reply_text(result_text)
    except Exception as e:
        error_msg = f"❌ Ошибка при проверке фида: {e}"
        logger.error("Ошибка в test_feed_command: %s", e, exc_info=True)
        await message.reply_text(error_msg)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику постов"""
    message = update.effective_message
    if message is None:
        return

    total = len(posts_cache)
    
    # Подсчет по типам
    types_count: dict[str, int] = {}
    for post in posts_cache:
        post_type = post.type
        types_count[post_type] = types_count.get(post_type, 0) + 1
    
    stats_message = "📊 Статистика:\n\n"
    stats_message += f"Всего постов: {total}\n\n"
    
    if types_count:
        stats_message += "По типам:\n"
        type_names = {
            'photo': '📷 Фото',
            'document': '📄 Документы',
            'video': '🎥 Видео',
            'sticker': '😊 Стикеры',
            'text': '📝 Текст'
        }
        for post_type, count in sorted(types_count.items(), key=lambda x: -x[1]):
            name = type_names.get(post_type, post_type.capitalize())
            stats_message += f"{name}: {count}\n"
    else:
        stats_message += "Нет постов в коллекции\n"
        stats_message += "Используйте /add_post для добавления"
    
    await message.reply_text(stats_message)


async def add_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для добавления поста из канала"""
    message = update.effective_message
    if message is None:
        return

    if not message.reply_to_message:
        await message.reply_text("Ответьте на сообщение, чтобы добавить его в коллекцию.")
        return
    
    msg = message.reply_to_message
    
    # Проверяем наличие хештега #showtitrvibe
    text = (msg.text or msg.caption or '').strip()
    if '#showtitrvibe' not in text.lower():
        await message.reply_text(
            "Этот пост не содержит хештег #showtitrvibe. "
            "Добавьте хештег в пост, чтобы он попал в рекомендации."
        )
        return
    
    caption = msg.caption or msg.text or ''
    post = PostItem(
        message_id=msg.message_id,
        caption=caption,
        type='text'
    )
    
    if msg.photo:
        post.type = 'photo'
        post.file_id = msg.photo[-1].file_id
    elif msg.document:
        post.type = 'document'
        post.file_id = msg.document.file_id
    elif msg.video:
        post.type = 'video'
        post.file_id = msg.video.file_id
    elif msg.sticker:
        post.type = 'sticker'
        post.file_id = msg.sticker.file_id
    else:
        post.type = 'text'
        post.content = msg.text or caption
    
    manual_posts.append(post)
    rebuild_posts_cache()
    await message.reply_text(f"✅ Пост добавлен! Всего постов в кэше: {len(posts_cache)}")


def rebuild_posts_cache() -> None:
    """Обновляет объединенный кэш постов из удаленного источника и ручных добавлений."""
    global posts_cache
    posts_cache = [*remote_posts, *manual_posts]


def wait_for_feed_ready(url: str) -> bool:
    """Ожидает, когда фид станет доступен."""
    deadline = time.time() + FEED_STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=2)
            if response.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def stop_feed_process() -> None:
    """Останавливает фоновый процесс парсера, если он запущен."""
    global feed_process
    if feed_process and feed_process.is_alive():
        feed_process.terminate()
        feed_process.join(timeout=5)
    feed_process = None


def start_feed_process_if_needed() -> None:
    """Запускает фид в отдельном процессе, если он указан и локальный."""
    global feed_process
    if not POSTS_FEED_URL:
        logger.info("POSTS_FEED_URL не указан, парсер не запускается")
        return
    
    if feed_process:
        logger.info("Парсер уже запущен")
        return

    parsed = urlparse(POSTS_FEED_URL)
    if parsed.scheme != "http":
        logger.info("POSTS_FEED_URL не http, парсер не запускается: %s", parsed.scheme)
        return

    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or ""

    if host not in {"127.0.0.1", "localhost"}:
        logger.info("POSTS_FEED_URL указывает на внешний хост %s, парсер не запускается", host)
        return
    
    if path.rstrip("/") != "/feed":
        logger.info("POSTS_FEED_URL путь не /feed: %s, парсер не запускается", path)
        return

    # Проверяем, не запущен ли уже парсер (доступен ли /feed)
    try:
        response = requests.get(POSTS_FEED_URL, timeout=2)
        if response.status_code == 200:
            logger.info("Парсер уже доступен на %s, не запускаем новый процесс", POSTS_FEED_URL)
            return
    except requests.RequestException:
        # Парсер не доступен, продолжаем запуск
        pass

    try:
        from app import run_feed_server
        logger.info("Импорт парсера успешен")
    except ImportError as error:
        logger.warning("Не удалось импортировать встроенный парсер: %s", error)
        return

    logger.info("Запускаем локальный парсер по адресу %s (host=%s, port=%d)", POSTS_FEED_URL, host, port)
    try:
        feed_process = Process(
            target=run_feed_server,
            kwargs={"host": host, "port": port},
            daemon=True,
        )
        feed_process.start()
        logger.info("Процесс парсера запущен, PID: %d", feed_process.pid)
    except Exception as error:
        logger.error("Ошибка при запуске процесса парсера: %s", error)
        return

    # Даём процессу немного времени на старт
    import time as time_module
    time_module.sleep(2)
    
    # Проверяем, что процесс жив
    if not feed_process.is_alive():
        logger.error("Процесс парсера завершился сразу после запуска!")
        feed_process = None
        return
    
    logger.info("Ожидаем готовности парсера (таймаут %d сек)...", FEED_STARTUP_TIMEOUT)
    if not wait_for_feed_ready(POSTS_FEED_URL):
        logger.warning(
            "Парсер по адресу %s не отвечает в течение %d секунд. Бот продолжит работу без автозагрузки.",
            POSTS_FEED_URL, FEED_STARTUP_TIMEOUT
        )
        # Проверяем статус процесса
        if feed_process and not feed_process.is_alive():
            logger.error("Процесс парсера завершился. Код возврата: %s", feed_process.exitcode)
    else:
        logger.info("Парсер готов и отвечает на %s", POSTS_FEED_URL)


atexit.register(stop_feed_process)


def fetch_posts_from_feed(force: bool = False) -> None:
    """Загружает посты из внешнего сервиса и обновляет кэш."""
    global remote_posts, cache_timestamp

    if not POSTS_FEED_URL:
        return

    now = time.time()
    if not force and remote_posts and (now - cache_timestamp) < CACHE_TTL_SECONDS:
        return

    try:
        response = requests.get(POSTS_FEED_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        logger.warning("Не удалось загрузить посты с %s: %s", POSTS_FEED_URL, error)
        # Не очищаем кэш при ошибке, просто возвращаемся
        return
    except ValueError as error:
        logger.warning("Неверный формат ответа от %s: %s", POSTS_FEED_URL, error)
        return

    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        # Пробуем разные ключи
        items = payload.get('posts', [])
        if not items:
            items = payload.get('items', [])
        if not items:
            items = payload.get('data', [])
        # Если всё ещё пусто, но есть ключи, пробуем взять первый список
        if not items and len(payload) == 1:
            first_value = list(payload.values())[0]
            if isinstance(first_value, list):
                items = first_value
    elif isinstance(payload, list):
        items = payload
    
    logger.info("Извлечено %d элементов из ответа фида", len(items))

    if not items:
        logger.info("Сервис %s вернул пустой список", POSTS_FEED_URL)
        return

    logger.info("Получено %d элементов из фида, начинаем фильтрацию", len(items))
    loaded_posts: List[PostItem] = []
    skipped_count = 0
    empty_text_count = 0
    no_hashtag_count = 0
    
    for idx, item in enumerate(items):
        try:
            text = str(item.get('caption') or item.get('text') or '').strip()
        except AttributeError:
            text = ''
        
        if not text:
            empty_text_count += 1
            skipped_count += 1
            continue
            
        if '#showtitrvibe' not in text.lower():
            no_hashtag_count += 1
            skipped_count += 1
            if idx < 3:  # Логируем первые 3 для отладки
                logger.debug("Пропущен пост без #showtitrvibe: %s", text[:50])
            continue

        message_id_raw = item.get('message_id') or item.get('id') or f"{int(now)}{idx}"
        try:
            message_id = int(message_id_raw)
        except (TypeError, ValueError):
            message_id = int(now) * 1000 + idx

        media_type: Literal['photo', 'document', 'video', 'sticker', 'text'] = 'text'
        media_type_raw = item.get('type') or item.get('media_type')
        if isinstance(media_type_raw, str):
            candidate = media_type_raw.lower()
            if candidate in ('photo', 'document', 'video', 'sticker', 'text'):
                media_type = candidate  # type: ignore

        file_id_raw = item.get('file_id') or item.get('media_file_id')
        file_id = str(file_id_raw) if file_id_raw else None

        content_raw = item.get('content') or item.get('text') or text
        content = str(content_raw or '')

        link_raw = item.get('link') or item.get('url') or ''
        post = PostItem(
            message_id=message_id,
            type=media_type,
            caption=text,
            content=content,
            file_id=file_id,
            link=str(link_raw) if link_raw else None,
        )
        loaded_posts.append(post)

    if not loaded_posts:
        logger.warning(
            "После фильтрации по #showtitrvibe постов не найдено. "
            "Пропущено %d элементов: %d без текста, %d без хештега",
            skipped_count, empty_text_count, no_hashtag_count
        )
        return

    remote_posts = loaded_posts
    cache_timestamp = now
    rebuild_posts_cache()
    logger.info(
        "Загружено %d постов из внешнего сервиса (пропущено %d без #showtitrvibe)",
        len(remote_posts), skipped_count
    )


async def ensure_posts_loaded(force: bool = False) -> None:
    """Асинхронно актуализирует кэш."""
    if not POSTS_FEED_URL:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, fetch_posts_from_feed, force)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline-запросов"""
    inline = update.inline_query
    if inline is None:
        logger.warning("Inline query is None")
        return

    query = (inline.query or '').strip().lower()
    logger.info("Получен inline-запрос: '%s'", query)

    try:
        await ensure_posts_loaded()
    except Exception as error:
        logger.error("Ошибка при загрузке постов: %s", error)

    # Фильтруем посты по запросу (если есть)
    posts: List[PostItem] = list(posts_cache)
    logger.info("Inline запрос: кэш содержит %d постов", len(posts))
    
    if query:
        filtered = [
            p for p in posts
            if query in (p.caption or p.content or '').lower()
        ]
        if filtered:
            posts = filtered
            logger.info("После фильтрации по запросу '%s': %d постов", query, len(posts))

    results: List[InlineQueryResult]

    if not posts:
        logger.warning("Кэш пуст или нет подходящих постов. Всего в кэше: %d", len(posts_cache))
        # Если кэш пуст или нет подходящих постов
        results = [
            InlineQueryResultArticle(
                id='no_posts',
                title='Что-то поломалось, скоро поправим',
                description='Попробуйте позже',
                input_message_content=InputTextMessageContent(
                    "Что-то поломалось, скоро поправим"
                )
            )
        ]
    else:
        # Выбираем случайный пост
        random_post = random.choice(posts)
        caption = random_post.caption or ''
        content = random_post.content or caption
        title_source = caption or content or DEFAULT_TITLE
        title = title_source[:64]
        description = caption[:96] if caption else None

        results = []

        # Формируем результат - всегда отправляем текст с ссылкой на оригинал
        # (Telethon не даёт file_id для Bot API, поэтому медиа не пересылаем напрямую)
        link = getattr(random_post, 'link', None) or ''
        
        # Формируем текст с информацией о типе поста
        type_emoji = {
            'photo': '📷',
            'document': '📄',
            'video': '🎥',
            'sticker': '😊',
            'text': '📝'
        }
        emoji = type_emoji.get(random_post.type, '📝')
        
        final_content = content or DEFAULT_TITLE
        if link:
            final_content = f"{emoji} {final_content}\n\n🔗 {link}".strip()
        else:
            final_content = f"{emoji} {final_content}".strip()
        
        results = [
            InlineQueryResultArticle(
                id=f"post_{random_post.message_id}",
                title=title,
                description=description or f"Нажмите, чтобы увидеть полный пост{(' с ' + random_post.type) if random_post.type != 'text' else ''}",
                input_message_content=InputTextMessageContent(
                    final_content,
                    parse_mode='HTML'
                )
            )
        ]
        logger.info("Сформирован результат для inline-запроса: %d элементов", len(results))

    try:
        await inline.answer(results, cache_time=1, is_personal=True)
        logger.info("Ответ на inline-запрос отправлен успешно")
    except Exception as error:
        logger.error("Ошибка при отправке ответа на inline-запрос: %s", error)


def main():
    """Главная функция запуска бота"""
    logger.info("=" * 50)
    logger.info("Запуск бота...")
    logger.info("=" * 50)
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден! Создайте файл .env с BOT_TOKEN=ваш_токен")
        return
    
    logger.info("BOT_TOKEN найден (длина: %d символов)", len(BOT_TOKEN))
    
    # Создаем приложение
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        logger.info("Приложение создано успешно")
    except Exception as e:
        logger.error("Ошибка при создании приложения: %s", e)
        return
    
    # Регистрируем обработчики
    logger.info("Регистрация обработчиков...")
    application.add_handler(CommandHandler("start", start))
    logger.info("  - /start зарегистрирован")
    application.add_handler(CommandHandler("help", help_command))
    logger.info("  - /help зарегистрирован")
    application.add_handler(CommandHandler("stats", stats_command))
    logger.info("  - /stats зарегистрирован")
    application.add_handler(CommandHandler("test_feed", test_feed_command))
    logger.info("  - /test_feed зарегистрирован")
    application.add_handler(CommandHandler("add_post", add_post))
    logger.info("  - /add_post зарегистрирован")
    application.add_handler(InlineQueryHandler(inline_query))
    logger.info("  - inline_query зарегистрирован")
    logger.info("Все обработчики зарегистрированы")
    
    # Запускаем парсер, если нужно
    start_feed_process_if_needed()
    
    # Запускаем бота
    logger.info("=" * 50)
    logger.info("Бот запущен и готов к работе!")
    logger.info("=" * 50)
    if POSTS_FEED_URL:
        logger.info("Попытка загрузить посты из %s", POSTS_FEED_URL)
        # Ждём немного, чтобы парсер успел запуститься
        import time
        time.sleep(3)
        fetch_posts_from_feed(force=True)
        logger.info("Текущий размер кэша: %d постов", len(posts_cache))
        if len(posts_cache) == 0:
            logger.warning("⚠️ Кэш пуст! Проверьте, что парсер запущен и доступен на %s", POSTS_FEED_URL)
            logger.warning("Попробуйте команду /test_feed в боте для диагностики")
    else:
        logger.warning("POSTS_FEED_URL не указан, бот будет работать только с ручными постами")
    
    try:
        logger.info("Запуск polling...")
        application.run_polling()
    except Exception as e:
        logger.error("КРИТИЧЕСКАЯ ОШИБКА при запуске polling: %s", e, exc_info=True)
        raise


if __name__ == '__main__':
    main()

