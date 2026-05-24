import os
import logging
import asyncio
import sys
from datetime import datetime, time
import pytz
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ConversationHandler,
    CallbackQueryHandler
)
from flask import Flask, request
from motor.motor_asyncio import AsyncIOMotorClient
from timezonefinder import TimezoneFinder
from bson import ObjectId

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8080))

# Initialize MongoDB & Timezone Finder
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client.telegram_bot
reminders_col = db.reminders
codes_col = db.saved_codes
tf = TimezoneFinder()

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL = range(4)

# Global loop variable
main_loop = None

# --- UTILS ---

def schedule_reminder_job(app, reminder_data):
    user_tz = pytz.timezone(reminder_data['timezone'])
    h, m = map(int, reminder_data['reminder_time'].split(':'))
    reminder_time = time(hour=h, minute=m, tzinfo=user_tz)
    
    job_name = str(reminder_data['_id'])
    
    # Remove existing job if it exists (for edits)
    current_jobs = app.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    app.job_queue.run_daily(
        send_daily_reminder,
        time=reminder_time,
        chat_id=reminder_data['user_id'],
        data=reminder_data,
        name=job_name
    )

async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    tz = pytz.timezone(data['timezone'])
    now = datetime.now(tz).date()
    target = datetime.strptime(data['target_date'], "%Y-%m-%d").date()
    days_left = (target - now).days
    
    if days_left >= 0:
        msg = f"🔔 REMINDER: {days_left} days left to '{data['label']}'!" if days_left > 0 else f"🎉 TODAY IS THE DAY: '{data['label']}'!"
        await context.bot.send_message(chat_id=job.chat_id, text=msg)
        if days_left == 0:
            await reminders_col.delete_one({"_id": data['_id']})
            job.schedule_removal()
    else:
        job.schedule_removal()

# --- FORWARDER LOGIC ---

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args:
        await update.message.reply_text("❌ Reply to a message with: /save CODE")
        return
    code = context.args[0].upper()
    await codes_col.update_one(
        {"code": code}, 
        {"$set": {"code": code, "chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id}}, 
        upsert=True
    )
    await update.message.reply_text(f"✅ Code '{code}' saved to Database!")

# --- REMINDER CONVERSATION ---

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # We DON'T clear user_data here because we might have an 'edit_id'
    keyboard = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Step 1: Select timezone or share location:", reply_markup=reply_markup)
    return GET_TZ_CHOICE

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        loc = update.message.location
        timezone_str = tf.timezone_at(lng=loc.longitude, lat=loc.latitude) or "UTC"
    else:
        choice = update.message.text
        timezone_str = "Asia/Tashkent" if "Tashkent" in choice else None
        if not timezone_str:
            await update.message.reply_text("Please use buttons.")
            return GET_TZ_CHOICE
    
    context.user_data['timezone'] = timezone_str
    await update.message.reply_text(f"✅ Timezone: {timezone_str}\nStep 2: Enter target date (YYYY-MM-DD):")
    return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%Y-%m-%d")
        context.user_data['target_date'] = update.message.text
        await update.message.reply_text("Step 3: Daily reminder time (HH:MM in 24h format):")
        return GET_TIME
    except ValueError:
        await update.message.reply_text("❌ Use YYYY-MM-DD.")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%H:%M")
        context.user_data['reminder_time'] = update.message.text
        await update.message.reply_text("Step 4: Label for this reminder:")
        return GET_LABEL
    except ValueError:
        await update.message.reply_text("❌ Use HH:MM.")
        return GET_TIME

async def handle_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text
    user_id = update.effective_user.id
    
    data = {
        "user_id": user_id,
        "timezone": context.user_data['timezone'],
        "target_date": context.user_data['target_date'],
        "reminder_time": context.user_data['reminder_time'],
        "label": label
    }

    # Check if we are updating an existing one
    edit_id = context.user_data.get('edit_id')
    if edit_id:
        await reminders_col.update_one({"_id": ObjectId(edit_id)}, {"$set": data})
        data['_id'] = ObjectId(edit_id)
        await update.message.reply_text(f"✅ Reminder '{label}' updated!")
        context.user_data.clear()
    else:
        result = await reminders_col.insert_one(data)
        data['_id'] = result.inserted_id
        await update.message.reply_text(f"✅ New reminder '{label}' is active!")
    
    schedule_reminder_job(application, data)
    return ConversationHandler.END

# --- LIST / EDIT / DELETE ---

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cursor = reminders_col.find({"user_id": user_id})
    reminders = await cursor.to_list(length=10)
    
    if not reminders:
        await update.message.reply_text("You have no active reminders. Use /remind to create one.")
        return

    for r in reminders:
        keyboard = [[InlineKeyboardButton("Edit ✏️", callback_data=f"edit_{r['_id']}"), InlineKeyboardButton("Delete 🗑️", callback_data=f"del_{r['_id']}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🔔 *{r['label']}*\n📅 {r['target_date']} | ⏰ {r['reminder_time']}\n🌍 {r['timezone']}",
            reply_markup=reply_markup, parse_mode="Markdown"
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, r_id = query.data.split('_')

    if action == "del":
        await reminders_col.delete_one({"_id": ObjectId(r_id)})
        jobs = application.job_queue.get_jobs_by_name(r_id)
        for job in jobs: job.schedule_removal()
        await query.edit_message_text("❌ Reminder deleted from Database.")
    
    elif action == "edit":
        context.user_data['edit_id'] = r_id
        await query.edit_message_text("✏️ Editing mode enabled. Type /remind to update the details.")

# --- TEXT HANDLING ---

async def handle_text_and_forwarder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.upper().strip()
    record = await codes_col.find_one({"code": text})
    if record:
        await context.bot.forward_message(chat_id=update.effective_chat.id, from_chat_id=record["chat_id"], message_id=record["message_id"])
    else:
        await update.message.reply_text(f"Echo: {update.message.text}\n(Hint: No forwarder code matched)")

# --- APP SETUP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={
            GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)],
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)],
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)],
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
    )
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Commands:\n/remind - Set reminder\n/list - Manage reminders\n/save CODE - Save message (Admin)")))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_forwarder))
    return app

application = create_application()
flask_app = Flask(__name__)

@flask_app.route('/')
def index(): return "Master Ultimate Bot is LIVE!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if main_loop:
        data = request.get_json(force=True)
        asyncio.run_coroutine_threadsafe(application.process_update(Update.de_json(data, application.bot)), main_loop)
    return "OK"

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    
    try:
        await client.admin.command('ismaster')
        logger.info("✅ MongoDB Connected")
    except Exception as e:
        logger.error(f"❌ MongoDB Failed: {e}")
        sys.exit(1)

    await application.initialize()
    await application.start()
    
    # Reload existing reminders
    cursor = reminders_col.find({})
    async for r in cursor: schedule_reminder_job(application, r)
    
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    logger.info(f"🚀 Master Ultimate Bot Ready.")
    
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
