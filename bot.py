import os
import logging
import asyncio
import sys
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from flask import Flask, request

# Setup Logging to show in Render Logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))

# --- DEBUG CHECKS ---
logger.info("Starting Debug Checks...")

if not TOKEN:
    logger.error("FATAL ERROR: 'TELEGRAM_TOKEN' environment variable is missing in Render settings!")
    # We don't sys.exit here so Render logs can finish printing
else:
    logger.info(f"TELEGRAM_TOKEN is present (Length: {len(TOKEN)})")

if not RENDER_URL:
    logger.error("FATAL ERROR: 'RENDER_EXTERNAL_URL' environment variable is missing in Render settings!")
else:
    logger.info(f"RENDER_EXTERNAL_URL is set to: {RENDER_URL}")

if not TOKEN or not RENDER_URL:
    logger.error("Startup aborted due to missing environment variables.")
    sys.exit(1)

# Bot Logic
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /start command from {update.effective_user.first_name}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, talk to me!")

# Initialize Application
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler('start', start))

# Flask Webhook Server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive and debug mode is ON!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
async def webhook():
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Error processing webhook update: {e}")
    return "OK"

async def setup_bot():
    try:
        await application.initialize()
        webhook_url = f"{RENDER_URL}/{TOKEN}"
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"WEBHOOK SUCCESS: Set to {webhook_url}")
    except Exception as e:
        logger.error(f"FAILED TO SET WEBHOOK: {e}")
        raise e

if __name__ == '__main__':
    try:
        logger.info("Attempting to initialize bot and event loop...")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(setup_bot())
        
        logger.info(f"Starting Flask server on port {PORT}...")
        flask_app.run(host='0.0.0.0', port=PORT)
    except Exception as e:
        logger.error(f"CRITICAL STARTUP ERROR: {e}")
        sys.exit(1)
