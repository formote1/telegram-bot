import os
import logging
import asyncio
import sys
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ConversationHandler
)
from flask import Flask, request

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))
TIMEZONE = pytz.timezone("Asia/Karachi")

# Conversation States
GET_DATE, GET_TIME, GET_LABEL = range(3)

# --- BOT HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! Use /remind to set a daily countdown reminder.")

async def remind_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Enter the target date (YYYY-MM-DD):")
    return GET_DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_date = datetime.strptime(update.message.text, "%Y-%m-%d").date()
        context.user_data['target_date'] = target_date
        await update.message.reply_text("⏰ What time daily? (HH:MM in 24h):")
        return GET_TIME
    except ValueError:
        await update.message.reply_text("❌ Use YYYY-MM-DD format.")
        return GET_DATE

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reminder_time = datetime.strptime(update.message.text, "%H:%M").time()
        context.user_data['reminder_time'] = reminder_time
        await update.message.reply_text("🏷️ Label for this reminder:")
        return GET_LABEL
    except ValueError:
        await update.message.reply_text("❌ Use HH:MM format.")
        return GET_TIME

async def daily_reminder_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    days_left = (job.data['target_date'] - datetime.now(TIMEZONE).date()).days
    if days_left >= 0:
        await context.bot.send_message(chat_id=job.chat_id, text=f"🔔 {days_left} days left to '{job.data['label']}'!")
    if days_left <= 0: job.schedule_removal()

async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text
    chat_id = update.effective_chat.id
    
    context.job_queue.run_daily(
        daily_reminder_task,
        time=context.user_data['reminder_time'],
        chat_id=chat_id,
        data={'target_date': context.user_data['target_date'], 'label': label}
    )
    await update.message.reply_text(f"✅ Reminder set for '{label}'!")
    return ConversationHandler.END

# --- APP SETUP ---

# We build the application WITHOUT building it in the global scope to avoid weakref issues
def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remind", remind_start)],
        states={
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(conv_handler)
    return app

application = create_application()
flask_app = Flask(__name__)

@flask_app.route('/')
def index(): return "Running!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    asyncio.run_coroutine_threadsafe(application.process_update(Update.de_json(data, application.bot)), main_loop)
    return "OK"

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    await application.initialize()
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    
    from werkzeug.serving import make_server
    import threading
    class ServerThread(threading.Thread):
        def __init__(self, app):
            threading.Thread.__init__(self)
            self.server = make_server('0.0.0.0', PORT, app)
        def run(self): self.server.serve_forever()
    
    ServerThread(flask_app).start()
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as e:
        logger.error(f"FATAL: {e}")
        sys.exit(1)
