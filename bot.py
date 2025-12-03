# bot.py
import os
import re
import time
import logging
from functools import wraps
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ChatMember, MessageEntity
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ChatMemberHandler
)

# local db helper (Mongo)
from db import (
    get_warn_count, set_warn_count, reset_warn,
    add_filter, remove_filter, get_filters,
    get_setting, set_setting, log_action
)

# Load env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "5"))
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "60"))
WELCOME_TEXT = os.getenv("WELCOME_TEXT", "Welcome! Please verify using the button.")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() in ("true", "1")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

# Logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Moderation constants
URL_REGEX = re.compile(r"(https?://\S+|\bwww\.\S+)", re.IGNORECASE)
DEFAULT_BADWORDS = ["spamword1","scam","porn","sex","casino","fake"]

# In-memory flood tracker (for production use Redis)
_flood_cache = {}  # {(chat_id,user_id): [timestamps]}

def check_flood(chat_id, user_id, limit):
    key = (int(chat_id), int(user_id))
    now = time.time()
    hits = _flood_cache.get(key, [])
    hits = [t for t in hits if now - t < 7]  # 7s window
    hits.append(now)
    _flood_cache[key] = hits
    return len(hits) > limit

# Admin decorator (works for commands)
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and (user.id in ADMIN_IDS):
            return await func(update, context)
        else:
            # Some handlers (callback query) may not have message
            try:
                if update.effective_message:
                    await update.effective_message.reply_text("❌ You are not authorized for this command.")
            except:
                pass
            return
    return wrapper

# Commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm your All-in-one Moderation Bot. Use /help for commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Admin commands:\n"
        "/addfilter <word> - add banned word\n"
        "/delfilter <word> - remove\n"
        "/listfilters - show\n"
        "/warn (reply) - warn user\n"
        "/unwarn (reply) - remove warns\n"
        "/setwarnlimit <n> - set warn limit for this chat\n"
    )
    await update.message.reply_text(help_text)

# Chat member join handler -> send captcha button and restrict
async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    old = result.old_chat_member
    new = result.new_chat_member
    # Detect join (left -> member)
    try:
        if old.status in (ChatMember.LEFT, ChatMember.KICKED) and new.status == ChatMember.MEMBER:
            user = result.new_chat_member.user
            chat = update.chat_member.chat
            # restrict temporarily
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            except Exception as e:
                logger.warning(f"Could not restrict new member: {e}")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I'm human", callback_data=f"captcha:{user.id}")]])
            await context.bot.send_message(chat_id=chat.id, text=f"{WELCOME_TEXT}\nPlease verify within {CAPTCHA_TIMEOUT}s.", reply_markup=keyboard)
            # log
            log_action(chat.id, user.id, "join_restriction", "captcha_sent")
    except Exception as e:
        logger.exception("chat_member_handler error")

# Captcha button click
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
        chat_id = query.message.chat.id
        # lift restrictions
        try:
            await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user.id,
                                                   permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                                                               can_add_web_page_previews=True, can_send_other_messages=True))
            await query.edit_message_text("✅ Verified — welcome!")
            log_action(chat_id, user.id, "verified", "captcha")
        except Exception as e:
            logger.exception("Could not lift restrictions after captcha")

# Warn helper
async def warn_user(context: ContextTypes.DEFAULT_TYPE, chat_id:int, user_id:int, reason:str=""):
    count = get_warn_count(chat_id, user_id) + 1
    set_warn_count(chat_id, user_id, count)
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ <a href='tg://user?id={user_id}'>User</a> warned ({count}/{WARN_LIMIT}). Reason: {reason}", parse_mode="HTML")
    except:
        pass
    log_action(chat_id, user_id, "warn", reason)
    if count >= WARN_LIMIT:
        # mute for 1 hour
        try:
            await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id,
                                                   permissions=ChatPermissions(can_send_messages=False),
                                                   until_date=int(time.time()) + 3600)
            await context.bot.send_message(chat_id=chat_id, text=f"🔇 User muted for 1 hour due to repeated warnings.")
            log_action(chat_id, user_id, "mute", "warn_limit_reached")
        except Exception as e:
            logger.exception("Could not mute user.")
        reset_warn(chat_id, user_id)

# Message moderation filter
async def message_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id
    user = msg.from_user
    if user and user.is_bot:
        return

    antispam = get_setting(chat_id, "antispam", 1) or 0
    block_links = get_setting(chat_id, "block_links", 1) or 0
    flood_limit = int(get_setting(chat_id, "flood_limit", F L O O D _ L I M I T)) if get_setting(chat_id, "flood_limit") is not None else F L O O D _ L I M I T

    text = (msg.text or msg.caption or "").lower()

    # flood
    if check_flood(chat_id, user.id, flood_limit):
        try:
            await msg.delete()
        except:
            pass
        log_action(chat_id, user.id, "deleted", "flood")
        await warn_user(context, chat_id, user.id, reason="Flooding")
        return

    # links
    if block_links and URL_REGEX.search(text):
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

    # caps spam heuristic
    if text and sum(1 for c in text if c.isupper()) > 25 and len(text) < 400:
        try:
            await msg.delete()
        except:
            pass
        log_action(chat_id, user.id, "deleted", "caps spam")
        await warn_user(context, chat_id, user.id, reason="Caps spam")
        return

# Admin commands
@admin_only
async def addfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /addfilter <word>")
        return
    word = " ".join(context.args).strip().lower()
    add_filter(chat_id, word)
    await update.effective_message.reply_text(f"Added filter: {word}")

@admin_only
async def delfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /delfilter <word>")
        return
    word = " ".join(context.args).strip().lower()
    remove_filter(chat_id, word)
    await update.effective_message.reply_text(f"Removed filter: {word}")

@admin_only
async def listfilters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    words = get_filters(chat_id)
    if not words:
        await update.effective_message.reply_text("No filters set.")
    else:
        await update.effective_message.reply_text("Filters:\n" + "\n".join(words))

@admin_only
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # support reply or /warn <user_id> <reason>
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.id
        reason = " ".join(context.args) if context.args else ""
    elif context.args:
        try:
            target = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        except:
            await update.effective_message.reply_text("Usage: reply to user or /warn <user_id> <reason>")
            return
    else:
        await update.effective_message.reply_text("Usage: reply to user or /warn <user_id> <reason>")
        return
    await warn_user(context, chat_id, target, reason)

@admin_only
async def unwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except:
            await update.effective_message.reply_text("Could not parse user id.")
            return
    else:
        await update.effective_message.reply_text("Usage: reply to user or /unwarn <user_id>")
        return
    reset_warn(chat_id, target)
    await update.effective_message.reply_text("Warnings reset.")

@admin_only
async def set_warn_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WARN_LIMIT
    if not context.args:
        await update.effective_message.reply_text("Usage: /setwarnlimit <n>")
        return
    try:
        WARN_LIMIT = int(context.args[0])
        await update.effective_message.reply_text(f"Global warn limit set to {WARN_LIMIT}")
    except:
        await update.effective_message.reply_text("Invalid number.")

# Build and run application
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addfilter", addfilter_cmd))
    app.add_handler(CommandHandler("delfilter", delfilter_cmd))
    app.add_handler(CommandHandler("listfilters", listfilters_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("unwarn", unwarn_cmd))
    app.add_handler(CommandHandler("setwarnlimit", set_warn_limit_cmd))

    # Chat member and callbacks
    app.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(captcha_click, pattern=r"^captcha:"))

    # Message moderation (all non-status updates)
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), message_filter))

    if USE_WEBHOOK and WEBHOOK_URL:
        logger.info("Webhook mode requested but webhook setup not implemented in this snippet.")
        # For production implement webhook start here
    else:
        logger.info("Starting polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
