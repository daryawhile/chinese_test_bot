import os
import json
import logging
import random
import aiohttp
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from tests_data import TESTS
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
    
    # Если словарь пустой, добавляем заполнитель, чтобы JSONBin не отверг его
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


async def is_completed(user_id: int, lesson_id: str) -> bool:
    data = await load_completed()
    return lesson_id in data.get(str(user_id), {})


async def get_user_results(user_id: int) -> dict:
    data = await load_completed()
    return data.get(str(user_id), {})


async def mark_completed(user_id: int, lesson_id: str, score: int, total: int) -> None:
    data = await load_completed()
    key = str(user_id)
    if key not in data:
        data[key] = {}
    data[key][lesson_id] = {
        "score": score,
        "total": total,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    await save_completed(data)


# ─── Вспомогательные функции ──────────────────────────────────

def build_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Мои результаты", callback_data="show_results")],
        [InlineKeyboardButton(text="───────────────", callback_data="noop")]
    ]
    for lesson_id, lesson in TESTS.items():
        buttons.append([InlineKeyboardButton(text=lesson["title"], callback_data=f"lesson_{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_answer_keyboard(lesson_id: str, q_index: int, options: list[str]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"ans_{lesson_id}_{q_index}_{i}")] for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_question(lesson_id: str, q_index: int, q: dict, total: int) -> str:
    lesson = TESTS[lesson_id]
    return f"📖 <b>{lesson['title']}</b>\nВопрос {q_index + 1} из {total}\n\n❓ {q['text']}"


async def format_results(user_id: int) -> str:
    results = await get_user_results(user_id)
    if not results or results == {"_cleared": True}:
        return "📊 <b>Ваши результаты</b>\n\nВы ещё не прошли ни одного теста.\n\nВыберите урок из списка, чтобы начать!"
    
    lines = ["📊 <b>Ваши результаты</b>\n"]
    total_score, total_questions, completed_count = 0, 0, 0
    
    for lesson_id, result in results.items():
        if lesson_id == "_cleared":
            continue
        lesson = TESTS.get(lesson_id)
        if not lesson: 
            continue
        completed_count += 1
        score, total, date = result.get("score", "?"), result.get("total", "?"), result.get("date", "неизвестно")
        
        if isinstance(score, int) and isinstance(total, int):
            percent = round(score / total * 100)
            total_score += score
            total_questions += total
            emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
            lines.append(f"{emoji} <b>{lesson['title']}</b>\n   Результат: {score}/{total} ({percent}%)\n   Дата: {date}\n")
        else:
            lines.append(f"✅ <b>{lesson['title']}</b>\n   Результат: {score}/{total}\n   Дата: {date}\n")

    if total_questions > 0:
        lines.extend(["───────────────", f"📈 <b>Общая статистика:</b>", f"   Пройдено тестов: {completed_count}/{len(TESTS)}", f"   Правильных ответов: {total_score}/{total_questions} ({round(total_score/total_questions*100)}%)"])
    else:
        lines.extend(["───────────────", f"📈 <b>Общая статистика:</b>", f"   Пройдено тестов: {completed_count}/{len(TESTS)}"])
    return "\n".join(lines)


# ─── Хендлеры (Обработчики команд) ────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🇨🇳 <b>Добро пожаловать!</b>\n\n"
        "Это бот для проверки знаний китайского языка.\n"
        "💡 Вы можете проходить тесты <b>сколько угодно раз</b>!\n"
        "В разделе «Мои результаты» всегда отображается ваш <b>последний</b> результат.", 
        reply_markup=build_main_keyboard(), 
        parse_mode="HTML"
    )


@router.callback_query(F.data == "show_results")
async def show_results(callback: CallbackQuery) -> None:
    text = await format_results(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к урокам", callback_data="back_to_lessons")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "back_to_lessons")
async def back_to_lessons(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🇨🇳 <b>Выберите урок:</b>", reply_markup=build_main_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("lesson_"))
async def start_lesson(callback: CallbackQuery) -> None:
    lesson_id = callback.data.removeprefix("lesson_")
    user_id = callback.from_user.id
    
    if lesson_id not in TESTS:
        await callback.answer("Урок не найден.", show_alert=True)
        return
    
    # Получаем вопросы и перемешиваем их порядок
    questions = [q.copy() for q in TESTS[lesson_id]["questions"]]
    random.shuffle(questions)
    
    # Для каждого вопроса перемешиваем варианты ответов
    for q in questions:
        options_with_correct = [(opt, i == q["correct"]) for i, opt in enumerate(q["options"])]
        random.shuffle(options_with_correct)
        q["shuffled_options"] = [opt for opt, _ in options_with_correct]
        q["shuffled_correct"] = next(i for i, (_, is_correct) in enumerate(options_with_correct) if is_correct)
    
    # Сохраняем перемешанные вопросы в сессии
    user_sessions[user_id] = {
        "lesson": lesson_id, 
        "question": 0, 
        "score": 0,
        "shuffled_questions": questions
    }
    
    q = questions[0]
    total = len(questions)
    await callback.message.edit_text(
        format_question(lesson_id, 0, q, total), 
        reply_markup=build_answer_keyboard(lesson_id, 0, q["shuffled_options"]), 
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ans_"))
async def handle_answer(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    option_index, q_index = int(parts[-1]), int(parts[-2])
    lesson_id = "_".join(parts[1:-2])
    user_id = callback.from_user.id
    session = user_sessions.get(user_id)

    if not session or session["lesson"] != lesson_id or session["question"] != q_index:
        await callback.answer("⚠️ Неактуальный вопрос.", show_alert=True)
        return

    # Берем перемешанные вопросы из сессии
    questions = session["shuffled_questions"]
    question = questions[q_index]
    total = len(questions)
    
    # Используем shuffled_correct вместо обычного correct
    is_correct = option_index == question["shuffled_correct"]
    feedback = "✅ Правильно!" if is_correct else f"❌ Неправильно.\nВерный ответ: <b>{question['shuffled_options'][question['shuffled_correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
        
    next_q = q_index + 1

    if next_q < total:
        session["question"] = next_q
        next_q_data = questions[next_q]
        await callback.message.edit_text(
            f"{feedback}\n\n{format_question(lesson_id, next_q, next_q_data, total)}", 
            reply_markup=build_answer_keyboard(lesson_id, next_q, next_q_data["shuffled_options"]), 
            parse_mode="HTML"
        )
    else:
        score = session["score"]
        await mark_completed(user_id, lesson_id, score, total)
        if user_id in user_sessions:
            del user_sessions[user_id]
            
        percent = round(score / total * 100)
        emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
        await callback.message.edit_text(
            f"{feedback}\n\n{emoji} <b>Тест завершён!</b>\n\n📖 {TESTS[lesson_id]['title']}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)\n\n💡 Вы можете пройти этот тест снова, чтобы улучшить результат!\nВернитесь к списку уроков: /start", 
            parse_mode="HTML"
        )
    await callback.answer()


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """Сбрасывает результаты тестов для текущего пользователя."""
    user_id = str(message.from_user.id)
    logger.info(f"🔄 ЗАПРОС СБРОСА от пользователя ID: {user_id}")
    
    data = await load_completed()
    logger.info(f"📦 Текущие данные в JSONBin: {data}")
    
    if user_id in data:
        logger.info(f"✅ Найден пользователь {user_id}, удаляем его данные.")
        del data[user_id]
        await save_completed(data)
        if message.from_user.id in user_sessions:
            del user_sessions[message.from_user.id]
        await message.answer("🔄 <b>Ваши результаты успешно сброшены!</b>\n\nТеперь вы можете пройти все тесты заново.", parse_mode="HTML")
    else:
        logger.warning(f"⚠️ Пользователь {user_id} НЕ НАЙДЕН в базе.")
        await message.answer(
            f"⚠️ Для этого аккаунта нет сохраненных результатов.\n\n"
            f"Пройдите тест, чтобы он сохранился!", 
            parse_mode="HTML"
        )


@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    """Проверяет, видит ли бот ключи от JSONBin."""
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        await message.answer(
            f"✅ <b>Ключи найдены!</b>\n\n"
            f"Bin ID: {JSONBIN_BIN_ID[:10]}...\n"
            f"API Key: {JSONBIN_API_KEY[:10]}...", 
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"❌ <b>Ключи НЕ найдены!</b>\n\n"
            f"JSONBIN_BIN_ID: {'Есть' if JSONBIN_BIN_ID else 'ОТСУТСТВУЕТ'}\n"
            f"JSONBIN_API_KEY: {'Есть' if JSONBIN_API_KEY else 'ОТСУТСТВУЕТ'}", 
            parse_mode="HTML"
        )


@router.message(Command("testsave"))
async def cmd_testsave(message: Message) -> None:
    """Принудительно проверяет запись в JSONBin."""
    logger.info("🚀 ЗАПУЩЕНА КОМАНДА /testsave")
    await message.answer("⏳ Тестирую сохранение... Смотрите логи Render!")
    
    user_id = str(message.from_user.id)
    test_data = {
        user_id: {
            "test_lesson": {"score": 99, "total": 100, "date": "TEST_MODE"}
        }
    }
    
    await save_completed(test_data)
    logger.info("🏁 КОМАНДА /testsave ЗАВЕРШЕНА")
    await message.answer("✅ Готово! Проверьте логи Render.")
