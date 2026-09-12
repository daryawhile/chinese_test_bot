import os
import json
import logging
import random
import aiohttp
import re
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from tests_data import TESTS, WORDS
from config import BOT_TOKEN

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
user_sessions: dict[int, dict] = {}

# ─── Настройки JSONBin ────────────────────────────────────────
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY")
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}" if JSONBIN_BIN_ID else ""
HEADERS = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"} if JSONBIN_API_KEY else {}


# ─── Асинхронные функции для работы с облаком ─────────────────

async def load_completed() -> dict:
    if not JSONBIN_BIN_ID:
        logger.warning("JSONBIN_BIN_ID не установлен, загрузка невозможна.")
        return {}
    try:
        logger.info(f"🔄 Загружаем данные из JSONBin: {JSONBIN_BIN_ID}")
        async with aiohttp.ClientSession() as session:
            async with session.get(JSONBIN_URL + "/latest", headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info("✅ Данные успешно загружены из JSONBin!")
                    return data.get("record", {})
                else:
                    logger.error(f"❌ Ошибка загрузки: статус {resp.status}")
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
    data[key][test_type][lesson_id] = {
        "score": score,
        "total": total,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
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
        [InlineKeyboardButton(text="📝 Тесты", callback_data="show_tests")],
        [InlineKeyboardButton(text="🎴 Изучение слов", callback_data="show_words")],
        [InlineKeyboardButton(text="📖 Словарь", callback_data="show_dictionary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_tests_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for lesson_id, lesson in TESTS.items():
        buttons.insert(0, [InlineKeyboardButton(text=lesson["title"], callback_data=f"test_{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_words_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for lesson_id in WORDS.keys():
        lesson_num = lesson_id.split("_")[1]
        buttons.insert(0, [InlineKeyboardButton(text=f"Урок {lesson_num}", callback_data=f"words_{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_dictionary_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]]
    for lesson_id in WORDS.keys():
        lesson_num = lesson_id.split("_")[1]
        buttons.insert(0, [InlineKeyboardButton(text=f"Урок {lesson_num}", callback_data=f"dict_{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_answer_keyboard(lesson_id: str, q_index: int, options: list[str], prefix: str = "ans") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"{prefix}_{lesson_id}_{q_index}_{i}")] for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def format_results(user_id: int) -> str:
    results = await get_user_results(user_id)
    if not results or results == {"_cleared": True}:
        return "📊 <b>Ваши результаты</b>\n\nВы ещё не прошли ни одного теста.\n\nВыберите раздел, чтобы начать!"
    
    lines = ["📊 <b>Ваши результаты</b>\n"]
    
    test_results = results.get("test", {})
    if test_results and test_results != {"_cleared": True}:
        lines.append("📝 <b>Тесты:</b>")
        for lesson_id, result in test_results.items():
            if lesson_id == "_cleared": continue
            lesson = TESTS.get(lesson_id)
            if not lesson: continue
            score, total, date = result.get("score", "?"), result.get("total", "?"), result.get("date", "неизвестно")
            percent = round(score / total * 100) if isinstance(score, int) and isinstance(total, int) else 0
            emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
            lines.append(f"  {emoji} {lesson['title']}: {score}/{total} ({percent}%) - {date}")
        lines.append("")
    
    words_results = results.get("words", {})
    if words_results and words_results != {"_cleared": True}:
        lines.append("🎴 <b>Изучение слов:</b>")
        for lesson_id, result in words_results.items():
            if lesson_id == "_cleared": continue
            lesson_num = lesson_id.split("_")[1]
            score, total, date = result.get("score", "?"), result.get("total", "?"), result.get("date", "неизвестно")
            percent = round(score / total * 100) if isinstance(score, int) and isinstance(total, int) else 0
            emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
            lines.append(f"  {emoji} Урок {lesson_num}: {score}/{total} ({percent}%) - {date}")
    
    return "\n".join(lines)


# ─── Хендлеры: Главное меню ───────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
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
    await callback.message.edit_text("🎴 <b>Выберите урок для изучения слов:</b>", reply_markup=build_words_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "show_dictionary")
async def show_dictionary(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📖 <b>Выберите урок:</b>", reply_markup=build_dictionary_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🇨🇳 <b>Главное меню</b>\n\nВыберите раздел:", reply_markup=build_main_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
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
    feedback = "✅ <b>Правильно!</b>" if is_correct else f"❌ <b>Неправильно.</b>\nВерный ответ: <b>{question['shuffled_options'][question['shuffled_correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
        
    next_q = q_index + 1

    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"test_next_{lesson_id}_{next_q}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос {q_index + 1} из {total} завершён.\n\nНажмите кнопку ниже, чтобы продолжить.", 
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Показать результаты", callback_data=f"test_finish_{lesson_id}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n📖 <b>{TESTS[lesson_id]['title']}</b>\nВопрос {q_index + 1} из {total} завершён.\n\nНажмите кнопку ниже, чтобы увидеть результаты.", 
            reply_markup=kb, parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("test_next_"))
async def handle_test_next(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    next_q = int(parts[-1])
    # ИСПРАВЛЕНО: берем элементы с 3-го по предпоследний, чтобы получить "lesson_1", а не "next_lesson_1"
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
    lesson_id = callback.data.removeprefix("words_")
    user_id = callback.from_user.id
    
    if lesson_id not in WORDS:
        await callback.answer("Урок не найден.", show_alert=True)
        return
    
    words = [parse_word(w) for w in WORDS[lesson_id]]
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
        "type": "words", "lesson": lesson_id, "question": 0, "score": 0, "questions": questions
    }
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(
        f"🎴 <b>Урок {lesson_id.split('_')[1]}</b>\nВопрос 1 из {total}\n\n❓ {q['question_text']}", 
        reply_markup=build_answer_keyboard(lesson_id, 0, q["options"], "wordans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wordans_"))
async def handle_word_answer(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    option_index, q_index = int(parts[-1]), int(parts[-2])
    lesson_id = "_".join(parts[1:-2])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words" or session["lesson"] != lesson_id or session["question"] != q_index:
        await callback.answer("⚠️ Неактуальный вопрос.", show_alert=True)
        return

    questions = session["questions"]
    question = questions[q_index]
    total = len(questions)
    
    is_correct = option_index == question["correct"]
    feedback = "✅ <b>Правильно!</b>" if is_correct else f"❌ <b>Неправильно.</b>\nВерный ответ: <b>{question['options'][question['correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
    
    words_info = [f"{word['hanzi']} [{word['pinyin']}] - {word['translation']}" for word in question["all_words"]]
    words_text = "\n".join(words_info)
    
    next_q = q_index + 1

    if next_q < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data=f"word_next_{lesson_id}_{next_q}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n📚 <b>Разбор слов из вариантов:</b>\n{words_text}\n\nНажмите кнопку ниже, чтобы продолжить.", 
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Показать результаты", callback_data=f"word_finish_{lesson_id}")
        ]])
        await callback.message.edit_text(
            f"{feedback}\n\n📚 <b>Разбор слов из вариантов:</b>\n{words_text}\n\nНажмите кнопку ниже, чтобы увидеть результаты.", 
            reply_markup=kb, parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("word_next_"))
async def handle_word_next(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    next_q = int(parts[-1])
    # ИСПРАВЛЕНО: берем элементы с 3-го по предпоследний, чтобы получить "lesson_1", а не "next_lesson_1"
    lesson_id = "_".join(parts[2:-1])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words" or session["lesson"] != lesson_id:
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    questions = session["questions"]
    q = questions[next_q]
    total = len(questions)
    session["question"] = next_q

    await callback.message.edit_text(
        f"🎴 <b>Урок {lesson_id.split('_')[1]}</b>\nВопрос {next_q + 1} из {total}\n\n❓ {q['question_text']}", 
        reply_markup=build_answer_keyboard(lesson_id, next_q, q["options"], "wordans"), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("word_finish_"))
async def handle_word_finish(callback: CallbackQuery) -> None:
    lesson_id = callback.data.removeprefix("word_finish_")
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get("type") != "words":
        await callback.answer("⚠️ Сессия не найдена.", show_alert=True)
        return

    score = session["score"]
    total = len(session["questions"])
    await mark_completed(user_id, lesson_id, score, total, "words")
    
    if user_id in user_sessions:
        del user_sessions[user_id]
        
    percent = round(score / total * 100)
    emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]])
    await callback.message.edit_text(
        f"{emoji} <b>Изучение слов завершено!</b>\n\n🎴 Урок {lesson_id.split('_')[1]}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)\n\n💡 Вы можете пройти этот урок снова!", 
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


# ─── Хендлеры: СЛОВАРЬ ───────────────────────────────────────

@router.callback_query(F.data.startswith("dict_"))
async def show_dict_lesson(callback: CallbackQuery) -> None:
    lesson_id = callback.data.removeprefix("dict_")
    
    if lesson_id not in WORDS:
        await callback.answer("Урок не найден.", show_alert=True)
        return
    
    words = WORDS[lesson_id]
    lesson_num = lesson_id.split('_')[1]
    
    lines = [f"📖 <b>Урок {lesson_num} - Словарь</b>\n"]
    for word_str in words:
        parsed = parse_word(word_str)
        if parsed:
            lines.append(f"• {parsed['hanzi']} [{parsed['pinyin']}] - {parsed['translation']}")
    
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к урокам", callback_data="show_dictionary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ─── СЛУЖЕБНЫЕ КОМАНДЫ ───────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user_id = str(message.from_user.id)
    logger.info(f"🔄 ЗАПРОС СБРОСА от пользователя ID: {user_id}")
    
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
