import asyncio
import re
import time
import uuid
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

# ================= ADMIN GROUP IDS =================
DEPOSIT_REQUESTS_GROUP   = -5291305798
DEPOSIT_PENDING_GROUP    = -5266076639
DEPOSIT_COMPLETED_GROUP  = -5204290005

# ================= SETTINGS =================
UTR_REGEX = r"^\d{12,18}$"
REQUEST_TIMEOUT = 120        # 2 minutes
PENDING_REMINDER = 60        # 1 minute

# ================= DATABASE =================
client = MongoClient(MONGO_URI)
db = client.telegrambot
users = db.users
deposits = db.deposits

# ================= USER START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    if not users.find_one({"telegram_id": u.id}):
        users.insert_one({
            "telegram_id": u.id,
            "username": u.username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0,
            "total_deposit": 0,
        })

    await update.message.reply_text(
        "Welcome.\n\n"
        "To submit a deposit:\n"
        "1️⃣ Send payment screenshot\n"
        "2️⃣ Then send UTR number"
    )

# ================= USER INPUT =================
async def user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # STEP 1: SCREENSHOT
    if update.message.photo:
        context.user_data["screenshot"] = update.message.photo[-1].file_id
        context.user_data["screenshot_time"] = time.time()
        await update.message.reply_text("Screenshot received. Now send UTR number.")
        return

    # STEP 2: UTR
    if update.message.text:
        text = update.message.text.strip()

        if "screenshot" not in context.user_data:
            await update.message.reply_text("❌ Please send payment screenshot first.")
            return

        if not re.match(UTR_REGEX, text):
            await update.message.reply_text(
                "❌ Invalid UTR.\nUTR must be 12–18 digits only."
            )
            return

        deposit_id = str(uuid.uuid4())

        msg = await context.bot.send_message(
            DEPOSIT_REQUESTS_GROUP,
            f"🟡 NEW DEPOSIT REQUEST\n\n"
            f"User ID: {user_id}\n"
            f"UTR: {text}\n\n"
            f"Reply:\nCONFIRM {text}"
        )

        deposits.insert_one({
            "deposit_id": deposit_id,
            "user_id": user_id,
            "utr": text,
            "amount": 0,
            "status": "REQUESTED",
            "admin_msg_id": msg.message_id,
            "created_at": time.time(),
            "last_reminder": time.time(),
        })

        await update.message.reply_text(
            "✅ Deposit submitted.\nWaiting for confirmation."
        )

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
        await update.message.reply_text("❌ No matching deposit found.")
        return

    try:
        old_group = (
            DEPOSIT_PENDING_GROUP
            if dep["status"] == "PENDING"
            else DEPOSIT_REQUESTS_GROUP
        )
        await context.bot.delete_message(old_group, dep["admin_msg_id"])
    except:
        pass

    deposits.update_one(
        {"_id": dep["_id"]},
        {"$set": {"status": "COMPLETED"}}
    )

    await context.bot.send_message(
        DEPOSIT_COMPLETED_GROUP,
        f"✅ DEPOSIT COMPLETED\n\n"
        f"User: {dep['user_id']}\n"
        f"UTR: {utr}"
    )

    users.update_one(
        {"telegram_id": dep["user_id"]},
        {"$inc": {"balance": dep["amount"], "total_deposit": dep["amount"]}}
    )

    await context.bot.send_message(
        dep["user_id"],
        "✅ Your deposit has been confirmed."
    )

    await update.message.reply_text("Deposit confirmed successfully.")

# ================= BACKGROUND WATCHER =================
async def deposit_watcher(app):
    while True:
        now = time.time()

        # REQUEST → PENDING
        for d in deposits.find({"status": "REQUESTED"}):
            if now - d["created_at"] > REQUEST_TIMEOUT:
                try:
                    await app.bot.delete_message(
                        DEPOSIT_REQUESTS_GROUP,
                        d["admin_msg_id"]
                    )
                except:
                    pass

                msg = await app.bot.send_message(
                    DEPOSIT_PENDING_GROUP,
                    f"🟠 DEPOSIT PENDING\n\n"
                    f"User: {d['user_id']}\n"
                    f"UTR: {d['utr']}"
                )

                deposits.update_one(
                    {"_id": d["_id"]},
                    {"$set": {
                        "status": "PENDING",
                        "admin_msg_id": msg.message_id,
                        "last_reminder": now
                    }}
                )

        # PENDING REMINDER
        for d in deposits.find({"status": "PENDING"}):
            if now - d["last_reminder"] > PENDING_REMINDER:
                await app.bot.send_message(
                    DEPOSIT_PENDING_GROUP,
                    f"⏰ STILL PENDING\nUTR: {d['utr']}"
                )
                deposits.update_one(
                    {"_id": d["_id"]},
                    {"$set": {"last_reminder": now}}
                )

        await asyncio.sleep(30)

# ================= POST INIT (PTB v21 CORRECT WAY) =================
async def post_init(app):
    app.create_task(deposit_watcher(app))

# ================= MAIN =================
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.post_init = post_init

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.User(ADMIN_ID) & filters.TEXT, admin_handler))
app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, user_input))

app.run_polling(
    drop_pending_updates=True,
    close_loop=False
)
