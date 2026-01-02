import re
import uuid
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from pymongo import MongoClient
from config import BOT_TOKEN, MONGO_URI, ADMIN_ID

# ---------------- DB ----------------
client = MongoClient(MONGO_URI)
db = client.telegrambot
users = db.users
deposits = db.deposits
settings = db.settings

# ---------------- CONSTANTS ----------------
UTR_REGEX = r"^\d{12,18}$"   # strict UTR format
SCREENSHOT_TIMEOUT = 30     # seconds

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = users.find_one({"telegram_id": u.id})

    if not user:
        users.insert_one({
            "telegram_id": u.id,
            "username": u.username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0,
            "total_deposit": 0,
        })

    await show_menu(update.message)

async def show_menu(message):
    kb = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("Match Code", callback_data="match")],
        [InlineKeyboardButton("My Info", callback_data="info")],
    ]
    await message.reply_text(
        "Choose an option:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ---------------- BUTTONS ----------------
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "deposit":
        context.user_data.clear()
        context.user_data["deposit_amount"] = 0
        await show_amounts(q, 0)

    elif q.data.startswith("add_"):
        amt = int(q.data.split("_")[1])
        context.user_data["deposit_amount"] += amt
        await show_amounts(q, context.user_data["deposit_amount"])

    elif q.data == "pay":
        amt = context.user_data.get("deposit_amount", 0)
        if amt == 0:
            await q.message.reply_text("Select amount first.")
            return

        context.user_data["awaiting_screenshot"] = True
        await q.message.reply_photo(
            photo=open("qr.jpg", "rb"),
            caption=f"Pay ₹{amt} and send screenshot within 30 seconds."
        )

        asyncio.create_task(remind_screenshot(q, context))

    elif q.data == "info":
        user = users.find_one({"telegram_id": uid})
        await q.message.reply_text(
            f"👤 USER INFO\n\n"
            f"Username: @{user.get('username')}\n"
            f"UID: {user['uid']}\n"
            f"Balance: ₹{user['balance']}\n"
            f"Total Deposit: ₹{user['total_deposit']}"
        )

    elif q.data == "match":
        m = settings.find_one({"key": "match"})
        await q.message.reply_text(
            m["value"] if m else "No match code set."
        )

async def show_amounts(q, total):
    kb = [
        [InlineKeyboardButton("₹10", callback_data="add_10"),
         InlineKeyboardButton("₹50", callback_data="add_50")],
        [InlineKeyboardButton("₹100", callback_data="add_100"),
         InlineKeyboardButton("₹200", callback_data="add_200")],
        [InlineKeyboardButton("₹300", callback_data="add_300")],
        [InlineKeyboardButton("Proceed", callback_data="pay")]
    ]
    await q.message.edit_text(
        f"Select amount:\n\nTotal: ₹{total}",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ---------------- REMINDER ----------------
async def remind_screenshot(q, context):
    await asyncio.sleep(SCREENSHOT_TIMEOUT)
    if context.user_data.get("awaiting_screenshot"):
        await q.message.reply_text(
            "⏰ Reminder: Please send payment screenshot."
        )

# ---------------- PAYMENT INPUT ----------------
async def payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_screenshot"):
        if update.message.photo:
            context.user_data["screenshot"] = update.message.photo[-1].file_id
            context.user_data["awaiting_screenshot"] = False
            context.user_data["awaiting_utr"] = True
            await update.message.reply_text("Screenshot received. Send UTR number.")
            return
        else:
            await update.message.reply_text("Please send a payment screenshot.")
            return

    if context.user_data.get("awaiting_utr"):
        text = update.message.text.strip()
        if not re.match(UTR_REGEX, text):
            await update.message.reply_text(
                "❌ Invalid UTR.\nUTR must be 12–18 digits."
            )
            return

        amt = context.user_data["deposit_amount"]
        deposits.insert_one({
            "telegram_id": update.effective_user.id,
            "amount": amt,
            "utr": text,
            "status": "PENDING",
        })

        await update.message.reply_text(
            "✅ Deposit submitted.\nWaiting for confirmation."
        )

        await context.bot.send_message(
            ADMIN_ID,
            f"NEW DEPOSIT\nUser: {update.effective_user.id}\n"
            f"Amount: ₹{amt}\nUTR: {text}\n\nCONFIRM {text}"
        )

        context.user_data.clear()

# ---------------- ADMIN ----------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    t = update.message.text.strip()

    if t.startswith("CONFIRM "):
        utr = t.split()[1]
        dep = deposits.find_one({"utr": utr, "status": "PENDING"})
        if not dep:
            await update.message.reply_text("No pending deposit.")
            return

        users.update_one(
            {"telegram_id": dep["telegram_id"]},
            {"$inc": {"balance": dep["amount"], "total_deposit": dep["amount"]}}
        )

        deposits.update_one(
            {"_id": dep["_id"]},
            {"$set": {"status": "CONFIRMED"}}
        )

        await context.bot.send_message(
            dep["telegram_id"],
            f"✅ Deposit Confirmed\n₹{dep['amount']} credited."
        )

        await update.message.reply_text("Confirmed.")

    elif t.startswith("SETMATCH "):
        settings.update_one(
            {"key": "match"},
            {"$set": {"value": t[9:]}},
            upsert=True
        )
        await update.message.reply_text("Match code updated.")

# ---------------- FALLBACK ----------------
async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update.message)

# ---------------- MAIN ----------------
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(buttons))
app.add_handler(MessageHandler(filters.User(ADMIN_ID) & filters.TEXT, admin))
app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, payment_input))
app.add_handler(MessageHandler(filters.ALL, fallback))
app.run_polling(close_loop=False)
