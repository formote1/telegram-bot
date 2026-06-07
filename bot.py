import os
import logging
import asyncio
import sys
import json
import threading
import html
import uuid
from datetime import datetime, time, timedelta
import pytz
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent,
    InlineQueryResultCachedDocument, InlineQueryResultCachedVideo,
    InlineQueryResultCachedPhoto, InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice, InlineQueryResultCachedMpeg4Gif
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
            await update.effective_message.reply_text(f"🔔 <b>{label}</b>\n📅 {r['target_date']} | ⏰ {r['reminder_time']}", reply_markup=kb, parse_mode="HTML")
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
    context.user_data.clear() 
    kb = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    await update.effective_message.reply_text("🕒 **Step 1: Timezone**\nTo ensure your reminders are accurate, I need to know your timezone.\n\nPlease select a city below or click 'Share Location' for precision.", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True), parse_mode="Markdown")
    return GET_TZ_CHOICE

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        user = update.effective_user
        if not msg: return GET_TZ_CHOICE
        await save_user_info(user, location=msg.location)
        
        timezone_str = "Asia/Tashkent"
        is_detected = False
        
        if msg.location:
            lat, lng = msg.location.latitude, msg.location.longitude
            timezone_str = tf.timezone_at(lng=lng, lat=lat) if tf else "Asia/Tashkent"
            is_detected = True
        elif msg.text == "🇺🇿 Tashkent/Uzbekistan":
            timezone_str = "Asia/Tashkent"
            is_detected = True
            
        context.user_data['timezone'] = timezone_str
        feedback = f"✅ Timezone set to: `{timezone_str}`" if is_detected else f"ℹ️ I couldn't detect your location, so I've defaulted to `{timezone_str}`."
        
        today_sample = datetime.now(pytz.timezone(timezone_str)).strftime("%Y-%m-%d")
        await msg.reply_text(f"{feedback}\n\n📅 **Step 2: Target Date**\nWhen is the big day? Enter the date in **YYYY-MM-DD** format.\n\nExample: `{today_sample}`", parse_mode="Markdown")
        return GET_DATE
    except Exception as e:
        logger.error(f"TZ Choice Error: {e}")
        context.user_data['timezone'] = "Asia/Tashkent"
        await update.message.reply_text("⚠️ Something went wrong. Defaulting to `Asia/Tashkent`.\n\n📅 **Step 2: Target Date**\nEnter the date (YYYY-MM-DD):", parse_mode="Markdown")
        return GET_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tz_str = context.user_data.get('timezone', 'UTC')
    try:
        target_date = datetime.strptime(text, "%Y-%m-%d").date()
        today = datetime.now(pytz.timezone(tz_str)).date()
        
        if target_date < today:
            await update.message.reply_text("⚠️ **Wait, that date is in the past!**\nPlease enter a future date (YYYY-MM-DD).", parse_mode="Markdown")
            return GET_DATE
            
        context.user_data['target_date'] = text
        await update.message.reply_text("⏰ **Step 3: Reminder Time**\nWhat time should I remind you daily? Enter in **HH:MM** (24-hour format).\n\nExample: `09:00` or `18:30`", parse_mode="Markdown")
        return GET_TIME
    except ValueError:
        sample = datetime.now(pytz.timezone(tz_str)).strftime("%Y-%m-%d")
        await update.message.reply_text(f"❌ **Invalid Format!**\nPlease use `YYYY-MM-DD`.\n\nExample of today: `{sample}`", parse_mode="Markdown")
        return GET_DATE
    except Exception as e:
        logger.error(f"Date Handle Error: {e}")
        await update.message.reply_text("❌ An unexpected error occurred. Please try entering the date again (YYYY-MM-DD):")
        return GET_DATE

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%H:%M")
        context.user_data['reminder_time'] = text
        await update.message.reply_text("🏷️ **Step 4: Label**\nFinally, give this reminder a name (e.g., 'Exam', 'Birthday', 'Meeting').", parse_mode="Markdown")
        return GET_LABEL
    except ValueError:
        await update.message.reply_text("❌ **Invalid Time!**\nPlease use the **HH:MM** 24-hour format.\n\nExample: `14:30` (for 2:30 PM)", parse_mode="Markdown")
        return GET_TIME
    except Exception as e:
        logger.error(f"Time Handle Error: {e}")
        await update.message.reply_text("❌ An unexpected error occurred. Please try entering the time again (HH:MM):")
        return GET_TIME

async def handle_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return ConversationHandler.END
    try:
        user = update.effective_user
        label = update.message.text.strip()
        data = {
            "user_id": user.id, 
            "timezone": context.user_data['timezone'], 
            "target_date": context.user_data['target_date'], 
            "reminder_time": context.user_data['reminder_time'], 
            "label": label
        }
        res = await reminders_col.insert_one(data)
        data['_id'] = res.inserted_id
        schedule_reminder_job(application, data)
        await log_event(user.id, user.username, f"Set reminder: {label}")
        await update.message.reply_text(f"🚀 **All Set!**\nI'll remind you about **{label}** every day at {data['reminder_time']} until {data['target_date']}.", parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Label Handle Error: {e}")
        await update.message.reply_text("❌ Failed to save reminder. Please try again later.")
        return ConversationHandler.END

# --- GREETING & ROUTING ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await save_user_info(user)
    if user.id == ADMIN_ID:
        text, markup = await admin_palette_msg()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        greet = "👋 **Welcome.**\n\n⏰ /remind - Set countdown\n📜 /list - Manage reminders\n📦 Use /get `CODE` to retrieve a file.\n🎨 Use /ascii `TEXT` or send an image with `/ascii` for ASCII art.\n\nCopyright © **NurAziz**"
        await update.message.reply_text(greet, parse_mode="Markdown")

async def get_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE, force_args=None):
    if codes_col is None: return
    args = force_args if force_args is not None else context.args
    chat_id = update.effective_chat.id
    if not args: return await update.message.reply_text("❌ Usage:\nSingle: `/get CODE`\nRange: `/get PREFIX START END`", parse_mode="Markdown")
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
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)], 
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)], 
            GET_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label)]
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
    app.add_handler(CommandHandler("rename_prefix", rename_prefix))
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CommandHandler("ascii", ascii_command_handler))
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern="^delrem_"))
    app.add_handler(rem_conv)
    app.add_handler(man_conv)
    # UNIVERSAL LISTENER
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, core_routing_manager))
    app.add_handler(InlineQueryHandler(inline_query_manager))
    return app

flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Supreme Commander Node Online."
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
