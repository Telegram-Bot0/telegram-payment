from flask import Flask, jsonify
import threading
import time
import os
import logging

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Payment Bot</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
            .status { color: green; font-size: 24px; }
            .info { margin-top: 20px; }
        </style>
    </head>
    <body>
        <h1>🤖 Telegram Payment Bot</h1>
        <div class="status">✅ Bot is Running</div>
        <div class="info">
            <p>This is a background Telegram bot for payment processing.</p>
            <p>Bot features: Deposit, Withdraw, Balance, Admin Panel</p>
            <p>Health check: <a href="/health">/health</a></p>
        </div>
    </body>
    </html>
    """

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "service": "telegram-payment-bot",
        "timestamp": time.time(),
        "port": os.getenv("PORT", "10000")
    })

# Bot runner in background
def run_bot():
    logger.info("Starting bot in 5 seconds...")
    time.sleep(5)  # Wait for Flask to fully start
    
    try:
        from bot import main
        logger.info("Bot imported successfully, starting main()...")
        main()
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        import traceback
        traceback.print_exc()

# Start bot in background thread
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
