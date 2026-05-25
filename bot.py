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

# --- UTILS & BACKGROUND WORKERS ---

async def delete_msg_callback(context: ContextTypes.DEFAULT_TYPE):
    """Background worker that handles the 6-minute self-destruct timer."""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.data["chat_id"], message_id=job.data["message_id"])
        logger.info(f"🗑️ Ephemeral cleanup executed for message {job.data['message_id']}")
    except Exception as e:
        logger.warning(f"Cleanup note: message already gone or couldn't delete: {e}")

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

# --- ADMIN CONFIGURATION OPERATIONS ---

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

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run INSIDE a database group to set its specific password. Format: /setkey PASSWORD"""
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

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run INSIDE a database group to index files. Format: /autobulk START_ID END_ID PREFIX"""
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

# --- REMINDER CONVERSATION HANDLERS ---

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

    # 🚦 PROTECTION FIX: If user is running through the reminder menu setup steps,
    # back completely out and don't let the file security layer block them.
    if context.user_data.get('timezone') and not context.user_data.get('label'):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    text_upper = text.upper()
    is_admin = (user_id == ADMIN_ID)

    # 🛑 STATE 1: User is currently locked out and submitting a password key credentials string
    pending_group_unlock = context.user_data.get('pending_unlock_group_id')

    if pending_group_unlock and not is_admin:
        # Step A: Delete user's text secret key entry message IMMEDIATELY
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.error(f"Failed immediate key deletion step: {e}")

        # Step B: Validate structural database matching parameters
        key_record = await group_keys_col.find_one({"chat_id": pending_group_unlock})
        if key_record and text == key_record["secret_key"]:
            # Commit authentication profile to cluster
            await unlocked_groups_col.update_one(
                {"user_id": user_id},
                {"$addToSet": {"unlocked_chats": pending_group_unlock}},
                upsert=True
            )
            
            # Step C: Delete the old "Encryption Alert" interface block message IMMEDIATELY
            alert_msg_id = context.user_data.get('alert_message_id')
            if alert_msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=alert_msg_id)
                except Exception as e:
                    logger.error(f"Failed deleting active encryption alert: {e}")

            # Step D: Extract original target text reference pointer straight from transient volatile RAM storage
            saved_file_code = context.user_data.get('interrupted_file_code')
            
            # Flush volatile temporary state cache variables from RAM segment
            context.user_data.pop('pending_unlock_group_id', None)
            context.user_data.pop('alert_message_id', None)
            context.user_data.pop('interrupted_file_code', None)

            # Step E: Automatically route execution to deliver the file originally wanted!
            if saved_file_code:
                record = await codes_col.find_one({"code": saved_file_code})
                if record:
                    await execute_file_delivery(chat_id, record, context)
        else:
            # Drop invalid response warning notice, self-destructing it shortly to keep environment completely pristine
            fail_msg = await update.message.reply_text("❌ Invalid Key credentials. Access Denied. Provide the correct authorization key:")
            context.job_queue.run_once(delete_msg_callback, when=20, data={"chat_id": chat_id, "message_id": fail_msg.message_id})
        return

    # 🏁 STATE 2: User is executing a standard text lookup routing address match (e.g., AAA001)
    record = await codes_col.find_one({"code": text_upper})
    if record:
        target_group_chat_id = record["chat_id"]
        
        # Step A: Clear user's input request text string IMMEDIATELY
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.error(f"Failed immediate matching index text removal: {e}")

        # Step B: Check permissions threshold
        if not is_admin:
            security_gate = await group_keys_col.find_one({"chat_id": target_group_chat_id})
            if security_gate:
                user_auth_profile = await unlocked_groups_col.find_one({"user_id": user_id})
                if not user_auth_profile or target_group_chat_id not in user_auth_profile.get("unlocked_chats", []):
                    
                    # Store session context into Render's volatile RAM cluster cache strings
                    context.user_data['pending_unlock_group_id'] = target_group_chat_id
                    context.user_data['interrupted_file_code'] = text_upper

                    alert_msg = await context.bot.send_message(
                        chat_id=chat_id, 
                        text="🔒 Encrypted Data Block. This asset collection group is locked. Please enter the specific SECRET_KEY password to open access:"
                    )
                    context.user_data['alert_message_id'] = alert_msg.message_id
                    return

        # Step C: Permissions verification confirmed. Issue clean deployment protocols
        await execute_file_delivery(chat_id, record, context)

# --- 🚀 SUB-SERVICE ROUTINE: DELIVERY AND SELF-DESTRUCT TIMERS ---

async def execute_file_delivery(chat_id: int, record: dict, context: ContextTypes.DEFAULT_TYPE):
    """Handles deep cloning replication of asset payload packages and wires ephemeral countdowns."""
    try:
        # 1. Pipeline copy replication request
        copied_file = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=record["chat_id"],
            message_id=record["message_id"]
        )
        
        # 2. Append text terminal notice underneath
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ **SYSTEM NOTICE:** This data stream is ephemeral. Save or forward this asset elsewhere immediately; this file and notice will self-destruct in 6 minutes.",
            parse_mode="Markdown"
        )
        
        # 3. Schedule asynchronous background structural deletions in 6 minutes (360 seconds)
        context.job_queue.run_once(delete_msg_callback, when=360, data={"chat_id": chat_id, "message_id": copied_file.message_id})
        context.job_queue.run_once(delete_msg_callback, when=360, data={"chat_id": chat_id, "message_id": warning_msg.message_id})
        
    except Exception as e:
        logger.error(f"Asset routing delivery execution exception: {e}")
        err_msg = await context.bot.send_message(chat_id=chat_id, text="⚠️ System proxy error: Unable to clone target package from storage pools.")
        context.job_queue.run_once(delete_msg_callback, when=20, data={"chat_id": chat_id, "message_id": err_msg.message_id})

# --- APPLICATION GENERATOR MATRIX ---

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
    
    # 🛠️ ORIGINAL MASTER GREETING LAYOUT RESTORED CLEANLY
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text(
        "👋 Welcome back to your Personal Assistant Master Node.\n\n"
        "⏰ **Reminder System:**\n"
        "🔹 /remind - Schedule a new daily countdown reminder\n"
        "🔹 /list - View and manage active countdowns\n\n"
        "📦 **Storage Infrastructure:**\n"
        "🔹 Enter any valid alphanumeric index key (e.g., `AAA001`) to cleanly replicate target assets.\n\n"
        "📊 **System Status:**\n"
        "🔹 /stats - Check database allocations"
    , parse_mode="Markdown")))

    app.add_handler(CommandHandler("list", list_reminders))
    
    # Internal Database Map Orchestration Actions
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("setkey", set_group_key))
    
    # Core Global Administration Operations
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(conv_handler)
    
    # Process text entry filters through tracking pipeline (safely ignores slash commands)
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