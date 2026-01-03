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
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://telegram-payment-bot:Jatin161@telegram-payment-bot.cabji2f.mongodb.net/?retryWrites=true&w=majority&serverSelectionTimeoutMS=10000&connectTimeoutMS=10000&socketTimeoutMS=10000")

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
        deposits.create_index("request_id")
        withdrawals.create_index("status")
        withdrawals.create_index("withdrawal_id")
        
        logger.info("✅ Database connected")
        return users, deposits, withdrawals, client
        
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        return None, None, None, None

users_col, deposits_col, withdrawals_col, mongo_client = init_db()

# ================= CONSTANTS =================
UTR_REGEX = r"^\d{12,18}$"
UPI_REGEX = r"^[\w\.\-]{3,256}@[\w]{3,64}$"
DEPOSIT_REQUEST_TIMEOUT = 120  # 2 minutes before reminder
DEPOSIT_CANCEL_TIMEOUT = 600   # 10 minutes before auto-cancel
MAX_REMINDERS = 5
REMINDER_INTERVAL = 120  # 2 minutes between reminders

# ================= CONVERSATION STATES =================
(
    MAIN_MENU,
    DEPOSIT_ENTER_AMOUNT,
    DEPOSIT_SHOW_QR,
    DEPOSIT_WAIT_SCREENSHOT,
    DEPOSIT_WAIT_UTR,
    WITHDRAW_ENTER_AMOUNT,
    WITHDRAW_ENTER_UPI,
    WITHDRAW_CONFIRM,
    USER_INFO
) = range(9)

# ================= KEYBOARDS =================
def get_main_menu_keyboard():
    """Main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("👤 My Info", callback_data="user_info")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard(text="❌ Cancel"):
    """Cancel button"""
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data="cancel")]])

def get_payment_done_keyboard():
    """Payment done button"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Payment Done", callback_data="payment_done")]])

def get_back_keyboard():
    """Back to main menu"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")]])

def get_deposit_cancel_keyboard(request_id=None):
    """Cancel deposit button with request ID"""
    if request_id:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Deposit", callback_data=f"cancel_deposit_{request_id}")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Deposit", callback_data="cancel")]])

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
        "• *Deposit:* Enter amount → Pay → Send screenshot+UTR\n"
        "• *Withdraw:* Enter amount → UPI ID → Wait approval\n"
        "• *Admin confirms:* CONFIRM <UTR>\n"
        "• *UTR:* 12-18 digits from payment\n"
        "• *UPI ID:* user@upi format\n"
        "• Admin approval required for all transactions\n"
        "• Deposit auto-cancels after 5 reminders (10 minutes)"
    )
    
    if update.message:
        await update.message.reply_text(help_text, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(
            help_text,
            reply_markup=get_back_keyboard(),
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
    
    if data == "back_to_main":
        await query.edit_message_text(
            "🤖 *Payment Bot*\n\nWelcome! Use menu below:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    elif data == "deposit":
        context.user_data.clear()
        await query.edit_message_text(
            "💰 *Deposit*\n\nEnter deposit amount (whole number, no decimals):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return DEPOSIT_ENTER_AMOUNT
    
    elif data == "withdraw":
        if user.get("balance", 0) <= 0:
            await query.edit_message_text(
                "❌ Insufficient balance.",
                reply_markup=get_main_menu_keyboard()
            )
            return MAIN_MENU
        
        context.user_data["withdraw_data"] = {}
        await query.edit_message_text(
            f"💸 *Withdraw*\n\nYour balance: ₹{user.get('balance', 0):.0f}\n\nEnter withdrawal amount (whole number, no decimals):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WITHDRAW_ENTER_AMOUNT
    
    elif data == "user_info":
        balance = user.get('balance', 0)
        total_deposits = user.get('total_deposits', 0)
        total_withdrawals = user.get('total_withdrawals', 0)
        
        info_text = (
            f"👤 *User Information*\n\n"
            f"• User ID: `{user.get('uid', 'N/A')}`\n"
            f"• Username: @{user.get('username', 'N/A')}\n"
            f"• Telegram ID: `{user_id}`\n"
            f"• Available Balance: ₹{balance:.0f}\n"
            f"• Total Deposits: ₹{total_deposits:.0f}\n"
            f"• Total Withdrawals: ₹{total_withdrawals:.0f}\n"
            f"• Account Created: {user.get('created_at', 'N/A')}"
        )
        
        await query.edit_message_text(
            info_text,
            reply_markup=get_back_keyboard(),
            parse_mode='Markdown'
        )
        return USER_INFO
    
    elif data == "help":
        return await help_command(update=Update(update_id=0, callback_query=query), context=context)
    
    elif data == "payment_done":
        # Send new message instead of editing photo
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📤 *Send Payment Proof*\n\n1. Send screenshot\n2. Send UTR (12-18 digits)",
            reply_markup=get_deposit_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return DEPOSIT_WAIT_SCREENSHOT
    
    elif data.startswith("cancel_deposit_"):
        request_id = data.split("_")[-1]
        if deposits_col:
            deposit = deposits_col.find_one({"request_id": request_id, "user_id": user_id})
            if deposit and deposit["status"] in ["REQUESTED", "PENDING"]:
                deposits_col.update_one(
                    {"request_id": request_id},
                    {"$set": {"status": "CANCELLED", "cancelled_at": datetime.now(timezone.utc)}}
                )
                
                # Notify admin group
                try:
                    await context.bot.send_message(
                        DEPOSIT_REQUESTS_GROUP_ID,
                        f"❌ DEPOSIT CANCELLED\n\nUser: {user_id}\nAmount: ₹{deposit['amount']}\nUTR: {deposit.get('utr', 'N/A')}"
                    )
                except:
                    pass
                
                await query.edit_message_text(
                    "✅ Deposit request cancelled.",
                    reply_markup=get_main_menu_keyboard()
                )
                context.user_data.clear()
                return MAIN_MENU
        
        await query.edit_message_text(
            "Deposit request not found or already processed.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    elif data == "cancel":
        context.user_data.clear()
        await query.edit_message_text(
            "Cancelled.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    return MAIN_MENU

# ================= DEPOSIT HANDLERS =================
async def handle_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deposit amount input"""
    try:
        amount_text = update.message.text.strip()
        amount = int(amount_text)
        
        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be greater than 0. Enter amount again:",
                reply_markup=get_cancel_keyboard()
            )
            return DEPOSIT_ENTER_AMOUNT
        
        context.user_data["deposit_amount"] = amount
        request_id = str(uuid.uuid4())[:8]
        context.user_data["deposit_request_id"] = request_id
        
        # Show QR code as new photo message
        try:
            with open("qr.jpg", "rb") as qr_file:
                await update.message.reply_photo(
                    photo=qr_file,
                    caption=f"💳 *Pay ₹{amount}*\n\nClick 'Payment Done' after payment\nRequest ID: `{request_id}`",
                    reply_markup=get_payment_done_keyboard(),
                    parse_mode='Markdown'
                )
        except FileNotFoundError:
            await update.message.reply_text(
                f"💳 *Pay ₹{amount}*\n\nClick 'Payment Done' after payment\nRequest ID: `{request_id}`",
                reply_markup=get_payment_done_keyboard(),
                parse_mode='Markdown'
            )
        
        return DEPOSIT_SHOW_QR
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Enter whole number only (no decimals):",
            reply_markup=get_cancel_keyboard()
        )
        return DEPOSIT_ENTER_AMOUNT

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle screenshot"""
    if update.message.photo:
        context.user_data["screenshot_id"] = update.message.photo[-1].file_id
        await update.message.reply_text(
            "✅ Screenshot received!\nNow send UTR (12-18 digits):",
            reply_markup=get_deposit_cancel_keyboard(context.user_data.get("deposit_request_id"))
        )
        return DEPOSIT_WAIT_UTR
    
    await update.message.reply_text(
        "❌ Please send a screenshot first.",
        reply_markup=get_deposit_cancel_keyboard(context.user_data.get("deposit_request_id"))
    )
    return DEPOSIT_WAIT_SCREENSHOT

async def handle_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UTR"""
    utr = update.message.text.strip()
    
    if not re.match(UTR_REGEX, utr):
        await update.message.reply_text(
            "❌ Invalid UTR (12-18 digits). Send UTR again:",
            reply_markup=get_deposit_cancel_keyboard(context.user_data.get("deposit_request_id"))
        )
        return DEPOSIT_WAIT_UTR
    
    if "screenshot_id" not in context.user_data:
        await update.message.reply_text(
            "❌ Send screenshot first.",
            reply_markup=get_deposit_cancel_keyboard(context.user_data.get("deposit_request_id"))
        )
        return DEPOSIT_WAIT_UTR
    
    amount = context.user_data.get("deposit_amount", 0)
    screenshot_id = context.user_data["screenshot_id"]
    request_id = context.user_data.get("deposit_request_id", str(uuid.uuid4())[:8])
    user_id = update.effective_user.id
    username = update.effective_user.username or "N/A"
    
    # Save to database
    if deposits_col:
        try:
            deposits_col.insert_one({
                "request_id": request_id,
                "user_id": user_id,
                "username": username,
                "amount": amount,
                "utr": utr,
                "screenshot_id": screenshot_id,
                "status": "REQUESTED",
                "reminder_count": 0,
                "created_at": datetime.now(timezone.utc),
                "last_reminder": datetime.now(timezone.utc)
            })
        except Exception as e:
            logger.error(f"Error saving deposit: {e}")
    
    # Send to admin group
    try:
        await context.bot.send_photo(
            DEPOSIT_REQUESTS_GROUP_ID,
            photo=screenshot_id,
            caption=(
                f"🟡 *NEW DEPOSIT REQUEST*\n\n"
                f"• User: @{username}\n"
                f"• User ID: `{user_id}`\n"
                f"• Amount: ₹{amount}\n"
                f"• UTR: `{utr}`\n"
                f"• Request ID: `{request_id}`\n\n"
                f"To confirm: `/confirm {utr}`\n"
                f"To cancel: `/cancel_deposit {request_id}`"
            ),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error sending to admin group: {e}")
        await context.bot.send_message(
            DEPOSIT_REQUESTS_GROUP_ID,
            f"🟡 NEW DEPOSIT\nUser: @{username}\nAmount: ₹{amount}\nUTR: {utr}\nRequest ID: {request_id}"
        )
    
    await update.message.reply_text(
        f"✅ *Deposit Request Submitted!*\n\n"
        f"Amount: ₹{amount}\n"
        f"UTR: `{utr}`\n"
        f"Request ID: `{request_id}`\n\n"
        f"Awaiting admin confirmation. Use 'Cancel Deposit' button if needed.",
        reply_markup=get_deposit_cancel_keyboard(request_id),
        parse_mode='Markdown'
    )
    
    # Don't clear context.user_data yet, need request_id for cancellation
    return MAIN_MENU

# ================= WITHDRAW HANDLERS =================
async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal amount input"""
    try:
        amount_text = update.message.text.strip()
        amount = int(amount_text)
        user = get_or_create_user(update.effective_user.id)
        balance = user.get("balance", 0)
        
        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be greater than 0. Enter amount again:",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        if amount > balance:
            await update.message.reply_text(
                f"❌ Insufficient balance. Your balance: ₹{balance:.0f}\nEnter amount again:",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        if amount < 10:
            await update.message.reply_text(
                "❌ Minimum withdrawal amount is ₹10. Enter amount again:",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        context.user_data["withdraw_data"]["amount"] = amount
        
        await update.message.reply_text(
            f"💸 *Enter UPI ID*\n\nAmount: ₹{amount}\n\nEnter your UPI ID (e.g., user@upi):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WITHDRAW_ENTER_UPI
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Enter whole number only (no decimals):",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_AMOUNT

async def handle_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI ID input"""
    upi_id = update.message.text.strip().lower()
    
    if not re.match(UPI_REGEX, upi_id):
        await update.message.reply_text(
            "❌ Invalid UPI ID. Use format: user@upi\nEnter UPI ID again:",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_UPI
    
    context.user_data["withdraw_data"]["upi_id"] = upi_id
    amount = context.user_data["withdraw_data"]["amount"]
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Withdrawal", callback_data="confirm_withdraw")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    
    await update.message.reply_text(
        f"💸 *Confirm Withdrawal*\n\n"
        f"Amount: ₹{amount}\n"
        f"UPI ID: `{upi_id}`\n\n"
        f"Please confirm:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return WITHDRAW_CONFIRM

async def handle_confirm_withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal confirmation from callback"""
    query = update.callback_query
    await query.answer()
    
    data = context.user_data.get("withdraw_data", {})
    upi_id = data.get("upi_id")
    amount = data.get("amount")
    user_id = query.from_user.id
    user = get_or_create_user(user_id, query.from_user.username)
    
    if not upi_id or not amount:
        await query.edit_message_text(
            "❌ Error. Please start over.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    # Double-check balance
    if user.get("balance", 0) < amount:
        await query.edit_message_text(
            f"❌ Insufficient balance. Current balance: ₹{user.get('balance', 0):.0f}",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    withdrawal_id = str(uuid.uuid4())[:8]
    
    # Save withdrawal
    if withdrawals_col:
        try:
            withdrawals_col.insert_one({
                "withdrawal_id": withdrawal_id,
                "user_id": user_id,
                "username": user.get("username", "N/A"),
                "upi_id": upi_id,
                "amount": amount,
                "status": "REQUESTED",
                "created_at": datetime.now(timezone.utc)
            })
        except Exception as e:
            logger.error(f"Error saving withdrawal: {e}")
    
    # Notify admin
    admin_text = (
        f"🟡 *WITHDRAWAL REQUEST*\n\n"
        f"• Request ID: `{withdrawal_id}`\n"
        f"• User: @{user.get('username', 'N/A')}\n"
        f"• User ID: `{user_id}`\n"
        f"• Amount: ₹{amount}\n"
        f"• UPI: `{upi_id}`\n\n"
        f"To process: `/process {withdrawal_id}`\n"
        f"To reject: `/reject {withdrawal_id}`"
    )
    
    try:
        await context.bot.send_message(WITHDRAW_REQUESTS_GROUP_ID, admin_text, parse_mode='Markdown')
    except:
        await context.bot.send_message(ADMIN_ID, admin_text)
    
    await query.edit_message_text(
        f"✅ *Withdrawal Request Submitted!*\n\n"
        f"Amount: ₹{amount}\n"
        f"UPI ID: `{upi_id}`\n"
        f"Request ID: `{withdrawal_id}`\n\n"
        f"Awaiting admin processing.",
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
    
    text = update.message.text.strip()
    
    # Deposit confirmation
    if text.startswith("/confirm"):
        try:
            parts = text.split()
            utr = parts[1] if len(parts) >= 2 else ""
            
            if not utr:
                await update.message.reply_text("Usage: /confirm <UTR>")
                return
            
            # Find deposit
            deposit = None
            if deposits_col:
                deposit = deposits_col.find_one({"utr": utr, "status": {"$in": ["REQUESTED", "PENDING"]}})
            
            if deposit:
                # Update status
                if deposits_col:
                    deposits_col.update_one(
                        {"utr": utr},
                        {"$set": {"status": "COMPLETED", "completed_at": datetime.now(timezone.utc)}}
                    )
                
                # Update user balance
                update_user_balance(deposit["user_id"], deposit["amount"], True)
                
                # Send to completed group
                try:
                    await context.bot.send_message(
                        DEPOSIT_COMPLETED_GROUP_ID,
                        f"✅ *DEPOSIT COMPLETED*\n\n"
                        f"• User: @{deposit.get('username', 'N/A')}\n"
                        f"• Amount: ₹{deposit['amount']}\n"
                        f"• UTR: `{utr}`\n"
                        f"• Request ID: `{deposit.get('request_id', 'N/A')}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                # Notify user
                try:
                    await context.bot.send_message(
                        deposit["user_id"],
                        f"✅ *Deposit Confirmed!*\n\n"
                        f"Amount: ₹{deposit['amount']}\n"
                        f"UTR: `{utr}`\n"
                        f"Your balance has been updated.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Deposit confirmed: {utr}")
            else:
                await update.message.reply_text(f"❌ Deposit not found: {utr}")
                
        except Exception as e:
            logger.error(f"Error confirming deposit: {e}")
            await update.message.reply_text("❌ Error confirming deposit")
    
    # Deposit cancellation by admin
    elif text.startswith("/cancel_deposit"):
        try:
            parts = text.split()
            request_id = parts[1] if len(parts) >= 2 else ""
            
            if not request_id:
                await update.message.reply_text("Usage: /cancel_deposit <REQUEST_ID>")
                return
            
            deposit = None
            if deposits_col:
                deposit = deposits_col.find_one({"request_id": request_id})
            
            if deposit:
                deposits_col.update_one(
                    {"request_id": request_id},
                    {"$set": {"status": "CANCELLED", "cancelled_at": datetime.now(timezone.utc)}}
                )
                
                # Notify user
                try:
                    await context.bot.send_message(
                        deposit["user_id"],
                        f"❌ *Deposit Cancelled*\n\n"
                        f"Amount: ₹{deposit['amount']}\n"
                        f"UTR: `{deposit.get('utr', 'N/A')}`\n"
                        f"Request ID: `{request_id}`\n\n"
                        f"Cancelled by admin.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Deposit cancelled: {request_id}")
            else:
                await update.message.reply_text(f"❌ Deposit not found: {request_id}")
                
        except Exception as e:
            logger.error(f"Error cancelling deposit: {e}")
            await update.message.reply_text("❌ Error cancelling deposit")
    
    # Process withdrawal
    elif text.startswith("/process"):
        try:
            parts = text.split()
            withdrawal_id = parts[1] if len(parts) >= 2 else ""
            
            if not withdrawal_id:
                await update.message.reply_text("Usage: /process <WITHDRAWAL_ID>")
                return
            
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
                        {"$set": {"status": "COMPLETED", "processed_at": datetime.now(timezone.utc)}}
                    )
                
                # Update user balance
                update_user_balance(withdrawal["user_id"], withdrawal["amount"], False)
                
                # Send to completed group
                try:
                    await context.bot.send_message(
                        WITHDRAW_COMPLETED_GROUP_ID,
                        f"✅ *WITHDRAWAL COMPLETED*\n\n"
                        f"• User: @{withdrawal.get('username', 'N/A')}\n"
                        f"• Amount: ₹{withdrawal['amount']:.0f}\n"
                        f"• UPI: `{withdrawal['upi_id']}`\n"
                        f"• Request ID: `{withdrawal_id}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                # Notify user
                try:
                    await context.bot.send_message(
                        withdrawal["user_id"],
                        f"✅ *Withdrawal Processed!*\n\n"
                        f"Amount: ₹{withdrawal['amount']:.0f}\n"
                        f"Sent to: `{withdrawal['upi_id']}`\n"
                        f"Request ID: `{withdrawal_id}`",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Withdrawal processed: {withdrawal_id}")
            else:
                await update.message.reply_text(f"❌ Withdrawal not found: {withdrawal_id}")
                
        except Exception as e:
            logger.error(f"Error processing withdrawal: {e}")
            await update.message.reply_text("❌ Error processing withdrawal")
    
    # Reject withdrawal
    elif text.startswith("/reject"):
        try:
            parts = text.split()
            withdrawal_id = parts[1] if len(parts) >= 2 else ""
            
            if not withdrawal_id:
                await update.message.reply_text("Usage: /reject <WITHDRAWAL_ID>")
                return
            
            withdrawal = None
            if withdrawals_col:
                withdrawal = withdrawals_col.find_one({
                    "withdrawal_id": withdrawal_id,
                    "status": "REQUESTED"
                })
            
            if withdrawal:
                withdrawals_col.update_one(
                    {"withdrawal_id": withdrawal_id},
                    {"$set": {"status": "REJECTED", "rejected_at": datetime.now(timezone.utc)}}
                )
                
                # Notify user
                try:
                    await context.bot.send_message(
                        withdrawal["user_id"],
                        f"❌ *Withdrawal Rejected*\n\n"
                        f"Amount: ₹{withdrawal['amount']:.0f}\n"
                        f"UPI: `{withdrawal['upi_id']}`\n"
                        f"Request ID: `{withdrawal_id}`\n\n"
                        f"Rejected by admin. Contact support for details.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='Markdown'
                    )
                except:
                    pass
                
                await update.message.reply_text(f"✅ Withdrawal rejected: {withdrawal_id}")
            else:
                await update.message.reply_text(f"❌ Withdrawal not found: {withdrawal_id}")
                
        except Exception as e:
            logger.error(f"Error rejecting withdrawal: {e}")
            await update.message.reply_text("❌ Error rejecting withdrawal")

# ================= BACKGROUND TASKS =================
async def deposit_reminder_task(application):
    """Monitor deposits and send reminders"""
    logger.info("Deposit reminder task started")
    
    while True:
        try:
            if deposits_col:
                now = datetime.now(timezone.utc)
                
                # Find pending deposits
                pending_deposits = list(deposits_col.find({
                    "status": {"$in": ["REQUESTED", "PENDING"]},
                    "reminder_count": {"$lt": MAX_REMINDERS}
                }))
                
                for deposit in pending_deposits:
                    time_diff = (now - deposit.get("last_reminder", deposit["created_at"])).total_seconds()
                    
                    if time_diff >= REMINDER_INTERVAL:
                        reminder_count = deposit.get("reminder_count", 0) + 1
                        
                        # Update reminder info
                        deposits_col.update_one(
                            {"_id": deposit["_id"]},
                            {
                                "$set": {
                                    "last_reminder": now,
                                    "status": "PENDING" if reminder_count > 0 else "REQUESTED"
                                },
                                "$inc": {"reminder_count": 1}
                            }
                        )
                        
                        # Send reminder to admin group
                        try:
                            await application.bot.send_message(
                                DEPOSIT_PENDING_GROUP_ID,
                                f"🟠 *DEPOSIT REMINDER #{reminder_count}*\n\n"
                                f"• User: @{deposit.get('username', 'N/A')}\n"
                                f"• Amount: ₹{deposit['amount']}\n"
                                f"• UTR: `{deposit.get('utr', 'N/A')}`\n"
                                f"• Request ID: `{deposit.get('request_id', 'N/A')}`\n"
                                f"• Pending for: {int((now - deposit['created_at']).total_seconds() / 60)} min\n"
                                f"• Reminder: {reminder_count}/{MAX_REMINDERS}\n\n"
                                f"Confirm: `/confirm {deposit.get('utr', '')}`",
                                parse_mode='Markdown'
                            )
                        except:
                            pass
                        
                        # Auto-cancel after MAX_REMINDERS
                        if reminder_count >= MAX_REMINDERS:
                            deposits_col.update_one(
                                {"_id": deposit["_id"]},
                                {"$set": {"status": "AUTO_CANCELLED", "cancelled_at": now}}
                            )
                            
                            # Notify user
                            try:
                                await application.bot.send_message(
                                    deposit["user_id"],
                                    f"❌ *Deposit Auto-Cancelled*\n\n"
                                    f"Amount: ₹{deposit['amount']}\n"
                                    f"UTR: `{deposit.get('utr', 'N/A')}`\n"
                                    f"Request ID: `{deposit.get('request_id', 'N/A')}`\n\n"
                                    f"Your deposit request was cancelled after {MAX_REMINDERS} reminders.",
                                    reply_markup=get_main_menu_keyboard(),
                                    parse_mode='Markdown'
                                )
                            except:
                                pass
                            
                            # Notify admin
                            try:
                                await application.bot.send_message(
                                    DEPOSIT_REQUESTS_GROUP_ID,
                                    f"❌ DEPOSIT AUTO-CANCELLED\n"
                                    f"User: {deposit['user_id']}\n"
                                    f"Amount: ₹{deposit['amount']}\n"
                                    f"UTR: {deposit.get('utr', 'N/A')}"
                                )
                            except:
                                pass
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            logger.error(f"Error in deposit reminder task: {e}")
            await asyncio.sleep(60)

# ================= MAIN =================
async def post_init(application):
    """Post initialization"""
    logger.info("Bot initialized")
    # Start background tasks
    asyncio.create_task(deposit_reminder_task(application))

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
                    CallbackQueryHandler(handle_callback_query, pattern="^(deposit|withdraw|user_info|help|back_to_main|cancel|cancel_deposit_.*)$"),
                ],
                DEPOSIT_ENTER_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deposit_amount),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                DEPOSIT_SHOW_QR: [
                    CallbackQueryHandler(handle_callback_query, pattern="^(payment_done|cancel)$"),
                ],
                DEPOSIT_WAIT_SCREENSHOT: [
                    MessageHandler(filters.PHOTO, handle_screenshot),
                    CallbackQueryHandler(handle_callback_query, pattern="^(cancel|cancel_deposit_.*)$"),
                ],
                DEPOSIT_WAIT_UTR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_utr),
                    CallbackQueryHandler(handle_callback_query, pattern="^(cancel|cancel_deposit_.*)$"),
                ],
                WITHDRAW_ENTER_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_amount),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                WITHDRAW_ENTER_UPI: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_upi_id),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
                WITHDRAW_CONFIRM: [
                    CallbackQueryHandler(handle_confirm_withdraw_callback, pattern="^(confirm_withdraw|cancel)$"),
                ],
                USER_INFO: [
                    CallbackQueryHandler(handle_callback_query, pattern="^back_to_main$"),
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
            filters.User(ADMIN_ID) & filters.TEXT & filters.Regex(r"^/(confirm|cancel_deposit|process|reject)"),
            handle_admin_message
        ))
        
        # Set post initialization
        app.post_init = post_init
        
        # Add error handler
        async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            logger.error(f"Update {update} caused error {context.error}")
        
        app.add_error_handler(error_handler)
        
        # Start bot
        logger.info("Starting bot polling...")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"Bot error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
