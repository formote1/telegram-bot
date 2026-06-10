import os
import logging
import asyncio
import sys
import json
import threading
import html
import uuid
import calendar
from datetime import datetime, time, timedelta
import pytz
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent,
    InlineQueryResultCachedDocument, InlineQueryResultCachedVideo,
    InlineQueryResultCachedPhoto, InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice, InlineQueryResultCachedMpeg4Gif,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ConversationHandler,
    CallbackQueryHandler,
    InlineQueryHandler
)
from flask import Flask, request
from motor.motor_asyncio import AsyncIOMotorClient
from timezonefinder import TimezoneFinder
from bson import ObjectId
from werkzeug.serving import make_server
from pyfiglet import Figlet 
from PIL import Image

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
users_col = db.users if db is not None else None 
prefix_labels_col = db.prefix_labels if db is not None else None

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

# --- ASCII LOGIC (The "Gut") ---

# ascii characters from dark to light
ASCII_CHARS = [".", ",", ":", ";", "+", "*", "?", "%", "S", "#", "@"]

def resize_image(image, new_width=100):
    width, height = image.size
    ratio = height / width / 2.2 
    new_height = int(new_width * ratio)
    resized_image = image.resize((new_width, new_height))
    return resized_image

def grayify(image):
    grayscale_image = image.convert("L")
    return grayscale_image

def pixels_to_ascii(image):
    pixels = image.getdata()
    characters = "".join([ASCII_CHARS[pixel//25] for pixel in pixels])
    return characters

def process_image_to_ascii(image, new_width=100):
    # convert image to ascii
    new_image_data = pixels_to_ascii(grayify(resize_image(image, new_width)))

    # format
    pixel_count = len(new_image_data)
    ascii_image = "\n".join(new_image_data[i:(i+new_width)] for i in range(0, pixel_count, new_width))
    return ascii_image

# --- HANDLERS ---

async def ascii_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers the ASCII conversion for text or images."""
    message = update.effective_message
    user = update.effective_user
    
    # 1. Check for text input directly in command
    text_content = " ".join(context.args)
    
    # 2. Check for image (attached or replied to)
    photo = None
    if message.photo:
        photo = message.photo[-1]
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
    
    ascii_result = ""
    filename = f"ascii_{uuid.uuid4().hex[:8]}.txt"
    
    try:
        if photo:
            # Image Processing
            status_msg = await message.reply_text("⏳ Processing image...")
            file = await context.bot.get_file(photo.file_id)
            temp_img_path = f"temp_{filename}.jpg"
            await file.download_to_drive(temp_img_path)
            
            with Image.open(temp_img_path) as img:
                ascii_result = process_image_to_ascii(img)
            
            os.remove(temp_img_path)
            await status_msg.delete()
        elif text_content:
            # Text Processing
            f = Figlet(font='slant')
            ascii_result = f.renderText(text_content)
        else:
            await message.reply_text("❌ Usage: Send an image with caption `/ascii`, reply to an image with `/ascii`, or use `/ascii YOUR TEXT`.")
            return

        if ascii_result:
            with open(filename, "w") as f:
                f.write(ascii_result)
            
            with open(filename, "rb") as f:
                await message.reply_document(
                    document=f,
                    filename="ascii_art.txt",
                    caption="For better visuals open the file on desktop/laptop"
                )
            
            os.remove(filename)
            await log_event(user.id, user.username, f"Generated ASCII (Type: {'Image' if photo else 'Text'})")
            
    except Exception as e:
        logger.error(f"ASCII Error: {e}")
        await message.reply_text(f"❌ Failed to generate ASCII: {e}")
        if os.path.exists(filename): os.remove(filename)

# --- UTILS & BACKGROUND WORKERS ---

def extract_file_data(message):
    """Identifies the file type and ID from a message."""
    if message.document: return "document", message.document.file_id, message.caption
    if message.video: return "video", message.video.file_id, message.caption
    if message.photo: return "photo", message.photo[-1].file_id, message.caption
    if message.audio: return "audio", message.audio.file_id, message.caption
    if message.voice: return "voice", message.voice.file_id, message.caption
    if message.animation: return "animation", message.animation.file_id, message.caption
    return None, None, None

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
            if days_left > 0:
                if days_left < 15:
                    time_str = f"{days_left} day{'s' if days_left > 1 else ''}"
                else:
                    years = days_left // 365
                    rem_days = days_left % 365
                    weeks = rem_days // 7
                    days = rem_days % 7
                    
                    parts = []
                    if years > 0: parts.append(f"{years} year{'s' if years > 1 else ''}")
                    if weeks > 0: parts.append(f"{weeks} week{'s' if weeks > 1 else ''}")
                    if days > 0: parts.append(f"{days} day{'s' if days > 1 else ''}")
                    
                    time_str = " ".join(parts) if parts else "0 days"
                    
                msg = f"🔔 REMINDER: {time_str} left to '{data['label']}'!"
            else:
                msg = f"🎉 TODAY IS THE DAY: '{data['label']}'!"
                
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
        [InlineKeyboardButton("🗑️ Manage DB", callback_data="pal_manage"), InlineKeyboardButton("📋 System Logs", callback_data="pal_logs")],
        [InlineKeyboardButton("🔄 Sync Metadata", callback_data="pal_sync")]
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
        "• `/list` - View your personal reminders\n"
        "• `/ascii` - Generate ASCII art (Text/Image)\n\n"
        "**Database Matrix:**\n"
        "• `/save CODE` - Index message (by reply)\n"
        "• `/autobulk START END PREFIX` - Mass index\n"
        "• `/del CODE` - Single file delete\n"
        "• `/del PREFIX START END` - Surgical range delete\n"
        "• `/setkey PREFIX PASSWORD` - Secure prefix partition\n"
        "• `/setlabel PREFIX NAME` - Assign title to prefix\n"
        "• `/rename_prefix OLD NEW` - Bulk migrate prefix\n"
        "• `/refresh` - Sync missing metadata from database\n\n"
        "**File Retrieval Engine:**\n"
        "• `/get CODE` - Fetch a single specific asset\n"
        "• `/get PREFIX START END` - Sequential range delivery\n\n"
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
    elif action == "users": await get_user_directory(update, context) 
    elif action == "export": await export_data(update, context)
    elif action == "alllists": await get_all_lists(update, context)
    elif action == "keys": await get_key_matrix(update, context)
    elif action == "logs": await get_system_logs(update, context)
    elif action == "sync": await refresh_metadata(update, context)
    elif action == "manage": 
        context.user_data.clear()
        await manage_db_gui(update, context)
        await query.delete_message()
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
        f"📅 **Total Reminders:** `{r_c:03d}`\n"
        f"🔑 **Indexed Assets:** `{c_c:03d}`\n"
        f"🔐 **Locked Prefixes:** `{k_c:03d}`\n"
        f"📋 **Stored Logs:** `{l_c:03d}`\n"
        f"👤 **Unique Users:** `{u_c:03d}`\n"
        "───────────────────\n"
        "*Auto-cleaning active: Logs purge every 7 days.*"
    )
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
            key_record = await group_keys_col.find_one({"chat_id": chat_id, "prefix": prefix})
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
    if codes_col is None: return ConversationHandler.END
    pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}}}, {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=100)
    if not prefixes: 
        await update.effective_message.reply_text("Database empty.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"Wipe {p['_id']} ({p['count']} items)", callback_data=f"pref_wipe_{p['_id']}")] for p in prefixes]
    await update.effective_message.reply_text("🗑️ Select Prefix to WIPE ENTIRELY:", reply_markup=InlineKeyboardMarkup(keyboard))
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
    args = context.args
    if not args: 
        return await update.message.reply_text("❌ Usage:\nSingle: `/del CODE`\nRange: `/del PREFIX START END`", parse_mode="Markdown")
        
    try:
        if len(args) == 1:
            code = args[0].upper().strip()
            res = await codes_col.delete_one({"code": code})
            if res.deleted_count > 0:
                await update.message.reply_text(f"🗑️ vaporized `{code}`.", parse_mode="Markdown")
                await log_event(ADMIN_ID, "ADMIN", f"Single Del: {code}")
            else:
                await update.message.reply_text(f"❌ `{code}` not found.")
                
        elif len(args) == 3:
            prefix, start, end = args[0].upper().strip()[:3], int(args[1]), int(args[2])
            target_codes = [f"{prefix}{i:03d}" for i in range(start, end + 1)]
            result = await codes_col.delete_many({"code": {"$in": target_codes}})
            await update.message.reply_text(f"🗑️ vaporized `{result.deleted_count}` items.", parse_mode="Markdown")
            await log_event(ADMIN_ID, "ADMIN", f"Range Del: {prefix}{start:03d}-{end:03d}")
        else:
            await update.message.reply_text("❌ Usage:\nSingle: `/del CODE`\nRange: `/del PREFIX START END`", parse_mode="Markdown")
    except Exception as e: 
        await update.message.reply_text(f"❌ Error: {e}")

async def rename_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/rename_prefix OLD NEW`", parse_mode="Markdown")
    
    old_prefix = args[0].upper().strip()[:3]
    new_prefix = args[1].upper().strip()[:3]
    
    if old_prefix == new_prefix:
        return await update.message.reply_text("❌ Prefixes are the same.")

    status = await update.message.reply_text(f"🔄 **Migrating `{old_prefix}` to `{new_prefix}`...**", parse_mode="Markdown")
    
    try:
        cursor = codes_col.find({"code": {"$regex": f"^{old_prefix}"}})
        count = 0
        async for record in cursor:
            old_code = record["code"]
            suffix = old_code[3:]
            new_code = f"{new_prefix}{suffix}"
            await codes_col.update_one({"_id": record["_id"]}, {"$set": {"code": new_code}})
            count += 1
            
        await group_keys_col.update_many({"prefix": old_prefix}, {"$set": {"prefix": new_prefix}})
        
        await status.edit_text(f"✅ **Migration Complete!**\nMoved `{count}` items from `{old_prefix}` to `{new_prefix}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Rename Prefix: {old_prefix} -> {new_prefix} ({count} items)")
    except Exception as e:
        logger.error(f"Rename error: {e}")
        await status.edit_text(f"❌ **Migration Failed:** {e}")

async def refresh_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    query_filter = {"$or": [{"file_id": {"$exists": False}}, {"file_id": None}, {"file_id": ""}]}
    total_to_sync = await codes_col.count_documents(query_filter)
    if total_to_sync == 0:
        return await update.effective_message.reply_text("✅ All assets are already synced with metadata.")
    status_msg = await update.effective_message.reply_text(f"🔄 **Metadata Sync Initiated**\nScanning `{total_to_sync}` assets...", parse_mode="Markdown")
    count = 0
    cursor = codes_col.find(query_filter)
    async for record in cursor:
        try:
            probe = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=record["chat_id"], message_id=record["message_id"])
            f_type, f_id, caption = extract_file_data(probe)
            await probe.delete()
            if f_id:
                await codes_col.update_one({"_id": record["_id"]}, {"$set": {"file_type": f_type, "file_id": f_id, "caption": caption or ""}})
                count += 1
            if (count % 5 == 0) or count == total_to_sync:
                await status_msg.edit_text(f"🔄 **Syncing Metadata...**\nProgress: `{count}/{total_to_sync}` updated.", parse_mode="Markdown")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Metadata fetch failed for {record.get('code')}: {e}")
            continue
    await status_msg.edit_text(f"✅ **Sync Phase Finalized!**\nSuccessfully updated `{count}` assets.", parse_mode="Markdown")
    await log_event(ADMIN_ID, "ADMIN", f"Metadata Sync: {count} updated")

async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    if len(context.args) < 3: return
    try:
        start_id, end_id, prefix = int(context.args[0]), int(context.args[1]), context.args[2].upper().strip()[:3]
        exist = await codes_col.count_documents({"code": {"$regex": f"^{prefix}"}})
        curr = exist + 1
        status_msg = await update.message.reply_text(f"🔄 Probing `{prefix}` range from {start_id} to {end_id}...")
        indexed_count = 0
        for m_id in range(start_id, end_id + 1):
            try:
                probe = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=update.effective_chat.id, message_id=m_id)
                f_type, f_id, caption = extract_file_data(probe)
                await probe.delete()
                if f_id:
                    code_to_save = f"{prefix}{curr:03d}"
                    await codes_col.update_one({"code": code_to_save}, {"$set": {"chat_id": update.effective_chat.id, "message_id": m_id, "file_type": f_type, "file_id": f_id, "caption": caption or ""}}, upsert=True)
                    indexed_count += 1
                    curr += 1
            except: continue
        await status_msg.edit_text(f"✅ **Bulk Indexing Complete!** Indexed `{indexed_count}` items for `{prefix}`.")
        await log_event(ADMIN_ID, "ADMIN", f"Autobulk: {prefix} ({indexed_count} items)")
    except Exception as e: await update.message.reply_text(f"❌ Error during autobulk: {e}")

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not context.args or codes_col is None: return
    code = context.args[0].upper().strip()
    f_type, f_id, caption = extract_file_data(update.message.reply_to_message)
    if not f_id: return await update.message.reply_text("❌ No recognizable file found in the replied message.")
    data = {"chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id, "file_type": f_type, "file_id": f_id, "caption": caption or ""}
    await codes_col.update_one({"code": code}, {"$set": data}, upsert=True)
    await update.message.reply_text(f"✅ Indexed `{code}` (Type: {f_type})")

async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or group_keys_col is None: return
    if len(context.args) < 2: return await update.message.reply_text("❌ Usage: `/setkey PREFIX PASSWORD`")
    prefix = context.args[0].upper().strip()[:3]
    password = context.args[1].strip()
    await group_keys_col.update_one({"chat_id": update.effective_chat.id, "prefix": prefix}, {"$set": {"secret_key": password}}, upsert=True)
    await update.message.reply_text(f"🔒 Key set for prefix `{prefix}` in this group!")

async def set_prefix_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if len(context.args) < 2: return await update.message.reply_text("❌ Usage: `/setlabel PREFIX NAME`")
    prefix = context.args[0].upper().strip()[:3]
    name = " ".join(context.args[1:])
    await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"name": name}}, upsert=True)
    await update.message.reply_text(f"🏷️ Label set: `{prefix}` ➔ **{name}**", parse_mode="Markdown")

# --- REMINDERS (GUI ENHANCED) ---

def create_calendar(year=None, month=None):
    now = datetime.now()
    if year is None: year = now.year
    if month is None: month = now.month
    
    markup = []
    # Month/Year header (Clickable for jumps)
    markup.append([
        InlineKeyboardButton(calendar.month_name[month], callback_data=f"cal_view_months_{year}"),
        InlineKeyboardButton(str(year), callback_data=f"cal_view_years_{month}")
    ])
    # Days of week
    markup.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]])
    
    month_calendar = calendar.monthcalendar(year, month)
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                cb_data = f"date_sel_{year}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=cb_data))
        markup.append(row)
    
    # Navigation
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1
    
    markup.append([
        InlineKeyboardButton("<<", callback_data=f"cal_nav_{prev_y}_{prev_m}"),
        InlineKeyboardButton(">>", callback_data=f"cal_nav_{next_y}_{next_m}")
    ])
    return InlineKeyboardMarkup(markup)

def create_month_grid(year):
    markup = []
    for i in range(1, 13, 3):
        markup.append([InlineKeyboardButton(calendar.month_name[j], callback_data=f"cal_nav_{year}_{j}") for j in range(i, i+3)])
    markup.append([InlineKeyboardButton("Back to Calendar", callback_data=f"cal_nav_{year}_{datetime.now().month}")])
    return InlineKeyboardMarkup(markup)

def create_year_grid(month, start_year=None):
    if start_year is None: start_year = datetime.now().year
    current_year = datetime.now().year
    markup = []
    for i in range(start_year, start_year + 6, 3):
        markup.append([InlineKeyboardButton(str(j), callback_data=f"cal_nav_{j}_{month}") for j in range(i, min(i+3, current_year + 51))])
    
    # Finite navigation for 50 years
    nav_row = []
    if start_year > current_year:
        nav_row.append(InlineKeyboardButton("<<", callback_data=f"cal_view_years_{month}_{start_year-6}"))
    if start_year + 6 <= current_year + 50:
        nav_row.append(InlineKeyboardButton(">>", callback_data=f"cal_view_years_{month}_{start_year+6}"))
    
    if nav_row: markup.append(nav_row)
    markup.append([InlineKeyboardButton("Back to Calendar", callback_data=f"cal_nav_{current_year}_{month}")])
    return InlineKeyboardMarkup(markup)

def create_time_grid(hour=None):
    markup = []
    if hour is None:
        text = "Select Hour (24h):"
        for i in range(0, 24, 4):
            markup.append([InlineKeyboardButton(f"{j:02d}:00", callback_data=f"time_hour_{j:02d}") for j in range(i, i+4)])
    else:
        text = f"Selected {hour}:... now select Minute:"
        for i in range(0, 60, 15):
            markup.append([InlineKeyboardButton(f"{hour}:{j:02d}", callback_data=f"time_min_{hour}:{j:02d}") for j in range(i, i+15, 5)])
        markup.append([InlineKeyboardButton("Back to Hours", callback_data="time_back_hour")])
    return text, InlineKeyboardMarkup(markup)

def create_label_buttons():
    labels = [
        ["🎂 Birthday", "📅 Meeting"],
        ["📚 Exam", "💊 Medicine"],
        ["🏃 Workout", "🛒 Shopping"],
        ["✏️ Custom Label"]
    ]
    markup = [[InlineKeyboardButton(l, callback_data=f"label_sel_{l}") for l in row] for row in labels]
    return InlineKeyboardMarkup(markup)

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
            await update.effective_message.reply_text(f"🔔 <b>{label}</b>\n📅 {r['target_date']} | ⏰ {r['reminder_time']}", reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"List error: {e}")
        await update.effective_message.reply_text("❌ Failed to list reminders.")

async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith("delrem_"):
        r_id = query.data.split('_')[1]
        if reminders_col is not None:
            await reminders_col.delete_one({"_id": ObjectId(r_id)})
            await query.edit_message_text("❌ Reminder deleted.")
    await query.answer()

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear() 
    kb = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    await update.effective_message.reply_text("🕒 **Step 1: Timezone**\nSelect your location or city:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True), parse_mode="Markdown")
    return GET_TZ_CHOICE

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    timezone_str = "Asia/Tashkent"
    display_name = "🇺🇿 Tashkent/Uzbekistan"
    if msg.location:
        lat, lng = msg.location.latitude, msg.location.longitude
        timezone_str = tf.timezone_at(lng=lng, lat=lat) if tf else "Asia/Tashkent"
        display_name = timezone_str
    elif msg.text == "🇺🇿 Tashkent/Uzbekistan":
        timezone_str = "Asia/Tashkent"
        display_name = "🇺🇿 Tashkent/Uzbekistan"
    
    context.user_data['timezone'] = timezone_str
    await msg.reply_text(f"✅ Timezone: `{display_name}`\n\n📅 **Step 2: Target Date**\nSelect a date from the calendar:", reply_markup=create_calendar(), parse_mode="Markdown", reply_markup_remove=ReplyKeyboardRemove())
    return GET_DATE

async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data.startswith("cal_nav_"):
        _, _, y, m = data.split('_')
        await query.edit_message_reply_markup(reply_markup=create_calendar(int(y), int(m)))
    elif data.startswith("cal_view_months_"):
        year = data.split('_')[3]
        await query.edit_message_reply_markup(reply_markup=create_month_grid(int(year)))
    elif data.startswith("cal_view_years_"):
        parts = data.split('_')
        month = parts[3]
        start_y = int(parts[4]) if len(parts) > 4 else None
        await query.edit_message_reply_markup(reply_markup=create_year_grid(int(month), start_y))
    elif data.startswith("date_sel_"):
        selected_date = data.split('_')[2]
        tz_str = context.user_data.get('timezone', 'UTC')
        target_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        today = datetime.now(pytz.timezone(tz_str)).date()
        
        if target_date < today:
            await query.answer("⚠️ Date is in the past!", show_alert=True)
            return GET_DATE
            
        context.user_data['target_date'] = selected_date
        text, markup = create_time_grid()
        await query.edit_message_text(f"📅 Date: `{selected_date}`\n\n⏰ **Step 3: Reminder Time**\n{text}", reply_markup=markup, parse_mode="Markdown")
        return GET_TIME
    await query.answer()
    return GET_DATE

async def time_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "time_back_hour":
        text, markup = create_time_grid()
        await query.edit_message_text(f"📅 Date: `{context.user_data['target_date']}`\n\n⏰ **Step 3: Reminder Time**\n{text}", reply_markup=markup, parse_mode="Markdown")
    elif data.startswith("time_hour_"):
        hour = data.split('_')[2]
        text, markup = create_time_grid(hour)
        await query.edit_message_text(f"📅 Date: `{context.user_data['target_date']}`\n\n⏰ **Step 3: Reminder Time**\n{text}", reply_markup=markup, parse_mode="Markdown")
    elif data.startswith("time_min_"):
        selected_time = data.split('_')[2]
        context.user_data['reminder_time'] = selected_time
        await query.edit_message_text(f"📅 Date: `{context.user_data['target_date']}`\n⏰ Time: `{selected_time}`\n\n🏷️ **Step 4: Label**\nSelect a category or type a custom name:", reply_markup=create_label_buttons(), parse_mode="Markdown")
        return GET_LABEL
    await query.answer()
    return GET_TIME

async def label_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "label_sel_✏️ Custom Label":
        await query.edit_message_text("⌨️ Please **type** your custom label now:")
        await query.answer()
        return GET_LABEL
    
    label = data.replace("label_sel_", "")
    context.user_data['label'] = label
    return await finish_reminder(query.message, context)

async def handle_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()
    context.user_data['label'] = label
    return await finish_reminder(update.message, context)

async def finish_reminder(message, context):
    if reminders_col is None: return ConversationHandler.END
    user = context._user_id 
    u_obj = await users_col.find_one({"user_id": user}) if users_col else None
    username = u_obj.get("username") if u_obj else "Unknown"
    
    data = {
        "user_id": user, 
        "timezone": context.user_data['timezone'], 
        "target_date": context.user_data['target_date'], 
        "reminder_time": context.user_data['reminder_time'], 
        "label": context.user_data['label']
    }
    res = await reminders_col.insert_one(data)
    data['_id'] = res.inserted_id
    schedule_reminder_job(application, data)
    
    success_text = f"🚀 **Reminder Armed!**\n\n🏷️ Label: `{data['label']}`\n📅 Date: `{data['target_date']}`\n⏰ Time: `{data['reminder_time']}` ({data['timezone']})\n\nI'll ping you daily until the day!"
    
    if hasattr(message, 'edit_text'): await message.edit_text(success_text, parse_mode="Markdown")
    else: await message.reply_text(success_text, parse_mode="Markdown")
    
    await log_event(user, username, f"Set GUI Reminder: {data['label']}")
    context.user_data.clear()
    return ConversationHandler.END

# --- LIBRARY GUI LOGIC ---

async def browse_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📺 Series", callback_data="lib_cat_series"), 
         InlineKeyboardButton("🎬 Movies", callback_data="lib_cat_movies")]
    ]
    text = "📂 **CONTENT LIBRARY**\n\nSelect a category to browse:"
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_library_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cat = query.data.split('_')[2]
    
    # Analyze library
    pipeline = [
        {"$match": {"file_type": "video"}},
        {"$project": {"prefix": {"$substr": ["$code", 0, 3]}}},
        {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}
    ]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=None)
    
    filtered = []
    for p in prefixes:
        if cat == "series" and p['count'] > 2: filtered.append(p)
        elif cat == "movies" and p['count'] <= 2: filtered.append(p)
    
    if not filtered:
        return await query.answer(f"No {cat} found.", show_alert=True)
    
    keyboard = []
    for p in filtered:
        label_rec = await prefix_labels_col.find_one({"prefix": p['_id']})
        name = label_rec['name'] if label_rec else f"Project {p['_id']}"
        keyboard.append([InlineKeyboardButton(name, callback_data=f"lib_pick_{cat}_{p['_id']}_{p['count']}")])
    
    keyboard.append([InlineKeyboardButton("Back", callback_data="lib_back")])
    await query.edit_message_text(f"📂 **{cat.upper()} LIST**\n\nChoose a title:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_library_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    cat, prefix, count = parts[2], parts[3], int(parts[4])
    
    label_rec = await prefix_labels_col.find_one({"prefix": prefix})
    name = label_rec['name'] if label_rec else prefix
    
    if cat == "movies":
        keyboard = [[InlineKeyboardButton(f"🎬 Watch {name}", callback_data=f"lib_dl_{prefix}_all")]]
        keyboard.append([InlineKeyboardButton("Back", callback_data="lib_cat_movies")])
        await query.edit_message_text(f"🍿 **{name}**\n\nReady to watch?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        # Series grid
        keyboard = []
        cursor = codes_col.find({"code": {"$regex": f"^{prefix}"}}).sort("code", 1)
        items = await cursor.to_list(length=None)
        row = []
        for i, item in enumerate(items):
            row.append(InlineKeyboardButton(f"Ep {i+1}", callback_data=f"lib_dl_{item['code']}_one"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
        keyboard.append([InlineKeyboardButton("Back", callback_data="lib_cat_series")])
        await query.edit_message_text(f"📺 **{name}**\n\nSelect an episode:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_library_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    target, mode = parts[2], parts[3]
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if mode == "all":
        cursor = codes_col.find({"code": {"$regex": f"^{target}"}}).sort("code", 1)
        records = await cursor.to_list(length=None)
        for r in records: await execute_file_delivery(chat_id, r, context, user, send_alert=True)
    else:
        record = await codes_col.find_one({"code": target})
        if record: await execute_file_delivery(chat_id, record, context, user, send_alert=True)
    
    await query.answer("Delivering...")

# --- GREETING & ROUTING ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await save_user_info(user)
    if user.id == ADMIN_ID:
        text, markup = await admin_palette_msg()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        greet = "👋 **Welcome.**\n\n⏰ /remind - Set countdown (GUI)\n📜 /list - Manage reminders\n📦 /get - Open Content Library\n🎨 /ascii - ASCII art\n\nCopyright © **NurAziz**"
        await update.message.reply_text(greet, parse_mode="Markdown")

async def get_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE, force_args=None):
    if codes_col is None: return
    args = force_args if force_args is not None else context.args
    chat_id = update.effective_chat.id
    
    if not args:
        return await browse_library(update, context)
        
    user = update.effective_user
    is_admin = (user.id == ADMIN_ID)
    if len(args) == 1:
        code = args[0].upper().strip()
        prefix = code[:3]
        record = await codes_col.find_one({"code": code})
        if record:
            if not is_admin:
                gate = await group_keys_col.find_one({"chat_id": record["chat_id"], "prefix": prefix})
                if gate:
                    auth = await unlocked_groups_col.find_one({"user_id": user.id})
                    auth_key = f"{record['chat_id']}_{prefix}"
                    if not auth or auth_key not in auth.get("unlocked_prefixes", []):
                        context.user_data['pending_unlock_group_id'] = record["chat_id"]
                        context.user_data['pending_unlock_prefix'] = prefix
                        context.user_data['interrupted_file_codes'] = [code]
                        alert = await context.bot.send_message(chat_id, f"🔒 Collection '{prefix}' Locked. Enter Key:")
                        context.user_data['alert_message_id'] = alert.message_id
                        return
            await execute_file_delivery(chat_id, record, context, user, send_alert=True)
        else: await update.message.reply_text("❌ File not found.")
    elif len(args) == 3:
        prefix_arg = args[0].upper().strip()[:3]
        try: start_num, end_num = int(args[1]), int(args[2])
        except ValueError: return await update.message.reply_text("❌ START and END must be numbers.")
        target_codes = [f"{prefix_arg}{i:03d}" for i in range(start_num, end_num + 1)]
        cursor = codes_col.find({"code": {"$in": target_codes}}).sort("code", 1)
        records = await cursor.to_list(length=None)
        if not records: return await update.message.reply_text("❌ No files found in that range.")
        groups_to_check = {}
        for record in records:
            group_key = (record["chat_id"], record["code"][:3])
            if group_key not in groups_to_check: groups_to_check[group_key] = []
            groups_to_check[group_key].append(record["code"])
        interrupted = False
        for (g_chat, g_pref), codes in groups_to_check.items():
            if not is_admin:
                gate = await group_keys_col.find_one({"chat_id": g_chat, "prefix": g_pref})
                if gate:
                    auth = await unlocked_groups_col.find_one({"user_id": user.id})
                    if not auth or f"{g_chat}_{g_pref}" not in auth.get("unlocked_prefixes", []):
                        context.user_data['pending_unlock_group_id'] = g_chat
                        context.user_data['pending_unlock_prefix'] = g_pref
                        context.user_data['interrupted_file_codes'] = target_codes
                        alert = await context.bot.send_message(chat_id, f"🔒 Collection '{g_pref}' Locked. Enter Key:")
                        context.user_data['alert_message_id'] = alert.message_id
                        interrupted = True
                        break
        if interrupted: return
        delivered_ids = []
        for record in records:
            msg = await execute_file_delivery(chat_id, record, context, user, send_alert=False)
            if msg: delivered_ids.append(msg.message_id)
            await asyncio.sleep(0.1)
        if delivered_ids:
            count = len(delivered_ids)
            msg_text = f"⚠️ **ALERT**: FILE IS EPHEMERAL" if count == 1 else f"⚠️ **ALERT**: {count} FILES ARE EPHEMERAL"
            warn = await context.bot.send_message(chat_id, f"{msg_text}\nSelf-destruct in 3 minutes.", parse_mode="Markdown")
            for m_id in delivered_ids: context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": m_id})
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})

async def execute_file_delivery(chat_id, record, context, user, send_alert=True):
    try:
        f_type, f_id, caption = record.get("file_type"), record.get("file_id"), record.get("caption", "")
        if f_id and f_type:
            if f_type == "video": msg = await context.bot.send_video(chat_id, video=f_id, caption=caption)
            elif f_type == "document": msg = await context.bot.send_document(chat_id, document=f_id, caption=caption)
            elif f_type == "photo": msg = await context.bot.send_photo(chat_id, photo=f_id, caption=caption)
            elif f_type == "audio": msg = await context.bot.send_audio(chat_id, audio=f_id, caption=caption)
            elif f_type == "voice": msg = await context.bot.send_voice(chat_id, voice=f_id, caption=caption)
            elif f_type == "animation": msg = await context.bot.send_animation(chat_id, animation=f_id, caption=caption)
            else: msg = await context.bot.copy_message(chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"])
        else: msg = await context.bot.copy_message(chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"])
        await log_event(user.id, user.username, f"Requested asset: {record['code']}")
        if send_alert:
            warn = await context.bot.send_message(chat_id, "⚠️ **ALERT**: FILE IS EPHEMERAL\nSelf-destruct in 3 minutes.", parse_mode="Markdown")
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": msg.message_id})
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})
        return msg
    except Exception as e:
        logger.error(f"Delivery Error: {e}")
        return None

async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or codes_col is None: return
    if context.user_data.get('timezone'): return
    user, chat_id, text = update.effective_user, update.effective_chat.id, update.message.text.strip()
    is_admin = (user.id == ADMIN_ID)
    await save_user_info(user)
    
    # EXCLUSIVE INLINE COMMAND LISTENER
    if text.upper().startswith("/GET "):
        parts = text.split()
        if len(parts) >= 2:
            return await get_file_command(update, context, force_args=parts[1:])
    
    # Handle /ascii command if sent as a caption with an image or just text
    if text.lower().startswith("/ascii"):
        return await ascii_command_handler(update, context)

    pending_chat = context.user_data.get('pending_unlock_group_id')
    pending_prefix = context.user_data.get('pending_unlock_prefix')
    if pending_chat and pending_prefix and not is_admin:
        try: await update.message.delete()
        except: pass
        gate = await group_keys_col.find_one({"chat_id": pending_chat, "prefix": pending_prefix})
        if gate and text == gate["secret_key"]:
            await unlocked_groups_col.update_one({"user_id": user.id}, {"$addToSet": {"unlocked_prefixes": f"{pending_chat}_{pending_prefix}"}}, upsert=True)
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id:
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            codes = context.user_data.pop('interrupted_file_codes', [])
            context.user_data.clear()
            if codes:
                delivered_ids = []
                cursor = codes_col.find({"code": {"$in": codes}}).sort("code", 1)
                async for record in cursor:
                    m = await execute_file_delivery(chat_id, record, context, user, send_alert=False)
                    if m: delivered_ids.append(m.message_id)
                    await asyncio.sleep(0.1)
                if delivered_ids:
                    count = len(delivered_ids)
                    msg_text = f"⚠️ **ALERT**: FILE IS EPHEMERAL" if count == 1 else f"⚠️ **ALERT**: {count} FILES ARE EPHEMERAL"
                    warn = await context.bot.send_message(chat_id, f"{msg_text}\nSelf-destruct in 3 minutes.", parse_mode="Markdown")
                    for m_id in delivered_ids: context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": m_id})
                    context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})
        else:
            m = await update.message.reply_text("❌ Key Denied.")
            context.job_queue.run_once(delete_msg_callback, 10, data={"chat_id": chat_id, "message_id": m.message_id})

async def inline_query_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip().upper()
    if not query.startswith("/GET "): return await update.inline_query.answer([], cache_time=0)
    parts = query.split()
    if len(parts) < 2: return await update.inline_query.answer([], cache_time=0)
    user_id = update.effective_user.id
    results = []
    if len(parts) == 2:
        code = parts[1]
        prefix = code[:3]
        record = await codes_col.find_one({"code": code})
        if not record: return await update.inline_query.answer([], cache_time=0)
        if user_id != ADMIN_ID:
            gate = await group_keys_col.find_one({"chat_id": record["chat_id"], "prefix": prefix})
            if gate:
                auth = await unlocked_groups_col.find_one({"user_id": user_id})
                if not auth or f"{record['chat_id']}_{prefix}" not in auth.get("unlocked_prefixes", []):
                    return await update.inline_query.answer([InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"🔒 {code} is Locked", description="Enter key in bot chat.", input_message_content=InputTextMessageContent(f"/get {code}"))], cache_time=0)
        f_type, f_id, caption = record.get("file_type"), record.get("file_id"), record.get("caption", "")
        title = f"📦 Deliver {code}"
        uid = str(uuid.uuid4())
        if f_id:
            if f_type == "video": results.append(InlineQueryResultCachedVideo(id=uid, video_file_id=f_id, title=title, caption=caption))
            elif f_type == "document": results.append(InlineQueryResultCachedDocument(id=uid, document_file_id=f_id, title=title, caption=caption))
            elif f_type == "photo": results.append(InlineQueryResultCachedPhoto(id=uid, photo_file_id=f_id, caption=caption))
            elif f_type == "audio": results.append(InlineQueryResultCachedAudio(id=uid, audio_file_id=f_id, title=title, caption=caption))
            elif f_type == "voice": results.append(InlineQueryResultCachedVoice(id=uid, voice_file_id=f_id, title=title, caption=caption))
            elif f_type == "animation": results.append(InlineQueryResultCachedMpeg4Gif(id=uid, mpeg4_file_id=f_id, title=title, caption=caption))
        if not results: results.append(InlineQueryResultArticle(id=uid, title=title, description="Pointer delivery.", input_message_content=InputTextMessageContent(f"/get {code}")))
    elif len(parts) == 4:
        prefix, start, end = parts[1], parts[2], parts[3]
        try:
            s_num, e_num = int(start), int(end)
            count = e_num - s_num + 1
            if count <= 0: return await update.inline_query.answer([], cache_time=0)
            results.append(InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"📦 ({count} files to send)", description=f"Batch: {prefix}{s_num:03d} to {prefix}{e_num:03d}", input_message_content=InputTextMessageContent(f"/get {prefix} {start} {end}")))
        except: pass
    await update.inline_query.answer(results, cache_time=0, is_personal=True)

# --- APP SETUP ---

def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    
    rem_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)], 
        states={
            GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)], 
            GET_DATE: [CallbackQueryHandler(date_callback_handler, pattern="^(cal_nav_|date_sel_|cal_view_|ignore)")], 
            GET_TIME: [CallbackQueryHandler(time_callback_handler, pattern="^(time_hour_|time_min_|time_back_hour)")], 
            GET_LABEL: [CallbackQueryHandler(label_callback_handler, pattern="^label_sel_"), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label_text)]
        }, 
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))], 
        per_message=False
    )
    
    man_conv = ConversationHandler(entry_points=[CallbackQueryHandler(manage_db_gui, pattern="^pal_manage$")], states={MANAGE_CHOOSE_PREFIX: [CallbackQueryHandler(handle_manage_callback, pattern="^pref_wipe_")]}, fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))], per_message=False)
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", start_command))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CommandHandler("get", get_file_command))
    app.add_handler(CommandHandler("del", range_delete))
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("refresh", refresh_metadata))
    app.add_handler(CommandHandler("setkey", set_group_key))
    app.add_handler(CommandHandler("setlabel", set_prefix_label))
    app.add_handler(CommandHandler("rename_prefix", rename_prefix))
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CommandHandler("ascii", ascii_command_handler))
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern="^delrem_"))
    app.add_handler(CallbackQueryHandler(browse_library, pattern="^lib_back$"))
    app.add_handler(CallbackQueryHandler(show_library_items, pattern="^lib_cat_"))
    app.add_handler(CallbackQueryHandler(handle_library_pick, pattern="^lib_pick_"))
    app.add_handler(CallbackQueryHandler(handle_library_delivery, pattern="^lib_dl_"))
    app.add_handler(rem_conv)
    app.add_handler(man_conv)
    # UNIVERSAL LISTENER
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, core_routing_manager))
    app.add_handler(InlineQueryHandler(inline_query_manager))
    return app

flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Supreme Commander Pro Max Online."
@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if main_loop:
        try:
            update_data = request.get_json(force=True)
            asyncio.run_coroutine_threadsafe(application.process_update(Update.de_json(update_data, application.bot)), main_loop)
        except Exception as e: logger.error(f"Webhook Error: {e}")
    return "OK"

async def main():
    global application, main_loop
    if not TOKEN or not MONGO_URI or not RENDER_URL:
        server = make_server('0.0.0.0', PORT, flask_app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        while True: await asyncio.sleep(3600)
    main_loop = asyncio.get_running_loop()
    try:
        if logs_col is not None: await logs_col.create_index("timestamp", expireAfterSeconds=604800)
        if users_col is not None: await users_col.create_index("user_id", unique=True)
    except: pass
    application = create_application()
    await application.initialize()
    await application.start()
    try:
        if reminders_col is not None:
            cursor = reminders_col.find({})
            async for r in cursor: schedule_reminder_job(application, r)
    except: pass
    try: await application.bot.set_webhook(url=f"{RENDER_URL.rstrip('/')}/{TOKEN}")
    except: pass
    server = make_server('0.0.0.0', PORT, flask_app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as e: logger.critical(f"FATAL SHUTDOWN: {e}", exc_info=True)
