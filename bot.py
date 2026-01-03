import asyncio
import re
import time
import uuid
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from pymongo import MongoClient
from config import BOT_TOKEN, MONGO_URI, ADMIN_ID

# ================= LOGGING =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= ADMIN GROUP IDS =================
DEPOSIT_REQUESTS_GROUP   = -5291305798
DEPOSIT_PENDING_GROUP    = -5266076639
DEPOSIT_COMPLETED_GROUP  = -5204290005

# ================= SETTINGS =================
UTR_REGEX = r"^\d{12,18}$"
REQUEST_TIMEOUT = 120
PENDING_REMINDER = 60

# ================= DATABASE =================
client = MongoClient(MONGO_URI)
db = client.telegrambot
users = db.users
deposits = db.deposits

# ================= USER START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    logger.info(f"User {u.id} ({u.username}) started the bot")

    users.update_one(
        {"telegram_id": u.id},
        {"$setOnInsert": {
            "telegram_id": u.id,
            "username": u.username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0,
            "total_deposit": 0,
        }},
        upsert=True
    )

    await update.message.reply_text(
        "Welcome.\n\n"
        "1️⃣ Send payment screenshot\n"
        "2️⃣ Then send UTR number"
    )

# ================= USER INPUT =================
async def user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"User {user_id} sent input")

    if update.message.photo:
        context.user_data["screenshot"] = update.message.photo[-1].file_id
        await update.message.reply_text("Screenshot received. Now send UTR.")
        return

    if update.message.text:
        text = update.message.text.strip()
        
        # Check if this is a command (starts with /)
        if text.startswith('/'):
            return

        if "screenshot" not in context.user_data:
            await update.message.reply_text("❌ Send screenshot first.")
            return

        if not re.match(UTR_REGEX, text):
            await update.message.reply_text("❌ Invalid UTR (12–18 digits).")
            return

        try:
            msg = await context.bot.send_message(
                DEPOSIT_REQUESTS_GROUP,
                f"🟡 NEW DEPOSIT\nUser: {user_id}\nUTR: {text}\n\nCONFIRM {text}"
            )

            deposits.insert_one({
                "user_id": user_id,
                "utr": text,
                "status": "REQUESTED",
                "admin_msg_id": msg.message_id,
                "created_at": time.time(),
                "last_reminder": time.time()
            })

            await update.message.reply_text("✅ Submitted. Await confirmation.")
            logger.info(f"Deposit request created for user {user_id}, UTR: {text}")
        except Exception as e:
            logger.error(f"Error creating deposit request: {e}")
            await update.message.reply_text("❌ An error occurred. Please try again.")
        finally:
            context.user_data.clear()

# ================= ADMIN CONFIRM =================
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip()
    if not text.startswith("CONFIRM "):
        return

    utr = text.split()[1]
    dep = deposits.find_one({"utr": utr})

    if not dep:
        await update.message.reply_text("❌ Not found.")
        return

    try:
        # Delete from current group
        if dep["status"] == "REQUESTED":
            await context.bot.delete_message(DEPOSIT_REQUESTS_GROUP, dep["admin_msg_id"])
        elif dep["status"] == "PENDING":
            await context.bot.delete_message(DEPOSIT_PENDING_GROUP, dep["admin_msg_id"])
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

    # Update deposit status
    deposits.update_one({"_id": dep["_id"]}, {"$set": {"status": "COMPLETED"}})
    
    # Update user balance
    users.update_one(
        {"telegram_id": dep["user_id"]},
        {"$inc": {"balance": 100, "total_deposit": 100}}  # Adjust amount as needed
    )

    try:
        await context.bot.send_message(
            DEPOSIT_COMPLETED_GROUP,
            f"✅ COMPLETED\nUser: {dep['user_id']}\nUTR: {utr}"
        )

        await context.bot.send_message(dep["user_id"], "✅ Deposit confirmed. Your balance has been updated.")
        logger.info(f"Deposit confirmed for UTR: {utr}")
    except Exception as e:
        logger.error(f"Error sending confirmation: {e}")

# ================= WATCHER =================
async def deposit_watcher(app):
    logger.info("Deposit watcher started")
    while True:
        try:
            now = time.time()

            # Move REQUESTED to PENDING after timeout
            for d in deposits.find({"status": "REQUESTED"}):
                if now - d.get("created_at", now) > REQUEST_TIMEOUT:
                    try:
                        await app.bot.delete_message(
                            DEPOSIT_REQUESTS_GROUP, d["admin_msg_id"]
                        )
                    except Exception as e:
                        logger.warning(f"Could not delete request message: {e}")

                    msg = await app.bot.send_message(
                        DEPOSIT_PENDING_GROUP,
                        f"🟠 PENDING\nUser: {d['user_id']}\nUTR: {d['utr']}"
                    )

                    deposits.update_one(
                        {"_id": d["_id"]},
                        {"$set": {
                            "status": "PENDING",
                            "admin_msg_id": msg.message_id,
                            "last_reminder": now
                        }}
                    )
                    logger.info(f"Moved deposit {d['utr']} to PENDING")

            # Send reminders for PENDING deposits
            for d in deposits.find({"status": "PENDING"}):
                if now - d.get("last_reminder", now) > PENDING_REMINDER:
                    await app.bot.send_message(
                        DEPOSIT_PENDING_GROUP,
                        f"⏰ STILL PENDING\nUTR: {d['utr']}"
                    )
                    deposits.update_one(
                        {"_id": d["_id"]},
                        {"$set": {"last_reminder": now}}
                    )
                    logger.info(f"Sent reminder for UTR: {d['utr']}")

            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Error in deposit_watcher: {e}")
            await asyncio.sleep(10)

# ================= POST INIT =================
async def post_init(application):
    logger.info("Bot started. Setting up background tasks...")
    asyncio.create_task(deposit_watcher(application))

# ================= MAIN =================
def main():
    # Create application
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Set up handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.User(ADMIN_ID) & filters.TEXT & ~filters.COMMAND, admin_handler))
    application.add_handler(MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, user_input))
    
    # Set post initialization
    application.post_init = post_init
    
    # Start the bot
    logger.info("Starting bot...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == '__main__':
    main()
