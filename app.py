from flask import Flask, jsonify, render_template_string
import threading
import time
import os
import logging
import asyncio
from queue import Queue
import subprocess
import sys
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Telegram Payment Bot</title>
    <style>
        body { 
            font-family: 'Segoe UI', Arial, sans-serif; 
            text-align: center; 
            padding: 40px; 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
        }
        .container {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            max-width: 800px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }
        h1 { 
            font-size: 42px; 
            margin-bottom: 20px; 
            color: white;
        }
        .status { 
            color: #4ade80; 
            font-size: 28px; 
            font-weight: bold;
            margin: 20px 0;
            padding: 15px;
            background: rgba(74, 222, 128, 0.2);
            border-radius: 10px;
            border: 2px solid #4ade80;
        }
        .info { 
            margin-top: 30px; 
            text-align: left;
            background: rgba(255, 255, 255, 0.15);
            padding: 25px;
            border-radius: 15px;
        }
        .info p {
            font-size: 18px;
            margin: 15px 0;
            line-height: 1.6;
        }
        .features {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }
        .feature {
            background: rgba(255, 255, 255, 0.15);
            padding: 20px;
            border-radius: 15px;
            transition: transform 0.3s;
        }
        .feature:hover {
            transform: translateY(-5px);
            background: rgba(255, 255, 255, 0.25);
        }
        .feature h3 {
            margin-top: 0;
            color: #ffd700;
        }
        .btn {
            display: inline-block;
            background: linear-gradient(45deg, #ff6b6b, #ffa726);
            color: white;
            padding: 12px 30px;
            text-decoration: none;
            border-radius: 50px;
            font-weight: bold;
            margin: 10px;
            transition: all 0.3s;
            border: none;
            font-size: 16px;
        }
        .btn:hover {
            transform: scale(1.05);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.3);
        }
        .bot-status {
            font-size: 20px;
            margin: 20px 0;
            padding: 15px;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.1);
        }
        .online { color: #4ade80; }
        .offline { color: #f87171; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Telegram Payment Bot</h1>
        <div class="status">✅ Bot is Running</div>
        
        <div class="bot-status">
            Status: <span class="online">● ONLINE</span>
        </div>
        
        <div class="features">
            <div class="feature">
                <h3>💰 Deposit</h3>
                <p>Easy payment system with QR code</p>
            </div>
            <div class="feature">
                <h3>💸 Withdraw</h3>
                <p>Fast UPI withdrawals with admin approval</p>
            </div>
            <div class="feature">
                <h3>👤 User Panel</h3>
                <p>Check balance and transaction history</p>
            </div>
            <div class="feature">
                <h3>🛡️ Secure</h3>
                <p>Manual admin verification for all transactions</p>
            </div>
        </div>
        
        <div class="info">
            <p>This is a professional Telegram bot for payment processing with manual verification.</p>
            <p><strong>Bot Features:</strong> Deposit, Withdraw, Balance Management, Admin Panel</p>
            <p><strong>Health Check:</strong> <a href="/health" style="color: #ffd700;">/health</a></p>
            <p><strong>Port:</strong> {{ port }}</p>
            <p><strong>Uptime:</strong> {{ uptime }}</p>
        </div>
        
        <div>
            <a href="/health" class="btn">Health Status</a>
            <a href="https://t.me/{{ bot_username }}" class="btn">Open Telegram Bot</a>
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    """Home page"""
    port = os.getenv("PORT", "10000")
    uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))
    bot_username = "your_bot_username"  # Change this to your bot's username
    
    return render_template_string(HTML_TEMPLATE, 
                                 port=port, 
                                 uptime=uptime,
                                 bot_username=bot_username)

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "telegram-payment-bot",
        "timestamp": time.time(),
        "port": os.getenv("PORT", "10000"),
        "bot_running": bot_process is not None and bot_process.poll() is None,
        "environment": "production",
        "version": "1.0.0"
    })

@app.route('/start-bot', methods=['POST'])
def start_bot():
    """Manually start bot endpoint"""
    global bot_process
    
    if bot_process is None or bot_process.poll() is not None:
        try:
            bot_process = subprocess.Popen(
                [sys.executable, "bot.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            logger.info("Bot started manually")
            return jsonify({"status": "started", "pid": bot_process.pid})
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return jsonify({"status": "already_running", "pid": bot_process.pid})

@app.route('/bot-logs')
def bot_logs():
    """View bot logs"""
    if bot_process and bot_process.stdout:
        try:
            logs = []
            import select
            import fcntl
            import os
            
            # Set non-blocking mode
            fl = fcntl.fcntl(bot_process.stdout, fcntl.F_GETFL)
            fcntl.fcntl(bot_process.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            
            # Try to read
            try:
                while True:
                    line = bot_process.stdout.readline()
                    if line:
                        logs.append(line.strip())
                    else:
                        break
            except:
                pass
            
            return jsonify({"logs": logs[-100:]})  # Last 100 lines
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    return jsonify({"logs": []})

# Global variables
bot_process = None
start_time = time.time()

def run_bot():
    """Run bot in a separate process"""
    global bot_process
    
    logger.info("Waiting 10 seconds for Flask to start...")
    time.sleep(10)
    
    try:
        logger.info("Starting Telegram bot...")
        
        bot_process = subprocess.Popen(
            [sys.executable, "bot.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Log bot output
        def log_output():
            while True:
                output = bot_process.stdout.readline()
                if output:
                    logger.info(f"BOT: {output.strip()}")
                elif bot_process.poll() is not None:
                    break
        
        # Start log thread
        log_thread = threading.Thread(target=log_output, daemon=True)
        log_thread.start()
        
        logger.info(f"Bot started with PID: {bot_process.pid}")
        
        # Wait for bot to finish
        bot_process.wait()
        logger.info(f"Bot process ended with code: {bot_process.returncode}")
        
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
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
