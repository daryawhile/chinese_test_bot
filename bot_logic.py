import os
import json
import logging
import random
import aiohttp
import re
import io
import asyncio
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent, BufferedInputFile, WebAppInfo
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest
from gtts import gTTS

from tests_data import TESTS, WORDS
from config import BOT_TOKEN

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
user_sessions: dict[int, dict] = {}

# ─── Админ-панель ─────────────────────────────────────────────
admin_users: set[int] = set()

# ─── Настройки JSONBin ────────────────────────────────────────
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY")
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}" if JSONBIN_BIN_ID else ""
HEADERS = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"} if JSONBIN_API_KEY else {}

# ─── УМНАЯ ЗАГРУЗКА ДАННЫХ (ЛЕНИВАЯ) ────────────────────────
async def ensure_user_loaded(user_id: int) -> dict:
    """Проверяет память. Если данных нет, загружает из JSONBin (таймаут 5 сек)."""
    session = user_sessions.get(user_id, {})
    
    # Если ключа "settings" нет, значит бот перезапустился или это первый запрос
    if "settings" not in session:
        data = await load_completed()
        user_data = data.get(str(user_id), {})
        
        # Обновляем сессию, не затирая временные поля (например, "type": "search")
        session["settings"] = user_data.get("settings", {"audio_enabled": False})
        session["progress"] = user_data  # Весь прогресс тоже в память для скорости
        
        user_sessions[user_id] = session
        
    return session


# ─── Функции для работы с настройками пользователя ────────────

async def get_user_settings(user_id: int) -> dict:
    data = await load_completed()
    user_data = data.get(str(user_id), {})
    return user_data.get("settings", {"audio_enabled": False})


async def update_user_setting(user_id: int, setting: str, value: any) -> None:
    data = await load_completed()
    key = str(user_id)
    if key not in data:
        data[key] = {}
    if "settings" not in data[key]:
        data[key]["settings"] = {}
    data[key]["settings"][setting] = value
    await save_completed(data)


# ─── Генерация аудио ──────────────────────────────────────────

async def send_audio_if_enabled(bot: Bot, chat_id: int, user_id: int, text: str) -> int | None:
    """Отправляет аудио, если включено. Берет настройку из ПАМЯТИ."""
    session = await ensure_user_loaded(user_id)
    
    if not session.get("settings", {}).get("audio_enabled", False):
        return None
    
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))
    
    if has_chinese:
        try:
            tts = gTTS(text=text, lang="zh")
            audio_buffer = io.BytesIO()
            tts.write_to_fp(audio_buffer)
            audio_buffer.seek(0)
            
            voice_file = BufferedInputFile(file=audio_buffer.read(), filename="audio.mp3")
            sent_message = await bot.send_voice(chat_id=chat_id, voice=voice_file)
            return sent_message.message_id
        except Exception as e:
            logger.error(f"Ошибка генерации или отправки аудио: {e}")
    
    return None


# ─── Асинхронные функции для работы с облаком ─────────────────

async def load_completed() -> dict:
    if not JSONBIN_BIN_ID:
        logger.warning("JSONBIN_BIN_ID не установлен, загрузка невозможна.")
        return {}
    try:
        logger.info(f"🔄 Загружаем данные из JSONBin: {JSONBIN_BIN_ID}")
        
        # ⚡️ ДОБАВЛЕН ЖЕСТКИЙ ТАЙМАУТ 5 СЕКУНД изменила на 1
        timeout = aiohttp.ClientTimeout(total=1)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(JSONBIN_URL + "/latest", headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info("✅ Данные успешно загружены из JSONBin!")
                    return data.get("record", {})
                else:
                    logger.error(f"❌ Ошибка загрузки: статус {resp.status}")
                    return {}  # Возвращаем пустой словарь, чтобы бот не зависал
                    
    except asyncio.TimeoutError:
        # ⚡️ Если JSONBin молчит больше 5 секунд, мы не ждем, а идем дальше
        logger.warning("⏱️ JSONBin не отвечает (таймаут 5 сек). Работаем с локальными данными.")
        return {}
    except Exception as e:
        logger.error(f"❌ Исключение при загрузке из JSONBin: {e}")
        return {}


async def save_completed(data: dict) -> None:
    if not JSONBIN_BIN_ID:
        logger.error("❌ JSONBIN_BIN_ID не установлен!")
        return
    if not JSONBIN_API_KEY:
        logger.error("❌ JSONBIN_API_KEY не установлен!")
        return
    
    if not data:
        data = {"_cleared": True}
    
    logger.info(f"🔄 Сохраняем данные в JSONBin: {JSONBIN_BIN_ID}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(JSONBIN_URL, json=data, headers=HEADERS) as resp:
                if resp.status == 200:
                    logger.info("✅ Данные успешно сохранены в JSONBin!")
                else:
                    text = await resp.text()
                    logger.error(f"❌ Ошибка сохранения в JSONBin: {resp.status} - {text}")
    except Exception as e:
        logger.error(f"❌ Исключение при сохранении в JSONBin: {e}")


async def get_user_results(user_id: int) -> dict:
    data = await load_completed()
    return data.get(str(user_id), {})


async def mark_completed(user_id: int, lesson_id: str, score: int, total: int, test_type: str = "test") -> None:
    data = await load_completed()
    key = str(user_id)
    if key not in data:
        data[key] = {}
    if test_type not in data[key]:
        data[key][test_type] = {}
    
    if lesson_id not in data[key][test_type]:
        data[key][test_type][lesson_id] = {
            "best_score": score,
            "best_total": total,
            "last_score": score,
            "last_total": total,
            "attempts": 1,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    else:
        record = data[key][test_type][lesson_id]
        record["attempts"] = record.get("attempts", 1) + 1
        record["last_score"] = score
        record["last_total"] = total
        record["date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        current_percent = (score / total * 100) if total > 0 else 0
        best_percent = (record["best_score"] / record["best_total"] * 100) if record["best_total"] > 0 else 0
        
        if current_percent > best_percent:
            record["best_score"] = score
            record["best_total"] = total
    
    await save_completed(data)


# ─── Парсер слов ──────────────────────────────────────────────

def parse_word(word_str: str) -> dict:
    match = re.match(r'^(.+?)\s*\[(.+?)\]\s*(.+)$', word_str)
    if match:
        return {
            "hanzi": match.group(1).strip(),
            "pinyin": match.group(2).strip(),
            "translation": match.group(3).strip(),
        }
    return None


# ─── Вспомогательные функции ──────────────────────────────────

def build_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Мои результаты", callback_data="show_results")],
        [InlineKeyboardButton(text="🎴 Слова", callback_data="show_words")],
        [InlineKeyboardButton(text="🔍 Найти слово", callback_data="search_word")],
        [InlineKeyboardButton(text="✍️ Порядок черт", callback_data="stroke_order_prompt")], # <-- НОВАЯ КНОПКА
        [InlineKeyboardButton(text="🔊 Озвучить текст", callback_data="tts_prompt")],
        [InlineKeyboardButton(text="📖 Словарь", callback_data="show_dictionary")],
        [InlineKeyboardButton(text="📝 Тесты", callback_data="show_tests")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="show_settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_tests_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for lesson_id, lesson in TESTS.items():
        buttons.insert(0, [InlineKeyboardButton(text=lesson["title"], callback_data=f"test_{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_words_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for topic_id, topic_data in WORDS.items():
        buttons.insert(0, [InlineKeyboardButton(text=topic_data["title"], callback_data=f"words_{topic_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_dictionary_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for topic_id, topic_data in WORDS.items():
        buttons.insert(0, [InlineKeyboardButton(text=topic_data["title"], callback_data=f"dict_{topic_id}")])
    buttons.insert(0, [InlineKeyboardButton(text="🔍 Найти слово", callback_data="search_word")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_answer_keyboard(lesson_id: str, q_index: int, options: list[str], prefix: str = "ans") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"{prefix}_{lesson_id}_{q_index}_{i}")] for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def format_results(user_id: int) -> str:
    session = await ensure_user_loaded(user_id)
    results = session.get("progress", {})
    
    if not results or results == {"_cleared": True}:
        return "📊 <b>Ваши результаты</b>\n\nВы ещё не прошли ни одного теста.\n\nВыберите раздел, чтобы начать!"
    
    lines = ["📊 <b>Ваши результаты</b>\n"]
    
    test_results = results.get("test", {})
    if test_results and test_results != {"_cleared": True}:
        lines.append("📝 <b>Тесты:</b>")
        for lesson_id, record in test_results.items():
            if lesson_id == "_cleared": continue
            lesson = TESTS.get(lesson_id)
            if not lesson: continue
            
            best_score = record.get("best_score", "?")
            best_total = record.get("best_total", "?")
            attempts = record.get("attempts", 1)
            date = record.get("date", "неизвестно")
            
            if isinstance(best_score, int) and isinstance(best_total, int):
                percent = round(best_score / best_total * 100)
                emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
                lines.append(f"  {emoji} {lesson['title']}")
                lines.append(f"     Лучший: {best_score}/{best_total} ({percent}%)")
                lines.append(f"     Попыток: {attempts} | Последняя: {date}")
            else:
                lines.append(f"  ✅ {lesson['title']}")
                lines.append(f"     Попыток: {attempts} | Последняя: {date}")
        lines.append("")
    
    words_results = results.get("words", {})
    if words_results and words_results != {"_cleared": True}:
        lines.append("🎴 <b>Изучение слов:</b>")
        for topic_id, record in words_results.items():
            if topic_id == "_cleared": continue
            topic_data = WORDS.get(topic_id)
            if not topic_data: continue
            
            topic_title = topic_data["title"]
            best_score = record.get("best_score", "?")
            best_total = record.get("best_total", "?")
            attempts = record.get("attempts", 1)
            date = record.get("date", "неизвестно")
            
            if isinstance(best_score, int) and isinstance(best_total, int):
                percent = round(best_score / best_total * 100)
                emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
                lines.append(f"  {emoji} {topic_title}")
                lines.append(f"     Лучший: {best_score}/{best_total} ({percent}%)")
                lines.append(f"     Попыток: {attempts} | Последняя: {date}")
            else:
                lines.append(f"  ✅ {topic_title}")
                lines.append(f"     Попыток: {attempts} | Последняя: {date}")
    
    return "\n".join(lines)


# ─── Хендлеры: Главное меню ───────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    
    # ⚡️ Предзагружаем данные в память при старте, чтобы всё летало
    await ensure_user_loaded(user_id)
    
    await message.answer(
        "🇨🇳 <b>Добро пожаловать!</b>\n\n"
        "Это бот для изучения китайского языка.\n\n"
        "💡 Вы можете проходить тесты и изучать слова сколько угодно раз!", 
        reply_markup=build_main_keyboard(), 
        parse_mode="HTML"
    )

@router.callback_query(F.data == "show_results")
async def show_results(callback: CallbackQuery) -> None:
    text = await format_results(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "show_tests")
async def show_tests(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📝 <b>Выберите тест:</b>", reply_markup=build_tests_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "show_words")
async def show_words(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🎴 <b>Выберите тему для изучения слов:</b>", reply_markup=build_words_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "show_dictionary")
async def show_dictionary(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📖 <b>Выберите тему:</b>", reply_markup=build_dictionary_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🇨🇳 <b>Главное меню</b>\n\nВыберите раздел:", reply_markup=build_main_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()

# ─── Команда /speak для озвучки (работает в группах и личных сообщениях) ───

@router.message(Command("speak"))
async def cmd_speak(message: Message) -> None:
    """Озвучивает текст после команды /speak"""
    # Получаем текст после команды
    text = message.text.replace("/speak", "", 1).strip()
    
    if not text:
        await message.answer(
            "🎤 <b>Использование:</b>\n\n"
            "<code>/speak 你好，我叫玛丽亚</code>\n\n"
            "Бот озвучит ваш текст голосом.",
            parse_mode="HTML"
        )
        return
    
    # Проверяем наличие иероглифов
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))
    
    if not has_chinese:
        await message.answer(
            "⚠️ <b>Для озвучки нужен текст с иероглифами.</b>\n\n"
            "Пример: <code>/speak 你好</code>",
            parse_mode="HTML"
        )
        return
    
    # Генерируем аудио
    try:
        tts = gTTS(text=text, lang="zh")
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_buffer.seek(0)
        
        voice_file = BufferedInputFile(file=audio_buffer.read(), filename="audio.mp3")
        await message.answer_voice(voice_file)
    except Exception as e:
        logger.error(f"Ошибка генерации аудио для /speak: {e}")
        await message.answer("⚠️ Не удалось озвучить текст. Попробуйте ещё раз.")


# ─── Хендлеры: Настройки ──────────────────────────────────────

@router.callback_query(F.data == "show_settings")
async def show_settings(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = await ensure_user_loaded(user_id)
    settings = session.get("settings", {"audio_enabled": False})
    audio_status = "✅ Включено" if settings.get("audio_enabled", False) else "❌ Выключено"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔊 Озвучка слов: {audio_status}", callback_data="toggle_audio")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        "🔊 <b>Озвучка слов</b>\n"
        "Если включено, бот будет отправлять голосовое сообщение с произношением правильного ответа сразу после вашего выбора.\n\n"
        f"Текущий статус: {audio_status}",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_audio")
async def toggle_audio(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = await ensure_user_loaded(user_id)
    
    current_status = session.get("settings", {}).get("audio_enabled", False)
    new_status = not current_status
    
    # 1. Мгновенно обновляем в ПАМЯТИ (чтобы бот реагировал без задержек)
    user_sessions[user_id]["settings"]["audio_enabled"] = new_status
    
    # 2. Сохраняем в JSONBin в фоне
    await update_user_setting(user_id, "audio_enabled", new_status)
    
    status_text = "✅ Включено" if new_status else "❌ Выключено"
    await callback.answer(f"Озвучка слов: {status_text}", show_alert=True)
    await show_settings(callback)


# ─── Хендлеры: Поиск слов ─────────────────────────────────────

@router.callback_query(F.data == "search_word")
async def search_word_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user_sessions[user_id] = {"type": "search"}
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_search")]
    ])
    
    await callback.message.edit_text(
        "🔍 <b>Поиск слова</b>\n\n"
        "Введите иероглиф, пиньинь или перевод слова, которое хотите найти.\n\n"
        "Например: <code>猫</code>, <code>māo</code> или <code>кошка</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get("type") == "search":
        del user_sessions[user_id]
    
    await callback.message.edit_text(
        "📖 <b>Выберите тему:</b>", 
        reply_markup=build_dictionary_keyboard(), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message) -> None:
    """Обрабатывает ввод пользователя для поиска или озвучки."""
    
    # ─── НОВОЕ: Игнорируем обычные сообщения в группах ─────────
    if message.chat.type in ["group", "supergroup"]:
        return  # Выходим, не обрабатываем
    # ────────────────────────────────────────────────────────────
    
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    
    # Если пользователь не в режиме поиска или озвучки, игнорируем
    if not session:
        return
    
    
    query = message.text.strip()
    
    if not query:
        await message.answer("⚠️ Введите текст.")
        return
    
    # ─── Режим озвучки ────────────────────────────────────────
    if session.get("type") == "tts":
        # Проверяем наличие иероглифов
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', query))
        
        if not has_chinese:
            await message.answer(
                "⚠️ <b>Для озвучки нужен текст с иероглифами.</b>\n\n"
                "Введите фразу на китайском языке, например:\n"
                "<code>你好，我叫玛丽亚</code>",
                parse_mode="HTML"
            )
            return
        
        # Отправляем голосовое сообщение
        try:
            tts = gTTS(text=query, lang="zh")
            audio_buffer = io.BytesIO()
            tts.write_to_fp(audio_buffer)
            audio_buffer.seek(0)
            
            voice_file = BufferedInputFile(file=audio_buffer.read(), filename="audio.mp3")
            await message.answer_voice(voice_file)
            
            # Предлагаем ввести ещё или отменить
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]
            ])
            await message.answer(
                "✅ Готово! Введите ещё одну фразу или нажмите кнопку ниже.",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Ошибка генерации аудио для TTS: {e}")
            await message.answer("⚠️ Не удалось озвучить текст. Попробуйте ещё раз.")
        
        return

        # ─── Режим: Порядок черт ──────────────────────────────────
    if session.get("type") == "stroke_order":
        # Берем только первый иероглиф, если ввели фразу (Hanzi Writer лучше работает с 1-2 символами)
        char_to_train = query[0] 
        
        # Пытаемся найти пиньинь и перевод в нашей базе
        pinyin = "pīnyīn"
        translation = "иероглиф"
        
        for topic_data in WORDS.values():
            for word_str in topic_data["words"]:
                parsed = parse_word(word_str)
                if parsed and parsed["hanzi"] == char_to_train:
                    pinyin = parsed["pinyin"]
                    translation = parsed["translation"]
                    break
            if pinyin != "pīnyīn":
                break
        
        # Формируем URL для Web App
        import urllib.parse
        web_app_url = (
            f"https://daryawhile.github.io/chinese_test_bot/?"
            f"char={urllib.parse.quote(char_to_train)}&"
            f"pinyin={urllib.parse.quote(pinyin)}&"
            f"translation={urllib.parse.quote(translation)}"
        )
        
        # Очищаем сессию
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        # Отправляем сообщение с кнопкой Web App
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Открыть", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]
        ])
        
        await message.answer(
            f"🎯 Иероглиф: <b>{char_to_train}</b>\n"
            f"Пиньинь: <code>{pinyin}</code>\n"
            f"Перевод: {translation}\n\n"
            f"Нажмите кнопку ниже, чтобы открыть интерактивный холст!",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return
        
    # ─── Режим поиска ─────────────────────────────────────────
    if session.get("type") == "search":
        query = query.lower()
        
        found_words = []
        for topic_id, topic_data in WORDS.items():
            for word_str in topic_data["words"]:
                parsed = parse_word(word_str)
                if not parsed:
                    continue
                
                if (query in parsed["hanzi"].lower() or 
                    query in parsed["pinyin"].lower() or 
                    query in parsed["translation"].lower()):
                    found_words.append({
                        "topic": topic_data["title"],
                        "word": parsed
                    })
        
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        if not found_words:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать ещё", callback_data="search_word")],
                [InlineKeyboardButton(text="🔙 К словарю", callback_data="show_dictionary")]
            ])
            await message.answer(
                "😔 <b>Слово не найдено</b>\n\n"
                "Проверьте правильность ввода или возможно это слово мы ещё не изучали.\n\n"
                "Попробуйте поискать в разделе <b>📖 Словарь</b>.",
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        
        lines = [f"🔍 <b>Найдено: {len(found_words)} слов(а)</b>\n"]
        for item in found_words[:10]:
            lines.append(f"• <b>{item['word']['hanzi']}</b> [{item['word']['pinyin']}] — {item['word']['translation']}")
            lines.append(f"  <i>(Тема: {item['topic']})</i>\n")
        
        if len(found_words) > 10:
            lines.append(f"... и ещё {len(found_words) - 10} слов")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Искать ещё", callback_data="search_word")],
            [InlineKeyboardButton(text="🔙 К словарю", callback_data="show_dictionary")]
        ])
        
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")

# ─── Хендлеры: Озвучка текста ─────────────────────────────────

@router.callback_query(F.data == "tts_prompt")
async def tts_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user_sessions[user_id] = {"type": "tts"}
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_tts")]
    ])
    
    await callback.message.edit_text(
        "🎤 <b>Озвучка текста</b>\n\n"
        "Введите фразу на китайском языке (с иероглифами).\n\n"
        "Например: <code>你好，我叫玛丽亚</code>\n\n"
        "Бот озвучит ваш текст голосом.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_tts")
async def cancel_tts(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get("type") == "tts":
        del user_sessions[user_id]
    
    await callback.message.edit_text(
        "🇨🇳 <b>Главное меню</b>\n\nВыберите раздел:", 
        reply_markup=build_main_keyboard(), 
        parse_mode="HTML"
    )
    await callback.answer()
    
# ─── Хендлеры: Порядок черт (Web App) ────────────────────────

@router.callback_query(F.data == "stroke_order_prompt")
async def stroke_order_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user_sessions[user_id] = {"type": "stroke_order"}
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(
        "✍️ <b>Порядок черт</b>\n\n"
        "Введите <b>один иероглиф</b> или короткое слово, которое хотите потренировать.\n\n"
        "Например: <code>猫</code> или <code>谢</code>\n",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "search_word")
async def prompt_search(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user_sessions[user_id] = {"type": "search"}
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(
        "🔍 <b>Поиск слова</b>\n\n"
        "Введите иероглиф, пиньинь или перевод на русском.\n\n"
        "Например: <code>猫</code>, <code>māo</code> или <code>кошка</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()
    
# ─── Хендлеры: ТЕСТЫ ─────────────────────────────────────────

@router.callback_query(F.data.startswith("test_lesson_"))
async def start_test(callback: CallbackQuery) -> None:
    lesson_id = callback.data.removeprefix("test_")
    user_id = callback.from_user.id
    
    if lesson_id not in TESTS:
        await callback.answer("Тест не найден.", show_alert=True)
        return
    
    questions = [q.copy() for q in TESTS[lesson_id]["questions"]]
    random.shuffle(questions)
    
    for q in questions:
        options_with_correct = [(opt, i == q["correct"]) for i, opt in enumerate(q["options"])]
        random.shuffle(options_with_correct)
        q["shuffled_options"] = [opt for opt, _ in options_with_correct]
        q["shuffled_correct"] = next(i for i, (_, is_correct) in enumerate(options_with_correct) if is_correct)
    
    user_sessions[user_id] = {
        "type": "test", "lesson": lesson_id, "question": 0, "score": 0, "shuffled_questions": questions
    }
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(
        f"📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос 1 из {total}\n\n❓ {q['text']}", 
        reply_markup=build_answer_keyboard(lesson_id, 0, q["shuffled_options"], "testans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("testans_"))
async def handle_test_answer(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    option_index, q_index = int(parts[-1]), int(parts[-2])
    lesson_id = "_".join(parts[1:-2])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "test" or session["lesson"] != lesson_id or session["question"] != q_index:
        await callback.answer("⚠️ Неактуальный вопрос.", show_alert=True)
        return

    questions = session["shuffled_questions"]
    question = questions[q_index]
    total = len(questions)
    
    is_correct = option_index == question["shuffled_correct"]
    correct_answer_text = question["shuffled_options"][question["shuffled_correct"]]
    
    feedback = "✅ <b>Правильно!</b>" if is_correct else f"❌ <b>Неправильно.</b>\nВерный ответ: <b>{correct_answer_text}</b>"
    
    if is_correct: 
        session["score"] += 1
        
    # Отправляем аудио правильного ответа, если включено
    audio_msg_id = await send_audio_if_enabled(callback.bot, callback.message.chat.id, user_id, correct_answer_text)
    if audio_msg_id:
        session["audio_message_id"] = audio_msg_id
        
    next_q = q_index + 1

    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"test_next_{lesson_id}_{next_q}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n💡 <b>Правильный ответ:</b>\n{correct_answer_text}\n\nНажмите кнопку ниже, чтобы продолжить.", 
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Показать результаты", callback_data=f"test_finish_{lesson_id}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n💡 <b>Правильный ответ:</b>\n{correct_answer_text}\n\nНажмите кнопку ниже, чтобы увидеть результаты.", 
            reply_markup=kb, parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("test_next_"))
async def handle_test_next(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    next_q = int(parts[-1])
    lesson_id = "_".join(parts[2:-1])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "test" or session["lesson"] != lesson_id:
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    questions = session["shuffled_questions"]
    q = questions[next_q]
    total = len(questions)
    session["question"] = next_q

    # Удаляем предыдущее голосовое сообщение, если оно было
    audio_msg_id = session.pop("audio_message_id", None)  # <-- ЕДИНСТВЕННОЕ ЧИСЛО
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass

    await callback.message.edit_text(
        f"📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос {next_q + 1} из {total}\n\n❓ {q['text']}", 
        reply_markup=build_answer_keyboard(lesson_id, next_q, q["shuffled_options"], "testans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("test_finish_"))
async def handle_test_finish(callback: CallbackQuery) -> None:
    lesson_id = callback.data.removeprefix("test_finish_")
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "test":
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    score = session["score"]
    total = len(session["shuffled_questions"])
    await mark_completed(user_id, lesson_id, score, total, "test")
    
    # Удаляем предыдущее голосовое сообщение, если оно было
    audio_msg_id = session.pop("audio_message_id", None)  # <-- ЕДИНСТВЕННОЕ ЧИСЛО
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
    
    if user_id in user_sessions:
        del user_sessions[user_id]
        
    percent = round(score / total * 100)
    emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]])
    await callback.message.edit_text(
        f"{emoji} <b>Тест завершён!</b>\n\n📖 {TESTS[lesson_id]['title']}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)\n\n💡 Вы можете пройти этот тест снова, чтобы улучшить результат!", 
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


# ─── Хендлеры: ИЗУЧЕНИЕ СЛОВ ─────────────────────────────────

@router.callback_query(F.data.startswith("words_"))
async def start_words(callback: CallbackQuery) -> None:
    topic_id = callback.data.removeprefix("words_")
    user_id = callback.from_user.id
    
    if topic_id not in WORDS:
        await callback.answer("Тема не найдена.", show_alert=True)
        return
    
    words_list = WORDS[topic_id]["words"]
    topic_title = WORDS[topic_id]["title"]
    
    words = [parse_word(w) for w in words_list]
    words = [w for w in words if w is not None]
    
    if len(words) < 4:
        await callback.answer("Недостаточно слов для теста.", show_alert=True)
        return
    
    questions = []
    for _ in range(min(10, len(words))):
        correct_word = random.choice(words)
        question_type = random.choice(["hanzi", "pinyin", "translation"])
        
        if question_type == "hanzi":
            question_text = correct_word["hanzi"]
            answer_type = random.choice(["pinyin", "translation"])
        elif question_type == "pinyin":
            question_text = correct_word["pinyin"]
            answer_type = random.choice(["hanzi", "translation"])
        else:
            question_text = correct_word["translation"]
            answer_type = random.choice(["hanzi", "pinyin"])
        
        wrong_words = [w for w in words if w != correct_word]
        random.shuffle(wrong_words)
        wrong_words = wrong_words[:3]
        
        options = [correct_word[answer_type]] + [w[answer_type] for w in wrong_words]
        random.shuffle(options)
        correct_index = options.index(correct_word[answer_type])
        
        questions.append({
            "question_text": question_text, "question_type": question_type, "answer_type": answer_type,
            "options": options, "correct": correct_index, "all_words": [correct_word] + wrong_words,
        })
    
    user_sessions[user_id] = {
        "type": "words", "lesson": topic_id, "question": 0, "score": 0, "questions": questions
    }
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(
        f"🎴 <b>{topic_title}</b>\nВопрос 1 из {total}\n\n❓ {q['question_text']}", 
        reply_markup=build_answer_keyboard(topic_id, 0, q["options"], "wordans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wordans_"))
async def handle_word_answer(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    option_index, q_index = int(parts[-1]), int(parts[-2])
    topic_id = "_".join(parts[1:-2])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words" or session["lesson"] != topic_id or session["question"] != q_index:
        await callback.answer("⚠️ Неактуальный вопрос.", show_alert=True)
        return

    questions = session["questions"]
    question = questions[q_index]
    total = len(questions)
    
    is_correct = option_index == question["correct"]
    
    # Находим правильное слово
    correct_word = next(w for w in question["all_words"] if w[question["answer_type"]] == question["options"][question["correct"]])
    
    # Находим слово, которое выбрал пользователь
    selected_word = next(w for w in question["all_words"] if w[question["answer_type"]] == question["options"][option_index])
    
    feedback = "✅ <b>Правильно!</b>" if is_correct else f"❌ <b>Неправильно.</b>\nВаш ответ: <b>{question['options'][option_index]}</b>\nВерный ответ: <b>{question['options'][question['correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
    
        # Отправляем аудио иероглифа правильного слова
    audio_msg_id = await send_audio_if_enabled(callback.bot, callback.message.chat.id, user_id, correct_word["hanzi"])
    if audio_msg_id:
        session["audio_message_id"] = audio_msg_id
    
    # Формируем разбор
    if is_correct:
        word_breakdown = f"💡 <b>Разбор слова:</b>\n{correct_word['hanzi']} [{correct_word['pinyin']}] — {correct_word['translation']}"
    else:
        word_breakdown = (
            f"💡 <b>Разбор слов:</b>\n\n"
            f"❌ <b>Ваш ответ:</b>\n{selected_word['hanzi']} [{selected_word['pinyin']}] — {selected_word['translation']}\n\n"
            f"✅ <b>Правильный ответ:</b>\n{correct_word['hanzi']} [{correct_word['pinyin']}] — {correct_word['translation']}"
        )
    
    next_q = q_index + 1

    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"word_next_{topic_id}_{next_q}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n{word_breakdown}\n\nНажмите кнопку ниже, чтобы продолжить.", 
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Показать результаты", callback_data=f"word_finish_{topic_id}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n{word_breakdown}\n\nНажмите кнопку ниже, чтобы увидеть результаты.", 
            reply_markup=kb, parse_mode="HTML"
        )
    await callback.answer()

@router.callback_query(F.data.startswith("word_next_"))
async def handle_word_next(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    next_q = int(parts[-1])
    topic_id = "_".join(parts[2:-1])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words" or session["lesson"] != topic_id:
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    questions = session["questions"]
    q = questions[next_q]
    total = len(questions)
    session["question"] = next_q
    
    # Удаляем предыдущее голосовое сообщение, если оно было
    audio_msg_id = session.pop("audio_message_id", None)  # <-- ЕДИНСТВЕННОЕ ЧИСЛО
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
    
    topic_title = WORDS[topic_id]["title"]

    await callback.message.edit_text(
        f"🎴 <b>{topic_title}</b>\nВопрос {next_q + 1} из {total}\n\n❓ {q['question_text']}", 
        reply_markup=build_answer_keyboard(topic_id, next_q, q["options"], "wordans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("word_finish_"))
async def handle_word_finish(callback: CallbackQuery) -> None:
    topic_id = callback.data.removeprefix("word_finish_")
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words":
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    score = session["score"]
    total = len(session["questions"])
    await mark_completed(user_id, topic_id, score, total, "words")
    
    # Удаляем предыдущее голосовое сообщение, если оно было
    audio_msg_id = session.pop("audio_message_id", None)  # <-- ЕДИНСТВЕННОЕ ЧИСЛО
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
    
    if user_id in user_sessions:
        del user_sessions[user_id]
        
    percent = round(score / total * 100)
    emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
    topic_title = WORDS[topic_id]["title"]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]])
    await callback.message.edit_text(
        f"{emoji} <b>Изучение слов завершено!</b>\n\n🎴 {topic_title}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)\n\n💡 Вы можете пройти эту тему снова!", 
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


# ─── Хендлеры: СЛОВАРЬ ───────────────────────────────────────

@router.callback_query(F.data.startswith("dict_"))
async def show_dict_lesson(callback: CallbackQuery) -> None:
    topic_id = callback.data.removeprefix("dict_")
    
    if topic_id not in WORDS:
        await callback.answer("Тема не найдена.", show_alert=True)
        return
    
    words_list = WORDS[topic_id]["words"]
    topic_title = WORDS[topic_id]["title"]
    
    lines = [f"📖 <b>{topic_title}</b>\n"]
    for i, word_str in enumerate(words_list, 1):
        parsed = parse_word(word_str)
        if parsed:
            lines.append(f"{i}. {parsed['hanzi']} [{parsed['pinyin']}] - {parsed['translation']}")
    
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к темам", callback_data="show_dictionary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ─── АДМИН-ПАНЕЛЬ ────────────────────────────────────────────

@router.message(Command("setadmin1234"))
async def cmd_set_admin(message: Message) -> None:
    user_id = message.from_user.id
    admin_users.add(user_id)
    await message.answer("✅ <b>Режим администратора включён!</b>\n\nТеперь вам доступна команда /admin_stats\n\nДля отключения используйте /setuser1234", parse_mode="HTML")

@router.message(Command("setuser1234"))
async def cmd_set_user(message: Message) -> None:
    user_id = message.from_user.id
    if user_id in admin_users:
        admin_users.remove(user_id)
    await message.answer("✅ <b>Режим администратора выключен.</b>\n\nТеперь вы обычный пользователь.\n\nДля включения используйте /setadmin1234", parse_mode="HTML")

@router.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message) -> None:
    user_id = message.from_user.id
    if user_id not in admin_users:
        await message.answer("⛔ У вас нет доступа к этой команде.\n\nИспользуйте /setadmin1234 для включения режима администратора.", parse_mode="HTML")
        return
    
    data = await load_completed()
    total_users = len([k for k in data.keys() if k != "_cleared"])
    total_tests_completed = 0
    total_words_completed = 0
    topic_stats = {}
    
    for uid, user_data in data.items():
        if uid == "_cleared": continue
        
        test_results = user_data.get("test", {})
        for lesson_id, record in test_results.items():
            if lesson_id == "_cleared": continue
            total_tests_completed += record.get("attempts", 1)
            lesson = TESTS.get(lesson_id)
            if lesson:
                topic_stats[lesson["title"]] = topic_stats.get(lesson["title"], 0) + record.get("attempts", 1)
        
        words_results = user_data.get("words", {})
        for topic_id, record in words_results.items():
            if topic_id == "_cleared": continue
            total_words_completed += record.get("attempts", 1)
            topic_data = WORDS.get(topic_id)
            if topic_data:
                topic_stats[topic_data["title"]] = topic_stats.get(topic_data["title"], 0) + record.get("attempts", 1)
    
    lines = [
        "👑 <b>Админ-панель</b>\n",
        f"👥 Всего пользователей: <b>{total_users}</b>",
        f"📝 Пройдено тестов: <b>{total_tests_completed}</b>",
        f"🎴 Пройдено тем слов: <b>{total_words_completed}</b>\n",
    ]
    
    if topic_stats:
        lines.append("🔥 <b>Топ-5 популярных тем:</b>")
        sorted_topics = sorted(topic_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        for i, (topic_name, count) in enumerate(sorted_topics, 1):
            lines.append(f"  {i}. {topic_name} — {count} прохождений")
    else:
        lines.append("📊 Статистика по темам пока пуста.")
    
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── СЛУЖЕБНЫЕ КОМАНДЫ ───────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user_id = str(message.from_user.id)
    data = await load_completed()
    if user_id in data:
        del data[user_id]
        await save_completed(data)
        if message.from_user.id in user_sessions:
            del user_sessions[message.from_user.id]
        await message.answer("🔄 <b>Ваши результаты успешно сброшены!</b>", parse_mode="HTML")
    else:
        await message.answer("⚠️ Для этого аккаунта нет сохраненных результатов.", parse_mode="HTML")

@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        await message.answer(f"✅ <b>Ключи найдены!</b>\n\nBin ID: {JSONBIN_BIN_ID[:10]}...\nAPI Key: {JSONBIN_API_KEY[:10]}...", parse_mode="HTML")
    else:
        await message.answer("❌ <b>Ключи НЕ найдены!</b>\n\nПроверьте Environment в Render.", parse_mode="HTML")

@router.message(Command("testsave"))
async def cmd_testsave(message: Message) -> None:
    logger.info("🚀 ЗАПУЩЕНА КОМАНДА /testsave")
    await message.answer("⏳ Тестирую сохранение... Смотрите логи Render!")
    user_id = str(message.from_user.id)
    test_data = {user_id: {"test": {"test_lesson": {"score": 99, "total": 100, "date": "TEST_MODE"}}}}
    await save_completed(test_data)
    logger.info("🏁 КОМАНДА /testsave ЗАВЕРШЕНА")
    await message.answer("✅ Готово! Проверьте логи Render.")


# ─── ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ───────────────────────────

@router.errors()
async def handle_errors(event: ErrorEvent, bot: Bot):
    exception = event.exception
    if isinstance(exception, TelegramBadRequest):
        if "message is not modified" in str(exception):
            return True
    return False
