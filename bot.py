import os
import logging
import asyncio
import sys
from datetime import datetime, time
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
TIMEZONE = pytz.timezone("Asia/Karachi") # Change this to your local timezone!

# Conversation States
GET_DATE, GET_TIME, GET_LABEL = range(3)

# --- REMINDER LOGIC ---

async def remind_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📅 Let's set a reminder!\nFirst, enter the target date (Format: YYYY-MM-DD)\nExample: 2026-12-25"
    )
    return GET_DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_date = datetime.strptime(update.message.text, "%Y-%m-%d").date()
        context.user_data['target_date'] = target_date
        await update.message.reply_text("⏰ Great! Now, what time should I remind you every day? (Format: HH:MM in 24h)\nExample: 09:30")
        return GET_TIME
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Please use YYYY-MM-DD (e.g., 2026-05-30).")
        return GET_DATE

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reminder_time = datetime.strptime(update.message.text, "%H:%M").time()
        context.user_data['reminder_time'] = reminder_time
        await update.message.reply_text("🏷️ Almost done! Give this reminder a label (e.g., 'My Birthday' or 'Project Deadline').")
        return GET_LABEL
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Please use HH:MM (e.g., 14:15).")
        return GET_TIME

async def daily_reminder_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    target_date = job.data['target_date']
    label = job.data['label']
    chat_id = job.chat_id
    
    today = datetime.now(TIMEZONE).date()
    days_left = (target_date - today).days
    
    if days_left > 0:
        await context.bot.send_message(chat_id=chat_id, text=f"🔔 REMINDER: {days_left} days left to '{label}'!")
    elif days_left == 0:
        await context.bot.send_message(chat_id=chat_id, text=f"🎉 TODAY IS THE DAY: '{label}' is here!")
        job.schedule_removal() # Stop the job
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"🏁 '{label}' has passed.")
        job.schedule_removal()

async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text
    target_date = context.user_data['target_date']
    reminder_time = context.user_data['reminder_time']
    chat_id = update.effective_chat.id

    # Schedule the daily job
    context.job_queue.run_daily(
        daily_reminder_task,
        time=reminder_time,
        days=(0, 1, 2, 3, 4, 5, 6), # Every day
        chat_id=chat_id,
        name=f"job_{chat_id}_{label}",
        data={'target_date': target_date, 'label': label}
    )

    await update.message.reply_text(
        f"✅ Success! I will remind you about '{label}' every day at {reminder_time.strftime('%H:%M')}.\n"
        f"Target Date: {target_date}"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reminder setup cancelled.")
    return ConversationHandler.END

# --- BOT SETUP ---

application = ApplicationBuilder().token(TOKEN).build()

# Add Conversation Handler
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("remind", remind_start)],
    states={
        GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

application.add_handler(conv_handler)
application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Use /remind to set a daily reminder!")))

# Flask Webhook Server
flask_app = Flask(__name__)

@flask_app.route('/')
def index(): return "Reminder Bot is active!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.method == "POST":
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
