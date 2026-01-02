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
import uuid

from config import BOT_TOKEN, MONGO_URI, ADMIN_ID

# ---------------- DATABASE ----------------
client = MongoClient(MONGO_URI)
db = client.telegrambot
users_col = db.users
deposits_col = db.deposits
settings_col = db.settings  # for match code

# ---------------- TEMP STATE ----------------
user_deposits = {}

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not users_col.find_one({"telegram_id": user.id}):
        users_col.insert_one({
            "telegram_id": user.id,
            "username": user.username,
            "uid": str(uuid.uuid4())[:8],
        })

    await show_main_menu(update.message)

async def show_main_menu(message):
    keyboard = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("Match Code", callback_data="match")],
        [InlineKeyboardButton("Referral", callback_data="referral")],
    ]
    await message.reply_text(
        "Welcome. Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ---------------- BUTTON HANDLER ----------------
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_deposits:
        user_deposits[user_id] = 0

    # DEPOSIT MENU
    if query.data == "deposit":
        user_deposits[user_id] = 0
        await show_amount_menu(query, 0)

    # ADD AMOUNT
    elif query.data.startswith("add_"):
        amt = int(query.data.split("_")[1])
        user_deposits[user_id] += amt
        await show_amount_menu(query, user_deposits[user_id])

    # PAY NOW
    elif query.data == "pay_now":
        total = user_deposits.get(user_id, 0)
        if total == 0:
            await query.message.reply_text("Please select amount first.")
            return

        await query.message.reply_photo(
            photo=open("qr.jpg", "rb"),
            caption=f"Pay ₹{total} using this QR.\n\nAfter payment click **Payment Done**.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Payment Done", callback_data="payment_done")]
            ])
        )

    # PAYMENT DONE
    elif query.data == "payment_done":
        context.user_data["awaiting_payment"] = True
        await query.message.reply_text(
            "Please send:\n1️⃣ Payment Screenshot\n2️⃣ UTR Number"
        )

    # MATCH CODE (USER VIEW)
    elif query.data == "match":
        data = settings_col.find_one({"key": "match_code"})
        if data:
            await query.message.reply_text(f"📌 Current Match Code:\n\n{data['value']}")
        else:
            await query.message.reply_text("No match code set yet.")

# ---------------- SHOW AMOUNT MENU ----------------
async def show_amount_menu(query, total):
    keyboard = [
        [
            InlineKeyboardButton("₹10", callback_data="add_10"),
            InlineKeyboardButton("₹50", callback_data="add_50"),
        ],
        [
            InlineKeyboardButton("₹100", callback_data="add_100"),
            InlineKeyboardButton("₹200", callback_data="add_200"),
        ],
        [
            InlineKeyboardButton("₹300", callback_data="add_300"),
        ],
        [
            InlineKeyboardButton("Proceed to Pay", callback_data="pay_now"),
        ],
    ]
    await query.message.edit_text(
        f"Select deposit amounts (tap multiple times):\n\nTotal: ₹{total}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ---------------- PAYMENT DETAILS ----------------
async def handle_payment_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_payment"):
        return

    user_id = update.effective_user.id

    if update.message.photo:
        context.user_data["screenshot"] = update.message.photo[-1].file_id
        await update.message.reply_text("Screenshot received. Now send UTR number.")

    elif update.message.text:
        utr = update.message.text.strip()
        total = user_deposits.get(user_id, 0)

        deposits_col.insert_one({
            "telegram_id": user_id,
            "amount": total,
            "utr": utr,
            "status": "PENDING",
        })

        await update.message.reply_text(
            f"Deposit submitted.\n\nAmount: ₹{total}\nUTR: {utr}\n\nWaiting for confirmation."
        )

        await context.bot.send_message(
            ADMIN_ID,
            f"NEW DEPOSIT\nUser: {user_id}\nAmount: ₹{total}\nUTR: {utr}\n\nReply:\nCONFIRM {utr}"
        )

        context.user_data.clear()
        user_deposits[user_id] = 0

# ---------------- ADMIN CONFIRMATION ----------------
async def handle_admin_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip()

    # SET MATCH CODE
    if text.startswith("SETMATCH "):
        code = text.replace("SETMATCH ", "").strip()
        settings_col.update_one(
            {"key": "match_code"},
            {"$set": {"value": code}},
            upsert=True
        )
        await update.message.reply_text("Match code updated.")
        return

    # CONFIRM DEPOSIT
    if not text.startswith("CONFIRM "):
        return

    utr = text.split(" ", 1)[1]
    dep = deposits_col.find_one({"utr": utr, "status": "PENDING"})

    if not dep:
        await update.message.reply_text("No pending deposit found.")
        return

    deposits_col.update_one(
        {"_id": dep["_id"]},
        {"$set": {"status": "CONFIRMED"}}
    )

    await context.bot.send_message(
        dep["telegram_id"],
        f"✅ Deposit Confirmed\n\nAmount: ₹{dep['amount']}\nUTR: {utr}"
    )

    await update.message.reply_text("Deposit confirmed.")

# ---------------- FALLBACK (PROFESSIONAL) ----------------
async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please use the menu buttons below to continue.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Menu", callback_data="deposit")]
        ])
    )

# ---------------- MAIN ----------------
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(handle_buttons))
app.add_handler(MessageHandler(filters.User(ADMIN_ID) & filters.TEXT, handle_admin_confirmation))
app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_payment_details))
app.add_handler(MessageHandler(filters.ALL, handle_unknown))

app.run_polling(close_loop=False)
