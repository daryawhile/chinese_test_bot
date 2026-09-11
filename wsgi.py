import logging
from flask import Flask, request, jsonify

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update

from config import BOT_TOKEN, WEBHOOK_PATH
from bot_logic import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(router)

# Создаем Flask-приложение
app = Flask(__name__)

# ВАЖНО: добавляем async перед def
@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        update_data = request.json
        update = Update(**update_data)
        
        # ВАЖНО: используем await вместо asyncio.run()
        await dp.feed_webhook_update(bot, update)
        
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is running!", 200
