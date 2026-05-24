import os
import logging
import asyncio
import sys
from datetime import datetime
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

# Setup Logging - Forced to stdout for Render
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO, 
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8080))

# Initialize Global Clients
# We add serverSelectionTimeoutMS to prevent hanging forever
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client.telegram_bot
reminders_col = db.reminders
codes_col = db.saved_codes
tf = TimezoneFinder()

# Conversation States
GET_LOCATION, GET_DATE, GET_TIME, GET_LABEL = range(4)

# --- DATABASE TEST ---
async def test_mongodb():
    logger.info("Step 1: Testing MongoDB Connection...")
    try:
        # The 'isMaster' command is cheap and confirms connectivity
        await client.admin.command('ismaster')
        logger.info("✅ MONGODB CONNECTION: SUCCESSFUL")
        return True
    except Exception as e:
        logger.error(f"❌ MONGODB CONNECTION: FAILED. Error: {e}")
        return False

# --- UTILS ---

def get_days_left(target_date_str, user_tz_str):
    tz = pytz.timezone(user_tz_str)
    now = datetime.now(tz).date()
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    return (target - now).days

def schedule_reminder_job(app, reminder_data):
    reminder_time = datetime.strptime(reminder_data['reminder_time'], "%H:%M").time()
    job_name = str(reminder_data['_id'])
    
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
    days_left = get_days_left(data['target_date'], data['timezone'])
    
    if days_left >= 0:
        msg = f"🔔 REMINDER: {days_left} days left to '{data['label']}'!" if days_left > 0 else f"🎉 TODAY IS THE DAY: '{data['label']}'!"
        await context.bot.send_message(chat_id=job.chat_id, text=msg)
        if days_left == 0:
            await reminders_col.delete_one({"_id": data['_id']})
            job.schedule_removal()
    else:
        job.schedule_removal()

# --- HANDLERS ---

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    contact_keyboard = [[KeyboardButton("📍 Share Location", request_location=True)]]
    reply_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Share location to detect timezone:", reply_markup=reply_markup)
    return GET_LOCATION

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_loc = update.message.location
    timezone_str = tf.timezone_at(lng=user_loc.longitude, lat=user_loc.latitude) or "UTC"
    context.user_data['timezone'] = timezone_str
    await update.message.reply_text(f"✅ Timezone: {timezone_str}\nEnter target date (YYYY-MM-DD):")
    return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%Y-%m-%d")
        context.user_data['target_date'] = update.message.text
        await update.message.reply_text("⏰ Daily reminder time (HH:MM):")
        return GET_TIME
    except ValueError:
        await update.message.reply_text("❌ Use YYYY-MM-DD.")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%H:%M")
        context.user_data['reminder_time'] = update.message.text
        await update.message.reply_text("🏷️ Reminder label:")
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

    reminder_id = context.user_data.get('edit_id')
    if reminder_id:
        await reminders_col.update_one({"_id": ObjectId(reminder_id)}, {"$set": data})
        data['_id'] = ObjectId(reminder_id)
        await update.message.reply_text(f"✅ Reminder updated!")
    else:
        result = await reminders_col.insert_one(data)
        data['_id'] = result.inserted_id
        await update.message.reply_text(f"✅ Reminder created!")
    
    schedule_reminder_job(application, data)
    return ConversationHandler.END

# --- APP SETUP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={
            GET_LOCATION: [MessageHandler(filters.LOCATION, handle_location)],
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)],
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)],
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
    )
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Commands:\n/remind - Set\n/list - Manage\n/save - Save (Admin)")))
    app.add_handler(conv_handler)
    # Echo / Forwarder fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: u.message.reply_text(f"Echo: {u.message.text}")))
    return app

application = create_application()
flask_app = Flask(__name__)

@flask_app.route('/')
def index(): return "Debug Bot is Up!"

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    asyncio.run_coroutine_threadsafe(application.process_update(Update.de_json(data, application.bot)), main_loop)
    return "OK"

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    
    logger.info("Step 0: Initializing system...")
    
    # TEST MONGODB
    if not await test_mongodb():
        logger.error("FATAL: Database connection failed. Shutting down.")
        sys.exit(1)
    
    # Reload existing reminders
    logger.info("Step 2: Syncing reminders from DB...")
    cursor = reminders_col.find({})
    async for r in cursor: schedule_reminder_job(application, r)
    
    # Set Webhook
    logger.info("Step 3: Setting Telegram Webhook...")
    await application.initialize()
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    logger.info("✅ WEBHOOK SET SUCCESSFULLY")
    
    from werkzeug.serving import make_server
    import threading
    class ServerThread(threading.Thread):
        def __init__(self, app):
            threading.Thread.__init__(self)
            self.server = make_server('0.0.0.0', PORT, app)
        def run(self): 
            logger.info(f"Step 4: Flask server starting on port {PORT}...")
            self.server.serve_forever()

    ServerThread(flask_app).start()
    logger.info("🚀 SYSTEM FULLY OPERATIONAL")
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as e:
        logger.error(f"FATAL SYSTEM ERROR: {e}")
        sys.exit(1)
