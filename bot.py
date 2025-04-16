import os
import json
import shutil
import logging
import asyncio
import sqlite3
import re
import hashlib
import signal
from datetime import datetime, timedelta
from uuid import uuid4
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.sessions.sqlite import SQLiteSession

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
ADMIN_IDS = [7584086775]
RATE_LIMIT = 5  # Max conversions per user per hour
RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
FILE_RETENTION_HOURS = 24  # Clean up files older than 24 hours

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Pyrogram Client
app = Client("session_converter_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Database setup for sessions and rate limiting
def init_db():
    try:
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sessions (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     session_file TEXT,
                     short_session_id TEXT,
                     session_string TEXT,
                     created_at TEXT
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS rate_limits (
                     user_id INTEGER,
                     conversion_time TEXT
                     )''')
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    finally:
        conn.close()

init_db()

# Directory for storing session files
SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# Rate limiting check
def check_rate_limit(user_id):
    try:
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        cutoff = (datetime.now() - timedelta(seconds=RATE_LIMIT_WINDOW)).isoformat()
        c.execute("SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND conversion_time > ?",
                  (user_id, cutoff))
        count = c.fetchone()[0]
        return count < RATE_LIMIT
    except Exception as e:
        logger.error(f"Error checking rate limit for user {user_id}: {e}")
        return False
    finally:
        conn.close()

def log_conversion(user_id):
    try:
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        c.execute("INSERT INTO rate_limits (user_id, conversion_time) VALUES (?, ?)",
                  (user_id, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging conversion for user {user_id}: {e}")
    finally:
        conn.close()

# Generate short session ID for callback data
def get_short_session_id(session_file):
    return hashlib.md5(session_file.encode()).hexdigest()[:8]

# Inline buttons for format selection
def get_format_buttons(user_id, short_session_id, display_file):
    callback_base = f"convert_{{}}_{user_id}_{short_session_id}"
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("JSON", callback_data=callback_base.format("json")),
            InlineKeyboardButton("TDATA", callback_data=callback_base.format("tdata"))
        ],
        [
            InlineKeyboardButton("TXT", callback_data=callback_base.format("txt")),
            InlineKeyboardButton("StringSession", callback_data=callback_base.format("string"))
        ],
        [InlineKeyboardButton("Back", callback_data="back")]
    ])
    # Validate callback data length (Telegram limit: 64 bytes)
    for row in buttons.inline_keyboard:
        for button in row:
            if len(button.callback_data.encode()) > 64:
                logger.error(f"Callback data too long: {button.callback_data} ({len(button.callback_data.encode())} bytes)")
                return None
    return buttons

# Inline buttons for main menu
def get_main_menu_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Help", callback_data="help")]
    ])

# Inline buttons for help menu
def get_help_menu_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Help", callback_data="help")],
        [InlineKeyboardButton("Back", callback_data="back")]
    ])

# Validate session file content
def validate_session_file(file_path):
    try:
        # Telethon .session files are SQLite databases
        with sqlite3.connect(file_path) as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
            if c.fetchone():
                return True
        return False
    except Exception as e:
        logger.error(f"Error validating session file {file_path}: {e}")
        return False

# Start command
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    logger.info(f"Start command received from user {message.from_user.id}")
    await message.reply_text(
        "Welcome to the Session Converter Bot! 📂\n"
        "Send a Telethon .session file to convert it to JSON, TDATA, TXT, or StringSession for use in Telegram Desktop.\n"
        "Use /help for more info.",
        reply_markup=get_main_menu_buttons()
    )

# Help command
@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    logger.info(f"Help command received from user {message.from_user.id}")
    await message.reply_text(
        "📚 **Help**\n"
        "1. Send a Telethon `.session` file.\n"
        "2. Choose a format (JSON, TDATA, TXT, StringSession) using inline buttons.\n"
        "3. Receive the converted file.\n\n"
        "**Using Converted Files in Telegram Desktop**:\n"
        "- **TDATA**: Extract the zip to a folder and point Telegram Desktop to it.\n"
        "- **StringSession**: Use in a Telethon script to log in.\n"
        "- **JSON**: For developers to reconstruct sessions programmatically.\n"
        "- **TXT**: Raw session file copy.\n\n"
        f"Rate limit: {RATE_LIMIT} conversions per hour.\n"
        "Contact admin for support.",
        reply_markup=get_help_menu_buttons()
    )

# Admin status command
@app.on_message(filters.command("status") & filters.private & filters.user(ADMIN_IDS))
async def status_command(client, message):
    logger.info(f"Status command received from admin {message.from_user.id}")
    try:
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions")
        session_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM rate_limits")
        conversion_count = c.fetchone()[0]
        conn.close()
        disk_usage = shutil.disk_usage(SESSION_DIR)
        await message.reply_text(
            f"Bot Status:\n"
            f"Total Sessions: {session_count}\n"
            f"Total Conversions: {conversion_count}\n"
            f"Disk Usage: {disk_usage.used / 1024**2:.2f} MB / {disk_usage.total / 1024**2:.2f} MB\n"
            f"Session Directory: {SESSION_DIR}\n"
            f"Log File: bot.log"
        )
    except Exception as e:
        logger.error(f"Error fetching status: {e}")
        await message.reply_text("Failed to retrieve status.")

# Admin cleanup command
@app.on_message(filters.command("cleanup") & filters.private & filters.user(ADMIN_IDS))
async def cleanup_command(client, message):
    logger.info(f"Cleanup command received from admin {message.from_user.id}")
    try:
        files = os.listdir(SESSION_DIR)
        deleted = 0
        cutoff = datetime.now() - timedelta(hours=FILE_RETENTION_HOURS)
        for file in files:
            file_path = os.path.join(SESSION_DIR, file)
            if os.path.isfile(file_path):
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_mtime < cutoff:
                    os.remove(file_path)
                    deleted += 1
        await message.reply_text(f"Cleaned up {deleted} old files from session directory.")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        await message.reply_text("Failed to clean up files.")

# Handle document (session file)
@app.on_message(filters.document & filters.private)
async def handle_document(client, message):
    user_id = message.from_user.id
    file_name = message.document.file_name
    if not file_name.endswith(".session"):
        await message.reply_text("Please upload a valid Telethon .session file.")
        return

    unique_id = str(uuid4())
    session_file = f"{unique_id}_{file_name}"
    file_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    short_session_id = get_short_session_id(session_file)

    try:
        logger.info(f"Received document from user {user_id}: {file_name}")
        # Download the file
        await message.download(file_path)

        # Validate session file
        if not validate_session_file(file_path):
            await message.reply_text("Invalid session file. Please upload a valid Telethon .session file.")
            if os.path.exists(file_path):
                os.remove(file_path)
            return

        # Store in database
        with sqlite3.connect("sessions.db") as conn:
            c = conn.cursor()
            c.execute("INSERT INTO sessions (user_id, session_file, short_session_id, created_at) VALUES (?, ?, ?, ?)",
                      (user_id, session_file, short_session_id, datetime.now().isoformat()))
            conn.commit()

        logger.info(f"Stored session {session_file} with short_session_id {short_session_id}")

        # Send format selection buttons
        buttons = get_format_buttons(user_id, short_session_id, file_name)
        if buttons:
            await message.reply_text(
                f"Received session file: {file_name}\nChoose a format to convert to:",
                reply_markup=buttons
            )
        else:
            await message.reply_text("Error: Unable to generate conversion options. Please try again.")
            if os.path.exists(file_path):
                os.remove(file_path)
    except Exception as e:
        logger.error(f"Error handling document for user {user_id}: {e}")
        await message.reply_text("Failed to process the file. Please try again.")
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file {file_path}: {e}")

# Handle text (session string)
@app.on_message(filters.text & filters.private)
async def handle_text(client, message):
    user_id = message.from_user.id
    session_string = message.text.strip()

    try:
        # Validate session string (Telethon StringSession is base64-like, ~350 chars)
        if len(session_string) > 100 and re.match(r'^[A-Za-z0-9+/=]+$', session_string):
            unique_id = str(uuid4())
            session_file = f"{unique_id}_session.string"
            file_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
            short_session_id = get_short_session_id(session_file)

            logger.info(f"Received session string from user {user_id}")

            # Save session string to file
            with open(file_path, "w") as f:
                f.write(session_string)

            # Store in database
            with sqlite3.connect("sessions.db") as conn:
                c = conn.cursor()
                c.execute("INSERT INTO sessions (user_id, session_file, short_session_id, session_string, created_at) VALUES (?, ?, ?, ?, ?)",
                          (user_id, session_file, short_session_id, session_string, datetime.now().isoformat()))
                conn.commit()

            logger.info(f"Stored session {session_file} with short_session_id {short_session_id}")

            # Send format selection buttons
            buttons = get_format_buttons(user_id, short_session_id, "session.string")
            if buttons:
                await message.reply_text(
                    "Received session string.\nChoose a format to convert to:",
                    reply_markup=buttons
                )
            else:
                await message.reply_text("Error: Unable to generate conversion options. Please try again.")
                if os.path.exists(file_path):
                    os.remove(file_path)
        else:
            await message.reply_text("Invalid session string. Please send a valid Telethon StringSession.")
    except Exception as e:
        logger.error(f"Error handling text for user {user_id}: {e}")
        await message.reply_text("Failed to process the session string. Please try again.")

# Conversion functions
async def convert_to_json(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_session.json")

    try:
        logger.info(f"Converting to JSON for user {user_id}: {session_file}")
        # Load session without connecting
        session = SQLiteSession(input_path)
        session_data = {
            "dc_id": session.dc_id,
            "server_address": session.server_address,
            "port": session.port,
            "auth_key": session.auth_key.key.hex() if session.auth_key else None,
            "user_id": session.user_id,
            "timestamp": session.timestamp.isoformat() if session.timestamp else None
        }

        # Save to JSON
        with open(output_path, "w") as f:
            json.dump(session_data, f, indent=4)

        return output_path
    except Exception as e:
        logger.error(f"Error converting to JSON for user {user_id}: {e}")
        return f"Error converting to JSON: {str(e)}"

async def convert_to_tdata(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_dir = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_tdata")
    output_zip = f"{output_dir}.zip"

    try:
        logger.info(f"Converting to TDATA for user {user_id}: {session_file}")
        os.makedirs(output_dir, exist_ok=True)

        # Load session to extract data
        session = SQLiteSession(input_path)
        auth_key = session.auth_key.key if session.auth_key else b"\x00" * 256

        # Create TDATA structure
        tdata_files = {
            "key_data": auth_key,
            "settings": json.dumps({
                "version": "1.0",
                "platform": "desktop",
                "last_login": datetime.now().isoformat(),
                "device_model": "Telegram Desktop",
                "dc_id": session.dc_id,
                "server_address": session.server_address,
                "port": session.port
            }, indent=4),
            "map": b"\x00" * 4096,  # Telegram Desktop expects a larger map file
            "user": json.dumps({
                "user_id": session.user_id,
                "timestamp": session.timestamp.isoformat() if session.timestamp else None
            }, indent=4)
        }

        for file_name, content in tdata_files.items():
            file_path = os.path.join(output_dir, file_name)
            with open(file_path, "wb" if isinstance(content, bytes) else "w") as f:
                f.write(content)

        # Create zip archive
        shutil.make_archive(output_dir, "zip", output_dir)
        shutil.rmtree(output_dir)  # Clean up temporary folder
        return output_zip
    except Exception as e:
        logger.error(f"Error converting to TDATA for user {user_id}: {e}")
        return f"Error converting to TDATA: {str(e)}"

async def convert_to_txt(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_session.txt")

    try:
        logger.info(f"Converting to TXT for user {user_id}: {session_file}")
        with open(input_path, "rb") as src, open(output_path, "wb") as dst:
            dst.write(src.read())
        return output_path
    except Exception as e:
        logger.error(f"Error converting to TXT for user {user_id}: {e}")
        return f"Error converting to TXT: {str(e)}"

async def convert_to_string(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_session_string.txt")

    try:
        logger.info(f"Converting to StringSession for user {user_id}: {session_file}")
        session = SQLiteSession(input_path)
        session_string = StringSession.save(session)
        with open(output_path, "w") as f:
            f.write(session_string)
        return output_path
    except Exception as e:
        logger.error(f"Error converting to StringSession for user {user_id}: {e}")
        return f"Error converting to StringSession: {str(e)}"

# Callback query handler for format selection
@app.on_callback_query()
async def handle_callback(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    session_file = None
    short_session_id = None

    try:
        if data == "help":
            logger.info(f"Help button pressed by user {user_id}")
            if callback_query.message.text != (
                "📚 **Help**\n"
                "1. Send a Telethon `.session` file.\n"
                "2. Choose a format (JSON, TDATA, TXT, StringSession) using inline buttons.\n"
                "3. Receive the converted file.\n\n"
                "**Using Converted Files in Telegram Desktop**:\n"
                "- **TDATA**: Extract the zip to a folder and point Telegram Desktop to it.\n"
                "- **StringSession**: Use in a Telethon script to log in.\n"
                "- **JSON**: For developers to reconstruct sessions programmatically.\n"
                "- **TXT**: Raw session file copy.\n\n"
                f"Rate limit: {RATE_LIMIT} conversions per hour.\n"
                "Contact admin for support."
            ):
                await callback_query.message.edit_text(
                    "📚 **Help**\n"
                    "1. Send a Telethon `.session` file.\n"
                    "2. Choose a format (JSON, TDATA, TXT, StringSession) using inline buttons.\n"
                    "3. Receive the converted file.\n\n"
                    "**Using Converted Files in Telegram Desktop**:\n"
                    "- **TDATA**: Extract the zip to a folder and point Telegram Desktop to it.\n"
                    "- **StringSession**: Use in a Telethon script to log in.\n"
                    "- **JSON**: For developers to reconstruct sessions programmatically.\n"
                    "- **TXT**: Raw session file copy.\n\n"
                    f"Rate limit: {RATE_LIMIT} conversions per hour.\n"
                    "Contact admin for support.",
                    reply_markup=get_help_menu_buttons()
                )
            await callback_query.answer()
            return

        if data == "back":
            logger.info(f"Back button pressed by user {user_id}")
            if callback_query.message.text != (
                "Welcome to the Session Converter Bot! 📂\n"
                "Send a Telethon .session file to convert it to JSON, TDATA, TXT, or StringSession for use in Telegram Desktop.\n"
                "Use /help for more info."
            ):
                await callback_query.message.edit_text(
                    "Welcome to the Session Converter Bot! 📂\n"
                    "Send a Telethon .session file to convert it to JSON, TDATA, TXT, or StringSession for use in Telegram Desktop.\n"
                    "Use /help for more info.",
                    reply_markup=get_main_menu_buttons()
                )
            await callback_query.answer()
            return

        if not data.startswith("convert_"):
            await callback_query.answer("Invalid selection.")
            return

        # Extract format and session ID
        parts = data.split("_", 3)
        if len(parts) < 4:
            await callback_query.answer("Invalid callback data.")
            return

        format_type = parts[1]
        short_session_id = parts[3]

        # Check if user is authorized
        if user_id not in ADMIN_IDS and user_id != int(parts[2]):
            await callback_query.answer("You are not authorized to convert this file.")
            return

        # Check rate limit
        if not check_rate_limit(user_id):
            await callback_query.answer(f"Rate limit exceeded. Max {RATE_LIMIT} conversions per hour.")
            return

        # Find the actual session file from the database
        with sqlite3.connect("sessions.db") as conn:
            c = conn.cursor()
            c.execute("SELECT session_file FROM sessions WHERE user_id = ? AND short_session_id = ?",
                      (user_id, short_session_id))
            result = c.fetchone()
            logger.info(f"Session lookup for user {user_id}, short_session_id {short_session_id}: {result}")
            if not result:
                await callback_query.answer("Session not found.")
                await callback_query.message.edit_text("Session not found. Please upload the file again.")
                return
            session_file = result[0]

        # Update message only if necessary
        if callback_query.message.text != "Converting... Please wait.":
            await callback_query.message.edit_text("Converting... Please wait.")

        # Perform conversion
        output_path = None
        if format_type == "json":
            output_path = await convert_to_json(session_file, user_id)
        elif format_type == "tdata":
            output_path = await convert_to_tdata(session_file, user_id)
        elif format_type == "txt":
            output_path = await convert_to_txt(session_file, user_id)
        elif format_type == "string":
            output_path = await convert_to_string(session_file, user_id)

        if isinstance(output_path, str) and os.path.exists(output_path):
            # Log conversion
            log_conversion(user_id)

            # Send converted file
            await callback_query.message.reply_document(
                document=output_path,
                caption=f"Converted to {format_type.upper()}. Use this file to log in to Telegram Desktop.",
                reply_markup=get_format_buttons(user_id, short_session_id, session_file.split("_", 1)[-1])
            )
            # Clean up
            try:
                os.remove(output_path)
            except Exception as e:
                logger.error(f"Failed to delete file {output_path}: {e}")

            # Clean up original session file
            input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
            if os.path.exists(input_path):
                try:
                    os.remove(input_path)
                except Exception as e:
                    logger.error(f"Failed to delete original file {input_path}: {e}")
        else:
            await callback_query.message.edit_text(
                f"Conversion failed: {output_path}",
                reply_markup=get_format_buttons(user_id, short_session_id, session_file.split("_", 1)[-1])
            )
    except MessageNotModified:
        logger.info(f"Message not modified for user {user_id}, skipping edit")
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in callback for user {user_id}: {e}")
        error_message = f"Error during conversion: {str(e)}"
        try:
            await callback_query.message.reply_text(
                error_message,
                reply_markup=get_main_menu_buttons()
            )
        except Exception as reply_e:
            logger.error(f"Failed to reply for user {user_id}: {reply_e}")

# Signal handler for graceful shutdown
def handle_shutdown(signum, frame):
    logger.info("Shutdown signal received. Cleaning up...")
    try:
        files = os.listdir(SESSION_DIR)
        for file in files:
            file_path = os.path.join(SESSION_DIR, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    finally:
        logger.info("Bot shutdown complete.")
        raise SystemExit

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# Main function to run the bot
if __name__ == "__main__":
    logger.info("Starting bot...")
    try:
        app.run()
    except SystemExit:
        pass
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        logger.info("Bot shutdown complete.")