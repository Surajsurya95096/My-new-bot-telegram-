# bot.py
import logging
import os
import re
import time
import sqlite3
from contextlib import closing
from functools import wraps
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions,
    ChatMember, MessageEntity
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ChatMemberHandler
)

# Load config
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
DATABASE_URL = os.getenv("DATABASE_URL","sqlite:///data/bot.db")
WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "5"))
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "60"))
WELCOME_TEXT = os.getenv("WELCOME_TEXT", "Welcome! Please verify using the button.")
LOG_LEVEL = os.getenv("LOG_LEVEL","INFO")

# Setup logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Simple DB helper (SQLite) ---
DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite:///") else "bot.db"

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            last_warn_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            word TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            antispam INTEGER DEFAULT 1,
            block_links INTEGER DEFAULT 1,
            flood_limit INTEGER DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            action TEXT,
            reason TEXT,
            ts INTEGER
        );
        """)
        conn.commit()

def db_execute(query, params=(), fetch=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
        conn.commit()
        return None

# helpers for warns
def get_warn_count(chat_id, user_id):
    rows = db_execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id), fetch=True)
    if rows:
        return rows[0][0]
    return 0

def set_warn_count(chat_id, user_id, count):
    cur = db_execute("SELECT id FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id), fetch=True)
    if cur:
        db_execute("UPDATE warns SET count=?, last_warn_ts=? WHERE chat_id=? AND user_id=?",
                   (count, int(time.time()), chat_id, user_id))
    else:
        db_execute("INSERT INTO warns (chat_id,user_id,count,last_warn_ts) VALUES (?,?,?,?)",
                   (chat_id, user_id, count, int(time.time())))

def reset_warn(chat_id, user_id):
    db_execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))

# filters table
def add_filter(chat_id, word):
    db_execute("INSERT INTO filters (chat_id, word) VALUES (?, ?)", (chat_id, word.lower()))

def remove_filter(chat_id, word):
    db_execute("DELETE FROM filters WHERE chat_id=? AND word=?", (chat_id, word.lower()))

def get_filters(chat_id):
    rows = db_execute("SELECT word FROM filters WHERE chat_id=?", (chat_id,), fetch=True)
    return [r[0] for r in rows]

# settings
def ensure_settings(chat_id):
    r = db_execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    if not r:
        db_execute("INSERT INTO settings (chat_id) VALUES (?)", (chat_id,))

def get_setting(chat_id, key, default=None):
    ensure_settings(chat_id)
    rows = db_execute(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    if rows:
        return rows[0][0]
    return default

def set_setting(chat_id, key, val):
    ensure_settings(chat_id)
    db_execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (val, chat_id))

# logging actions
def log_action(chat_id, user_id, action, reason=""):
    db_execute("INSERT INTO logs (chat_id,user_id,action,reason,ts) VALUES (?,?,?,?,?)",
               (chat_id, user_id, action, reason, int(time.time())))

# small decorator to restrict admin commands
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and user.id in ADMIN_IDS:
            return await func(update, context)
        else:
            await update.message.reply_text("❌ You are not authorized for this command.")
            return
    return wrapper

# --- Moderation heuristics ---
URL_REGEX = re.compile(r"(https?://\S+|\bwww\.\S+)", re.IGNORECASE)
DEFAULT_BADWORDS = ["spamword1","scam","porn","sex","casino","fake"]  # extendable

# rate limit tracking (in-memory, reset on restart - consider Redis for prod)
_flood_cache = {}  # {(chat_id,user_id): [timestamps]}

def check_flood(chat_id, user_id, limit):
    key = (chat_id, user_id)
    now = time.time()
    hits = _flood_cache.get(key, [])
    # remove old
    hits = [t for t in hits if now - t < 7]  # 7 second window
    hits.append(now)
    _flood_cache[key] = hits
    return len(hits) > limit

# --- Handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm your All-in-one Moderation Bot. Use /help for commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Admin commands:\n"
        "/addfilter <word> - add banned word\n"
        "/delfilter <word> - remove\n"
        "/listfilters - show\n"
        "/warn @user <reason> - warn\n"
        "/unwarn @user - remove warns\n"
        "/setwarnlimit <n> - set warn limit for this chat\n        "
    )
    await update.message.reply_text(help_text)

# When a new member joins, send captcha button
async def member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.chat_member.new_chat_members if hasattr(update.chat_member, 'new_chat_members') else []:
        pass  # not used; using ChatMemberHandler for join events

async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    # Detect new member join (from "left" -> "member")
    old = result.old_chat_member
    new = result.new_chat_member
    if old.status in (ChatMember.LEFT, ChatMember.KICKED) and new.status == ChatMember.MEMBER:
        user = result.new_chat_member.user
        chat = update.chat_member.chat
        # restrict user until verified
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except Exception as e:
            logger.warning(f"Could not restrict new member: {e}")
        # send captcha
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I'm human", callback_data=f"captcha:{user.id}")]])
        await context.bot.send_message(chat_id=chat.id, text=f"{WELCOME_TEXT}\nPlease verify within {CAPTCHA_TIMEOUT}s.", reply_markup=keyboard)

# handle captcha clicks
async def captcha_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("captcha:"):
        expected_id = int(data.split(":",1)[1])
        user = query.from_user
        if user.id != expected_id:
            await query.edit_message_text("❌ This verification is not for you.")
            return
        # lift restrictions
        chat_id = query.message.chat_id
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user.id,
                                               permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                                                           can_add_web_page_previews=True, can_send_other_messages=True))
        await query.edit_message_text("✅ Verified — welcome!")
        log_action(chat_id, user.id, "verified", "captcha")

# background job to remove unverified after timeout (simple)
async def check_unverified(context: ContextTypes.DEFAULT_TYPE):
    # This is a placeholder for more robust unverified tracking (store join ts and user id)
    # For production, store join info in DB and kick after CAPTCHA_TIMEOUT.
    return

# message content filter
async def message_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat_id
    user = msg.from_user
    if user and user.is_bot:
        return

    # check group settings
    antispam = get_setting(chat_id, "antispam", 1)
    block_links = get_setting(chat_id, "block_links", 1)
    flood_limit = get_setting(chat_id, "flood_limit", FLOOD_LIMIT)

    text = (msg.text or msg.caption or "") .lower()
    # flood
    if check_flood(chat_id, user.id, int(flood_limit)):
        try:
            await msg.delete()
        except:
            pass
        log_action(chat_id, user.id, "deleted", "flood")
        await warn_user(context, chat_id, user.id, reason="Flooding")
        return

    # links
    if block_links and URL_REGEX.search(text):
        # allow if admin
        if user.id not in ADMIN_IDS:
            try:
                await msg.delete()
            except:
                pass
            log_action(chat_id, user.id, "deleted", "link")
            await warn_user(context, chat_id, user.id, reason="Posting links")
            return

    # bad words
    filters = get_filters(chat_id) or DEFAULT_BADWORDS
    for w in filters:
        if w and w in text:
            try:
                await msg.delete()
            except:
                pass
            log_action(chat_id, user.id, "deleted", f"badword:{w}")
            await warn_user(context, chat_id, user.id, reason=f"Use of banned word: {w}")
            return

    # Additional heuristics (caps, emojis spam, mention spam)
    if text and sum(1 for c in text if c.isupper()) > 25 and len(text) < 400:
        try:
            await msg.delete()
        except:
            pass
        log_action(chat_id, user.id, "deleted", "caps spam")
        await warn_user(context, chat_id, user.id, reason="Caps spam")
        return

async def warn_user(context: ContextTypes.DEFAULT_TYPE, chat_id:int= None, user_id:int=None, reason:str=""):
    # called from message_filter or from admin commands
    # ensure args
    if chat_id is None or user_id is None:
        return
    count = get_warn_count(chat_id, user_id) + 1
    set_warn_count(chat_id, user_id, count)
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ <a href='tg://user?id={user_id}'>User</a> warned ({count}/{WARN_LIMIT}). Reason: {reason}", parse_mode="HTML")
    except:
        pass
    log_action(chat_id, user_id, "warn", reason)
    if count >= WARN_LIMIT:
        # take action: mute for 1 hour then reset warns
        try:
            await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id,
                                                   permissions=ChatPermissions(can_send_messages=False),
                                                   until_date=int(time.time()) + 3600)
            await context.bot.send_message(chat_id=chat_id, text=f"🔇 User muted for 1 hour due to repeated warnings.")
            log_action(chat_id, user_id, "mute", "warn_limit_reached")
        except Exception as e:
            logger.exception("Could not mute user.")
        reset_warn(chat_id, user_id)

# Admin commands
@admin_only
async def addfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /addfilter <word>")
        return
    word = " ".join(context.args).strip().lower()
    add_filter(chat_id, word)
    await update.message.reply_text(f"Added filter: {word}")

@admin_only
async def delfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /delfilter <word>")
        return
    word = " ".join(context.args).strip().lower()
    remove_filter(chat_id, word)
    await update.message.reply_text(f"Removed filter: {word}")

@admin_only
async def listfilters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    words = get_filters(chat_id)
    if not words:
        await update.message.reply_text("No filters set.")
    else:
        await update.message.reply_text("Filters:\n" + "\n".join(words))

@admin_only
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.message.reply_to_message and not context.args:
        await update.message.reply_text("Usage: reply to user or /warn <user_id> <reason>")
        return
    target = None
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else (" ".join(context.args) if context.args else "")
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.id
    else:
        try:
            target = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args)>1 else ""
        except:
            await update.message.reply_text("Could not find user id.")
            return
    await warn_user(context, chat_id, target, reason)

@admin_only
async def unwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.message.reply_to_message and not context.args:
        await update.message.reply_text("Usage: reply to user or /unwarn <user_id>")
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.id
    else:
        try:
            target = int(context.args[0])
        except:
            await update.message.reply_text("Could not parse user id.")
            return
    reset_warn(chat_id, target)
    await update.message.reply_text("Warnings reset.")

@admin_only
async def set_warn_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WARN_LIMIT
    if not context.args:
        await update.message.reply_text("Usage: /setwarnlimit <n>")
        return
    try:
        WARN_LIMIT = int(context.args[0])
        await update.message.reply_text(f"Global warn limit set to {WARN_LIMIT}")
    except:
        await update.message.reply_text("Invalid number.")

# start point
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # basic commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # admin commands
    app.add_handler(CommandHandler("addfilter", addfilter_cmd))
    app.add_handler(CommandHandler("delfilter", delfilter_cmd))
    app.add_handler(CommandHandler("listfilters", listfilters_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("unwarn", unwarn_cmd))
    app.add_handler(CommandHandler("setwarnlimit", set_warn_limit_cmd))

    # chat member join handler
    app.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))

    # callback queries (captcha)
    app.add_handler(CallbackQueryHandler(captcha_click, pattern=r"^captcha:"))

    # message handler for moderation
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), message_filter))

    # start polling
    if os.getenv("USE_WEBHOOK","false").lower() in ("true","1"):
        # implement webhook startup if needed
        logger.info("Webhook mode requested but not configured in this example.")
    else:
        logger.info("Starting polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
