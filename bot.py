import os
import logging
import asyncio
import sys
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from flask import Flask, request

# Setup Logging
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

# Initialize Application
application = ApplicationBuilder().token(TOKEN).build()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /start received from {update.effective_user.id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, talk to me!")

application.add_handler(CommandHandler('start', start_command))

# Flask Webhook Server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive and listening!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    # We use a special trick here to run the async bot logic from the Flask request
    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            # Use the main event loop to process the update
            asyncio.run_coroutine_threadsafe(
                application.process_update(Update.de_json(data, application.bot)),
                main_loop
            )
        except Exception as e:
            logger.error(f"Error in webhook processing: {e}")
    return "OK"

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()

    # 1. Initialize and Set Webhook
    await application.initialize()
    webhook_url = f"{RENDER_URL}/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"WEBHOOK SUCCESS: Set to {webhook_url}")
    
    # 2. Start Flask Server
    from werkzeug.serving import make_server
    import threading

    class ServerThread(threading.Thread):
        def __init__(self, app):
            threading.Thread.__init__(self)
            self.server = make_server('0.0.0.0', PORT, app)
            self.ctx = app.app_context()
            self.ctx.push()

        def run(self):
            logger.info(f"Flask server starting on port {PORT}...")
            self.server.serve_forever()

    server = ServerThread(flask_app)
    server.start()
    
    # 3. Keep the main loop alive
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        sys.exit(1)
