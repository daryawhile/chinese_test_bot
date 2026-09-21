import os
import logging
import random
import re
import io
import asyncpg
import json
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent, BufferedInputFile, WebAppInfo
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest
from gtts import gTTS
from tests_data import TESTS, WORDS
from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
user_sessions: dict[int, dict] = {}

# ─── ПОДКЛЮЧЕНИЕ К NEON (POSTGRESQL) ────────────────────────
db_pool = None

async def init_db():
    """Инициализирует подключение к Neon."""
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("❌ DATABASE_URL не найден в переменных окружения!")
        return
    
    db_pool = await asyncpg.create_pool(db_url)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR PRIMARY KEY,
                data JSONB DEFAULT '{"settings": {"audio_enabled": false}, "progress": {}}'::jsonb,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    logger.info("✅ Таблица users в Neon проверена/создана.")

async def get_user_data(user_id: int) -> dict:
    """Получает данные пользователя из Neon."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT data FROM users WHERE user_id = $1', str(user_id))
        if row:
            # asyncpg сам превратит JSONB из базы обратно в словарь Python
            return row['data']
        else:
            default_data = {"settings": {"audio_enabled": False}, "progress": {}}
            # ⚡️ ИСПРАВЛЕНИЕ: Превращаем словарь в JSON-строку через json.dumps()
            await conn.execute(
                'INSERT INTO users (user_id, data) VALUES ($1, $2)', 
                str(user_id), 
                json.dumps(default_data)
            )
            return default_data

async def save_user_data(user_id: int, data: dict) -> None:
    """Сохраняет (или обновляет) данные пользователя в Neon."""
    async with db_pool.acquire() as conn:
        # ⚡️ ИСПРАВЛЕНИЕ: Превращаем словарь в JSON-строку через json.dumps()
        await conn.execute(
            '''
            INSERT INTO users (user_id, data) 
            VALUES ($1, $2)
            ON CONFLICT (user_id) 
            DO UPDATE SET data = EXCLUDED.data, updated_at = CURRENT_TIMESTAMP
            ''',
            str(user_id), 
            json.dumps(data)
        )

# ─── УМНАЯ ЗАГРУЗКА ДАННЫХ (ЛЕНИВАЯ) ────────────────────────
async def ensure_user_loaded(user_id: int) -> dict:
    """Проверяет память. Если данных нет, загружает из Neon."""
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    
    session = user_sessions[user_id]
    if "settings" not in session or "progress" not in session:
        user_data = await get_user_data(user_id)
        session["settings"] = user_data.get("settings", {"audio_enabled": False})
        session["progress"] = user_data
        
    return session

# ── Админ-панель ─────────────────────────────────────────────
admin_users: set[int] = set()

# ─── Функции для работы с настройками пользователя ────────────
async def update_user_setting(user_id: int, setting: str, value: any) -> None:
    """Обновляет одну настройку пользователя."""
    data = await get_user_data(user_id)
    if "settings" not in data:
        data["settings"] = {}
    data["settings"][setting] = value
    await save_user_data(user_id, data)

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

async def mark_completed(user_id: int, lesson_id: str, score: int, total: int, test_type: str = "test") -> None:
    """Сохраняет результат теста или изучения слов."""
    data = await get_user_data(user_id)
    if test_type not in data:
        data[test_type] = {}
    
    if lesson_id not in data[test_type]:
        data[test_type][lesson_id] = {
            "best_score": score, "best_total": total, "last_score": score,
            "last_total": total, "attempts": 1, "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    else:
        record = data[test_type][lesson_id]
        record["attempts"] = record.get("attempts", 1) + 1
        record["last_score"] = score
        record["last_total"] = total
        record["date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        current_percent = (score / total * 100) if total > 0 else 0
        best_percent = (record["best_score"] / record["best_total"] * 100) if record["best_total"] > 0 else 0
        if current_percent > best_percent:
            record["best_score"] = score
            record["best_total"] = total
            
    await save_user_data(user_id, data)

# ─── Парсер слов ──────────────────────────────────────────────
def parse_word(word_str: str) -> dict:
    match = re.match(r'^(.+?)\s*\[(.+?)\]\s*(.+)$', word_str)
    if match:
        return {"hanzi": match.group(1).strip(), "pinyin": match.group(2).strip(), "translation": match.group(3).strip()}
    return None

# ─── Вспомогательные функции ──────────────────────────────────
def build_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Мои результаты", callback_data="show_results")],
        [InlineKeyboardButton(text="🎴 Слова", callback_data="show_words")],
        [InlineKeyboardButton(text=" Найти слово", callback_data="search_word")],
        [InlineKeyboardButton(text="✍️ Порядок черт", callback_data="stroke_order_prompt")],
        [InlineKeyboardButton(text="🔊 Озвучить текст", callback_data="tts_prompt")],
        [InlineKeyboardButton(text=" Словарь", callback_data="show_dictionary")],
        [InlineKeyboardButton(text="📝 Тесты", callback_data="show_tests")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="show_settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_tests_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=" Назад", callback_data="back_to_main")]]
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
    logger.info(f"🚀 ПОЛУЧЕНА КОМАНДА /start от пользователя {message.from_user.id}")
    user_id = message.from_user.id
    try:
        await ensure_user_loaded(user_id)
        logger.info("✅ Данные из Neon успешно загружены в память")
        await message.answer(
            "🇨🇳 <b>Добро пожаловать!</b>\n\n"
            "Это бот для изучения китайского языка.\n\n"
            "💡 Вы можете проходить тесты и изучать слова сколько угодно раз!", 
            reply_markup=build_main_keyboard(), 
            parse_mode="HTML"
        )
        logger.info(" СООБЩЕНИЕ /start УСПЕШНО ОТПРАВЛЕНО!")
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА в команде /start: {e}", exc_info=True)
        await message.answer("⚠️ Произошла техническая ошибка. Попробуйте еще раз.")

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

# ─── Команда /speak ─────────────────────────────────────────
@router.message(Command("speak"))
async def cmd_speak(message: Message) -> None:
    text = message.text.replace("/speak", "", 1).strip()
    if not text:
        await message.answer("🎤 <b>Использование:</b>\n\n<code>/speak 你好，我叫玛丽亚</code>", parse_mode="HTML")
        return
    if not bool(re.search(r'[\u4e00-\u9fff]', text)):
        await message.answer("⚠️ <b>Для озвучки нужен текст с иероглифами.</b>\n\nПример: <code>/speak 你好</code>", parse_mode="HTML")
        return
    try:
        tts = gTTS(text=text, lang="zh")
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_buffer.seek(0)
        voice_file = BufferedInputFile(file=audio_buffer.read(), filename="audio.mp3")
        await message.answer_voice(voice_file)
    except Exception as e:
        logger.error(f"Ошибка генерации аудио для /speak: {e}")
        await message.answer("⚠️ Не удалось озвучить текст.")

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
        "⚙️ <b>Настройки</b>\n\n <b>Озвучка слов</b>\nЕсли включено, бот будет отправлять голосовое сообщение с произношением.\n\n"
        f"Текущий статус: {audio_status}",
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "toggle_audio")
async def toggle_audio(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = await ensure_user_loaded(user_id)
    current_status = session.get("settings", {}).get("audio_enabled", False)
    new_status = not current_status
    
    user_sessions[user_id]["settings"]["audio_enabled"] = new_status
    await update_user_setting(user_id, "audio_enabled", new_status)
    
    status_text = "✅ Включено" if new_status else "❌ Выключено"
    await callback.answer(f"Озвучка слов: {status_text}", show_alert=True)
    await show_settings(callback)

# ─── Хендлеры: Поиск и Озвучка ────────────────────────────────
@router.callback_query(F.data == "search_word")
async def search_word_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = user_sessions.setdefault(user_id, {})
    session["type"] = "search"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_search")]])
    await callback.message.edit_text("🔍 <b>Поиск слова</b>\n\nВведите иероглиф, пиньинь или перевод.", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get("type") == "search":
        del user_sessions[user_id]
    await callback.message.edit_text("📖 <b>Выберите тему:</b>", reply_markup=build_dictionary_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "tts_prompt")
async def tts_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = user_sessions.setdefault(user_id, {})
    session["type"] = "tts"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_tts")]])
    await callback.message.edit_text(" <b>Озвучка текста</b>\n\nВведите фразу на китайском языке.", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "cancel_tts")
async def cancel_tts(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get("type") == "tts":
        del user_sessions[user_id]
    await callback.message.edit_text("🇨 <b>Главное меню</b>", reply_markup=build_main_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "stroke_order_prompt")
async def stroke_order_prompt(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = user_sessions.setdefault(user_id, {})
    session["type"] = "stroke_order"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_main")]])
    await callback.message.edit_text("✍️ <b>Порядок черт</b>\n\nВведите <b>один иероглиф</b>.", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message) -> None:
    if message.chat.type in ["group", "supergroup"]:
        return
    
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    if not session:
        return
    
    query = message.text.strip()
    if not query:
        await message.answer("⚠️ Введите текст.")
        return

    if session.get("type") == "tts":
        if not bool(re.search(r'[\u4e00-\u9fff]', query)):
            await message.answer("️ <b>Для озвучки нужен текст с иероглифами.</b>", parse_mode="HTML")
            return
        try:
            tts = gTTS(text=query, lang="zh")
            audio_buffer = io.BytesIO()
            tts.write_to_fp(audio_buffer)
            audio_buffer.seek(0)
            voice_file = BufferedInputFile(file=audio_buffer.read(), filename="audio.mp3")
            await message.answer_voice(voice_file)
            await message.answer("✅ Готово!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]]))
        except Exception as e:
            logger.error(f"Ошибка TTS: {e}")
        return

    if session.get("type") == "stroke_order":
        char_to_train = query[0]
        pinyin, translation = "pīnyīn", "иероглиф"
        for topic_data in WORDS.values():
            for word_str in topic_data["words"]:
                parsed = parse_word(word_str)
                if parsed and parsed["hanzi"] == char_to_train:
                    pinyin, translation = parsed["pinyin"], parsed["translation"]
                    break
            if pinyin != "pīnyīn":
                break
        
        import urllib.parse
        web_app_url = f"https://daryawhile.github.io/chinese_test_bot/?char={urllib.parse.quote(char_to_train)}&pinyin={urllib.parse.quote(pinyin)}&translation={urllib.parse.quote(translation)}"
        
        if user_id in user_sessions:
            del user_sessions[user_id]
            
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Открыть", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton(text=" В главное меню", callback_data="back_to_main")]
        ])
        await message.answer(f"🎯 Иероглиф: <b>{char_to_train}</b>\nПиньинь: <code>{pinyin}</code>\nПеревод: {translation}", reply_markup=kb, parse_mode="HTML")
        return

    if session.get("type") == "search":
        query = query.lower()
        found_words = []
        for topic_id, topic_data in WORDS.items():
            for word_str in topic_data["words"]:
                parsed = parse_word(word_str)
                if not parsed: continue
                if query in parsed["hanzi"].lower() or query in parsed["pinyin"].lower() or query in parsed["translation"].lower():
                    found_words.append({"topic": topic_data["title"], "word": parsed})
        
        if user_id in user_sessions:
            del user_sessions[user_id]
            
        if not found_words:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Искать ещё", callback_data="search_word")], [InlineKeyboardButton(text="🔙 К словарю", callback_data="show_dictionary")]])
            await message.answer("😔 <b>Слово не найдено</b>", reply_markup=kb, parse_mode="HTML")
            return
            
        lines = [f"🔍 <b>Найдено: {len(found_words)} слов(а)</b>\n"]
        for item in found_words[:10]:
            lines.append(f"• <b>{item['word']['hanzi']}</b> [{item['word']['pinyin']}] — {item['word']['translation']}")
            lines.append(f"  <i>(Тема: {item['topic']})</i>\n")
            
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Искать ещё", callback_data="search_word")], [InlineKeyboardButton(text="🔙 К словарю", callback_data="show_dictionary")]])
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")

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
        
    session = user_sessions.setdefault(user_id, {})
    session.update({"type": "test", "lesson": lesson_id, "question": 0, "score": 0, "shuffled_questions": questions})
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(f"📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос 1 из {total}\n\n❓ {q['text']}", reply_markup=build_answer_keyboard(lesson_id, 0, q["shuffled_options"], "testans"), parse_mode="HTML")
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
        
    audio_msg_id = await send_audio_if_enabled(callback.bot, callback.message.chat.id, user_id, correct_answer_text)
    if audio_msg_id:
        session["audio_message_id"] = audio_msg_id
        
    next_q = q_index + 1
    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"test_next_{lesson_id}_{next_q}")]])
        await callback.message.edit_text(f"{feedback}\n\n💡 <b>Правильный ответ:</b>\n{correct_answer_text}", reply_markup=kb, parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=" Показать результаты", callback_data=f"test_finish_{lesson_id}")]])
        await callback.message.edit_text(f"{feedback}\n\n💡 <b>Правильный ответ:</b>\n{correct_answer_text}", reply_markup=kb, parse_mode="HTML")
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
    
    audio_msg_id = session.pop("audio_message_id", None)
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
            
    await callback.message.edit_text(f"📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос {next_q + 1} из {total}\n\n❓ {q['text']}", reply_markup=build_answer_keyboard(lesson_id, next_q, q["shuffled_options"], "testans"), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("test_finish_"))
async def handle_test_finish(callback: CallbackQuery) -> None:
    logger.info(f"🏁 НАЖАТА КНОПКА 'ПОКАЗАТЬ РЕЗУЛЬТАТЫ' пользователем {callback.from_user.id}")
    lesson_id = callback.data.removeprefix("test_finish_")
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "test":
        logger.warning(f"⚠️ Сессия не найдена. Текущая сессия: {session}")
        await callback.answer("⚠️ Сессия не найдена. Начните тест заново.", show_alert=True)
        return
        
    score = session.get("score", 0)
    total = len(session.get("shuffled_questions", []))
    
    logger.info(f"💾 Сохраняем результат: {score}/{total} для урока {lesson_id}")
    await mark_completed(user_id, lesson_id, score, total, "test")
    
    audio_msg_id = session.pop("audio_message_id", None)
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
            
    if user_id in user_sessions:
        temp_session = user_sessions[user_id]
        for key in ["type", "lesson", "question", "score", "shuffled_questions", "questions", "audio_message_id"]:
            temp_session.pop(key, None)
            
    percent = round(score / total * 100) if total > 0 else 0
    emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
    
    # ⚡️ БЕЗОПАСНОЕ получение названия, чтобы избежать скрытых ошибок
    lesson_title = TESTS.get(lesson_id, {}).get("title", "Неизвестный тест")
    text_to_send = f"{emoji} <b>Тест завершён!</b>\n\n📖 {lesson_title}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)"
    
    logger.info(f"📤 Пытаемся обновить сообщение текстом:\n{text_to_send}")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]])
    
    try:
        await callback.message.edit_text(text_to_send, reply_markup=kb, parse_mode="HTML")
        logger.info("✅ Сообщение успешно обновлено!")
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении сообщения: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка при показе результатов.", show_alert=True)
        
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
        
    session = user_sessions.setdefault(user_id, {})
    session.update({"type": "words", "lesson": topic_id, "question": 0, "score": 0, "questions": questions})
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(f"🎴 <b>{topic_title}</b>\nВопрос 1 из {total}\n\n❓ {q['question_text']}", reply_markup=build_answer_keyboard(topic_id, 0, q["options"], "wordans"), parse_mode="HTML")
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
    
    correct_word = next(w for w in question["all_words"] if w[question["answer_type"]] == question["options"][question["correct"]])
    selected_word = next(w for w in question["all_words"] if w[question["answer_type"]] == question["options"][option_index])
    feedback = "✅ <b>Правильно!</b>" if is_correct else f"❌ <b>Неправильно.</b>\nВаш ответ: <b>{question['options'][option_index]}</b>\nВерный ответ: <b>{question['options'][question['correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
        audio_msg_id = await send_audio_if_enabled(callback.bot, callback.message.chat.id, user_id, correct_word["hanzi"])
        if audio_msg_id:
            session["audio_message_id"] = audio_msg_id
            
    if is_correct:
        word_breakdown = f"💡 <b>Разбор слова:</b>\n{correct_word['hanzi']} [{correct_word['pinyin']}] — {correct_word['translation']}"
    else:
        word_breakdown = f"💡 <b>Разбор слов:</b>\n\n❌ <b>Ваш ответ:</b>\n{selected_word['hanzi']} [{selected_word['pinyin']}] — {selected_word['translation']}\n\n✅ <b>Правильный ответ:</b>\n{correct_word['hanzi']} [{correct_word['pinyin']}] — {correct_word['translation']}"
        
    next_q = q_index + 1
    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"word_next_{topic_id}_{next_q}")]])
        await callback.message.edit_text(f"{feedback}\n\n{word_breakdown}", reply_markup=kb, parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏁 Показать результаты", callback_data=f"word_finish_{topic_id}")]])
        await callback.message.edit_text(f"{feedback}\n\n{word_breakdown}", reply_markup=kb, parse_mode="HTML")
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
    
    audio_msg_id = session.pop("audio_message_id", None)
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
            
    topic_title = WORDS[topic_id]["title"]
    await callback.message.edit_text(f"🎴 <b>{topic_title}</b>\nВопрос {next_q + 1} из {total}\n\n❓ {q['question_text']}", reply_markup=build_answer_keyboard(topic_id, next_q, q["options"], "wordans"), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("word_finish_"))
async def handle_word_finish(callback: CallbackQuery) -> None:
    logger.info(f"🏁 НАЖАТА КНОПКА 'ПОКАЗАТЬ РЕЗУЛЬТАТЫ' (СЛОВА) пользователем {callback.from_user.id}")
    topic_id = callback.data.removeprefix("word_finish_")
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "words":
        logger.warning(f"⚠️ Сессия не найдена. Текущая сессия: {session}")
        await callback.answer("⚠️ Сессия не найдена. Начните тему заново.", show_alert=True)
        return
        
    score = session.get("score", 0)
    total = len(session.get("questions", []))
    
    logger.info(f"💾 Сохраняем результат слов: {score}/{total} для темы {topic_id}")
    await mark_completed(user_id, topic_id, score, total, "words")
    
    audio_msg_id = session.pop("audio_message_id", None)
    if audio_msg_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=audio_msg_id)
        except Exception:
            pass
            
    if user_id in user_sessions:
        temp_session = user_sessions[user_id]
        for key in ["type", "lesson", "question", "score", "shuffled_questions", "questions", "audio_message_id"]:
            temp_session.pop(key, None)
            
    percent = round(score / total * 100) if total > 0 else 0
    emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
    
    topic_title = WORDS.get(topic_id, {}).get("title", "Неизвестная тема")
    text_to_send = f"{emoji} <b>Изучение слов завершено!</b>\n\n🎴 {topic_title}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)"
    
    logger.info(f"📤 Пытаемся обновить сообщение текстом:\n{text_to_send}")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]])
    
    try:
        await callback.message.edit_text(text_to_send, reply_markup=kb, parse_mode="HTML")
        logger.info("✅ Сообщение успешно обновлено!")
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении сообщения: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка при показе результатов.", show_alert=True)
        
    await callback.answer()

# ─── Хендлеры: СЛОВАРЬ ──────────────────────────────────────
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
    admin_users.add(message.from_user.id)
    await message.answer("✅ <b>Режим администратора включён!</b>", parse_mode="HTML")

@router.message(Command("setuser1234"))
async def cmd_set_user(message: Message) -> None:
    admin_users.discard(message.from_user.id)
    await message.answer("✅ <b>Режим администратора выключен.</b>", parse_mode="HTML")

@router.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message) -> None:
    if message.from_user.id not in admin_users:
        await message.answer("⛔ У вас нет доступа к этой команде.", parse_mode="HTML")
        return
        
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT data FROM users')
        
    total_users = len(rows)
    total_tests_completed = 0
    total_words_completed = 0
    topic_stats = {}
    
    for row in rows:
        user_data = row['data']
        test_results = user_data.get("test", {})
        for lesson_id, record in test_results.items():
            total_tests_completed += record.get("attempts", 1)
            lesson = TESTS.get(lesson_id)
            if lesson:
                topic_stats[lesson["title"]] = topic_stats.get(lesson["title"], 0) + record.get("attempts", 1)
                
        words_results = user_data.get("words", {})
        for topic_id, record in words_results.items():
            total_words_completed += record.get("attempts", 1)
            topic_data = WORDS.get(topic_id)
            if topic_data:
                topic_stats[topic_data["title"]] = topic_stats.get(topic_data["title"], 0) + record.get("attempts", 1)
                
    lines = ["👑 <b>Админ-панель</b>\n", f"👥 Всего пользователей: <b>{total_users}</b>", f"📝 Пройдено тестов: <b>{total_tests_completed}</b>", f" Пройдено тем слов: <b>{total_words_completed}</b>\n"]
    
    if topic_stats:
        lines.append(" <b>Топ-5 популярных тем:</b>")
        sorted_topics = sorted(topic_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        for i, (topic_name, count) in enumerate(sorted_topics, 1):
            lines.append(f"  {i}. {topic_name} — {count} прохождений")
    else:
        lines.append("📊 Статистика по темам пока пуста.")
        
    await message.answer("\n".join(lines), parse_mode="HTML")

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user_id = str(message.from_user.id)
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM users WHERE user_id = $1', user_id)
    if message.from_user.id in user_sessions: 
        del user_sessions[message.from_user.id]
    await message.answer("🔄 <b>Ваши результаты успешно сброшены!</b>", parse_mode="HTML")

# ─── ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ───────────────────────────
@router.errors()
async def handle_errors(event: ErrorEvent, bot: Bot):
    exception = event.exception
    # ⚡️ ТЕПЕРЬ МЫ БУДЕМ ВИДЕТЬ ВСЕ ОШИБКИ В ЛОГАХ!
    logger.error(f"❌ ГЛОБАЛЬНАЯ ОШИБКА: {type(exception).__name__}: {exception}")
    
    if isinstance(exception, TelegramBadRequest):
        if "message is not modified" in str(exception):
            logger.info("ℹ️ Сообщение не изменено (это нормально, если текст тот же)")
            return True
    return False
