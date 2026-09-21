import logging
from quart import Quart, request, jsonify
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from config import BOT_TOKEN, WEBHOOK_PATH
from bot_logic import router, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(router)

app = Quart(__name__)

# ⚡️ Инициализируем базу данных при запуске сервера
@app.before_serving
async def startup():
    await init_db()
    logger.info("✅ База данных Neon успешно инициализирована!")

@app.route(WEBHOOK_PATH, methods=['POST'])
async def webhook():
    try:
        update_data = await request.get_json()
        update = Update(**update_data)
        await dp.feed_webhook_update(bot, update)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/', methods=['GET'])
async def health_check():
    return "Bot is running!", 200
