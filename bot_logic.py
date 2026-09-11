import os
import json
import aiohttp
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from tests_data import TESTS
from config import BOT_TOKEN

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
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(JSONBIN_URL + "/latest", headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("record", {})
    except Exception as e:
        print(f"Ошибка загрузки из JSONBin: {e}")
    return {}


async def save_completed(data: dict) -> None:
    if not JSONBIN_BIN_ID:
        print("❌ JSONBIN_BIN_ID не установлен!")
        return
    if not JSONBIN_API_KEY:
        print("❌ JSONBIN_API_KEY не установлен!")
        return
    
    print(f"🔄 Сохраняем данные в JSONBin: {JSONBIN_BIN_ID}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(JSONBIN_URL, json=data, headers=HEADERS) as resp:
                if resp.status == 200:
                    print("✅ Данные успешно сохранены в JSONBin!")
                else:
                    text = await resp.text()
                    print(f"❌ Ошибка сохранения в JSONBin: {resp.status} - {text}")
    except Exception as e:
        print(f"❌ Исключение при сохранении в JSONBin: {e}")


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


def format_question(lesson_id: str, q_index: int) -> str:
    lesson = TESTS[lesson_id]
    q = lesson["questions"][q_index]
    return f"📖 <b>{lesson['title']}</b>\nВопрос {q_index + 1} из {len(lesson['questions'])}\n\n❓ {q['text']}"


async def format_results(user_id: int) -> str:
    results = await get_user_results(user_id)
    if not results:
        return "📊 <b>Ваши результаты</b>\n\nВы ещё не прошли ни одного теста.\n\nВыберите урок из списка, чтобы начать!"
    
    lines = ["📊 <b>Ваши результаты</b>\n"]
    total_score, total_questions, completed_count = 0, 0, len(results)
    
    for lesson_id, result in results.items():
        lesson = TESTS.get(lesson_id)
        if not lesson: continue
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
        "🇨🇳 <b>Добро пожаловать!</b>\n\nЭто бот для проверки знаний китайского языка.\n⚠️ Каждый тест можно пройти <b>только один раз</b>.", 
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
    
    if lesson_id not in TESTS or await is_completed(user_id, lesson_id):
        await callback.answer("✅ Вы уже прошли этот тест!", show_alert=True)
        return
    
    user_sessions[user_id] = {"lesson": lesson_id, "question": 0, "score": 0}
    q = TESTS[lesson_id]["questions"][0]
    await callback.message.edit_text(format_question(lesson_id, 0), reply_markup=build_answer_keyboard(lesson_id, 0, q["options"]), parse_mode="HTML")
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

    lesson = TESTS[lesson_id]
    question = lesson["questions"][q_index]
    is_correct = option_index == question["correct"]
    feedback = "✅ Правильно!" if is_correct else f"❌ Неправильно.\nВерный ответ: <b>{question['options'][question['correct']]}</b>"
    
    if is_correct: 
        session["score"] += 1
        
    next_q = q_index + 1
    total = len(lesson["questions"])

    if next_q < total:
        session["question"] = next_q
        next_q_data = lesson["questions"][next_q]
        await callback.message.edit_text(f"{feedback}\n\n{format_question(lesson_id, next_q)}", reply_markup=build_answer_keyboard(lesson_id, next_q, next_q_data["options"]), parse_mode="HTML")
    else:
        score = session["score"]
        await mark_completed(user_id, lesson_id, score, total)
        if user_id in user_sessions:
            del user_sessions[user_id]
            
        percent = round(score / total * 100)
        emoji = "🏆" if percent == 100 else "🎉" if percent >= 75 else "👍" if percent >= 50 else "📚"
        await callback.message.edit_text(
            f"{feedback}\n\n{emoji} <b>Тест завершён!</b>\n\n📖 {lesson['title']}\n📊 Результат: <b>{score}/{total}</b> ({percent}%)\n\nЭтот тест отмечен как пройденный.\nВернитесь к списку уроков: /start", 
            parse_mode="HTML"
        )
    await callback.answer()


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """Сбрасывает результаты тестов для текущего пользователя."""
    user_id = str(message.from_user.id)
    data = await load_completed()
    
    if user_id in data:
        del data[user_id]
        await save_completed(data)
        if message.from_user.id in user_sessions:
            del user_sessions[message.from_user.id]
        await message.answer("🔄 <b>Ваши результаты успешно сброшены!</b>\n\nТеперь вы можете пройти все тесты заново.", parse_mode="HTML")
    else:
        await message.answer("У вас и так нет пройденных тестов. Можете смело начинать!", parse_mode="HTML")


@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    """Проверяет, видит ли бот ключи от JSONBin."""
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        await message.answer(
            f"✅ <b>Отлично! Ключи найдены!</b>\n\n"
            f"Bin ID: {JSONBIN_BIN_ID[:10]}...\n"
            f"API Key: {JSONBIN_API_KEY[:10]}...\n\n"
            f"Теперь результаты будут сохраняться в облаке.", 
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"❌ <b>ВНИМАНИЕ: Ключи НЕ найдены!</b>\n\n"
            f"JSONBIN_BIN_ID: {'Есть' if JSONBIN_BIN_ID else 'ОТСУТСТВУЕТ'}\n"
            f"JSONBIN_API_KEY: {'Есть' if JSONBIN_API_KEY else 'ОТСУТСТВУЕТ'}\n\n"
            f"Проверьте вкладку Environment в Render. Убедитесь, что нет лишних пробелов.", 
            parse_mode="HTML"
        )
