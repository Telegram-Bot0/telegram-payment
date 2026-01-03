import asyncio
import logging
import re
import time
import uuid
import signal
import sys
from datetime import datetime, timezone
from typing import Dict, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
import telegram.error

from config import (
    BOT_TOKEN,
    ADMIN_ID,
    DEPOSIT_REQUESTS_GROUP_ID,
    DEPOSIT_PENDING_GROUP_ID,
    DEPOSIT_COMPLETED_GROUP_ID,
    WITHDRAW_REQUESTS_GROUP_ID,
    WITHDRAW_COMPLETED_GROUP_ID,
    MONGO_URI
)

# ================= LOGGING SETUP =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= GRACEFUL SHUTDOWN =================
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info("Received shutdown signal. Stopping bot gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ================= DATABASE SETUP =================
def initialize_database():
    """Initialize database connection with retry logic"""
    logger.info("Initializing database connection...")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000,
                maxPoolSize=10,
                minPoolSize=1,
                retryWrites=True,
                retryReads=True
            )
            
            # Test connection
            client.admin.command('ping')
            logger.info(f"Database ping successful (attempt {attempt + 1})")
            
            db = client.telegram_payment_bot
            users_col = db.users
            deposits_col = db.deposits
            withdrawals_col = db.withdrawals
            
            # Create indexes if they don't exist
            users_col.create_index("telegram_id", unique=True)
            deposits_col.create_index("utr", unique=True)
            deposits_col.create_index("status")
            withdrawals_col.create_index("status")
            
            logger.info("Database indexes created/verified")
            return users_col, deposits_col, withdrawals_col, client
            
        except Exception as e:
            logger.warning(f"Database connection attempt {attempt + 1} failed: {str(e)[:100]}")
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                logger.error("All database connection attempts failed. Running in fallback mode.")
                return None, None, None, None

# Initialize database
users_col, deposits_col, withdrawals_col, mongo_client = initialize_database()

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
    """Return main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("📊 Balance", callback_data="balance")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_deposit_amount_keyboard(selected_amounts: List[int] = None):
    """Keyboard for deposit amount selection"""
    if selected_amounts is None:
        selected_amounts = []
    
    keyboard = []
    row = []
    for amount in DEPOSIT_AMOUNTS:
        prefix = "✅ " if amount in selected_amounts else ""
        row.append(InlineKeyboardButton(
            f"{prefix}₹{amount}",
            callback_data=f"amount_{amount}"
        ))
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
    """Keyboard after showing QR code"""
    keyboard = [[
        InlineKeyboardButton("✅ Payment Done", callback_data="payment_done")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    """Cancel option keyboard"""
    keyboard = [[
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ]]
    return InlineKeyboardMarkup(keyboard)

# ================= UTILITY FUNCTIONS =================
def get_current_utc_time():
    """Get current UTC time (timezone aware)"""
    return datetime.now(timezone.utc)

# ================= USER MANAGEMENT =================
def get_or_create_user(telegram_id: int, username: str = None):
    """Get user from DB or create if not exists"""
    if users_col is None:
        # Fallback without database
        return {
            "telegram_id": telegram_id,
            "username": username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0.0,
            "total_deposits": 0.0,
            "total_withdrawals": 0.0,
            "created_at": get_current_utc_time()
        }
    
    try:
        user = users_col.find_one_and_update(
            {"telegram_id": telegram_id},
            {"$setOnInsert": {
                "telegram_id": telegram_id,
                "username": username,
                "uid": str(uuid.uuid4())[:8],
                "balance": 0.0,
                "total_deposits": 0.0,
                "total_withdrawals": 0.0,
                "created_at": get_current_utc_time(),
                "updated_at": get_current_utc_time()
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return user
    except Exception as e:
        logger.error(f"Error getting/creating user {telegram_id}: {e}")
        return {
            "telegram_id": telegram_id,
            "username": username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0.0,
            "total_deposits": 0.0,
            "total_withdrawals": 0.0,
            "created_at": get_current_utc_time()
        }

def update_user_balance(telegram_id: int, amount: float, is_deposit: bool = True):
    """Update user balance after deposit/withdrawal"""
    if users_col is None:
        logger.warning("Cannot update balance: Database not available")
        return False
    
    try:
        update_data = {
            "$inc": {"balance": amount},
            "$set": {"updated_at": get_current_utc_time()}
        }
        
        if is_deposit:
            update_data["$inc"]["total_deposits"] = amount
        else:
            update_data["$inc"]["total_withdrawals"] = amount
            
        result = users_col.update_one(
            {"telegram_id": telegram_id},
            update_data
        )
        
        if result.modified_count > 0:
            logger.debug(f"Updated balance for user {telegram_id}: {'+' if is_deposit else '-'}{amount}")
            return True
        else:
            logger.warning(f"Failed to update balance for user {telegram_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating balance for user {telegram_id}: {e}")
        return False

# ================= COMMAND HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        user = update.effective_user
        
        # Store user in database
        get_or_create_user(user.id, user.username)
        
        # Send welcome message with main menu
        welcome_text = (
            "🤖 *Payment Bot*\n\n"
            "Welcome! Use the menu below to manage your payments.\n\n"
            "💰 *Deposit* - Add funds to your account\n"
            "💸 *Withdraw* - Withdraw funds to your UPI\n"
            "📊 *Balance* - Check your current balance\n"
            "ℹ️ *Help* - Get assistance\n"
        )
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text(
            "❌ An error occurred. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    try:
        help_text = (
            "📚 *Help Guide*\n\n"
            "• *Deposit Process:*\n"
            "  1. Select Deposit → Choose amount(s)\n"
            "  2. View QR code and make payment\n"
            "  3. Click 'Payment Done'\n"
            "  4. Send payment screenshot\n"
            "  5. Send UTR number (12-18 digits)\n\n"
            
            "• *Withdraw Process:*\n"
            "  1. Select Withdraw\n"
            "  2. Enter your UPI ID\n"
            "  3. Enter withdrawal amount\n"
            "  4. Submit for admin approval\n\n"
            
            "• *UTR Number:*\n"
            "  A 12-18 digit number from your payment receipt\n\n"
            
            "⚠️ *Important:*\n"
            "• Always use official payment methods\n"
            "• Keep screenshots for reference\n"
            "• Admin approval required for all transactions\n"
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
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        return MAIN_MENU

# ================= CALLBACK QUERY HANDLERS =================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    try:
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        
        # Get user from database
        user = get_or_create_user(user_id, query.from_user.username)
        
        # Handle different callback actions
        if data == "deposit":
            return await handle_deposit(query, context)
        elif data == "withdraw":
            return await handle_withdraw(query, context, user)
        elif data == "balance":
            return await handle_balance(query, context, user)
        elif data == "help":
            return await handle_help(query, context)
        elif data.startswith("amount_"):
            return await handle_amount_selection(query, context)
        elif data == "proceed":
            return await handle_proceed(query, context, user)
        elif data == "payment_done":
            return await handle_payment_done(query, context)
        elif data == "cancel":
            return await handle_cancel(query, context)
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in callback query handler: {e}")
        return MAIN_MENU

async def handle_deposit(query, context):
    """Handle deposit button click"""
    try:
        # Initialize selected amounts if not exists
        if "selected_amounts" not in context.user_data:
            context.user_data["selected_amounts"] = []
        
        text = (
            "💰 *Deposit Funds*\n\n"
            "Select amount(s) to deposit:\n"
            f"Total selected: ₹{sum(context.user_data['selected_amounts'])}\n\n"
            "Click amounts to select/deselect, then click ✅ Proceed"
        )
        
        await query.edit_message_text(
            text,
            reply_markup=get_deposit_amount_keyboard(context.user_data["selected_amounts"]),
            parse_mode='Markdown'
        )
        
        return DEPOSIT_SELECT_AMOUNT
    except Exception as e:
        logger.error(f"Error in handle_deposit: {e}")
        await query.edit_message_text(
            "❌ An error occurred. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_withdraw(query, context, user):
    """Handle withdraw button click"""
    try:
        if user.get("balance", 0) <= 0:
            await query.edit_message_text(
                "❌ Insufficient balance for withdrawal.",
                reply_markup=get_main_menu_keyboard()
            )
            return MAIN_MENU
        
        await query.edit_message_text(
            "💸 *Withdraw Funds*\n\n"
            "Please enter your UPI ID (e.g., username@upi):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        
        context.user_data["withdraw_data"] = {}
        return WITHDRAW_ENTER_UPI
    except Exception as e:
        logger.error(f"Error in handle_withdraw: {e}")
        await query.edit_message_text(
            "❌ An error occurred. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_balance(query, context, user):
    """Handle balance check"""
    try:
        balance_text = (
            f"📊 *Account Balance*\n\n"
            f"• Available Balance: ₹{user.get('balance', 0):.2f}\n"
            f"• Total Deposits: ₹{user.get('total_deposits', 0):.2f}\n"
            f"• Total Withdrawals: ₹{user.get('total_withdrawals', 0):.2f}\n\n"
            f"User ID: `{user.get('uid', 'N/A')}`"
        )
        
        await query.edit_message_text(
            balance_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in handle_balance: {e}")
        await query.edit_message_text(
            "❌ Error fetching balance. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_help(query, context):
    """Handle help from menu"""
    return await help_command(update=Update(update_id=0, callback_query=query), context=context)

async def handle_amount_selection(query, context):
    """Handle amount selection for deposit"""
    try:
        amount = int(query.data.split("_")[1])
        selected_amounts = context.user_data.get("selected_amounts", [])
        
        if amount in selected_amounts:
            selected_amounts.remove(amount)
        else:
            selected_amounts.append(amount)
        
        context.user_data["selected_amounts"] = selected_amounts
        
        total = sum(selected_amounts)
        text = (
            f"💰 *Deposit Funds*\n\n"
            f"Select amount(s) to deposit:\n"
            f"Total selected: ₹{total}\n\n"
            f"Click amounts to select/deselect, then click ✅ Proceed"
        )
        
        await query.edit_message_text(
            text,
            reply_markup=get_deposit_amount_keyboard(selected_amounts),
            parse_mode='Markdown'
        )
        
        return DEPOSIT_SELECT_AMOUNT
    except Exception as e:
        logger.error(f"Error in handle_amount_selection: {e}")
        await query.edit_message_text(
            "❌ Error selecting amount. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_proceed(query, context, user):
    """Handle proceed to payment - SHOWS QR CODE"""
    try:
        selected_amounts = context.user_data.get("selected_amounts", [])
        
        if not selected_amounts:
            await query.edit_message_text(
                "❌ Please select at least one amount.",
                reply_markup=get_deposit_amount_keyboard()
            )
            return DEPOSIT_SELECT_AMOUNT
        
        total_amount = sum(selected_amounts)
        context.user_data["deposit_amount"] = total_amount
        
        # Store deposit info in user data
        context.user_data["deposit_info"] = {
            "amount": total_amount,
            "user_id": user["telegram_id"],
            "username": user.get("username", "N/A"),
            "user_uid": user.get("uid", "N/A")
        }
        
        # Show QR code image
        try:
            with open("qr.jpg", "rb") as qr_file:
                await query.message.reply_photo(
                    photo=qr_file,
                    caption=f"💳 *Pay ₹{total_amount}*\n\nClick '✅ Payment Done' after payment",
                    reply_markup=get_payment_done_keyboard(),
                    parse_mode='Markdown'
                )
        except FileNotFoundError:
            # Fallback if QR file is missing
            payment_text = (
                f"💳 *Pay ₹{total_amount}*\n\n"
                f"1. Pay ₹{total_amount} to merchant\n"
                f"2. Click '✅ Payment Done' after payment\n\n"
                f"⚠️ Do not click until payment is completed!"
            )
            await query.edit_message_text(
                payment_text,
                reply_markup=get_payment_done_keyboard(),
                parse_mode='Markdown'
            )
        
        return DEPOSIT_WAIT_PAYMENT
    except Exception as e:
        logger.error(f"Error in handle_proceed: {e}")
        await query.edit_message_text(
            "❌ Error processing payment. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_payment_done(query, context):
    """Handle payment done button"""
    try:
        await query.edit_message_text(
            "📤 *Send Payment Proof*\n\n"
            "Please send:\n"
            "1. Payment screenshot\n"
            "2. UTR number (12-18 digits)\n\n"
            "Send screenshot first, then UTR in next message.",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        
        return DEPOSIT_WAIT_SCREENSHOT
    except Exception as e:
        logger.error(f"Error in handle_payment_done: {e}")
        await query.edit_message_text(
            "❌ Error. Please start over.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_cancel(query, context):
    """Handle cancel action"""
    try:
        # Clear user data
        context.user_data.clear()
        
        await query.edit_message_text(
            "Operation cancelled. What would you like to do?",
            reply_markup=get_main_menu_keyboard()
        )
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in handle_cancel: {e}")
        return MAIN_MENU

# ================= MESSAGE HANDLERS =================
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot"""
    try:
        if update.message.photo:
            # Store the file_id of the largest photo
            context.user_data["screenshot_id"] = update.message.photo[-1].file_id
            
            await update.message.reply_text(
                "✅ Screenshot received!\n\n"
                "📝 *Now send UTR Number*\n\n"
                "Please enter the UTR number (12-18 digits):",
                reply_markup=get_cancel_keyboard(),
                parse_mode='Markdown'
            )
            
            return DEPOSIT_WAIT_UTR
        
        await update.message.reply_text(
            "❌ Please send a valid screenshot image.",
            reply_markup=get_cancel_keyboard()
        )
        return DEPOSIT_WAIT_SCREENSHOT
    except Exception as e:
        logger.error(f"Error in handle_screenshot: {e}")
        await update.message.reply_text(
            "❌ Error processing screenshot. Please try again.",
            reply_markup=get_cancel_keyboard()
        )
        return DEPOSIT_WAIT_SCREENSHOT

async def handle_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UTR input"""
    try:
        utr = update.message.text.strip()
        
        # Validate UTR
        if not re.match(UTR_REGEX, utr):
            await update.message.reply_text(
                "❌ Invalid UTR format.\n"
                "Please enter 12-18 digits only.\n\n"
                "Example: 123456789012",
                reply_markup=get_cancel_keyboard()
            )
            return DEPOSIT_WAIT_UTR
        
        # Check if UTR already exists
        if deposits_col is not None:
            existing = deposits_col.find_one({"utr": utr})
            if existing:
                await update.message.reply_text(
                    "❌ This UTR has already been submitted.\n"
                    "Please contact admin if this is an error.",
                    reply_markup=get_main_menu_keyboard()
                )
                return MAIN_MENU
        
        # Get deposit info
        deposit_info = context.user_data.get("deposit_info", {})
        amount = context.user_data.get("deposit_amount", 0)
        
        if not deposit_info or amount <= 0:
            await update.message.reply_text(
                "❌ Session expired. Please start over.",
                reply_markup=get_main_menu_keyboard()
            )
            return MAIN_MENU
        
        # Create deposit record
        deposit_id = str(uuid.uuid4())[:8]
        deposit_record = {
            "deposit_id": deposit_id,
            "user_id": deposit_info["user_id"],
            "user_uid": deposit_info["user_uid"],
            "username": deposit_info.get("username"),
            "amount": amount,
            "utr": utr,
            "screenshot_id": context.user_data.get("screenshot_id"),
            "status": "REQUESTED",
            "created_at": get_current_utc_time(),
            "updated_at": get_current_utc_time(),
            "last_reminder": get_current_utc_time(),
            "admin_msg_id": None
        }
        
        # Insert deposit record
        if deposits_col is not None:
            deposits_col.insert_one(deposit_record)
        
        # Send screenshot to admin group
        screenshot_id = context.user_data.get("screenshot_id", "")
        try:
            if screenshot_id:
                await context.bot.send_photo(
                    DEPOSIT_REQUESTS_GROUP_ID,
                    photo=screenshot_id,
                    caption=f"🟡 NEW DEPOSIT\n\nUser: {deposit_info['user_id']}\nAmount: ₹{amount}\nUTR: {utr}\n\nReply: CONFIRM {utr}"
                )
            else:
                await context.bot.send_message(
                    DEPOSIT_REQUESTS_GROUP_ID,
                    f"🟡 NEW DEPOSIT\n\nUser: {deposit_info['user_id']}\nAmount: ₹{amount}\nUTR: {utr}\n\nReply: CONFIRM {utr}"
                )
        except Exception as e:
            logger.error(f"Error sending to admin group: {e}")
            # Try to send to admin directly
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"DEPOSIT REQUEST (Group send failed):\nUser: {deposit_info['user_id']}\nAmount: ₹{amount}\nUTR: {utr}\n\nReply: CONFIRM {utr}"
                )
            except:
                pass
        
        # Notify user
        await update.message.reply_text(
            f"✅ *Deposit Submitted Successfully!*\n\n"
            f"Amount: ₹{amount}\n"
            f"UTR: `{utr}`\n"
            f"Status: Awaiting admin confirmation\n\n"
            f"You will be notified once approved.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        # Clear user data
        context.user_data.clear()
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in handle_utr: {e}")
        await update.message.reply_text(
            "❌ Error submitting deposit. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI ID input for withdrawal"""
    try:
        upi_id = update.message.text.strip().lower()
        
        # Basic UPI validation
        if not "@" in upi_id or len(upi_id) < 5:
            await update.message.reply_text(
                "❌ Invalid UPI ID format.\n"
                "Please enter a valid UPI ID (e.g., username@upi):",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_UPI
        
        context.user_data["withdraw_data"]["upi_id"] = upi_id
        
        user = get_or_create_user(update.effective_user.id)
        max_amount = user.get("balance", 0)
        
        await update.message.reply_text(
            f"💸 *Enter Withdrawal Amount*\n\n"
            f"Your balance: ₹{max_amount:.2f}\n"
            f"UPI ID: {upi_id}\n\n"
            f"Enter amount (max ₹{max_amount:.2f}):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        
        return WITHDRAW_ENTER_AMOUNT
    except Exception as e:
        logger.error(f"Error in handle_upi_id: {e}")
        await update.message.reply_text(
            "❌ Error processing UPI ID. Please try again.",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_UPI

async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal amount input"""
    try:
        amount = float(update.message.text.strip())
        
        # Get user balance
        user = get_or_create_user(update.effective_user.id)
        balance = user.get("balance", 0)
        
        # Validate amount
        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be greater than 0.",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        if amount > balance:
            await update.message.reply_text(
                f"❌ Insufficient balance.\n"
                f"Your balance: ₹{balance:.2f}\n"
                f"Enter amount (max ₹{balance:.2f}):",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        # Minimum withdrawal check
        if amount < 10:
            await update.message.reply_text(
                "❌ Minimum withdrawal amount is ₹10.",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        context.user_data["withdraw_data"]["amount"] = amount
        
        # Ask for confirmation
        upi_id = context.user_data["withdraw_data"]["upi_id"]
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_withdraw"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]
        ]
        
        await update.message.reply_text(
            f"💸 *Confirm Withdrawal*\n\n"
            f"UPI ID: `{upi_id}`\n"
            f"Amount: ₹{amount:.2f}\n"
            f"Fee: ₹0.00\n"
            f"Total: ₹{amount:.2f}\n\n"
            f"Are you sure?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
        return WITHDRAW_CONFIRM
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Please enter numbers only.",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_AMOUNT
    except Exception as e:
        logger.error(f"Error in handle_withdraw_amount: {e}")
        await update.message.reply_text(
            "❌ Error processing amount. Please try again.",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_AMOUNT

async def handle_confirm_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal confirmation"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user = get_or_create_user(user_id, query.from_user.username)
        
        withdraw_data = context.user_data.get("withdraw_data", {})
        upi_id = withdraw_data.get("upi_id")
        amount = withdraw_data.get("amount")
        
        if not upi_id or not amount:
            await query.edit_message_text(
                "❌ Withdrawal data missing. Please start over.",
                reply_markup=get_main_menu_keyboard()
            )
            return MAIN_MENU
        
        # Create withdrawal record
        withdrawal_id = str(uuid.uuid4())[:8]
        withdrawal_record = {
            "withdrawal_id": withdrawal_id,
            "user_id": user_id,
            "user_uid": user.get("uid"),
            "username": user.get("username"),
            "upi_id": upi_id,
            "amount": amount,
            "status": "REQUESTED",
            "created_at": get_current_utc_time(),
            "updated_at": get_current_utc_time(),
            "admin_msg_id": None
        }
        
        # Insert withdrawal record
        if withdrawals_col is not None:
            withdrawals_col.insert_one(withdrawal_record)
        
        # Send notification to admin group
        admin_text = (
            f"🟡 *NEW WITHDRAWAL REQUEST*\n\n"
            f"• Withdrawal ID: `{withdrawal_id}`\n"
            f"• User: {user_id}\n"
            f"• UID: `{user.get('uid')}`\n"
            f"• Amount: ₹{amount:.2f}\n"
            f"• UPI ID: `{upi_id}`\n\n"
            f"To mark as done, reply:\n"
            f"`DONE {withdrawal_id}`"
        )
        
        # Try to send to withdraw group, fallback to admin
        try:
            await context.bot.send_message(
                WITHDRAW_REQUESTS_GROUP_ID,
                admin_text,
                parse_mode='Markdown'
            )
        except:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    admin_text,
                    parse_mode='Markdown'
                )
            except:
                pass
        
        # Notify user
        await query.edit_message_text(
            f"✅ *Withdrawal Request Submitted!*\n\n"
            f"Amount: ₹{amount:.2f}\n"
            f"UPI ID: `{upi_id}`\n"
            f"Status: Awaiting admin approval\n\n"
            f"You will be notified once processed.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        # Clear user data
        context.user_data.clear()
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in handle_confirm_withdraw: {e}")
        await update.callback_query.edit_message_text(
            "❌ Error submitting withdrawal. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

# ================= ADMIN HANDLER =================
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin commands - FIXED CONFIRM LOGIC"""
    try:
        if update.effective_user.id != ADMIN_ID:
            return
        
        text = update.message.text.strip().upper()
        
        # Handle deposit confirmation
        if text.startswith("CONFIRM"):
            # Extract UTR from message
            parts = text.split()
            if len(parts) >= 2:
                utr = parts[1]  # "CONFIRM 123456"
            elif len(text) > 7:
                utr = text[7:].strip()  # "CONFIRM123456"
            else:
                await update.message.reply_text("❌ Format: CONFIRM UTR")
                return
            
            # Find deposit
            deposit = None
            if deposits_col is not None:
                deposit = deposits_col.find_one({"utr": utr, "status": {"$in": ["REQUESTED", "PENDING"]}})
            
            if not deposit:
                await update.message.reply_text(f"❌ Deposit not found or already processed.")
                return
            
            # Delete message from current group
            try:
                if deposit.get("status") == "REQUESTED":
                    await context.bot.delete_message(
                        DEPOSIT_REQUESTS_GROUP_ID,
                        deposit.get("admin_msg_id")
                    )
                elif deposit.get("status") == "PENDING":
                    await context.bot.delete_message(
                        DEPOSIT_PENDING_GROUP_ID,
                        deposit.get("admin_msg_id")
                    )
            except Exception as e:
                logger.warning(f"Could not delete message: {e}")
            
            # Update deposit status
            if deposits_col is not None:
                deposits_col.update_one(
                    {"utr": utr},
                    {"$set": {"status": "COMPLETED", "updated_at": get_current_utc_time()}}
                )
            
            # Update user balance
            update_user_balance(deposit["user_id"], deposit["amount"], is_deposit=True)
            
            # Send to completed group
            completed_text = (
                f"✅ *DEPOSIT COMPLETED*\n\n"
                f"• User: {deposit['user_id']}\n"
                f"• Amount: ₹{deposit['amount']}\n"
                f"• UTR: `{utr}`\n"
                f"• Time: {get_current_utc_time().strftime('%H:%M:%S')}"
            )
            
            await context.bot.send_message(
                DEPOSIT_COMPLETED_GROUP_ID,
                completed_text,
                parse_mode='Markdown'
            )
            
            # Notify user
            try:
                await context.bot.send_message(
                    deposit["user_id"],
                    f"✅ Deposit of ₹{deposit['amount']} has been confirmed!\n"
                    f"Your balance has been updated.",
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Could not notify user: {e}")
            
            await update.message.reply_text(f"✅ Deposit confirmed for UTR: {utr}")
        
        # Handle withdrawal completion
        elif text.startswith("DONE"):
            # Extract withdrawal ID
            parts = text.split()
            if len(parts) >= 2:
                withdrawal_id = parts[1]
            elif len(text) > 4:
                withdrawal_id = text[4:].strip()
            else:
                await update.message.reply_text("❌ Format: DONE WITHDRAWAL_ID")
                return
            
            # Find withdrawal
            withdrawal = None
            if withdrawals_col is not None:
                withdrawal = withdrawals_col.find_one({
                    "withdrawal_id": withdrawal_id,
                    "status": "REQUESTED"
                })
            
            if not withdrawal:
                await update.message.reply_text(f"❌ Withdrawal not found or already processed.")
                return
            
            # Delete from requests group
            try:
                await context.bot.delete_message(
                    WITHDRAW_REQUESTS_GROUP_ID,
                    withdrawal.get("admin_msg_id")
                )
            except Exception as e:
                logger.warning(f"Could not delete message: {e}")
            
            # Update withdrawal status
            if withdrawals_col is not None:
                withdrawals_col.update_one(
                    {"withdrawal_id": withdrawal_id},
                    {"$set": {"status": "COMPLETED", "updated_at": get_current_utc_time()}}
                )
            
            # Update user balance (deduct)
            update_user_balance(withdrawal["user_id"], -withdrawal["amount"], is_deposit=False)
            
            # Send to completed group
            completed_text = (
                f"✅ *WITHDRAWAL COMPLETED*\n\n"
                f"• User: {withdrawal['user_id']}\n"
                f"• Amount: ₹{withdrawal['amount']:.2f}\n"
                f"• UPI ID: `{withdrawal['upi_id']}`\n"
                f"• Time: {get_current_utc_time().strftime('%H:%M:%S')}"
            )
            
            await context.bot.send_message(
                WITHDRAW_COMPLETED_GROUP_ID,
                completed_text,
                parse_mode='Markdown'
            )
            
            # Notify user
            try:
                await context.bot.send_message(
                    withdrawal["user_id"],
                    f"✅ Withdrawal of ₹{withdrawal['amount']:.2f} has been processed!\n"
                    f"Amount sent to: {withdrawal['upi_id']}\n"
                    f"Your balance has been updated.",
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Could not notify user: {e}")
            
            await update.message.reply_text(f"✅ Withdrawal processed for ID: {withdrawal_id}")
    except Exception as e:
        logger.error(f"Error in admin handler: {e}")
        try:
            await update.message.reply_text(f"❌ Admin command error: {str(e)[:50]}")
        except:
            pass

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unexpected messages"""
    try:
        await update.message.reply_text(
            "Please use the menu buttons to navigate.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in handle_unknown_message: {e}")
        return MAIN_MENU

# ================= BACKGROUND TASKS =================
async def deposit_watcher(application):
    """Background task to monitor deposits"""
    logger.info("Deposit watcher started")
    
    while True:
        try:
            # Proper None check for MongoDB collections
            if deposits_col is None:
                await asyncio.sleep(30)
                continue
            
            now = get_current_utc_time()
            
            # Check REQUESTED deposits (move to PENDING after timeout)
            requested_deposits = deposits_col.find({"status": "REQUESTED"})
            
            for deposit in requested_deposits:
                time_diff = (now - deposit["created_at"]).total_seconds()
                
                if time_diff > REQUEST_TIMEOUT:
                    # Delete from requests group
                    try:
                        await application.bot.delete_message(
                            DEPOSIT_REQUESTS_GROUP_ID,
                            deposit.get("admin_msg_id")
                        )
                    except Exception as e:
                        logger.warning(f"Could not delete request message: {e}")
                    
                    # Send to pending group
                    pending_text = (
                        f"🟠 *DEPOSIT PENDING*\n\n"
                        f"• User: {deposit['user_id']}\n"
                        f"• Amount: ₹{deposit['amount']}\n"
                        f"• UTR: `{deposit['utr']}`\n"
                        f"• Waiting for: {int(time_diff/60)} minutes"
                    )
                    
                    msg = await application.bot.send_message(
                        DEPOSIT_PENDING_GROUP_ID,
                        pending_text,
                        parse_mode='Markdown'
                    )
                    
                    # Update deposit status
                    deposits_col.update_one(
                        {"_id": deposit["_id"]},
                        {"$set": {
                            "status": "PENDING",
                            "admin_msg_id": msg.message_id,
                            "updated_at": now,
                            "last_reminder": now
                        }}
                    )
                    
                    logger.info(f"Moved deposit UTR {deposit['utr']} to PENDING")
            
            # Send reminders for PENDING deposits
            pending_deposits = deposits_col.find({"status": "PENDING"})
            
            for deposit in pending_deposits:
                time_diff = (now - deposit["last_reminder"]).total_seconds()
                
                if time_diff > PENDING_REMINDER:
                    reminder_text = (
                        f"⏰ *REMINDER: Deposit Still Pending*\n\n"
                        f"• UTR: `{deposit['utr']}`\n"
                        f"• Amount: ₹{deposit['amount']}\n"
                        f"• User: {deposit['user_id']}\n"
                        f"• Waiting: {int((now - deposit['created_at']).total_seconds() / 60)} min"
                    )
                    
                    await application.bot.send_message(
                        DEPOSIT_PENDING_GROUP_ID,
                        reminder_text,
                        parse_mode='Markdown'
                    )
                    
                    # Update reminder time
                    deposits_col.update_one(
                        {"_id": deposit["_id"]},
                        {"$set": {"last_reminder": now}}
                    )
                    
                    logger.info(f"Sent reminder for UTR: {deposit['utr']}")
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in deposit_watcher: {e}")
            await asyncio.sleep(10)

# ================= APPLICATION SETUP =================
async def post_init(application):
    """Run after bot is initialized"""
    logger.info("Bot initialized. Starting background tasks...")
    asyncio.create_task(deposit_watcher(application))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors in the bot"""
    logger.error(f"Error: {context.error}")
    
    # Handle conflict error gracefully
    if isinstance(context.error, telegram.error.Conflict):
        logger.error("Conflict error: Another bot instance might be running. Waiting...")
        await asyncio.sleep(10)
        return
    
    try:
        # Try to notify user of error
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again.",
                reply_markup=get_main_menu_keyboard()
            )
    except:
        pass

def main():
    """Main function to start the bot"""
    # Add startup delay to prevent conflict with previous instance
    logger.info("Starting bot with 5-second delay to avoid conflict...")
    time.sleep(5)
    
    try:
        # Create application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Conversation handler for deposit flow
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
                    CallbackQueryHandler(handle_confirm_withdraw, pattern="^confirm_withdraw$"),
                    CallbackQueryHandler(handle_callback_query, pattern="^cancel$"),
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                CommandHandler("help", help_command),
                MessageHandler(filters.ALL, handle_unknown_message)
            ],
            allow_reentry=True,
            per_message=True
        )
        
        # Add handlers
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(
            filters.User(ADMIN_ID) & filters.TEXT & ~filters.COMMAND,
            handle_admin_message
        ))
        
        # Set post initialization
        application.post_init = post_init
        
        # Start polling
        logger.info("Bot starting polling...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,
            pool_timeout=30
        )
        
    except telegram.error.Conflict as e:
        logger.error(f"CONFLICT ERROR during startup: {e}")
        logger.info("Waiting 15 seconds before possible restart...")
        time.sleep(15)
        
    except Exception as e:
        logger.error(f"Fatal error starting bot: {e}")
        logger.info("Bot will exit. Check logs for details.")

if __name__ == "__main__":
    main()
