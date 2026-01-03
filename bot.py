import os
import asyncio
import logging
import re
import time
import uuid
import signal
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
import telegram.error

# ================= CONFIG FROM ENVIRONMENT =================
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
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ================= HEALTH CHECK =================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Disable default logging
        pass

def start_health_server():
    """Start a simple HTTP server for health checks"""
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    server.serve_forever()

# ================= GRACEFUL SHUTDOWN =================
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ================= DATABASE =================
class Database:
    """Database connection manager with retry logic"""
    
    def __init__(self):
        self.client: Optional[MongoClient] = None
        self.users_col = None
        self.deposits_col = None
        self.withdrawals_col = None
        self.connected = False
        
    def connect(self, max_retries: int = 3):
        """Connect to MongoDB with retry logic"""
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to MongoDB (attempt {attempt + 1}/{max_retries})...")
                
                self.client = MongoClient(
                    MONGO_URI,
                    serverSelectionTimeoutMS=10000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=10000,
                    maxPoolSize=10,
                    retryWrites=True,
                    retryReads=True
                )
                
                # Test connection
                self.client.admin.command('ping')
                
                db = self.client.telegram_payment_bot
                self.users_col = db.users
                self.deposits_col = db.deposits
                self.withdrawals_col = db.withdrawals
                
                # Create indexes
                self.users_col.create_index("telegram_id", unique=True)
                self.deposits_col.create_index("utr", unique=True)
                self.deposits_col.create_index("status")
                self.deposits_col.create_index("created_at")
                self.withdrawals_col.create_index("status")
                
                self.connected = True
                logger.info("✅ MongoDB connected successfully")
                return True
                
            except Exception as e:
                logger.warning(f"❌ MongoDB connection failed (attempt {attempt + 1}): {str(e)[:100]}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    logger.error("⚠️ MongoDB connection failed. Running in fallback mode.")
                    self.connected = False
                    return False
    
    def disconnect(self):
        """Disconnect from MongoDB"""
        if self.client:
            try:
                self.client.close()
                logger.info("MongoDB connection closed")
            except:
                pass
    
    def is_connected(self):
        """Check if database is connected"""
        return self.connected and self.client is not None

db = Database()

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

# ================= UTILITIES =================
def get_current_utc_time():
    """Get current UTC time"""
    return datetime.now(timezone.utc)

def format_time(dt: datetime) -> str:
    """Format datetime to readable string"""
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

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
    """Deposit amount selection keyboard"""
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
    """Payment done keyboard"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Payment Done", callback_data="payment_done")]])

def get_cancel_keyboard():
    """Cancel keyboard"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def get_confirm_cancel_keyboard():
    """Confirm and Cancel keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_withdraw")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])

# ================= USER MANAGEMENT =================
def get_or_create_user(telegram_id: int, username: str = None) -> Dict:
    """Get or create user"""
    if not db.is_connected():
        # Fallback mode
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
        user = db.users_col.find_one_and_update(
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
        logger.error(f"Error getting user {telegram_id}: {e}")
        return {
            "telegram_id": telegram_id,
            "username": username,
            "uid": str(uuid.uuid4())[:8],
            "balance": 0.0,
            "total_deposits": 0.0,
            "total_withdrawals": 0.0,
            "created_at": get_current_utc_time()
        }

def update_user_balance(telegram_id: int, amount: float, is_deposit: bool = True) -> bool:
    """Update user balance"""
    if not db.is_connected():
        logger.warning("Cannot update balance: Database not connected")
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
        
        result = db.users_col.update_one(
            {"telegram_id": telegram_id},
            update_data
        )
        
        if result.modified_count > 0:
            logger.info(f"Updated balance for user {telegram_id}: {'+' if is_deposit else '-'}{amount}")
            return True
        else:
            logger.warning(f"User {telegram_id} not found for balance update")
            return False
            
    except Exception as e:
        logger.error(f"Error updating balance for user {telegram_id}: {e}")
        return False

# ================= COMMAND HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        user = update.effective_user
        logger.info(f"User {user.id} ({user.username}) started bot")
        
        get_or_create_user(user.id, user.username)
        
        welcome_text = (
            "🤖 *Payment Bot*\n\n"
            "Welcome! Use the menu below:\n\n"
            "💰 *Deposit* - Add funds to your account\n"
            "💸 *Withdraw* - Withdraw funds to UPI\n"
            "📊 *Balance* - Check your balance\n"
            "ℹ️ *Help* - Get assistance"
        )
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text(
            "❌ Error starting bot. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "📚 *Help Guide*\n\n"
        "• *Deposit:*\n"
        "  1. Select Deposit → Choose amount(s)\n"
        "  2. View QR code and pay\n"
        "  3. Click 'Payment Done'\n"
        "  4. Send screenshot + UTR (12-18 digits)\n\n"
        
        "• *Withdraw:*\n"
        "  1. Select Withdraw\n"
        "  2. Enter UPI ID\n"
        "  3. Enter amount\n"
        "  4. Submit for approval\n\n"
        
        "• *Admin confirms:* CONFIRM <UTR>\n"
        "• *Admin completes withdrawal:* DONE <ID>\n\n"
        
        "⚠️ *Important:*\n"
        "• Keep payment proof\n"
        "• UTR must be 12-18 digits\n"
        "• Admin approval required"
    )
    
    try:
        if update.message:
            await update.message.reply_text(help_text, parse_mode='Markdown')
        else:
            await update.callback_query.edit_message_text(
                help_text,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Error in help: {e}")
    
    return MAIN_MENU

# ================= CALLBACK HANDLERS =================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries"""
    try:
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        logger.debug(f"Callback from {user_id}: {data}")
        
        user = get_or_create_user(user_id, query.from_user.username)
        
        # Route to appropriate handler
        handlers = {
            "deposit": handle_deposit_start,
            "withdraw": handle_withdraw_start,
            "balance": handle_balance_check,
            "help": handle_help_menu,
            "proceed": handle_proceed_payment,
            "payment_done": handle_payment_done,
            "cancel": handle_cancel_action,
            "confirm_withdraw": handle_confirm_withdrawal
        }
        
        if data.startswith("amount_"):
            return await handle_amount_selection(query, context)
        elif data in handlers:
            return await handlers[data](query, context, user)
        else:
            logger.warning(f"Unknown callback: {data}")
            return MAIN_MENU
            
    except Exception as e:
        logger.error(f"Error in callback handler: {e}")
        return MAIN_MENU

async def handle_deposit_start(query, context, user):
    """Start deposit process"""
    context.user_data["selected_amounts"] = []
    
    text = (
        "💰 *Deposit Funds*\n\n"
        "Select amount(s):\n"
        "Total selected: ₹0\n\n"
        "Click amounts to select/deselect\n"
        "Then click ✅ Proceed"
    )
    
    await query.edit_message_text(
        text,
        reply_markup=get_deposit_amount_keyboard(),
        parse_mode='Markdown'
    )
    return DEPOSIT_SELECT_AMOUNT

async def handle_withdraw_start(query, context, user):
    """Start withdrawal process"""
    balance = user.get("balance", 0)
    
    if balance <= 0:
        await query.edit_message_text(
            "❌ Insufficient balance for withdrawal.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    await query.edit_message_text(
        "💸 *Withdraw Funds*\n\n"
        "Enter your UPI ID (e.g., username@upi):",
        reply_markup=get_cancel_keyboard(),
        parse_mode='Markdown'
    )
    
    context.user_data["withdraw_data"] = {}
    return WITHDRAW_ENTER_UPI

async def handle_balance_check(query, context, user):
    """Check balance"""
    balance_text = (
        f"📊 *Account Balance*\n\n"
        f"• Available: ₹{user.get('balance', 0):.2f}\n"
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

async def handle_help_menu(query, context, user):
    """Show help from menu"""
    return await help_command(update=Update(update_id=0, callback_query=query), context=context)

async def handle_amount_selection(query, context):
    """Handle amount selection"""
    amount = int(query.data.split("_")[1])
    selected_amounts = context.user_data.get("selected_amounts", [])
    
    if amount in selected_amounts:
        selected_amounts.remove(amount)
    else:
        selected_amounts.append(amount)
    
    context.user_data["selected_amounts"] = selected_amounts
    total = sum(selected_amounts)
    
    text = f"💰 *Deposit Funds*\n\nSelect amount(s):\nTotal selected: ₹{total}\n\nClick amounts then ✅ Proceed"
    
    await query.edit_message_text(
        text,
        reply_markup=get_deposit_amount_keyboard(selected_amounts),
        parse_mode='Markdown'
    )
    return DEPOSIT_SELECT_AMOUNT

async def handle_proceed_payment(query, context, user):
    """Proceed to payment"""
    selected_amounts = context.user_data.get("selected_amounts", [])
    
    if not selected_amounts:
        await query.edit_message_text(
            "❌ Select at least one amount.",
            reply_markup=get_deposit_amount_keyboard()
        )
        return DEPOSIT_SELECT_AMOUNT
    
    total_amount = sum(selected_amounts)
    context.user_data["deposit_amount"] = total_amount
    context.user_data["deposit_info"] = {
        "user_id": user["telegram_id"],
        "user_uid": user.get("uid"),
        "username": user.get("username")
    }
    
    # Show QR code
    try:
        with open("qr.jpg", "rb") as qr_file:
            await query.message.reply_photo(
                photo=qr_file,
                caption=f"💳 *Pay ₹{total_amount}*\n\nClick '✅ Payment Done' after payment",
                reply_markup=get_payment_done_keyboard(),
                parse_mode='Markdown'
            )
    except FileNotFoundError:
        await query.edit_message_text(
            f"💳 *Pay ₹{total_amount}*\n\nClick '✅ Payment Done' after payment",
            reply_markup=get_payment_done_keyboard(),
            parse_mode='Markdown'
        )
    
    return DEPOSIT_WAIT_PAYMENT

async def handle_payment_done(query, context, user):
    """Payment done clicked"""
    await query.edit_message_text(
        "📤 *Send Payment Proof*\n\n"
        "Please send:\n"
        "1. Payment screenshot\n"
        "2. UTR number (12-18 digits)\n\n"
        "Send screenshot first, then UTR.",
        reply_markup=get_cancel_keyboard(),
        parse_mode='Markdown'
    )
    return DEPOSIT_WAIT_SCREENSHOT

async def handle_cancel_action(query, context, user):
    """Cancel action"""
    context.user_data.clear()
    await query.edit_message_text(
        "Operation cancelled.",
        reply_markup=get_main_menu_keyboard()
    )
    return MAIN_MENU

# ================= MESSAGE HANDLERS =================
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle screenshot"""
    if update.message.photo:
        context.user_data["screenshot_id"] = update.message.photo[-1].file_id
        await update.message.reply_text(
            "✅ Screenshot received!\n\nNow send UTR (12-18 digits):",
            reply_markup=get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return DEPOSIT_WAIT_UTR
    
    await update.message.reply_text(
        "❌ Please send a screenshot.",
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
                "❌ Invalid UTR (12-18 digits).\nExample: 123456789012",
                reply_markup=get_cancel_keyboard()
            )
            return DEPOSIT_WAIT_UTR
        
        if "screenshot_id" not in context.user_data:
            await update.message.reply_text(
                "❌ Send screenshot first.",
                reply_markup=get_cancel_keyboard()
            )
            return DEPOSIT_WAIT_UTR
        
        amount = context.user_data.get("deposit_amount", 0)
        deposit_info = context.user_data.get("deposit_info", {})
        screenshot_id = context.user_data["screenshot_id"]
        
        if amount <= 0 or not deposit_info:
            await update.message.reply_text(
                "❌ Session expired. Start over.",
                reply_markup=get_main_menu_keyboard()
            )
            return MAIN_MENU
        
        # Check duplicate UTR
        if db.is_connected():
            existing = db.deposits_col.find_one({"utr": utr})
            if existing:
                await update.message.reply_text(
                    "❌ UTR already submitted.",
                    reply_markup=get_main_menu_keyboard()
                )
                return MAIN_MENU
        
        # Create deposit record
        deposit_data = {
            "deposit_id": str(uuid.uuid4())[:8],
            "user_id": deposit_info["user_id"],
            "user_uid": deposit_info.get("user_uid"),
            "username": deposit_info.get("username"),
            "amount": amount,
            "utr": utr,
            "screenshot_id": screenshot_id,
            "status": "REQUESTED",
            "created_at": get_current_utc_time(),
            "updated_at": get_current_utc_time(),
            "last_reminder": get_current_utc_time(),
            "admin_msg_id": None
        }
        
        # Save to database
        if db.is_connected():
            db.deposits_col.insert_one(deposit_data)
        
        # Send to admin group
        try:
            await context.bot.send_photo(
                DEPOSIT_REQUESTS_GROUP_ID,
                photo=screenshot_id,
                caption=(
                    f"🟡 *NEW DEPOSIT*\n\n"
                    f"• User: {deposit_info['user_id']}\n"
                    f"• UID: `{deposit_info.get('user_uid', 'N/A')}`\n"
                    f"• Amount: ₹{amount}\n"
                    f"• UTR: `{utr}`\n\n"
                    f"Reply: CONFIRM {utr}"
                ),
                parse_mode='Markdown'
            )
        except:
            await context.bot.send_message(
                DEPOSIT_REQUESTS_GROUP_ID,
                f"🟡 NEW DEPOSIT\n\nUser: {deposit_info['user_id']}\nAmount: ₹{amount}\nUTR: {utr}\n\nReply: CONFIRM {utr}"
            )
        
        # Notify user
        await update.message.reply_text(
            f"✅ *Deposit Submitted!*\n\n"
            f"Amount: ₹{amount}\n"
            f"UTR: `{utr}`\n"
            f"Status: Awaiting confirmation\n\n"
            f"You'll be notified when approved.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        
        context.user_data.clear()
        return MAIN_MENU
        
    except Exception as e:
        logger.error(f"Error in UTR handler: {e}")
        await update.message.reply_text(
            "❌ Error submitting deposit. Try again.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

async def handle_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI ID input"""
    upi_id = update.message.text.strip().lower()
    
    if "@" not in upi_id or len(upi_id) < 5:
        await update.message.reply_text(
            "❌ Invalid UPI ID.\nUse: username@upi",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_UPI
    
    context.user_data["withdraw_data"]["upi_id"] = upi_id
    
    user = get_or_create_user(update.effective_user.id)
    balance = user.get("balance", 0)
    
    await update.message.reply_text(
        f"💸 *Enter Amount*\n\n"
        f"Balance: ₹{balance:.2f}\n"
        f"UPI: {upi_id}\n\n"
        f"Enter amount (max ₹{balance:.2f}):",
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
        
        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be > 0.",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        if amount > balance:
            await update.message.reply_text(
                f"❌ Insufficient balance.\nYour balance: ₹{balance:.2f}",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        if amount < 10:
            await update.message.reply_text(
                "❌ Minimum withdrawal: ₹10",
                reply_markup=get_cancel_keyboard()
            )
            return WITHDRAW_ENTER_AMOUNT
        
        context.user_data["withdraw_data"]["amount"] = amount
        upi_id = context.user_data["withdraw_data"]["upi_id"]
        
        await update.message.reply_text(
            f"💸 *Confirm Withdrawal*\n\n"
            f"UPI: `{upi_id}`\n"
            f"Amount: ₹{amount:.2f}\n"
            f"Fee: ₹0.00\n"
            f"Total: ₹{amount:.2f}\n\n"
            f"Confirm?",
            reply_markup=get_confirm_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WITHDRAW_CONFIRM
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Use numbers.",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_AMOUNT
    except Exception as e:
        logger.error(f"Error in withdraw amount: {e}")
        await update.message.reply_text(
            "❌ Error. Try again.",
            reply_markup=get_cancel_keyboard()
        )
        return WITHDRAW_ENTER_AMOUNT

async def handle_confirm_withdrawal(query, context, user):
    """Confirm withdrawal"""
    withdraw_data = context.user_data.get("withdraw_data", {})
    upi_id = withdraw_data.get("upi_id")
    amount = withdraw_data.get("amount")
    
    if not upi_id or not amount:
        await query.edit_message_text(
            "❌ Withdrawal data missing.",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU
    
    withdrawal_id = str(uuid.uuid4())[:8]
    
    # Save withdrawal record
    if db.is_connected():
        withdrawal_data = {
            "withdrawal_id": withdrawal_id,
            "user_id": user["telegram_id"],
            "user_uid": user.get("uid"),
            "username": user.get("username"),
            "upi_id": upi_id,
            "amount": amount,
            "status": "REQUESTED",
            "created_at": get_current_utc_time(),
            "updated_at": get_current_utc_time(),
            "admin_msg_id": None
        }
        db.withdrawals_col.insert_one(withdrawal_data)
    
    # Notify admin
    admin_text = (
        f"🟡 *WITHDRAWAL REQUEST*\n\n"
        f"• ID: `{withdrawal_id}`\n"
        f"• User: {user['telegram_id']}\n"
        f"• UID: `{user.get('uid', 'N/A')}`\n"
        f"• Amount: ₹{amount:.2f}\n"
        f"• UPI: `{upi_id}`\n\n"
        f"Reply: DONE {withdrawal_id}"
    )
    
    try:
        await context.bot.send_message(
            WITHDRAW_REQUESTS_GROUP_ID,
            admin_text,
            parse_mode='Markdown'
        )
    except:
        await context.bot.send_message(ADMIN_ID, admin_text)
    
    await query.edit_message_text(
        f"✅ *Withdrawal Requested!*\n\n"
        f"Amount: ₹{amount:.2f}\n"
        f"UPI: `{upi_id}`\n"
        f"Status: Awaiting approval\n\n"
        f"You'll be notified when processed.",
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
    logger.info(f"Admin command: {text}")
    
    # Deposit confirmation
    if text.startswith("CONFIRM"):
        try:
            parts = text.split()
            utr = parts[1] if len(parts) >= 2 else text[7:].strip()
            
            if not utr:
                await update.message.reply_text("❌ Format: CONFIRM UTR")
                return
            
            # Find deposit
            deposit = None
            if db.is_connected():
                deposit = db.deposits_col.find_one({"utr": utr, "status": {"$in": ["REQUESTED", "PENDING"]}})
            
            if not deposit:
                await update.message.reply_text(f"❌ Deposit not found: {utr}")
                return
            
            # Update status
            if db.is_connected():
                db.deposits_col.update_one(
                    {"utr": utr},
                    {"$set": {"status": "COMPLETED", "updated_at": get_current_utc_time()}}
                )
            
            # Update user balance
            update_user_balance(deposit["user_id"], deposit["amount"], True)
            
            # Send to completed group
            await context.bot.send_message(
                DEPOSIT_COMPLETED_GROUP_ID,
                f"✅ DEPOSIT COMPLETED\n\nUser: {deposit['user_id']}\nAmount: ₹{deposit['amount']}\nUTR: {utr}\nTime: {format_time(get_current_utc_time())}",
                parse_mode='Markdown'
            )
            
            # Notify user
            try:
                await context.bot.send_message(
                    deposit["user_id"],
                    f"✅ Deposit confirmed!\nAmount: ₹{deposit['amount']}\nYour balance has been updated.",
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
            
            await update.message.reply_text(f"✅ Deposit confirmed: {utr}")
            
        except Exception as e:
            logger.error(f"Error confirming deposit: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)[:50]}")
    
    # Withdrawal completion
    elif text.startswith("DONE"):
        try:
            parts = text.split()
            withdrawal_id = parts[1] if len(parts) >= 2 else text[4:].strip()
            
            if not withdrawal_id:
                await update.message.reply_text("❌ Format: DONE WITHDRAWAL_ID")
                return
            
            # Find withdrawal
            withdrawal = None
            if db.is_connected():
                withdrawal = db.withdrawals_col.find_one({
                    "withdrawal_id": withdrawal_id,
                    "status": "REQUESTED"
                })
            
            if not withdrawal:
                await update.message.reply_text(f"❌ Withdrawal not found: {withdrawal_id}")
                return
            
            # Update status
            if db.is_connected():
                db.withdrawals_col.update_one(
                    {"withdrawal_id": withdrawal_id},
                    {"$set": {"status": "COMPLETED", "updated_at": get_current_utc_time()}}
                )
            
            # Update user balance
            update_user_balance(withdrawal["user_id"], -withdrawal["amount"], False)
            
            # Send to completed group
            await context.bot.send_message(
                WITHDRAW_COMPLETED_GROUP_ID,
                f"✅ WITHDRAWAL COMPLETED\n\nUser: {withdrawal['user_id']}\nAmount: ₹{withdrawal['amount']:.2f}\nUPI: {withdrawal['upi_id']}\nTime: {format_time(get_current_utc_time())}",
                parse_mode='Markdown'
            )
            
            # Notify user
            try:
                await context.bot.send_message(
                    withdrawal["user_id"],
                    f"✅ Withdrawal processed!\nAmount: ₹{withdrawal['amount']:.2f}\nSent to: {withdrawal['upi_id']}\nYour balance has been updated.",
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
            
            await update.message.reply_text(f"✅ Withdrawal processed: {withdrawal_id}")
            
        except Exception as e:
            logger.error(f"Error processing withdrawal: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)[:50]}")

# ================= BACKGROUND TASKS =================
async def deposit_watcher(application):
    """Monitor and move deposits"""
    logger.info("Deposit watcher started")
    
    while True:
        try:
            if not db.is_connected():
                await asyncio.sleep(30)
                continue
            
            now = get_current_utc_time()
            
            # Move REQUESTED to PENDING after 2 minutes
            requested = db.deposits_col.find({"status": "REQUESTED"})
            for deposit in requested:
                age = (now - deposit["created_at"]).total_seconds()
                if age > REQUEST_TIMEOUT:
                    # Move to pending group
                    pending_msg = await application.bot.send_message(
                        DEPOSIT_PENDING_GROUP_ID,
                        f"🟠 PENDING\n\nUser: {deposit['user_id']}\nAmount: ₹{deposit['amount']}\nUTR: {deposit['utr']}\nWaiting: {int(age/60)} min"
                    )
                    
                    db.deposits_col.update_one(
                        {"_id": deposit["_id"]},
                        {"$set": {
                            "status": "PENDING",
                            "admin_msg_id": pending_msg.message_id,
                            "updated_at": now,
                            "last_reminder": now
                        }}
                    )
                    logger.info(f"Moved deposit {deposit['utr']} to PENDING")
            
            # Send reminders for PENDING
            pending = db.deposits_col.find({"status": "PENDING"})
            for deposit in pending:
                since_reminder = (now - deposit["last_reminder"]).total_seconds()
                if since_reminder > PENDING_REMINDER:
                    await application.bot.send_message(
                        DEPOSIT_PENDING_GROUP_ID,
                        f"⏰ REMINDER\n\nUTR: {deposit['utr']}\nAmount: ₹{deposit['amount']}\nUser: {deposit['user_id']}\nWaiting: {int((now - deposit['created_at']).total_seconds()/60)} min"
                    )
                    
                    db.deposits_col.update_one(
                        {"_id": deposit["_id"]},
                        {"$set": {"last_reminder": now}}
                    )
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in deposit watcher: {e}")
            await asyncio.sleep(10)

# ================= ERROR HANDLER =================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    error_msg = str(context.error) if context.error else "Unknown error"
    logger.error(f"Bot error: {error_msg}")
    
    # Don't crash on conflict errors
    if isinstance(context.error, telegram.error.Conflict):
        logger.warning("Conflict error detected. Another instance may be running.")
        return
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again.",
                reply_markup=get_main_menu_keyboard()
            )
    except:
        pass

# ================= APPLICATION SETUP =================
async def post_init(application):
    """Run after initialization"""
    logger.info("Bot initialized. Starting background tasks...")
    asyncio.create_task(deposit_watcher(application))

def main():
    """Main function"""
    logger.info("=" * 50)
    logger.info("Starting Telegram Payment Bot")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"Database connected: {db.connect()}")
    logger.info("=" * 50)
    
    # Start health server in background thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    logger.info("Health server started on port 8080")
    
    # Add startup delay to avoid conflict
    time.sleep(3)
    
    try:
        # Create application
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Add error handler
        app.add_error_handler(error_handler)
        
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
            allow_reentry=True,
            per_message=False
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
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
        
    except telegram.error.Conflict as e:
        logger.error(f"CONFLICT ERROR: {e}")
        logger.error("Another bot instance is running. Stopping this instance.")
        db.disconnect()
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        db.disconnect()
        raise

if __name__ == "__main__":
    main()
