import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from flask import Flask, request

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
# Fallback to a placeholder if the variable is missing to prevent total crash
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://missing-url.onrender.com")
PORT = int(os.getenv("PORT", 8080))

# Bot Logic
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, talk to me!")

# Initialize Application
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler('start', start))

# Flask Webhook Server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
async def webhook():
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Error processing update: {e}")
    return "OK"

async def setup_bot():
    # This ensures the webhook is set correctly with Telegram
    await application.initialize()
    webhook_url = f"{RENDER_URL}/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook successfully set to: {webhook_url}")

if __name__ == '__main__':
    # Run setup
    asyncio.get_event_loop().run_until_complete(setup_bot())
    
    # Start Flask
    logger.info(f"Starting server on port {PORT}")
    flask_app.run(host='0.0.0.0', port=PORT)
