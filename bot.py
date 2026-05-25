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

# Initialize MongoDB & Timezone Finder
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client.telegram_bot
reminders_col = db.reminders
codes_col = db.saved_codes
group_keys_col = db.group_keys
unlocked_groups_col = db.unlocked_users
tf = TimezoneFinder()

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL = range(4)
MANAGE_CHOOSE_PREFIX, MANAGE_ACTION = range(4, 6)

# Global application and loop instances
application = None
main_loop = None

# --- UTILS & BACKGROUND WORKERS ---

async def delete_msg_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.data["chat_id"], message_id=job.data["message_id"])
    except Exception as e:
        logger.warning(f"Cleanup note: {e}")

def schedule_reminder_job(app, reminder_data):
    if not app.job_queue: return
    try:
        user_tz = pytz.timezone(reminder_data['timezone'])
        h, m = map(int, reminder_data['reminder_time'].split(':'))
        reminder_time = time(hour=h, minute=m)
        job_name = str(reminder_data['_id'])
        current_jobs = app.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs: job.schedule_removal()

        app.job_queue.run_daily(
            send_daily_reminder,
            time=reminder_time,
            timezone=user_tz,
            chat_id=reminder_data['user_id'],
            data=reminder_data,
            name=job_name
        )
    except Exception as e:
        logger.error(f"Scheduling error: {e}")

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
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}}}, {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=100)
    if not prefixes:
        await update.message.reply_text("Database empty.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"{p['_id']} ({p['count']} items)", callback_data=f"pref_{p['_id']}")] for p in prefixes]
    await update.message.reply_text("🛠️ **DB Management**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MANAGE_CHOOSE_PREFIX

async def handle_manage_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix = query.data.split('_')[1]
    context.user_data['manage_prefix'] = prefix
    keyboard = [[InlineKeyboardButton("Change Prefix ✏️", callback_data="act_rename"), InlineKeyboardButton("Delete All 🗑️", callback_data="act_delete")], [InlineKeyboardButton("Cancel ❌", callback_data="act_cancel")]]
    await query.edit_message_text(f"Selected: **{prefix}**\nAction?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MANAGE_ACTION

async def handle_manage_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_')[1]
    prefix = context.user_data.get('manage_prefix')
    if action == "delete":
        await codes_col.delete_many({"code": {"$regex": f"^{prefix}"}})
        await query.edit_message_text(f"✅ Deleted prefix '{prefix}'.")
        return ConversationHandler.END
    elif action == "rename":
        await query.edit_message_text(f"Enter NEW prefix for '{prefix}':")
        return MANAGE_ACTION
    else:
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

async def handle_new_prefix_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_p = context.user_data.get('manage_prefix')
    new_p = update.message.text.upper().strip()
    cursor = codes_col.find({"code": {"$regex": f"^{old_p}"}})
    async for doc in cursor:
        await codes_col.update_one({"_id": doc["_id"]}, {"$set": {"code": doc['code'].replace(old_p, new_p, 1)}})
    await update.message.reply_text(f"✅ Renamed {old_p} -> {new_p}")
    return ConversationHandler.END

# --- ADMIN OPS ---

async def admin_palette_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates the Admin Palette text and keyboard."""
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="pal_stats"), InlineKeyboardButton("📦 Export", callback_data="pal_export")],
        [InlineKeyboardButton("🛠️ Manage", callback_data="pal_manage"), InlineKeyboardButton("🔒 Key", callback_data="pal_setkey")],
        [InlineKeyboardButton("⏰ New Reminder", callback_data="pal_remind"), InlineKeyboardButton("❌ Close", callback_data="pal_close")]
    ]
    text = (
        "👑 **MASTER ADMIN DASHBOARD**\n\n"
        "Welcome, Your Honor. The system is fully operational.\n\n"
        "🛠️ **Quick Management:**\n"
        "• Use buttons below for database & system control.\n\n"
        "📝 **Manual Commands:**\n"
        "• `/save CODE` - Reply to an asset to index it.\n"
        "• `/autobulk START END PREFIX` - Mass index messages.\n"
        "• `/setkey PASSWORD` - Lock the current group chat."
    )
    return text, InlineKeyboardMarkup(keyboard)

async def handle_palette_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split('_')[1]
    if action == "stats": await get_stats(update, context)
    elif action == "export": await export_data(update, context)
    elif action == "manage": await manage_db_start(update, context); await query.delete_message()
    elif action == "remind": await start_remind(update, context); await query.delete_message()
    elif action == "close": await query.delete_message()
    await query.answer()

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r_c = await reminders_col.count_documents({})
    c_c = await codes_col.count_documents({})
    k_c = await group_keys_col.count_documents({})
    await update.effective_message.reply_text(
        f"📊 **System Statistics**\n\n"
        f"📅 Active Reminders: {r_c}\n"
        f"🔑 Indexed Assets: {c_c}\n"
        f"🔐 Locked Groups: {k_c}",
        parse_mode="Markdown"
    )

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor = reminders_col.find({})
    data = await cursor.to_list(length=None)
    for r in data: r['_id'] = str(r['_id'])
    with open("export.json", "w") as f: json.dump(data, f, indent=4)
    await update.effective_message.reply_document(document=open("export.json", "rb"), filename="database_backup.json")
    os.remove("export.json")

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args: 
        await update.message.reply_text("❌ Usage: `/setkey PASSWORD`")
        return
    await group_keys_col.update_one({"chat_id": update.effective_chat.id}, {"$set": {"secret_key": context.args[0].strip()}}, upsert=True)
    await update.message.reply_text("🔒 Custom security password set for this group partition!")

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) < 3: 
        await update.message.reply_text("❌ Usage: `/autobulk START END PREFIX`")
        return
    try:
        start_id, end_id, prefix = int(context.args[0]), int(context.args[1]), context.args[2].upper().strip()
        exist = await codes_col.count_documents({"chat_id": update.effective_chat.id, "code": {"$regex": f"^{prefix}"}})
        curr = exist + 1
        msg = await update.message.reply_text(f"🔄 Starting from {prefix}{curr:03d}...")
        for m_id in range(start_id, end_id + 1):
            code = f"{prefix}{curr:03d}"
            await codes_col.update_one({"code": code}, {"$set": {"chat_id": update.effective_chat.id, "message_id": m_id}}, upsert=True)
            curr += 1
        await msg.edit_text(f"✅ Matrix build finalized! Indexed up to {prefix}{curr-1:03d}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args: 
        await update.message.reply_text("❌ Reply to a message with `/save CODE`")
        return
    code = context.args[0].upper().strip()
    await codes_col.update_one({"code": code}, {"$set": {"chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id}}, upsert=True)
    await update.message.reply_text(f"✅ Mapping for '{code}' written to index!")

# --- REMINDERS ---

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    await update.effective_message.reply_text("Step 1: Select timezone or share location:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return GET_TZ_CHOICE

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = tf.timezone_at(lng=update.message.location.longitude, lat=update.message.location.latitude) if update.message.location else "Asia/Tashkent"
    context.user_data['timezone'] = tz
    await update.message.reply_text(f"✅ Timezone: {tz}\nStep 2: Enter target date (YYYY-MM-DD):")
    return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%Y-%m-%d")
        context.user_data['target_date'] = update.message.text
        await update.message.reply_text("Step 3: Daily reminder time (HH:MM in 24h format):")
        return GET_TIME
    except:
        await update.message.reply_text("❌ Use YYYY-MM-DD.")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%H:%M")
        context.user_data['reminder_time'] = update.message.text
        await update.message.reply_text("Step 4: Label for this reminder:")
        return GET_LABEL
    except:
        await update.message.reply_text("❌ Use HH:MM.")
        return GET_TIME

async def handle_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = {"user_id": update.effective_user.id, "timezone": context.user_data['timezone'], "target_date": context.user_data['target_date'], "reminder_time": context.user_data['reminder_time'], "label": update.message.text}
    res = await reminders_col.insert_one(data)
    data['_id'] = res.inserted_id
    schedule_reminder_job(application, data)
    await update.message.reply_text("✅ New reminder is active!")
    return ConversationHandler.END

# --- GREETING LOGIC ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        text, markup = await admin_palette_msg(update, context)
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        greeting = (
            "👋 **Welcome to your Personal Assistant Node.**\n\n"
            "⏰ **Reminder System:**\n"
            "• `/remind` - Schedule a daily countdown to a specific date.\n"
            "• Set your timezone via location or manual selection.\n\n"
            "📦 **Storage Infrastructure:**\n"
            "• Enter a valid Alphanumeric Code (e.g., `AAA001`) to retrieve target assets.\n"
            "• **Note:** Some collections require a `SECRET_KEY` for access.\n\n"
            "💡 **Getting Started:**\n"
            "Just type `/remind` to set your first alert or enter a file code to get data!"
        )
        await update.message.reply_text(greeting, parse_mode="Markdown")

# --- ROUTING ---

async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    if context.user_data.get('timezone') or context.user_data.get('manage_prefix'): return
    
    user_id, chat_id, text = update.effective_user.id, update.effective_chat.id, update.message.text.strip().upper()
    is_admin = (user_id == ADMIN_ID)

    # UNLOCK
    pending = context.user_data.get('pending_unlock_group_id')
    if pending and not is_admin:
        try: await context.bot.delete_message(chat_id, update.message.message_id)
        except: pass
        gate = await group_keys_col.find_one({"chat_id": pending})
        if gate and text == gate["secret_key"].upper():
            await unlocked_groups_col.update_one({"user_id": user_id}, {"$addToSet": {"unlocked_chats": pending}}, upsert=True)
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id: 
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            code = context.user_data.pop('interrupted_file_code', None)
            context.user_data.pop('pending_unlock_group_id', None)
            if code:
                record = await codes_col.find_one({"code": code})
                if record: await execute_file_delivery(chat_id, record, context)
        else:
            m = await update.message.reply_text("❌ Access Denied. Correct key?")
            context.job_queue.run_once(delete_msg_callback, 10, data={"chat_id": chat_id, "message_id": m.message_id})
        return

    # LOOKUP
    record = await codes_col.find_one({"code": text})
    if record:
        try: await context.bot.delete_message(chat_id, update.message.message_id)
        except: pass
        if not is_admin:
            gate = await group_keys_col.find_one({"chat_id": record["chat_id"]})
            if gate:
                auth = await unlocked_groups_col.find_one({"user_id": user_id})
                if not auth or record["chat_id"] not in auth.get("unlocked_chats", []):
                    context.user_data['pending_unlock_group_id'] = record["chat_id"]
                    context.user_data['interrupted_file_code'] = text
                    alert = await context.bot.send_message(chat_id, "🔒 This collection is locked. Enter SECRET_KEY:")
                    context.user_data['alert_message_id'] = alert.message_id
                    return
        await execute_file_delivery(chat_id, record, context)

async def execute_file_delivery(chat_id, record, context):
    try:
        copied = await context.bot.copy_message(chat_id=chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"])
        warn = await context.bot.send_message(chat_id, "⚠️ **EPHEMERAL:** This data will self-destruct in 6 minutes.", parse_mode="Markdown")
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": copied.message_id})
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": warn.message_id})
    except Exception as e:
        logger.error(f"Error: {e}")

# --- APP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    rem_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)], GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)], GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)], GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)]},
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
    )
    man_conv = ConversationHandler(
        entry_points=[CommandHandler("manage", manage_db_start)],
        states={MANAGE_CHOOSE_PREFIX: [CallbackQueryHandler(handle_manage_prefix)], MANAGE_ACTION: [CallbackQueryHandler(handle_manage_action, pattern="^act_"), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_prefix_text)]},
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
    )
    
    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", lambda u,c: start_command(u,c))) # Manual trigger
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("setkey", set_group_key))
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    
    app.add_handler(rem_conv)
    app.add_handler(man_conv)
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
