import os
import logging
import asyncio
import sys
import json
import threading
import html
import uuid
from datetime import datetime, time
import pytz
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultCachedDocument, InlineQueryResultCachedAudio,
    InlineQueryResultCachedVideo, InlineQueryResultCachedVoice,
    InlineQueryResultArticle, InputTextMessageContent
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
codes_col = db.saved_codes if db is not None else None
group_keys_col = db.group_keys if db is not None else None
unlocked_groups_col = db.unlocked_users if db is not None else None
logs_col = db.system_logs if db is not None else None
users_col = db.users if db is not None else None 

application = None
main_loop = None

# --- UTILS ---
def extract_media_meta(message):
    """Helper to pull unique file identifier properties from compound objects."""
    if message.document:
        return message.document.file_id, "document", message.document.file_name or "Document"
    elif message.audio:
        title = f"{message.audio.performer or ''} - {message.audio.title or ''}".strip()
        return message.audio.file_id, "audio", title or "Audio"
    elif message.video:
        return message.video.file_id, "video", "Video Asset"
    elif message.voice:
        return message.voice.file_id, "voice", "Voice Memo"
    return None, None, None

async def save_user_info(user):
    if users_col is None: return
    try:
        update_data = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_seen": datetime.utcnow()
        }
        await users_col.update_one({"user_id": user.id}, {"$set": update_data}, upsert=True)
    except Exception as e:
        logger.error(f"Error saving user info: {e}")

# --- BULK FORWARDING ENGINE ---
async def bulk_init_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initializes the high-speed forward listener."""
    if update.effective_user.id != ADMIN_ID: return
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/bulkinit PREFIX START_NUMBER` (e.g., `/bulkinit IHD 1`)", parse_mode="Markdown")
    
    prefix = args[0].upper().strip()
    try:
        start_num = int(args[1])
    except ValueError:
        return await update.message.reply_text("❌ START_NUMBER must be an integer.")
        
    context.user_data['bulk_mode'] = True
    context.user_data['bulk_prefix'] = prefix
    context.user_data['bulk_counter'] = start_num
    context.user_data['bulk_total_saved'] = 0
    
    await update.message.reply_text(
        f"📥 **Bulk Indexing Engine Active**\n"
        f"───────────────────────────\n"
        f"🏷️ Prefix: `{prefix}`\n"
        f"🔢 Starting Code: `{prefix}{start_num:03d}`\n\n"
        f"👉 Forward files from your channel directly to this chat.\n"
        f"👉 When finished, type `/bulkstop` to commit changes.",
        parse_mode="Markdown"
    )

async def bulk_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terminates the forward listener."""
    if update.effective_user.id != ADMIN_ID: return
    if not context.user_data.get('bulk_mode'):
        return await update.message.reply_text("❌ Bulk engine is not currently active.")
        
    total = context.user_data.get('bulk_total_saved', 0)
    prefix = context.user_data.get('bulk_prefix', 'UNKNOWN')
    
    context.user_data.pop('bulk_mode', None)
    context.user_data.pop('bulk_prefix', None)
    context.user_data.pop('bulk_counter', None)
    context.user_data.pop('bulk_total_saved', None)
    
    await update.message.reply_text(f"🛑 **Bulk Indexing Complete**\n✅ Successfully indexed `{total}` files for cluster `{prefix}`.", parse_mode="Markdown")

# --- CORE ROUTING (TEXT & INTERCEPTOR) ---
async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or codes_col is None: return
    user = update.effective_user
    chat_id = update.effective_chat.id
    is_admin = (user.id == ADMIN_ID)
    
    await save_user_info(user)
    
    # 1. THE BULK INTERCEPTOR
    if is_admin and context.user_data.get('bulk_mode'):
        file_id, media_type, file_title = extract_media_meta(update.message)
        if file_id:
            prefix = context.user_data['bulk_prefix']
            current_num = context.user_data['bulk_counter']
            code = f"{prefix}{current_num:03d}"
            
            payload = {
                "code": code,
                "prefix": prefix,
                "file_id": file_id,
                "media_type": media_type,
                "title": file_title,
                "chat_id": chat_id,
                "message_id": update.message.message_id
            }
            
            await codes_col.update_one({"code": code}, {"$set": payload}, upsert=True)
            context.user_data['bulk_counter'] += 1
            context.user_data['bulk_total_saved'] += 1
            
            # Silent notification so it doesn't flood your chat during batch forwards
            if context.user_data['bulk_total_saved'] % 10 == 0:
                await update.message.reply_text(f"⏳ Indexed {context.user_data['bulk_total_saved']} files... (Latest: {code})")
        return

    # 2. STANDARD TEXT ROUTING
    text = update.message.text
    if text:
        text = text.strip().upper()
        record = await codes_col.find_one({"code": text})
        if record:
            if not is_admin:
                gate = await group_keys_col.find_one({"prefix": record.get("prefix")})
                if gate:
                    auth = await unlocked_groups_col.find_one({"user_id": user.id})
                    if not auth or record["prefix"] not in auth.get("unlocked_prefixes", []):
                        return await update.message.reply_text("🔒 This cluster partition is restricted. Use inline query to pass keys: `@bot CODE PASSWORD`", parse_mode="Markdown")
            
            # Direct file delivery via file_id if available
            if "file_id" in record:
                m_type = record["media_type"]
                try:
                    if m_type == "document": await context.bot.send_document(chat_id, record["file_id"])
                    elif m_type == "audio": await context.bot.send_audio(chat_id, record["file_id"])
                    elif m_type == "video": await context.bot.send_video(chat_id, record["file_id"])
                    elif m_type == "voice": await context.bot.send_voice(chat_id, record["file_id"])
                except Exception as e:
                    logger.error(f"Delivery error: {e}")

# --- SHAZAM-STYLE INLINE ENGINE ---
async def inline_query_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_query = update.inline_query.query.strip()
    if not raw_query or codes_col is None: return
    
    parts = raw_query.split()
    search_code = parts[0].upper()
    provided_key = parts[1] if len(parts) > 1 else None
    
    record = await codes_col.find_one({"code": search_code})
    if not record or "file_id" not in record: return
    
    user_id = update.effective_user.id
    is_admin = (user_id == ADMIN_ID)
    prefix = record.get("prefix", search_code[:3])
    
    if not is_admin:
        gate = await group_keys_col.find_one({"prefix": prefix})
        if gate:
            auth = await unlocked_groups_col.find_one({"user_id": user_id})
            is_unlocked = auth and prefix in auth.get("unlocked_prefixes", [])
            
            if not is_unlocked:
                if provided_key and provided_key == gate["secret_key"]:
                    await unlocked_groups_col.update_one(
                        {"user_id": user_id}, 
                        {"$addToSet": {"unlocked_prefixes": prefix}}, 
                        upsert=True
                    )
                else:
                    results = [
                        InlineQueryResultArticle(
                            id=str(uuid.uuid4()),
                            title=f"🔒 Cluster {prefix} is Locked",
                            description="Syntax: @bot CODE PASSWORD to unlock",
                            input_message_content=InputTextMessageContent(f"⚠️ Access denied. Type `@bot {search_code} PASSWORD` to unlock.", parse_mode="Markdown")
                        )
                    ]
                    return await update.inline_query.answer(results, cache_time=0, is_personal=True)

    file_id = record["file_id"]
    media_type = record["media_type"]
    title = record.get("title", f"Asset {search_code}")
    res_id = str(uuid.uuid4())
    
    results = []
    try:
        if media_type == "document":
            results.append(InlineQueryResultCachedDocument(id=res_id, title=f"📦 {title}", document_file_id=file_id, description=search_code))
        elif media_type == "audio":
            results.append(InlineQueryResultCachedAudio(id=res_id, audio_file_id=file_id, caption=f"🎵 Shared via Vector Engine"))
        elif media_type == "video":
            results.append(InlineQueryResultCachedVideo(id=res_id, title=title, video_file_id=file_id, description=search_code))
        elif media_type == "voice":
            results.append(InlineQueryResultCachedVoice(id=res_id, title=title, voice_file_id=file_id))
    except Exception as e:
        logger.error(f"Inline caching failed: {e}")
        return

    if results:
        await update.inline_query.answer(results, cache_time=0, is_personal=True)

# --- SINGLE MANUAL SAVE COMMAND ---
async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backup single-file manual save via reply."""
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    if not update.message.reply_to_message or not context.args:
        return await update.message.reply_text("❌ Reply to a document/audio message with `/save CODE`")
    
    target_msg = update.message.reply_to_message
    file_id, media_type, file_title = extract_media_meta(target_msg)
    
    if not file_id: return await update.message.reply_text("❌ Target message does not contain supported media.")
        
    code = context.args[0].upper().strip()
    prefix = ''.join([c for c in code if c.isalpha()]) or code[:3]
    
    payload = {
        "code": code,
        "prefix": prefix,
        "file_id": file_id,
        "media_type": media_type,
        "title": file_title,
        "chat_id": target_msg.chat_id,
        "message_id": target_msg.message_id
    }
    
    await codes_col.update_one({"code": code}, {"$set": payload}, upsert=True)
    await update.message.reply_text(f"✅ Asset Mapped: `{code}` | Type: `{media_type}`", parse_mode="Markdown")

# --- APP SETUP & RUNNER ---
def create_application():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("bulkinit", bulk_init_command))
    app.add_handler(CommandHandler("bulkstop", bulk_stop_command))
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, core_routing_manager))
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
        except Exception as e:
            logger.error(f"Webhook error: {e}")
    return "OK"

async def main():
    global application, main_loop
    main_loop = asyncio.get_running_loop()
    
    try: await users_col.create_index("user_id", unique=True)
    except: pass
    
    application = create_application()
    await application.initialize()
    await application.start()
    
    try: await application.bot.set_webhook(url=f"{RENDER_URL.rstrip('/')}/{TOKEN}")
    except Exception as e: logger.error(f"Webhook set failed: {e}")
    
    server = make_server('0.0.0.0', PORT, flask_app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
