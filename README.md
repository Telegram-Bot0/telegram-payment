# Telegram Payment Bot

A professional, menu-based Telegram bot for manual payment verification with automated tracking and admin workflow.

## Features

- **User-Friendly Menu System** - Simple button-based navigation
- **Deposit Flow** - QR code payment with screenshot and UTR verification
- **Withdrawal System** - UPI-based withdrawals with admin approval
- **Admin Dashboard** - Separate groups for different transaction states
- **Automatic Tracking** - Time-based movement between groups with reminders
- **Balance Management** - Real-time balance updates
- **Safe & Secure** - Manual admin approval for all transactions

## Prerequisites

1. Python 3.8 or higher
2. MongoDB database (local or Atlas)
3. Telegram account with admin rights
4. Telegram Bot Token from @BotFather

## Setup Instructions

### 1. Get Bot Token
1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow instructions
3. Copy the bot token (format: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 2. Get Your Admin ID
1. Search for `@userinfobot` on Telegram
2. Send `/start`
3. Copy your numeric ID (e.g., `123456789`)

### 3. Create Admin Groups
Create **5** separate Telegram supergroups:

1. **Deposit Requests** - New deposits appear here
2. **Deposit Pending** - Deposits waiting >2 minutes
3. **Deposit Completed** - Approved deposits archive
4. **Withdraw Requests** - New withdrawal requests
5. **Withdraw Completed** - Processed withdrawals

For each group:
1. Create as a **Supergroup**
2. Add your bot as **Admin** with all permissions
3. Send `/id` in the group via @RawDataBot
4. Copy the group ID (starts with `-100`)

### 4. Configure Database

#### Option A: MongoDB Atlas (Recommended)
1. Go to [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
2. Create free cluster
3. Get connection string: `mongodb+srv://username:password@cluster.mongodb.net/`

#### Option B: Local MongoDB
1. Install MongoDB
2. Connection string: `mongodb://localhost:27017/`

### 5. Install Dependencies
```bash
pip install -r requirements.txt
