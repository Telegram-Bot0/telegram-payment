import os
import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from pymongo import MongoClient

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7998689373:AAGUaTxrHzWabJvqZAUM2NjRQb1PHXpmGqA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7495749312"))
DEPOSIT_REQUESTS_GROUP_ID = int(os.getenv("DEPOSIT_REQUESTS_GROUP_ID", "-1003687850459"))
DEPOSIT_PENDING_GROUP_ID = int(os.getenv("DEPOSIT_PENDING_GROUP_ID", "-1003415130807"))
DEPOSIT_COMPLETED_GROUP_ID = int(os.getenv("DEPOSIT_COMPLETED_GROUP_ID", "-1003513719508"))
WITHDRAW_REQUESTS_GROUP_ID = int(os.getenv("WITHDRAW_REQUESTS_GROUP_ID", "-1003522192758"))
WITHDRAW_COMPLETED_GROUP_ID = int(os.getenv("WITHDRAW_COMPLETED_GROUP_ID", "-1003637541728"))
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://telegram-payment-bot:Jatin161@telegram-payment-bot.cabji2f.mongodb.net/?retryWrites=true&w=majority&serverSelectionTimeoutMS=10000&connectTimeoutMS=10000")

# ================= LOGGING =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= DATABASE =================
def init_db():
    """Initialize MongoDB connection"""
    try:
        client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000
        )
        client.admin.command('ping')
        db = client.telegram_payment_bot
        
        # Collections
        users = db.users
        deposits = db.deposits
        withdrawals = db.withdrawals
        
        # Create indexes
        users.create_index("telegram_id", unique=True)
        deposits.create_index("utr", unique=True)
        deposits.create_index("status")
        withdrawals.create_index("status")
        
        logger.info("✅ Database connected")
        return users, deposits, withdrawals, client
        
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        return None, None, None, None

users_col, deposits_col, withdrawals_col, mongo_client = init_db()

# ================= CONSTANTS =================
UTR_REGEX = r"^\d{12,18}$"
REQUEST_TIMEOUT = 120  # 2 minutes
PENDING_REMINDER = 60  # 1 minute
DEPOSIT_AMOUNTS = [10, 50, 100, 200, 300]

# ================= CONVERSATION STATES =================
(
    MAIN_MENU,
    DEPOSIT_SELECT_AMOUNT,
    DEPOSIT_WAIT_PAYMENT,
    DEPOSIT_WAIT_SCREENSHOT,
    DEPOSIT_WAIT_UTR,
    WITHDRAW_ENTER_UPI,
    WITHDRAW_ENTER_AMOUNT,
    WITHDRAW_CONFIRM
) = range(8)

# ================= KEYBOARDS =================
def get_main_menu_keyboard():
    """Main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("📊 Balance", callback_data="balance")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_deposit_amount_keyboard(selected_amounts: List[int] = None):
    """Deposit amount selection"""
    if selected_amounts is None:
        selected_amounts = []
    
    keyboard = []
    row = []
    for amount in DEPOSIT_AMOUNTS:
        prefix = "✅ " if amount in selected_amounts else ""
        row.append(InlineKeyboardButton(f"{prefix}₹{amount}", callback_data=f"amount_{amount}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([
        InlineKeyboardButton("✅ Proceed", callback_data="proceed"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ])
    return InlineKeyboardMarkup(keyboard)

def get_payment_done_keyboard():
    """Payment done button"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Payment Done", callback_data="payment_done")]])

def get_cancel_keyboard():
    """Cancel button"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

# ================= USER MANAGEMENT =================
def get_or_create_user(telegram_id: int, username: str = None):
    """Get or create user"""
    if users_col is None:
        return {
            "telegram_id": telegram_id,
            "username": username,
            "balance": 0.0,
            "total_deposits": 0.0,
            "total_withdrawals": 0.0
        }
    
    try:
        user = users_col.find_one({"telegram_id": telegram_id})
        if not user:
            user = {
                "telegram_id": telegram_id,
                "username": username,
                "uid": str(uuid.uuid4())[:8],
                "balance": 0.0,
                "total_deposits": 0.0,
                "total_withdrawals": 0.0,
                "created_at": datetime.now(timezone.utc)
            }
            users_col.insert_one(user)
        return user
    except:
        return {
            "telegram_id": telegram_id,
            "username": username,
            "balance": 0.0,
            "total_deposits": 0.0,
            "total_withdrawals": 0.0
        }

def update_user_balance(telegram_id: int, amount: float, is_deposit: bool = True):
    """Update user balance"""
    if users_col is None:
        return False
    
    try:
        if is_deposit:
            users_col.update_one(
                {"telegram_id": telegram_id},
                {"$inc": {"balance": amount, "total_deposits": amount}}
            )
        else:
            users_col.update_one(
                {"telegram_id": telegram_id},
                {"$inc": {"balance": -amount, "total_withdrawals": amount}}
            )
        return True
    except:
        return False

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    get_or_create_user(update.effective_user.id, update.effective_user.username)
    
    await update.message.reply_text(
        "🤖 *Payment Bot*\n\nWelcome! Use menu below:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode='Markdown'
    )
    return MAIN_MENU

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help"""
    help_text = (
        "📚 *Help Guide*\n\n"
        "• *Deposit:* Select amount → Pay → Send screenshot+UTR\n"
        "• *Withdraw:* Enter UPI → Amount → Wait approval\n"
        "• *Admin confirms:* CONFIRM <UTR>\n"
        "• *UTR:* 12-18 digits from payment\n"
        "• Admin approval required for all transactions"
    )
    
    if update.message:
        await update.message.reply_text(help_text, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(
            help_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
    return MAIN_MENU

# ================= CALLBACK HANDLERS =================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    user = get_or_create_user(user_id, query.from_user.username)
    
    if data == "deposit":
        context.user_data["selected_amounts"] = []
        text = "💰 *Deposit*\nSelect amounts:\nTotal: ₹0\nClick amounts then ✅ Proceed"
        await query.edit_message_text(text, reply_markup=get_deposit_amount_keyboard(), parse_mode='Markdown')
        return DEPOSIT_SELECT_AMOUNT
    
    elif data == "withdraw":
        if user.get("balance", 0) <= 0:
            await query.edit_message_text("❌ Insufficient balance.", reply_markup=get_main_menu_keyboard())
            return MAIN_MENU
        context.user_data["withdraw_data"] = {}
        await query.edit_message_text("Enter UPI ID (e.g., user@upi):", reply_markup=get_cancel_keyboard())
        return WITHDRAW_ENTER_UPI
    
    elif data == "balance":
        balance_text = f"📊 *Balance*\n\nAvailable: ₹{user.get('balance', 0):.2f}\nDeposits: ₹{user.get('total_deposits', 0):.2f}\nWithdrawals: ₹{user.get('total_withdrawals', 0):.2f}"
        await query.edit_message_text(balance_text, reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')
        return MAIN_MENU
    
    elif data == "help":
        return await help_command(update=Update(update_id=0, callback_query=query), context=context)
    
    elif data.startswith("amount_"):
        amount = int(data.split("_")[1])
        selected = context.user_data.get("selected_amounts", [])
        if amount in selected:
            selected.remove(amount)
        else:
            selected.append(amount)
        context.user_data["selected_amounts"] = selected
        total = sum(selected)
        text = f"💰 *Deposit*\nSelect amounts:\nTotal: ₹{total}\nClick amounts then ✅ Proceed"
        await query.edit_message_text(text, reply_markup=get_deposit_amount_keyboard(selected), parse_mode='Markdown')
        return DEPOSIT_SELECT_AMOUNT
    
    elif data == "proceed":
        selected = context.user_data.get("selected_amounts", [])
        if not selected:
            await query.edit_message_text("❌ Select at least one amount.", reply_markup=get_deposit_amount_keyboard())
            return DEPOSIT_SELECT_AMOUNT
        
        total = sum(selected)
        context.user_data["deposit_amount"] = total
        
        # Show QR code
        try:
            with open("qr.jpg", "rb") as qr_file:
                await query.message.reply_photo(
                    photo=qr_file,
                    caption=f"💳 *Pay ₹{total}*\nClick 'Payment Done' after payment",
                    reply_markup=get_payment_done_keyboard(),
                    parse_mode='Markdown'
                )
        except:
            await query.edit_message_text(
                f"💳 *Pay ₹{total}*\nClick 'Payment Done' after payment",
                reply_markup=get_payment_done_keyboard(),
                parse_mode='Markdown'
            )
        return DEPOSIT_WAIT_PAYMENT
    
    elif data == "payment_done":
        await query.edit_message_text(
            "📤 *Send Payment Proof*\n\n1. Send screenshot\n2. Send UTR (12-18 digits)",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return DEPOSIT_WAIT_SCREENSHOT
    
    elif data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("Cancelled.", reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    
    elif data == "confirm_withdraw":
        return await handle_confirm_withdraw(update, context, user)
    
    return MAIN_MENU

# ================= MESSAGE HANDLERS =================
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle screenshot"""
    if update.message.photo:
        context.user_data["screenshot_id"] = update.message.photo[-1].file_id
        await update.message.reply_text("✅ Screenshot received!\nNow send UTR:", reply_markup=get_cancel_keyboard())
        return DEPOSIT_WAIT_UTR
    
    await update.message.reply_text("❌ Send screenshot first.", reply_markup=get_cancel_keyboard())
    return DEPOSIT_WAIT_SCREENSHOT

async def handle_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UTR"""
    utr = update.message.text.strip()
    
    if not re.match(UTR_REGEX, utr):
        await update.message.reply_text("❌ Invalid UTR (12-18 digits).", reply_markup=get_cancel_keyboard())
        return DEPOSIT_WAIT_UTR
    
    if "screenshot_id" not in context.user_data:
        await update.message.reply_text("❌ Send screenshot first.", reply_markup=get_cancel_keyboard())
        return DEPOSIT_WAIT_UTR
    
    amount = context.user_data.get("deposit_amount", 0)
    screenshot_id = context.user_data["screenshot_id"]
    user_id = update.effective_user.id
    
    # Save to database
    if deposits_col:
        try:
            deposits_col.insert_one({
                "user_id": user_id,
                "amount": amount,
                "utr": utr,
                "status": "REQUESTED",
                "created_at": datetime.now(timezone.utc)
            })
        except:
            pass
    
    # Send to admin group
    try:
        await context.bot.send_photo(
            DEPOSIT_REQUESTS_GROUP_ID,
            photo=screenshot_id,
            caption=f"🟡 NEW DEPOSIT\n\nUser: {user_id}\nAmount: ₹{amount}\nUTR: {utr}\n\nCONFIRM {utr}"
        )
    except:
        await context.bot.send_message(
            DEPOSIT_REQUESTS_GROUP_ID,
            f"🟡 NEW DEPOSIT\n\nUser: {user_id}\nAmount: ₹{amount}\nUTR: {utr}\n\nCONFIRM {utr}"
        )
    
    await update.message.reply_text(
        f"✅ Submitted!\nAmount: ₹{amount}\nUTR: {utr}\nAwaiting confirmation.",
        reply_markup=get_main_menu_keyboard()
    )
    
    context.user_data.clear()
    return MAIN_MENU

async def handle_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI input"""
    upi_id = update.message.text.strip().lower()
    
    if "@" not in upi_id:
        await update.message.reply_text("❌ Invalid UPI. Use: user@upi", reply_markup=get_cancel_keyboard())
        return WITHDRAW_ENTER_UPI
    
    context.user_data["withdraw_data"]["upi_id"] = upi_id
    
    user = get_or_create_user(update.effective_user.id)
    balance = user.get("balance", 0)
    
    await update.message.reply_text(
        f"💸 *Enter Amount*\n\nBalance: ₹{balance:.2f}\nUPI: {upi_id}\n\nEnter amount:",
        reply_markup=get_cancel_keyboard(),
        parse_mode='Markdown'
    )
    return WITHDRAW_ENTER_AMOUNT

async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal amount"""
    try:
        amount = float(update.message.text.strip())
        user = get_or_create_user(update.effective_user.id)
        balance = user.get("balance", 0)
        
        if amount <= 0 or amount > balance or amount < 10:
            await update.message.reply_text(
                f"❌ Invalid amount.\nMin: ₹10, Max: ₹{balance:.2f}",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        context.user_data["withdraw_data"]["amount"] = amount
        
        keyboard = [
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm_withdraw")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        
        upi_id = context.user_data["withdraw_data"]["upi_id"]
        await update.message.reply_text(
            f"💸 *Confirm Withdrawal*\n\nUPI: `{upi_id}`\nAmount: ₹{amount:.2f}\n\nConfirm?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return WITHDRAW_CONFIRM
        
    except:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=get_cancel_keyboard())
        return WITHDRAW_ENTER_AMOUNT

async def handle_confirm_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    """Confirm withdrawal"""
    query = update.callback_query
    await query.answer()
    
    data = context.user_data.get("withdraw_data", {})
    upi_id = data.get("upi_id")
    amount = data.get("amount")
    
    if not upi_id or not amount:
        await query.edit_message_text("❌ Error. Start over.", reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    
    withdrawal_id = str(uuid.uuid4())[:8]
    
    # Save withdrawal
    if withdrawals_col:
        try:
            withdrawals_col.insert_one({
                "withdrawal_id": withdrawal_id,
                "user_id": user["telegram_id"],
                "upi_id": upi_id,
                "amount": amount,
                "status": "REQUESTED",
                "created_at": datetime.now(timezone.utc)
            })
        except:
            pass
    
    # Notify admin
    admin_text = (
        f"🟡 *WITHDRAWAL REQUEST*\n\n"
        f"• ID: `{withdrawal_id}`\n"
        f"• User: {user['telegram_id']}\n"
        f"• Amount: ₹{amount:.2f}\n"
        f"• UPI: `{upi_id}`\n\n"
        f"Reply: DONE {withdrawal_id}"
    )
    
    try:
        await context.bot.send_message(WITHDRAW_REQUESTS_GROUP_ID, admin_text, parse_mode='Markdown')
    except:
        await context.bot.send_message(ADMIN_ID, admin_text)
    
    await query.edit_message_text(
        f"✅ *Withdrawal Requested!*\n\nAmount: ₹{amount:.2f}\nUPI: `{upi_id}`\n\nAwaiting approval.",
        reply_markup=get_main_menu_keyboard(),
        parse_mode='Markdown'
    )
    
    context.user_data.clear()
    return MAIN_MENU

# ================= ADMIN HANDLER =================
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin commands"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = update.message.text.strip().upper()
    
    # Deposit confirmation
    if text.startswith("CONFIRM"):
        try:
            parts = text.split()
            utr = parts[1] if len(parts) >= 2 else text[7:].strip()
            
            # Find deposit
            deposit = None
            if deposits_col:
                deposit = deposits_col.find_one({"utr": utr, "status": "REQUESTED"})
            
            if deposit:
                # Update status
                if deposits_col:
                    deposits_col.update_one(
                        {"utr": utr},
                        {"$set": {"status": "COMPLETED"}}
                    )
                
                # Update user balance
                update_user_balance(deposit["user_id"], deposit["amount"], True)
                
                # Send to completed group
                await context.bot.send_message(
                    DEPOSIT_COMPLETED_GROUP_ID,
                    f"✅ DEPOSIT COMPLETED\n\nUser: {deposit['user_id']}\nAmount: ₹{deposit['amount']}\nUTR: {utr}"
                )
                
                # Notify user
                try:
                    await context.bot.send_message(
                        deposit["user_id"],
                        f"✅ Deposit confirmed!\nAmount: ₹{deposit['amount']}\nBalance updated.",
                        reply_markup=get_main_menu_keyboard()
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Deposit confirmed: {utr}")
            else:
                await update.message.reply_text(f"❌ Deposit not found: {utr}")
                
        except:
            await update.message.reply_text("❌ Error confirming deposit")
    
    # Withdrawal completion
    elif text.startswith("DONE"):
        try:
            parts = text.split()
            withdrawal_id = parts[1] if len(parts) >= 2 else text[4:].strip()
            
            # Find withdrawal
            withdrawal = None
            if withdrawals_col:
                withdrawal = withdrawals_col.find_one({
                    "withdrawal_id": withdrawal_id,
                    "status": "REQUESTED"
                })
            
            if withdrawal:
                # Update status
                if withdrawals_col:
                    withdrawals_col.update_one(
                        {"withdrawal_id": withdrawal_id},
                        {"$set": {"status": "COMPLETED"}}
                    )
                
                # Update user balance
                update_user_balance(withdrawal["user_id"], withdrawal["amount"], False)
                
                # Send to completed group
                await context.bot.send_message(
                    WITHDRAW_COMPLETED_GROUP_ID,
                    f"✅ WITHDRAWAL COMPLETED\n\nUser: {withdrawal['user_id']}\nAmount: ₹{withdrawal['amount']:.2f}\nUPI: {withdrawal['upi_id']}"
                )
                
                # Notify user
                try:
                    await context.bot.send_message(
                        withdrawal["user_id"],
                        f"✅ Withdrawal processed!\nAmount: ₹{withdrawal['amount']:.2f}\nSent to: {withdrawal['upi_id']}",
                        reply_markup=get_main_menu_keyboard()
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Withdrawal processed: {withdrawal_id}")
            else:
                await update.message.reply_text(f"❌ Withdrawal not found: {withdrawal_id}")
                
        except:
            await update.message.reply_text("❌ Error processing withdrawal")

# ================= BACKGROUND TASK =================
async def deposit_watcher(application):
    """Monitor deposits"""
    logger.info("Deposit watcher started")
    
    while True:
        try:
            if deposits_col:
                now = datetime.now(timezone.utc)
                
                # Move REQUESTED to PENDING after 2 minutes
                for deposit in deposits_col.find({"status": "REQUESTED"}):
                    age = (now - deposit["created_at"]).total_seconds()
                    if age > REQUEST_TIMEOUT:
                        deposits_col.update_one(
                            {"_id": deposit["_id"]},
                            {"$set": {"status": "PENDING"}}
                        )
                        
                        await application.bot.send_message(
                            DEPOSIT_PENDING_GROUP_ID,
                            f"🟠 PENDING\n\nUser: {deposit['user_id']}\nAmount: ₹{deposit['amount']}\nUTR: {deposit['utr']}\nWaiting: {int(age/60)} min"
                        )
            
            await asyncio.sleep(30)
        except:
            await asyncio.sleep(30)

# ================= MAIN =================
async def post_init(application):
    """Post initialization"""
    logger.info("Bot initialized")
    asyncio.create_task(deposit_watcher(application))

def main():
    """Main function"""
    logger.info("=" * 50)
    logger.info("Starting Telegram Payment Bot")
    logger.info(f"Admin: {ADMIN_ID}")
    logger.info("=" * 50)
    
    # Add startup delay to avoid conflict
    time.sleep(5)
    
    try:
        # Create application
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                MAIN_MENU: [
                    CallbackQueryHandler(handle_callback_query, pattern="^(deposit|withdraw|balance|help)$"),
                ],
                DEPOSIT_SELECT_AMOUNT: [
                    CallbackQueryHandler(handle_callback_query, pattern="^(amount_|proceed|cancel)"),
                ],
                DEPOSIT_WAIT_PAYMENT: [
                    CallbackQueryHandler(handle_callback_query, pattern="^(payment_done|cancel)"),
                ],
                DEPOSIT_WAIT_SCREENSHOT: [
                    MessageHandler(filters.PHOTO, handle_screenshot),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                DEPOSIT_WAIT_UTR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_utr),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                WITHDRAW_ENTER_UPI: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_upi_id),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                WITHDRAW_ENTER_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_amount),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                WITHDRAW_CONFIRM: [
                    CallbackQueryHandler(handle_callback_query, pattern="^(confirm_withdraw|cancel)$"),
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                CommandHandler("help", help_command),
            ],
            allow_reentry=True
        )
        
        # Add handlers
        app.add_handler(conv_handler)
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(MessageHandler(
            filters.User(ADMIN_ID) & filters.TEXT & ~filters.COMMAND,
            handle_admin_message
        ))
        
        # Set post initialization
        app.post_init = post_init
        
        # Start bot
        logger.info("Starting bot polling...")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"Bot error: {e}")

if __name__ == "__main__":
    main()
