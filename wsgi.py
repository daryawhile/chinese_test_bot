import logging
from quart import Quart, request, jsonify

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

# Создаем приложение Quart (вместо Flask)
app = Quart(__name__)

@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        # В Quart нужно использовать await для получения JSON
        update_data = await request.get_json()
        update = Update(**update_data)
        
        # Теперь await работает идеально и не закрывает цикл событий
        await dp.feed_webhook_update(bot, update)
        
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/', methods=['GET'])
async def health_check():
    return "Bot is running!", 200
