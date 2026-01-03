import asyncio
import logging
import re
import time
import uuid
from datetime import datetime
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

# ================= DATABASE SETUP =================
try:
    client = MongoClient(MONGO_URI)
    db = client.telegram_payment_bot
    users_col = db.users
    deposits_col = db.deposits
    withdrawals_col = db.withdrawals
    
    # Create indexes
    users_col.create_index("telegram_id", unique=True)
    deposits_col.create_index("utr", unique=True)
    deposits_col.create_index("status")
    withdrawals_col.create_index("status")
    logger.info("Database connection established successfully")
except PyMongoError as e:
    logger.error(f"Database connection failed: {e}")
    raise

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

# ================= USER MANAGEMENT =================
def get_or_create_user(telegram_id: int, username: str = None):
    """Get user from DB or create if not exists"""
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
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return user
    except PyMongoError as e:
        logger.error(f"Error getting/creating user {telegram_id}: {e}")
        return None

def update_user_balance(telegram_id: int, amount: float, is_deposit: bool = True):
    """Update user balance after deposit/withdrawal"""
    try:
        update_data = {
            "$inc": {"balance": amount},
            "$set": {"updated_at": datetime.utcnow()}
        }
        
        if is_deposit:
            update_data["$inc"]["total_deposits"] = amount
        else:
            update_data["$inc"]["total_withdrawals"] = amount
            
        users_col.update_one(
            {"telegram_id": telegram_id},
            update_data
        )
        return True
    except PyMongoError as e:
        logger.error(f"Error updating balance for user {telegram_id}: {e}")
        return False

# ================= COMMAND HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
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

# ================= CALLBACK QUERY HANDLERS =================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    # Get user from database
    user = get_or_create_user(user_id, query.from_user.username)
    if not user:
        await query.edit_message_text("❌ Database error. Please try again.")
        return ConversationHandler.END
    
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

async def handle_deposit(query, context):
    """Handle deposit button click"""
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

async def handle_withdraw(query, context, user):
    """Handle withdraw button click"""
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

async def handle_balance(query, context, user):
    """Handle balance check"""
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

async def handle_help(query, context):
    """Handle help from menu"""
    return await help_command(update=Update(update_id=0, callback_query=query), context=context)

async def handle_amount_selection(query, context):
    """Handle amount selection for deposit"""
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

async def handle_proceed(query, context, user):
    """Handle proceed to payment"""
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
    
    # In production, you would use a real QR code image
    # For now, we'll use a placeholder message
    payment_text = (
        f"💳 *Payment Instructions*\n\n"
        f"Amount: ₹{total_amount}\n\n"
        f"1. Pay ₹{total_amount} to the merchant\n"
        f"2. Keep the payment screenshot ready\n"
        f"3. Note down the UTR number (12-18 digits)\n"
        f"4. Click '✅ Payment Done' after payment\n\n"
        f"⚠️ Do not click until payment is completed!"
    )
    
    await query.edit_message_text(
        payment_text,
        reply_markup=get_payment_done_keyboard(),
        parse_mode='Markdown'
    )
    
    return DEPOSIT_WAIT_PAYMENT

async def handle_payment_done(query, context):
    """Handle payment done button"""
    await query.edit_message_text(
        "📸 *Step 1: Send Screenshot*\n\n"
        "Please send the payment confirmation screenshot.\n"
        "Make sure it clearly shows:\n"
        "• Payment amount\n"
        "• Transaction details\n"
        "• Timestamp",
        reply_markup=get_cancel_keyboard(),
        parse_mode='Markdown'
    )
    
    return DEPOSIT_WAIT_SCREENSHOT

async def handle_cancel(query, context):
    """Handle cancel action"""
    # Clear user data
    if "selected_amounts" in context.user_data:
        del context.user_data["selected_amounts"]
    if "deposit_amount" in context.user_data:
        del context.user_data["deposit_amount"]
    if "deposit_info" in context.user_data:
        del context.user_data["deposit_info"]
    if "withdraw_data" in context.user_data:
        del context.user_data["withdraw_data"]
    
    await query.edit_message_text(
        "Operation cancelled. What would you like to do?",
        reply_markup=get_main_menu_keyboard()
    )
    
    return MAIN_MENU

# ================= MESSAGE HANDLERS =================
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot"""
    if update.message.photo:
        # Store the file_id of the largest photo
        context.user_data["screenshot_id"] = update.message.photo[-1].file_id
        
        await update.message.reply_text(
            "✅ Screenshot received!\n\n"
            "📝 *Step 2: Send UTR Number*\n\n"
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

async def handle_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UTR input"""
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
    deposit_id = str(uuid.uuid4())
    deposit_record = {
        "deposit_id": deposit_id,
        "user_id": deposit_info["user_id"],
        "user_uid": deposit_info["user_uid"],
        "username": deposit_info.get("username"),
        "amount": amount,
        "utr": utr,
        "screenshot_id": context.user_data.get("screenshot_id"),
        "status": "REQUESTED",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "last_reminder": datetime.utcnow(),
        "admin_msg_id": None
    }
    
    try:
        # Insert deposit record
        deposits_col.insert_one(deposit_record)
        
        # Send notification to admin group
        admin_text = (
            f"🟡 *NEW DEPOSIT REQUEST*\n\n"
            f"• Deposit ID: `{deposit_id}`\n"
            f"• User: {deposit_info['user_id']}\n"
            f"• UID: `{deposit_info['user_uid']}`\n"
            f"• Amount: ₹{amount}\n"
            f"• UTR: `{utr}`\n\n"
            f"To confirm, reply:\n"
            f"`CONFIRM {utr}`"
        )
        
        msg = await context.bot.send_message(
            DEPOSIT_REQUESTS_GROUP_ID,
            admin_text,
            parse_mode='Markdown'
        )
        
        # Update with message ID
        deposits_col.update_one(
            {"deposit_id": deposit_id},
            {"$set": {"admin_msg_id": msg.message_id}}
        )
        
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
        
    except PyMongoError as e:
        logger.error(f"Error creating deposit: {e}")
        await update.message.reply_text(
            "❌ Database error. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
    
    return MAIN_MENU

async def handle_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI ID input for withdrawal"""
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

async def handle_confirm_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal confirmation"""
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
    withdrawal_id = str(uuid.uuid4())
    withdrawal_record = {
        "withdrawal_id": withdrawal_id,
        "user_id": user_id,
        "user_uid": user.get("uid"),
        "username": user.get("username"),
        "upi_id": upi_id,
        "amount": amount,
        "status": "REQUESTED",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "admin_msg_id": None
    }
    
    try:
        # Insert withdrawal record
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
        
        msg = await context.bot.send_message(
            WITHDRAW_REQUESTS_GROUP_ID,
            admin_text,
            parse_mode='Markdown'
        )
        
        # Update with message ID
        withdrawals_col.update_one(
            {"withdrawal_id": withdrawal_id},
            {"$set": {"admin_msg_id": msg.message_id}}
        )
        
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
        
    except PyMongoError as e:
        logger.error(f"Error creating withdrawal: {e}")
        await query.edit_message_text(
            "❌ Database error. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )
    
    return MAIN_MENU

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin commands"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = update.message.text.strip().upper()
    
    # Handle deposit confirmation
    if text.startswith("CONFIRM "):
        utr = text.split()[1]
        
        # Find deposit
        deposit = deposits_col.find_one({"utr": utr, "status": {"$in": ["REQUESTED", "PENDING"]}})
        if not deposit:
            await update.message.reply_text(f"❌ Deposit not found or already processed.")
            return
        
        # Delete message from current group
        try:
            if deposit["status"] == "REQUESTED":
                await context.bot.delete_message(
                    DEPOSIT_REQUESTS_GROUP_ID,
                    deposit.get("admin_msg_id")
                )
            elif deposit["status"] == "PENDING":
                await context.bot.delete_message(
                    DEPOSIT_PENDING_GROUP_ID,
                    deposit.get("admin_msg_id")
                )
        except Exception as e:
            logger.warning(f"Could not delete message: {e}")
        
        # Update deposit status
        deposits_col.update_one(
            {"utr": utr},
            {"$set": {"status": "COMPLETED", "updated_at": datetime.utcnow()}}
        )
        
        # Update user balance
        update_user_balance(deposit["user_id"], deposit["amount"], is_deposit=True)
        
        # Send to completed group
        completed_text = (
            f"✅ *DEPOSIT COMPLETED*\n\n"
            f"• Deposit ID: `{deposit.get('deposit_id')}`\n"
            f"• User: {deposit['user_id']}\n"
            f"• UID: `{deposit.get('user_uid')}`\n"
            f"• Amount: ₹{deposit['amount']}\n"
            f"• UTR: `{utr}`\n"
            f"• Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
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
    elif text.startswith("DONE "):
        withdrawal_id = text.split()[1]
        
        # Find withdrawal
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
        withdrawals_col.update_one(
            {"withdrawal_id": withdrawal_id},
            {"$set": {"status": "COMPLETED", "updated_at": datetime.utcnow()}}
        )
        
        # Update user balance (deduct)
        update_user_balance(withdrawal["user_id"], -withdrawal["amount"], is_deposit=False)
        
        # Send to completed group
        completed_text = (
            f"✅ *WITHDRAWAL COMPLETED*\n\n"
            f"• Withdrawal ID: `{withdrawal_id}`\n"
            f"• User: {withdrawal['user_id']}\n"
            f"• UID: `{withdrawal.get('user_uid')}`\n"
            f"• Amount: ₹{withdrawal['amount']:.2f}\n"
            f"• UPI ID: `{withdrawal['upi_id']}`\n"
            f"• Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
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

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unexpected messages"""
    await update.message.reply_text(
        "Please use the menu buttons to navigate.",
        reply_markup=get_main_menu_keyboard()
    )
    return MAIN_MENU

# ================= BACKGROUND TASKS =================
async def deposit_watcher(application):
    """Background task to monitor deposits"""
    logger.info("Deposit watcher started")
    
    while True:
        try:
            now = datetime.utcnow()
            
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
                        f"• Deposit ID: `{deposit.get('deposit_id')}`\n"
                        f"• User: {deposit['user_id']}\n"
                        f"• UID: `{deposit.get('user_uid')}`\n"
                        f"• Amount: ₹{deposit['amount']}\n"
                        f"• UTR: `{deposit['utr']}`\n"
                        f"• Pending since: {deposit['created_at'].strftime('%H:%M:%S')}"
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
                    
                    logger.info(f"Moved deposit {deposit.get('deposit_id')} to PENDING")
            
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
                        f"• Waiting for: {int((now - deposit['created_at']).total_seconds() / 60)} minutes"
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

def main():
    """Main function to start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
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
        allow_reentry=True
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
    logger.info("Starting bot...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
