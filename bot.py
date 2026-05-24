import os
import logging
import asyncio
import sys
import json
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
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", 0))
PORT = int(os.getenv("PORT", 8080))

# Initialize MongoDB & Timezone Finder
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client.telegram_bot
reminders_col = db.reminders
codes_col = db.saved_codes
group_keys_col = db.group_keys          # Maps: chat_id -> unique secret key
unlocked_groups_col = db.unlocked_users  # Maps: user_id -> list of unlocked chat_ids
tf = TimezoneFinder()

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL = range(4)

# Global loop variable
main_loop = None

# --- UTILS ---

def schedule_reminder_job(app, reminder_data):
    if not app.job_queue:
        logger.error("❌ JobQueue is missing! Make sure python-telegram-bot[job-queue] is installed.")
        return

    user_tz = pytz.timezone(reminder_data['timezone'])
    h, m = map(int, reminder_data['reminder_time'].split(':'))
    reminder_time = time(hour=h, minute=m, tzinfo=user_tz)
    
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

# --- ADMIN COMMANDS ---

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    reminders_count = await reminders_col.count_documents({})
    codes_count = await codes_col.count_documents({})
    keys_count = await group_keys_col.count_documents({})
    
    msg = (
        "📊 **Master Statistics**\n\n"
        f"👥 Active Reminders: {reminders_count}\n"
        f"🔑 Indexed Storage Codes: {codes_count}\n"
        f"🔐 Password Secured Groups: {keys_count}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("📦 Preparing data dump from MongoDB...")
    cursor = reminders_col.find({})
    reminders = await cursor.to_list(length=1000)
    
    for r in reminders:
        r['_id'] = str(r['_id'])

    file_path = "database_dump.json"
    with open(file_path, "w") as f:
        json.dump(reminders, f, indent=4)
    
    with open(file_path, "rb") as backup_file:
        await update.message.reply_document(document=backup_file, filename="reminders_backup.json")
        
    os.remove(file_path)

# --- 🔐 SECURITY KEY MANAGEMENT COMMANDS ---

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run this INSIDE a database group to set its specific password. Format: /setkey PASSWORD"""
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("❌ Use format: `/setkey YOUR_PASSWORD`", parse_mode="Markdown")
        return

    secret_key = context.args[0].strip()
    chat_id = update.effective_chat.id

    await group_keys_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "secret_key": secret_key}},
        upsert=True
    )
    await update.message.reply_text(f"🔒 Custom security password set successfully for this storage group partition!")

# --- 🔄 AUTOMATED BULK INDEXER ---

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run this INSIDE a database group to index files. Format: /autobulk START_ID END_ID PREFIX"""
    if update.effective_user.id != ADMIN_ID:
        return

    if len(context.args) < 3:
        await update.message.reply_text("❌ Use format: `/autobulk START_ID END_ID PREFIX`\nExample: `/autobulk 45 120 AAA`", parse_mode="Markdown")
        return

    try:
        start_id = int(context.args[0])
        end_id = int(context.args[1])
        prefix = context.args[2].upper().strip()
    except ValueError:
        await update.message.reply_text("❌ Start and End positions must be numerical digits.")
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🔄 Compiling row indexing map from message {start_id} to {end_id} under sequence prefix '{prefix}'...")

    success_count = 0
    current_code_number = 1

    for msg_id in range(start_id, end_id + 1):
        try:
            code = f"{prefix}{current_code_number:03d}"
            await codes_col.update_one(
                {"code": code}, 
                {"$set": {
                    "code": code, 
                    "chat_id": chat_id, 
                    "message_id": msg_id
                }}, 
                upsert=True
            )
            success_count += 1
            current_code_number += 1
            await asyncio.sleep(0.05) 
        except Exception as e:
            logger.warning(f"Skipping empty row index slot {msg_id}: {e}")
            continue

    await update.message.reply_text(f"✅ Matrix build finalized! Indexed {success_count} assets.\nRange sequence: {prefix}001 to {prefix}{success_count:03d}")

# --- MANUAL SAVE LOGIC ---

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args:
        await update.message.reply_text("❌ Reply to an asset item with: /save UNIQUE_CODE")
        return
    code = context.args[0].upper().strip()
    await codes_col.update_one(
        {"code": code}, 
        {"$set": {"code": code, "chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id}}, 
        upsert=True
    )
    await update.message.reply_text(f"✅ Mapping coordinates for '{code}' successfully written to index!")

# --- REMINDER CONVERSATION ---

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    edit_id = context.user_data.get('edit_id')
    if edit_id:
        await reminders_col.update_one({"_id": ObjectId(edit_id)}, {"$set": data})
        data['_id'] = ObjectId(edit_id)
        await update.message.reply_text(f"✅ Reminder updated!")
        context.user_data.clear()
    else:
        result = await reminders_col.insert_one(data)
        data['_id'] = result.inserted_id
        await update.message.reply_text(f"✅ New reminder '{label}' is active!")
    
    schedule_reminder_job(application, data)
    return ConversationHandler.END

# --- LIST / EDIT / DELETE REMINDERS ---

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cursor = reminders_col.find({"user_id": user_id})
    reminders = await cursor.to_list(length=10)
    
    if not reminders:
        await update.message.reply_text("No active reminders.")
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
        if application.job_queue:
            jobs = application.job_queue.get_jobs_by_name(r_id)
            for job in jobs: job.schedule_removal()
        await query.edit_message_text("❌ Reminder deleted.")
    
    elif action == "edit":
        context.user_data['edit_id'] = r_id
        await query.edit_message_text("✏️ Editing enabled. Type /remind to update.")

# --- CORE DATA PROCESSING ENGINE & ROUTING HANDLER ---

async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    text_upper = text.upper()
    is_admin = (user_id == ADMIN_ID)

    # State tracking: Check if the user is currently stuck trying to unlock a specific storage group
    pending_group_unlock = context.user_data.get('pending_unlock_group_id')

    if pending_group_unlock and not is_admin:
        # User is trying to submit a secret key password for a specific group
        key_record = await group_keys_col.find_one({"chat_id": pending_group_unlock})
        if key_record and text == key_record["secret_key"]:
            # Successful unlock! Save their authorization profile array to MongoDB
            await unlocked_groups_col.update_one(
                {"user_id": user_id},
                {"$addToSet": {"unlocked_chats": pending_group_unlock}},
                upsert=True
            )
            del context.user_data['pending_unlock_group_id']
            await update.message.reply_text("🔓 Authentication verified! Group repository node decrypted. Please retype your asset file code request.")
        else:
            await update.message.reply_text("🔒 Invalid Key credentials. Access Denied. Provide the correct matching authorization key:")
        return

    # Process file code lookups
    record = await codes_col.find_one({"code": text_upper})
    if record:
        target_group_chat_id = record["chat_id"]
        
        # Security Verification Check
        if not is_admin:
            # Does this specific file group require a key password map pointer?
            security_gate = await group_keys_col.find_one({"chat_id": target_group_chat_id})
            if security_gate:
                # Has this user already unlocked access session status for this chat_id?
                user_auth_profile = await unlocked_groups_col.find_one({"user_id": user_id})
                if not user_auth_profile or target_group_chat_id not in user_auth_profile.get("unlocked_chats", []):
                    # Flag their session state waiting for this specific key
                    context.user_data['pending_unlock_group_id'] = target_group_chat_id
                    await update.message.reply_text(f"🔒 Encrypted Data Block. This asset collection group is locked. Please enter the specific SECRET_KEY password to open access:")
                    return

        # Execution Sequence: Replicate storage file using clean copy protocols
        try:
            await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=target_group_chat_id,
                message_id=record["message_id"]
            )
        except Exception as e:
            logger.error(f"Copy sequence execution error: {e}")
            await update.message.reply_text("⚠️ System proxy error: Unable to clone target package from storage pool nodes.")

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
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🛠️ Cloud Database Nodes Status: ONLINE.\n\nEnter valid catalog storage keys to decrypt and pull assets.")))
    app.add_handler(CommandHandler("list", list_reminders))
    
    # Storage Group Internal Configuration Commands
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("setkey", set_group_key))
    
    # System Admin Operations
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(conv_handler)
    
    # Core processing pipeline catch-all
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_routing_manager))
    return app

application = create_application()
flask_app = Flask(__name__)

@flask_app.route('/')
def index(): return "Master Storage Engine Live Cluster Online."

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
        logger.info("✅ MongoDB Cluster Linked")
    except Exception as e:
        logger.error(f"❌ MongoDB initialization error: {e}")
        sys.exit(1)

    await application.initialize()
    await application.start()
    
    cursor = reminders_col.find({})
    async for r in cursor: 
        schedule_reminder_job(application, r)
    
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    logger.info(f"🚀 Microservice Worker Matrix operational on cluster node port {PORT}.")
    
    from werkzeug.serving import make_server
    import threading
    
    class ServerThread(threading.Thread):
        def __init__(self, app):
            super().__init__()
            self.server = make_server('0.0.0.0', PORT, app)
            self.daemon = True
        def run(self): 
            self.server.serve_forever()
    
    ServerThread(flask_app).start()
    
    while True: 
        await asyncio.sleep(3600)

if __name__ == '__main__':
    try: 
        asyncio.run(main())
    except Exception as e:
        logger.error(f"FATAL SYSTEM EXIT: {e}")
        sys.exit(1)
