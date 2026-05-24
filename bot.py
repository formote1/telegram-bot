import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from flask import Flask, request

# 1. Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 2. Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL") # Provided automatically by Render
PORT = int(os.getenv("PORT", 8080))

# 3. Bot Logic
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, talk to me!")

# 4. Initialize Application
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler('start', start))

# 5. Flask Webhook Server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is running!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
async def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)
    return "OK"

async def main():
    # Set webhook
    await application.initialize()
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    logging.info(f"Webhook set to {RENDER_URL}/{TOKEN}")
    
    # Start Flask server
    # Note: In production, you'd use gunicorn, but this works for a simple setup
    flask_app.run(host='0.0.0.0', port=PORT)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
