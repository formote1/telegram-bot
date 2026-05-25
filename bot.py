import os
import logging
import asyncio
import sys
import json
import threading
import html
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
from werkzeug.serving import make_server

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO, 
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION & ENV VALIDATION ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID_RAW = os.getenv("ADMIN_USER_ID", "0")
PORT = int(os.getenv("PORT", 10000))

# Validate critical variables
missing_vars = []
if not TOKEN: missing_vars.append("TELEGRAM_TOKEN")
if not MONGO_URI: missing_vars.append("MONGO_URI")
if not RENDER_URL: missing_vars.append("RENDER_EXTERNAL_URL")

if missing_vars:
    logger.critical(f"❌ MISSING ENV VARS: {', '.join(missing_vars)}")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0

# --- DATABASE INITIALIZATION ---
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
db = client.telegram_bot if client is not None else None
reminders_col = db.reminders if db is not None else None
codes_col = db.saved_codes if db is not None else None
group_keys_col = db.group_keys if db is not None else None
unlocked_groups_col = db.unlocked_users if db is not None else None
logs_col = db.system_logs if db is not None else None
users_col = db.users if db is not None else None # New collection for user data

# Initialize TimezoneFinder once
logger.info("Initializing TimezoneFinder...")
try:
    tf = TimezoneFinder()
    logger.info("TimezoneFinder initialized.")
except Exception as e:
    logger.error(f"Failed to initialize TimezoneFinder: {e}")
    tf = None

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL = range(4)
MANAGE_CHOOSE_PREFIX = 4

# Global instances
application = None
main_loop = None

# --- UTILS & BACKGROUND WORKERS ---

async def log_event(user_id, username, action):
    if logs_col is None: return
    try:
        await logs_col.insert_one({
            "timestamp": datetime.utcnow(),
            "user_id": user_id,
            "username": username or "Unknown",
            "action": action
        })
    except Exception as e:
        logger.error(f"Logging error: {e}")

async def save_user_info(user, location=None):
    if users_col is None: return
    try:
        update_data = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_seen": datetime.utcnow()
        }
        if location:
            update_data["location"] = {
                "lat": location.latitude,
                "lng": location.longitude
            }
        await users_col.update_one(
            {"user_id": user.id},
            {"$set": update_data},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving user info: {e}")

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
        reminder_time = time(hour=h, minute=m, tzinfo=user_tz)
        job_name = str(reminder_data['_id'])
        for job in app.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
        app.job_queue.run_daily(send_daily_reminder, time=reminder_time, chat_id=reminder_data['user_id'], data=reminder_data, name=job_name)
    except Exception as e: logger.error(f"Scheduling error: {e}")

async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    try:
        tz = pytz.timezone(data['timezone'])
        now = datetime.now(tz).date()
        target = datetime.strptime(data['target_date'], "%Y-%m-%d").date()
        days_left = (target - now).days
        if days_left >= 0:
            msg = f"🔔 REMINDER: {days_left} days left to '{data['label']}'!" if days_left > 0 else f"🎉 TODAY IS THE DAY: '{data['label']}'!"
            await context.bot.send_message(chat_id=job.chat_id, text=msg)
            if days_left == 0 and reminders_col is not None:
                await reminders_col.delete_one({"_id": data['_id']})
                job.schedule_removal()
        else: job.schedule_removal()
    except Exception as e: logger.error(f"Reminder error: {e}")

# --- MASTER CONSOLE & ADMIN OPS ---

async def admin_palette_msg():
    keyboard = [
        [InlineKeyboardButton("📊 System Stats", callback_data="pal_stats"), InlineKeyboardButton("📦 Export Data", callback_data="pal_export")],
        [InlineKeyboardButton("📜 All Reminders", callback_data="pal_alllists"), InlineKeyboardButton("🗝️ Key Matrix", callback_data="pal_keys")],
        [InlineKeyboardButton("🗑️ Manage DB", callback_data="pal_manage"), InlineKeyboardButton("📋 System Logs", callback_data="pal_logs")]
    ]
    text = (
        "👑 **MASTER CONSOLE** 🔞\n"
        "───────────────────────\n"
        "**System Status:** 🌐 Operational\n"
        "───────────────────────\n"
        "📜 **COMPLETE COMMAND LIST:**\n\n"
        "**Core Commands:**\n"
        "• `/start` - Launch node / Admin Dashboard\n"
        "• `/remind` - Setup new countdown reminder\n"
        "• `/list` - View your personal reminders\n\n"
        "**Database Matrix:**\n"
        "• `/save CODE` - Index message (by reply)\n"
        "• `/autobulk START END PREFIX` - Mass index\n"
        "• `/del PREFIX START END` - Surgical range delete\n"
        "• `/setkey PASSWORD` - Secure group partition\n\n"
        "**Monitoring:**\n"
        "• `/stats` - Live system audit report\n"
        "• `/export` - Full database JSON backup\n"
        "• `/admin` - Re-trigger this Master Console\n"
        "───────────────────────\n"
        "Select a monitoring tool below:"
    )
    return text, InlineKeyboardMarkup(keyboard)

async def handle_palette_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split('_')[1]
    if action == "stats": await get_stats(update, context)
    elif action == "users": await get_user_directory(update, context) # New action
    elif action == "export": await export_data(update, context)
    elif action == "alllists": await get_all_lists(update, context)
    elif action == "keys": await get_key_matrix(update, context)
    elif action == "logs": await get_system_logs(update, context)
    elif action == "manage": await manage_db_gui(update, context); await query.delete_message()
    await query.answer()

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if codes_col is None: return
    r_c = await reminders_col.count_documents({})
    c_c = await codes_col.count_documents({})
    l_c = await logs_col.count_documents({})
    k_c = await group_keys_col.count_documents({})
    u_c = await users_col.count_documents({}) if users_col is not None else 0
    
    msg = (
        "📊 **SYSTEM AUDIT REPORT**\n"
        "───────────────────\n"
        f"📅 **Total Reminders:**   `{r_c:03d}`\n"
        f"🔑 **Indexed Assets:**   `{c_c:03d}`\n"
        f"🔐 **Locked Groups:**    `{k_c:03d}`\n"
        f"📋 **Stored Logs:**      `{l_c:03d}`\n"
        f"👤 **Unique Users:**     `{u_c:03d}`\n"
        "───────────────────\n"
        "*Auto-cleaning active: Logs purge every 7 days.*"
    )
    # Add User Directory button as requested
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 View User Directory", callback_data="pal_users")]])
    await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def get_user_directory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if users_col is None: return
    try:
        cursor = users_col.find({}).sort("last_seen", -1).limit(50)
        users = await cursor.to_list(length=50)
        if not users: return await update.effective_message.reply_text("👤 No user data found.")
        
        report = ["👤 <b>USER DIRECTORY</b>\n"]
        for u in users:
            uname = f"@{u.get('username')}" if u.get('username') else "No Username"
            name = html.escape(f"{u.get('first_name', '')} {u.get('last_name', '')}".strip())
            loc = u.get('location')
            loc_str = f"📍 {loc['lat']:.2f}, {loc['lng']:.2f}" if loc else "📍 No Location"
            report.append(f"• <b>{name}</b> ({uname})\n  {loc_str}")
        
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"User directory error: {e}")
        await update.effective_message.reply_text("❌ Failed to retrieve user directory.")

async def get_all_lists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    try:
        cursor = reminders_col.find({}).sort("user_id", 1)
        reminders = await cursor.to_list(length=50)
        if not reminders: return await update.effective_message.reply_text("📜 No active reminders.")
        report = ["📜 <b>GLOBAL REMINDER AUDIT</b>\n"]
        for r in reminders:
            label = html.escape(str(r.get('label', 'No Label')))
            report.append(f"👤 <code>{r['user_id']}</code>: {label} ({r['target_date']})")
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Audit error: {e}")
        await update.effective_message.reply_text("❌ Audit failed.")

async def get_key_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if codes_col is None: return
    try:
        pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}, "chat_id": 1}}, {"$group": {"_id": {"prefix": "$prefix", "chat_id": "$chat_id"}, "count": {"$sum": 1}}}]
        cursor = codes_col.aggregate(pipeline)
        results = await cursor.to_list(length=100)
        if not results: return await update.effective_message.reply_text("🗝️ No indexed groups.")
        report = ["🗝️ <b>LIVE SECRET KEY MATRIX</b>\n"]
        for r in results:
            prefix, chat_id, count = r['_id']['prefix'], r['_id']['chat_id'], r['count']
            key_record = await group_keys_col.find_one({"chat_id": chat_id})
            passkey = html.escape(key_record["secret_key"] if key_record else "NO KEY SET")
            report.append(f"• <code>{prefix}</code> - {count:02d} items  =>  <code>{passkey}</code>")
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Key Matrix error: {e}")
        await update.effective_message.reply_text("❌ Key Matrix failed.")

async def get_system_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if logs_col is None: return
    try:
        cursor = logs_col.find({}).sort("timestamp", -1).limit(15)
        logs = await cursor.to_list(length=15)
        if not logs: 
            return await update.effective_message.reply_text("📋 Logs empty.")
        
        report = ["📋 <b>SYSTEM ACTIVITY LOGS</b>\n"]
        for l in logs:
            time_str = l['timestamp'].strftime('%H:%M:%S')
            user = html.escape(str(l.get('username', 'Unknown')))
            action = html.escape(str(l.get('action', 'Unknown')))
            report.append(f"<code>[{time_str}]</code> {user}: {action}")
        
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error retrieving logs: {e}")
        await update.effective_message.reply_text("❌ Failed to retrieve logs.")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    cursor = reminders_col.find({})
    data = await cursor.to_list(length=None)
    for r in data: r['_id'] = str(r['_id'])
    file_path = f"backup_{datetime.now().strftime('%Y%m%d')}.json"
    with open(file_path, "w") as f: json.dump(data, f, indent=4)
    await update.effective_message.reply_document(document=open(file_path, "rb"), filename=file_path)
    os.remove(file_path)

async def manage_db_gui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if codes_col is None: return
    pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}}}, {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=100)
    if not prefixes: return await update.effective_message.reply_text("Database empty.")
    keyboard = [[InlineKeyboardButton(f"Wipe {p['_id']} ({p['count']} items)", callback_data=f"pref_wipe_{p['_id']}")] for p in prefixes]
    await update.effective_message.reply_text("🗑️ **Select Prefix to WIPE ENTIRELY:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_CHOOSE_PREFIX

async def handle_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix = query.data.split('_')[2]
    await codes_col.delete_many({"code": {"$regex": f"^{prefix}"}})
    await query.edit_message_text(f"🔥 **NUKE COMPLETE:** `{prefix}` vaporized.", parse_mode="Markdown")
    await log_event(ADMIN_ID, "ADMIN", f"Full Wipe: {prefix}")
    return ConversationHandler.END

async def range_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    if len(context.args) < 3: return await update.message.reply_text("❌ `/del PREFIX START END`", parse_mode="Markdown")
    try:
        prefix, start, end = context.args[0].upper().strip(), int(context.args[1]), int(context.args[2])
        target_codes = [f"{prefix}{i:03d}" for i in range(start, end + 1)]
        result = await codes_col.delete_many({"code": {"$in": target_codes}})
        await update.message.reply_text(f"🗑️ vaporized `{result.deleted_count}` items.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Range Del: {prefix}{start:03d}-{end:03d}")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    if len(context.args) < 3: return
    try:
        start_id, end_id, prefix = int(context.args[0]), int(context.args[1]), context.args[2].upper().strip()
        exist = await codes_col.count_documents({"chat_id": update.effective_chat.id, "code": {"$regex": f"^{prefix}"}})
        curr = exist + 1
        msg = await update.message.reply_text(f"🔄 **Indexing {prefix} from {curr:03d}...**", parse_mode="Markdown")
        for m_id in range(start_id, end_id + 1):
            await codes_col.update_one({"code": f"{prefix}{curr:03d}"}, {"$set": {"chat_id": update.effective_chat.id, "message_id": m_id}}, upsert=True)
            curr += 1
        await msg.edit_text(f"✅ **Build Finalized!** Indexed up to `{prefix}{curr-1:03d}`", parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args or codes_col is None: return
    code = context.args[0].upper().strip()
    await codes_col.update_one({"code": code}, {"$set": {"chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id}}, upsert=True)
    await update.message.reply_text(f"✅ Saved as `{code}`", parse_mode="Markdown")

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args or group_keys_col is None: return
    await group_keys_col.update_one({"chat_id": update.effective_chat.id}, {"$set": {"secret_key": context.args[0].strip()}}, upsert=True)
    await update.message.reply_text("🔒 Key set for this group!")

# --- REMINDERS ---

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    user_id = update.effective_user.id
    try:
        cursor = reminders_col.find({"user_id": user_id})
        reminders = await cursor.to_list(length=10)
        if not reminders: return await update.effective_message.reply_text("No active reminders.")
        for r in reminders:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Delete 🗑️", callback_data=f"delrem_{r['_id']}")]])
            label = html.escape(str(r.get('label', 'No Label')))
            await update.effective_message.reply_text(
                f"🔔 <b>{label}</b>\n📅 {r['target_date']} | ⏰ {r['reminder_time']}", 
                reply_markup=kb, 
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"List error: {e}")
        await update.effective_message.reply_text("❌ Failed to list reminders.")

async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, r_id = query.data.split('_')
    if action == "delrem" and reminders_col is not None:
        await reminders_col.delete_one({"_id": ObjectId(r_id)})
        await query.edit_message_text("❌ Reminder deleted.")
    await query.answer()

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear() # Clear state for fresh start
    kb = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    await update.effective_message.reply_text(
        "Step 1: Timezone\nPlease select a city or share your location for precision:", 
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
    )
    return GET_TZ_CHOICE

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        user = update.effective_user
        if not msg: return GET_TZ_CHOICE

        # Capture user info regardless of choice
        await save_user_info(user, location=msg.location)

        # Robust location check
        if msg.location:
            logger.info(f"Received location update from user {user.id}")
            lat, lng = msg.location.latitude, msg.location.longitude
            if tf:
                timezone_str = tf.timezone_at(lng=lng, lat=lat) or "UTC"
            else:
                logger.error("TimezoneFinder not initialized. Using UTC.")
                timezone_str = "UTC"
        else:
            choice = msg.text or ""
            timezone_str = "Asia/Tashkent" if "Tashkent" in choice else "Asia/Tashkent"
                
        context.user_data['timezone'] = timezone_str
        await msg.reply_text(f"✅ Timezone set to: {timezone_str}\nStep 2: Enter target date (YYYY-MM-DD):")
        return GET_DATE

    except Exception as e:
        logger.error(f"CRITICAL Error in handle_tz_choice: {e}", exc_info=True)
        context.user_data['timezone'] = "Asia/Tashkent"
        await update.effective_message.reply_text(
            f"❌ Timezone detection failed. Defaulting to Asia/Tashkent.\n\nStep 2: Enter target date (YYYY-MM-DD):"
        )
        return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        datetime.strptime(text, "%Y-%m-%d")
        context.user_data['target_date'] = text
        await update.message.reply_text("Step 3: Enter time (HH:MM) - e.g., 09:00:")
        return GET_TIME
    except ValueError:
        await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD:")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        datetime.strptime(text, "%H:%M")
        context.user_data['reminder_time'] = text
        await update.message.reply_text("Step 4: Enter a label for this reminder:")
        return GET_LABEL
    except ValueError:
        await update.message.reply_text("❌ Invalid time format. Please use HH:MM (24h format):")
        return GET_TIME

async def handle_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return ConversationHandler.END
    user = update.effective_user
    data = {
        "user_id": user.id, 
        "timezone": context.user_data['timezone'], 
        "target_date": context.user_data['target_date'], 
        "reminder_time": context.user_data['reminder_time'], 
        "label": update.message.text
    }
    res = await reminders_col.insert_one(data)
    data['_id'] = res.inserted_id
    schedule_reminder_job(application, data)
    await log_event(user.id, user.username, f"Set reminder: {data['label']}")
    await update.message.reply_text("✅ New reminder is active!")
    context.user_data.clear()
    return ConversationHandler.END

# --- GREETING & ROUTING ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await save_user_info(user) # Basic info storage on start
    if user.id == ADMIN_ID:
        text, markup = await admin_palette_msg()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        greet = "👋 **Welcome.**\n\n⏰ `/remind` - Set countdown\n📜 `/list` - Manage reminders\n📦 Enter an asset code (e.g. `AAA001`) to retrieve data.\n\nCopyright © **NurAziz**"
        await update.message.reply_text(greet, parse_mode="Markdown")

async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or codes_col is None: return
    if context.user_data.get('timezone'): return
    
    user, chat_id, text = update.effective_user, update.effective_chat.id, update.message.text.strip().upper()
    is_admin = (user.id == ADMIN_ID)
    await save_user_info(user) # Track user activity

    # UNLOCK
    pending = context.user_data.get('pending_unlock_group_id')
    if pending and not is_admin:
        try: await context.bot.delete_message(chat_id, update.message.message_id)
        except: pass
        gate = await group_keys_col.find_one({"chat_id": pending})
        if gate and text == gate["secret_key"].upper():
            await unlocked_groups_col.update_one({"user_id": user.id}, {"$addToSet": {"unlocked_chats": pending}}, upsert=True)
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id: 
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            code = context.user_data.pop('interrupted_file_code', None)
            context.user_data.pop('pending_unlock_group_id', None)
            if code:
                record = await codes_col.find_one({"code": code})
                if record: await execute_file_delivery(chat_id, record, context, user)
        else:
            m = await update.message.reply_text("❌ Key Denied.")
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
                auth = await unlocked_groups_col.find_one({"user_id": user.id})
                if not auth or record["chat_id"] not in auth.get("unlocked_chats", []):
                    context.user_data['pending_unlock_group_id'] = record["chat_id"]
                    context.user_data['interrupted_file_code'] = text
                    alert = await context.bot.send_message(chat_id, "🔒 Collection Locked. Enter Key:")
                    context.user_data['alert_message_id'] = alert.message_id
                    return
        await execute_file_delivery(chat_id, record, context, user)

async def execute_file_delivery(chat_id, record, context, user):
    try:
        copied = await context.bot.copy_message(chat_id=chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"])
        await log_event(user.id, user.username, f"Requested asset: {record['code']}")
        warn = await context.bot.send_message(chat_id, "⚠️ **EPHEMERAL:** Self-destruct in 6 minutes.", parse_mode="Markdown")
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": copied.message_id})
        context.job_queue.run_once(delete_msg_callback, 360, data={"chat_id": chat_id, "message_id": warn.message_id})
    except Exception as e: logger.error(f"Delivery error: {e}")

# --- APP SETUP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    rem_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={
            GET_TZ_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), 
                MessageHandler(filters.LOCATION, handle_tz_choice)
            ], 
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)], 
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)], 
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)]
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
        per_message=False
    )
    man_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(manage_db_gui, pattern="^pal_manage$")],
        states={MANAGE_CHOOSE_PREFIX: [CallbackQueryHandler(handle_manage_callback, pattern="^pref_wipe_")]},
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))],
        per_message=False
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", start_command))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CommandHandler("del", range_delete))
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("setkey", set_group_key))
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern="^delrem_"))
    app.add_handler(rem_conv)
    app.add_handler(man_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_routing_manager))
    return app

flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Supreme Commander Node Online."

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if main_loop: 
        try:
            update_data = request.get_json(force=True)
            asyncio.run_coroutine_threadsafe(
                application.process_update(Update.de_json(update_data, application.bot)), 
                main_loop
            )
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")
    return "OK"

async def main():
    global application, main_loop
    if not TOKEN or not MONGO_URI or not RENDER_URL:
        server = make_server('0.0.0.0', PORT, flask_app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        while True: await asyncio.sleep(3600)
    
    main_loop = asyncio.get_running_loop()
    
    # DB Cleanup/Index
    try: 
        await logs_col.create_index("timestamp", expireAfterSeconds=604800)
        await users_col.create_index("user_id", unique=True)
    except: pass
    
    application = create_application()
    await application.initialize()
    await application.start()
    
    # Reschedule reminders
    try:
        cursor = reminders_col.find({})
        async for r in cursor: schedule_reminder_job(application, r)
    except: pass
    
    # Set Webhook
    try: await application.bot.set_webhook(url=f"{RENDER_URL.rstrip('/')}/{TOKEN}")
    except: pass
    
    server = make_server('0.0.0.0', PORT, flask_app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as e:
        logger.critical(f"FATAL SHUTDOWN: {e}", exc_info=True)
