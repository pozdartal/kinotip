import os
import random
import logging
from dataclasses import dataclass
from typing import List, Literal, Optional
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

@dataclass
class PostItem:
    message_id: int
    type: Literal['photo', 'document', 'video', 'sticker', 'text'] = 'text'
    caption: str = ''
    content: str = ''
    file_id: Optional[str] = None


DEFAULT_TITLE = "Рекомендация фильма"


# Кэш для хранения постов с хештегом #showtitrvibe
posts_cache: List[PostItem] = []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    message = update.effective_message
    if message is None:
        return

    welcome_message = (
        "Привет! Я бот-рекомендатель фильмов из шоу 'Титр'.\n\n"
        "Просто начни вводить мой username в любом чате и выбери фильм из списка.\n"
        "Я буду показывать случайные посты из канала шоу 'Титр', "
        "отмеченные хештегом #showtitrvibe.\n\n"
        "Использование: @ваш_username_бота в любом чате"
    )
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
    
    posts_cache.append(post)
    await message.reply_text(f"✅ Пост добавлен! Всего постов в кэше: {len(posts_cache)}")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline-запросов"""
    inline = update.inline_query
    if inline is None:
        return

    query = (inline.query or '').strip().lower()

    # Фильтруем посты по запросу (если есть)
    posts: List[PostItem] = posts_cache
    if query:
        posts = [
            p for p in posts
            if query in (p.caption or p.content or '').lower()
        ]

    results: List[InlineQueryResult]

    if not posts:
        # Если кэш пуст или нет подходящих постов
        results = [
            InlineQueryResultArticle(
                id='no_posts',
                title='Нет постов для рекомендации',
                description='Добавьте посты через команду /add_post',
                input_message_content=InputTextMessageContent(
                    "К сожалению, сейчас нет доступных рекомендаций.\n\n"
                    "Используйте команду /add_post, чтобы добавить пост с хештегом #showtitrvibe."
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

        # Формируем результат в зависимости от типа поста
        post_type = random_post.type
        if post_type == 'photo' and random_post.file_id:
            results.append(
                InlineQueryResultCachedPhoto(
                    id=f"post_{random_post.message_id}",
                    photo_file_id=random_post.file_id,
                    caption=caption or None
                )
            )
        elif post_type == 'document' and random_post.file_id:
            results.append(
                InlineQueryResultCachedDocument(
                    id=f"post_{random_post.message_id}",
                    document_file_id=random_post.file_id,
                    title=title,
                    description=description,
                    caption=caption or None
                )
            )
        elif post_type == 'video' and random_post.file_id:
            results.append(
                InlineQueryResultCachedVideo(
                    id=f"post_{random_post.message_id}",
                    video_file_id=random_post.file_id,
                    title=title,
                    caption=caption or None
                )
            )
        elif post_type == 'sticker' and random_post.file_id:
            results.append(
                InlineQueryResultCachedSticker(
                    id=f"post_{random_post.message_id}",
                    sticker_file_id=random_post.file_id
                )
            )
        else:  # text
            results.append(
                InlineQueryResultArticle(
                    id=f"post_{random_post.message_id}",
                    title=title,
                    description=description,
                    input_message_content=InputTextMessageContent(
                        content or DEFAULT_TITLE
                    )
                )
            )

    await inline.answer(results, cache_time=1, is_personal=True)


def main():
    """Главная функция запуска бота"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден! Создайте файл .env с BOT_TOKEN=ваш_токен")
        return
    
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("add_post", add_post))
    application.add_handler(InlineQueryHandler(inline_query))
    
    # Запускаем бота
    logger.info("Бот запущен!")
    application.run_polling()


if __name__ == '__main__':
    main()

