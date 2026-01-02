from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from pymongo import MongoClient
import uuid

from config import BOT_TOKEN, MONGO_URI, ADMIN_ID

client = MongoClient(MONGO_URI)
db = client.botdb
users = db.users

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    existing = users.find_one({"telegram_id": user.id})
    if not existing:
        uid = str(uuid.uuid4())[:8]
        users.insert_one({
            "telegram_id": user.id,
            "username": user.username,
            "uid": uid,
            "referrals": 0
        })

    keyboard = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("Match Timings", callback_data="match")],
        [InlineKeyboardButton("Referral", callback_data="referral")]
    ]

    await update.message.reply_text(
        "Welcome. Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))

app.run_polling()
