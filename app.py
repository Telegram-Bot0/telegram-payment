from flask import Flask, jsonify
import threading
import asyncio
import time

app = Flask(__name__)

@app.route('/')
def home():
    return "Telegram Payment Bot is running!"

@app.route('/health')
def health():
    return jsonify({"status": "ok", "message": "Bot is running"})

# Start bot in background thread
def run_bot():
    time.sleep(3)  # Wait for Flask to start
    from bot import main
    main()

# Start bot thread
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
