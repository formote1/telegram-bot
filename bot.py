import os
import logging
import asyncio
import sys
import json
from datetime import datetime, time, timedelta
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
PORT = int(os.getenv("PORT", 10000))

# Global application and loop instances
application = None
main_loop = None

# --- UTILS & BACKGROUND WORKERS ---

async def delete_msg_callback(context: ContextTypes.DEFAULT_TYPE):
    """Background worker that handles the self-destruct timer."""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.data["chat_id"], message_id=job.data["message_id"])
        logger.info(f"🗑️ Ephemeral cleanup executed for message {job.data['message_id']}")
    except Exception as e:
        logger.warning(f"Cleanup note: message already gone or couldn't delete: {e}")

def schedule_reminder_job(app, reminder_data):
    if not app.job_queue:
        logger.error("❌ JobQueue is missing!")
        return

    try:
        user_tz = pytz.timezone(reminder_data['timezone'])
        h, m = map(int, reminder_data['reminder_time'].split(':'))
        
        # BUG FIX: Use naive time + timezone object in run_daily for better DST handling
        reminder_time = time(hour=h, minute=m)
        
        job_name = str(reminder_data['_id'])
        
        # Remove existing job if it exists (for updates)
        current_jobs = app.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs:
            job.schedule_removal()

        app.job_queue.run_daily(
            send_daily_reminder,
            time=reminder_time,
            timezone=user_tz,
            chat_id=reminder_data['user_id'],
            data=reminder_data,
            name=job_name
        )
    except Exception as e:
        logger.error(f"Error scheduling job: {e}")

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

# --- ADMIN MANAGEMENT OPERATIONS ---

async def manage_db_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    
    # Aggregate to find unique prefixes
    pipeline = [
        {"$project": {"prefix": {"$substr": ["$code", 0, 3]}}},
        {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}
    ]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=100)
    
    if not prefixes:
        await update.message.reply_text("No data indexed in database.")
        return ConversationHandler.END

    keyboard = []
    for p in prefixes:
        prefix_str = p['_id']
        keyboard.append([InlineKeyboardButton(f"{prefix_str} ({p['count']} items)", callback_data=f"pref_{prefix_str}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🛠️ **Database Management**\nSelect a prefix to manage or edit:", reply_markup=reply_markup, parse_mode="Markdown")
    return MANAGE_CHOOSE_PREFIX

async def handle_manage_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix = query.data.split('_')[1]
    context.user_data['manage_prefix'] = prefix
    
    keyboard = [
        [InlineKeyboardButton("Change Prefix ✏️", callback_data=f"act_rename"), 
         InlineKeyboardButton("Delete All 🗑️", callback_data=f"act_delete")],
        [InlineKeyboardButton("Cancel ❌", callback_data="act_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"Selected: **{prefix}**\nWhat would you like to do?", reply_markup=reply_markup, parse_mode="Markdown")
    return MANAGE_ACTION

async def handle_manage_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_')[1]
    prefix = context.user_data.get('manage_prefix')

    if action == "delete":
        result = await codes_col.delete_many({"code": {"$regex": f"^{prefix}"}})
        await query.edit_message_text(f"✅ Deleted {result.deleted_count} items with prefix '{prefix}'.")
        return ConversationHandler.END
    elif action == "rename":
        await query.edit_message_text(f"Enter the NEW prefix for '{prefix}':")
        return MANAGE_ACTION # Wait for text input
    else:
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

async def handle_new_prefix_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_prefix = context.user_data.get('manage_prefix')
    new_prefix = update.message.text.upper().strip()
    
    if not old_prefix or not new_prefix:
        return ConversationHandler.END

    # Update all codes starting with old_prefix
    cursor = codes_col.find({"code": {"$regex": f"^{old_prefix}"}})
    updates = 0
    async for doc in cursor:
        new_code = doc['code'].replace(old_prefix, new_prefix, 1)
        await codes_col.update_one({"_id": doc["_id"]}, {"$set": {"code": new_code}})
        updates += 1

    await update.message.reply_text(f"✅ Renamed {updates} items: {old_prefix} -> {new_prefix}")
    return ConversationHandler.END

# --- EXISTING ADMIN OPS IMPROVED ---

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
    await update.message.reply_text("📦 Preparing full data dump...")
    
    # IMPROVEMENT: Fetch all instead of limit 1000
    cursor = reminders_col.find({})
    reminders = await cursor.to_list(length=None)
    
    for r in reminders:
        r['_id'] = str(r['_id'])

    file_path = f"backup_{datetime.now().strftime('%Y%m%d')}.json"
    with open(file_path, "w") as f:
        json.dump(reminders, f, indent=4)
    
    await update.message.reply_document(document=open(file_path, "rb"), filename=file_path)
    os.remove(file_path)

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Use: `/setkey PASSWORD`")
        return
    secret_key = context.args[0].strip()
    chat_id = update.effective_chat.id
    await group_keys_col.update_one({"chat_id": chat_id}, {"$set": {"secret_key": secret_key}}, upsert=True)
    await update.message.reply_text(f"🔒 Security password set for this group!")

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text("❌ Format: `/autobulk START_ID END_ID PREFIX`")
        return

    try:
        start_id, end_id = int(context.args[0]), int(context.args[1])
        prefix = context.args[2].upper().strip()
    except:
        await update.message.reply_text("❌ Invalid numbers.")
        return

    chat_id = update.effective_chat.id
    
    # SMART CHECK: Find how many items already exist for this prefix in this group
    existing_count = await codes_col.count_documents({
        "chat_id": chat_id,
        "code": {"$regex": f"^{prefix}"}
    })
    
    start_num = existing_count + 1
    msg = await update.message.reply_text(f"🔍 Found {existing_count} items for {prefix}.\n🔄 Starting bulk indexing from {prefix}{start_num:03d}...")

    success = 0
    current_num = start_num
    for msg_id in range(start_id, end_id + 1):
        code = f"{prefix}{current_num:03d}"
        await codes_col.update_one({"code": code}, {"$set": {"chat_id": chat_id, "message_id": msg_id}}, upsert=True)
        success += 1
        current_num += 1
        if success % 20 == 0: await msg.edit_text(f"🔄 Progress: {success} items indexed (currently at {code})...")

    await msg.edit_text(f"✅ Matrix build finalized!\n📦 Indexed {success} new assets.\n🔢 Range: {prefix}{start_num:03d} to {prefix}{current_num-1:03d}")

async def admin_palette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command palette GUI."""
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📊 System Stats", callback_data="pal_stats"), InlineKeyboardButton("📦 Export Data", callback_data="pal_export")],
        [InlineKeyboardButton("🛠️ Manage DB", callback_data="pal_manage"), InlineKeyboardButton("🔒 Set Group Key", callback_data="pal_setkey")],
        [InlineKeyboardButton("⏰ New Reminder", callback_data="pal_remind"), InlineKeyboardButton("❌ Close", callback_data="pal_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🛠️ **ADMIN COMMAND PALETTE**\n"
        "Welcome, Your Honor. Select a quick action below:\n\n"
        "📝 **Manual Commands:**\n"
        "• `/save CODE` - Reply to an asset\n"
        "• `/autobulk START END PREFIX` - Bulk index\n"
        "• `/remind` - Setup notification",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_palette_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split('_')[1]
    
    if action == "stats":
        await get_stats(update, context)
        await query.answer()
    elif action == "export":
        await export_data(update, context)
        await query.answer()
    elif action == "manage":
        await manage_db_start(update, context)
        await query.answer()
        await query.delete_message()
    elif action == "setkey":
        await query.edit_message_text("To set a key, use: `/setkey YOUR_PASSWORD` inside the target group.")
        await query.answer()
    elif action == "remind":
        await start_remind(update, context)
        await query.answer()
        await query.delete_message()
    elif action == "close":
        await query.delete_message()
        await query.answer()

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args:
        await update.message.reply_text("❌ Reply to a message with `/save CODE`")
        return
    code = context.args[0].upper().strip()
    await codes_col.update_one(
        {"code": code}, 
        {"$set": {"chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id}}, 
        upsert=True
    )
    await update.message.reply_text(f"✅ Saved as `{code}`", parse_mode="Markdown")

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
        timezone_str = "Asia/Tashkent" if "Tashkent" in choice else "UTC"
    
    context.user_data['timezone'] = timezone_str
    await update.message.reply_text(f"✅ Timezone: {timezone_str}\nStep 2: Date (YYYY-MM-DD):")
    return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%Y-%m-%d")
        context.user_data['target_date'] = update.message.text
        await update.message.reply_text("Step 3: Time (HH:MM):")
        return GET_TIME
    except:
        await update.message.reply_text("❌ Use YYYY-MM-DD.")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%H:%M")
        context.user_data['reminder_time'] = update.message.text
        await update.message.reply_text("Step 4: Label:")
        return GET_LABEL
    except:
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
    else:
        result = await reminders_col.insert_one(data)
        data['_id'] = result.inserted_id
    
    schedule_reminder_job(application, data)
    await update.message.reply_text(f"✅ Reminder active!")
    context.user_data.clear()
    return ConversationHandler.END

# --- CORE ROUTING & SECURITY ---

async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    
    # Ignore if in a conversation (Fixes overlapping)
    if context.user_data.get('timezone') or context.user_data.get('manage_prefix'): return

    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    text = update.message.text.strip()
    is_admin = (user_id == ADMIN_ID)

    # PASSWORD UNLOCK STATE
    pending_group = context.user_data.get('pending_unlock_group_id')
    if pending_group and not is_admin:
        try: await context.bot.delete_message(chat_id, update.message.message_id)
        except: pass

        key_record = await group_keys_col.find_one({"chat_id": pending_group})
        if key_record and text == key_record["secret_key"]:
            await unlocked_groups_col.update_one({"user_id": user_id}, {"$addToSet": {"unlocked_chats": pending_group}}, upsert=True)
            
            # Cleanup alert and resume
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id: 
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            
            saved_code = context.user_data.pop('interrupted_file_code', None)
            context.user_data.pop('pending_unlock_group_id', None)
            
            if saved_code:
                record = await codes_col.find_one({"code": saved_code})
                if record: await execute_file_delivery(chat_id, record, context)
        else:
            msg = await update.message.reply_text("❌ Access Denied. Correct key?")
            context.job_queue.run_once(delete_msg_callback, 10, data={"chat_id": chat_id, "message_id": msg.message_id})
        return

    # CODE LOOKUP
    record = await codes_col.find_one({"code": text.upper()})
    if record:
        try: await context.bot.delete_message(chat_id, update.message.message_id)
        except: pass

        if not is_admin:
            gate = await group_keys_col.find_one({"chat_id": record["chat_id"]})
            if gate:
                user_auth = await unlocked_groups_col.find_one({"user_id": user_id})
                if not user_auth or record["chat_id"] not in user_auth.get("unlocked_chats", []):
                    context.user_data['pending_unlock_group_id'] = record["chat_id"]
                    context.user_data['interrupted_file_code'] = text.upper()
                    alert = await context.bot.send_message(chat_id, "🔒 This collection is locked. Enter SECRET_KEY:")
                    context.user_data['alert_message_id'] = alert.message_id
                    return
        
        await execute_file_delivery(chat_id, record, context)

async def execute_file_delivery(chat_id: int, record: dict, context: ContextTypes.DEFAULT_TYPE):
    try:
        copied = await context.bot.copy_message(chat_id=chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"])
        warn = await context.bot.send_message(chat_id, "⚠️ **EPHEMERAL:** Self-destruct in 6m.", parse_mode="Markdown")
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": copied.message_id})
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": warn.message_id})
    except Exception as e:
        logger.error(f"Delivery error: {e}")

# --- APP SETUP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Main Conversations
    remind_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={
            GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)],
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)],
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)],
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
    )

    manage_conv = ConversationHandler(
        entry_points=[CommandHandler("manage", manage_db_start)],
        states={
            MANAGE_CHOOSE_PREFIX: [CallbackQueryHandler(handle_manage_prefix)],
            MANAGE_ACTION: [
                CallbackQueryHandler(handle_manage_action, pattern="^act_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_prefix_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
    )
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("👋 Assistant Ready.\n/remind - New Reminder\n/manage - Admin DB Edit\n/stats - System Status")))
    app.add_handler(CommandHandler("list", lambda u,c: u.message.reply_text("Use /remind to edit or /stats for counts."))) # Placeholder for brevity
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("setkey", set_group_key))
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    
    app.add_handler(CommandHandler("admin", admin_palette))
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    
    app.add_handler(remind_conv)
    app.add_handler(manage_conv)
    app.add_handler(CallbackQueryHandler(lambda u,c: None)) # Catch-all
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_routing_manager))
    
    return app

flask_app = Flask(__name__)

@flask_app.route('/')
def health(): return "Master Storage Engine Live Cluster Online."

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if main_loop:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), main_loop)
    return "OK"

async def main():
    global application, main_loop
    main_loop = asyncio.get_running_loop()
    application = create_application()
    
    await application.initialize()
    await application.start()
    
    # Re-schedule reminders on boot
    cursor = reminders_col.find({})
    async for r in cursor: schedule_reminder_job(application, r)
    
    await application.bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    
    from werkzeug.serving import make_server
    import threading
    server = make_server('0.0.0.0', PORT, flask_app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
