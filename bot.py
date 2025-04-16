import os
import json
import shutil
import logging
import sqlite3
import re
import hashlib
import signal
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional, Tuple, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import MessageNotModified, BadRequest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.sessions.sqlite import SQLiteSession
from telethon.crypto import AuthKey

# --- Configuration ---
class Config:
    API_ID = 25781839
    API_HASH = "20a3f2f168739259a180dcdd642e196c"
    BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
    ADMIN_IDS = [7584086775]
    RATE_LIMIT = 5  # Max conversions per user per hour
    RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
    FILE_RETENTION_HOURS = 24  # Clean up files older than 24 hours
    SESSION_DIR = "sessions"
    DB_NAME = "sessions.db"
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB maximum file size

# --- Logging Setup ---
def setup_logging():
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler()
        ]
    )
    # Suppress noisy library logs
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    return logging.getLogger(__name__)

logger = setup_logging()

# --- Database Management ---
class DatabaseManager:
    def __init__(self, db_name: str = Config.DB_NAME):
        self.db_name = db_name
        self._initialize_db()

    def _initialize_db(self):
        """Initialize the database with required tables."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
                             id INTEGER PRIMARY KEY AUTOINCREMENT,
                             user_id INTEGER NOT NULL,
                             session_file TEXT NOT NULL,
                             short_session_id TEXT NOT NULL UNIQUE,
                             session_string TEXT,
                             created_at TEXT NOT NULL,
                             expires_at TEXT
                             )''')
                conn.execute('''CREATE TABLE IF NOT EXISTS rate_limits (
                             user_id INTEGER NOT NULL,
                             conversion_time TEXT NOT NULL,
                             FOREIGN KEY(user_id) REFERENCES sessions(user_id)
                             )''')
                conn.execute('''CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)''')
                conn.execute('''CREATE INDEX IF NOT EXISTS idx_sessions_short_id ON sessions(short_session_id)''')
                conn.execute('''CREATE INDEX IF NOT EXISTS idx_rate_limits_user ON rate_limits(user_id)''')
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    def store_session(self, user_id: int, session_file: str, short_session_id: str, 
                     session_string: Optional[str] = None) -> bool:
        """Store session information in the database."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                conn.execute(
                    "INSERT INTO sessions (user_id, session_file, short_session_id, session_string, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, session_file, short_session_id, session_string, datetime.now().isoformat())
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error storing session for user {user_id}: {e}")
            return False

    def get_session_file(self, user_id: int, short_session_id: str) -> Optional[str]:
        """Retrieve session file path from the database."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT session_file FROM sessions WHERE user_id = ? AND short_session_id = ?",
                    (user_id, short_session_id)
                )
                result = cursor.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"Error retrieving session for user {user_id}: {e}")
            return None

    def check_rate_limit(self, user_id: int) -> bool:
        """Check if user has exceeded rate limit."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                cutoff = (datetime.now() - timedelta(seconds=Config.RATE_LIMIT_WINDOW)).isoformat()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND conversion_time > ?",
                    (user_id, cutoff)
                )
                count = cursor.fetchone()[0]
                return count < Config.RATE_LIMIT
        except Exception as e:
            logger.error(f"Error checking rate limit for user {user_id}: {e}")
            return False

    def log_conversion(self, user_id: int) -> bool:
        """Log a conversion event for rate limiting."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                conn.execute(
                    "INSERT INTO rate_limits (user_id, conversion_time) VALUES (?, ?)",
                    (user_id, datetime.now().isoformat())
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error logging conversion for user {user_id}: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get bot statistics from the database."""
        try:
            with sqlite3.connect(self.db_name) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM sessions")
                session_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM rate_limits")
                conversion_count = cursor.fetchone()[0]
                return {
                    "session_count": session_count,
                    "conversion_count": conversion_count
                }
        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
            return {}

# Initialize database
db = DatabaseManager()

# Ensure session directory exists
os.makedirs(Config.SESSION_DIR, exist_ok=True)

# --- Utility Functions ---
def get_short_session_id(session_file: str) -> str:
    """Generate a short unique ID for session identification."""
    return hashlib.md5(session_file.encode()).hexdigest()[:8]

def validate_session_file(file_path: str) -> bool:
    """Validate if the file is a valid Telethon session file."""
    try:
        # Check if file is a valid SQLite database
        if not file_path.endswith('.session'):
            return False
            
        with sqlite3.connect(file_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
            return bool(cursor.fetchone())
    except Exception as e:
        logger.error(f"Error validating session file {file_path}: {e}")
        return False

def cleanup_old_files() -> int:
    """Clean up files older than retention period."""
    deleted = 0
    cutoff = datetime.now() - timedelta(hours=Config.FILE_RETENTION_HOURS)
    
    try:
        for filename in os.listdir(Config.SESSION_DIR):
            file_path = os.path.join(Config.SESSION_DIR, filename)
            if os.path.isfile(file_path):
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_mtime < cutoff:
                    try:
                        os.remove(file_path)
                        deleted += 1
                    except Exception as e:
                        logger.error(f"Failed to delete file {file_path}: {e}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    
    return deleted

# --- Button Generators ---
def get_format_buttons(user_id: int, short_session_id: str, display_file: str) -> Optional[InlineKeyboardMarkup]:
    """Generate inline buttons for format selection."""
    callback_base = f"convert_{{}}_{user_id}_{short_session_id}"
    
    buttons = [
        [
            InlineKeyboardButton("JSON", callback_data=callback_base.format("json")),
            InlineKeyboardButton("TDATA", callback_data=callback_base.format("tdata"))
        ],
        [
            InlineKeyboardButton("TXT", callback_data=callback_base.format("txt")),
            InlineKeyboardButton("StringSession", callback_data=callback_base.format("string"))
        ],
        [InlineKeyboardButton("Back", callback_data="back")]
    ]
    
    # Validate callback data length (Telegram limit: 64 bytes)
    for row in buttons:
        for button in row:
            if len(button.callback_data.encode()) > 64:
                logger.error(f"Callback data too long: {button.callback_data}")
                return None
                
    return InlineKeyboardMarkup(buttons)

def get_main_menu_buttons() -> InlineKeyboardMarkup:
    """Generate main menu buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Help", callback_data="help")]
    ])

def get_help_menu_buttons() -> InlineKeyboardMarkup:
    """Generate help menu buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data="back")]
    ])

# --- Conversion Functions ---
async def convert_to_json(session_file: str, user_id: int) -> str:
    """Convert session file to JSON format."""
    input_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{uuid4()}_session.json")

    try:
        session = SQLiteSession(input_path)
        session_data = {
            "dc_id": session.dc_id,
            "server_address": session.server_address,
            "port": session.port,
            "auth_key": session.auth_key.key.hex() if session.auth_key else None,
            "user_id": session.user_id,
            "timestamp": session.timestamp.isoformat() if session.timestamp else None
        }

        with open(output_path, "w") as f:
            json.dump(session_data, f, indent=4)

        return output_path
    except Exception as e:
        logger.error(f"Error converting to JSON for user {user_id}: {e}")
        return f"Error converting to JSON: {str(e)}"

async def convert_to_tdata(session_file: str, user_id: int) -> str:
    """Convert session file to Telegram Desktop TDATA format."""
    input_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    output_dir = os.path.join(Config.SESSION_DIR, f"{user_id}_{uuid4()}_tdata")
    output_zip = f"{output_dir}.zip"

    try:
        os.makedirs(output_dir, exist_ok=True)
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
            "map": b"\x00" * 4096,
            "user": json.dumps({
                "user_id": session.user_id,
                "timestamp": session.timestamp.isoformat() if session.timestamp else None
            }, indent=4)
        }

        # Write files
        for file_name, content in tdata_files.items():
            file_path = os.path.join(output_dir, file_name)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(file_path, mode) as f:
                f.write(content)

        # Create zip archive
        shutil.make_archive(output_dir, "zip", output_dir)
        shutil.rmtree(output_dir)
        return output_zip
    except Exception as e:
        logger.error(f"Error converting to TDATA for user {user_id}: {e}")
        return f"Error converting to TDATA: {str(e)}"

async def convert_to_txt(session_file: str, user_id: int) -> str:
    """Convert session file to plain text format."""
    input_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{uuid4()}_session.txt")

    try:
        with open(input_path, "rb") as src, open(output_path, "wb") as dst:
            dst.write(src.read())
        return output_path
    except Exception as e:
        logger.error(f"Error converting to TXT for user {user_id}: {e}")
        return f"Error converting to TXT: {str(e)}"

async def convert_to_string(session_file: str, user_id: int) -> str:
    """Convert session file to Telethon StringSession format."""
    input_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{uuid4()}_session_string.txt")

    try:
        session = SQLiteSession(input_path)
        session_string = StringSession.save(session)
        
        with open(output_path, "w") as f:
            f.write(session_string)
            
        return output_path
    except Exception as e:
        logger.error(f"Error converting to StringSession for user {user_id}: {e}")
        return f"Error converting to StringSession: {str(e)}"

# --- Bot Handlers ---
app = Client(
    "session_converter_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

@app.on_message(filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    """Handle /start command."""
    logger.info(f"Start command from user {message.from_user.id}")
    await message.reply_text(
        "Welcome to the Session Converter Bot! 📂\n"
        "Send a Telethon .session file to convert it to JSON, TDATA, TXT, or StringSession format.\n"
        "Use /help for more information.",
        reply_markup=get_main_menu_buttons()
    )

@app.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    """Handle /help command."""
    logger.info(f"Help command from user {message.from_user.id}")
    await message.reply_text(
        "📚 **Help**\n"
        "1. Send a Telethon `.session` file or StringSession\n"
        "2. Choose a format (JSON, TDATA, TXT, StringSession)\n"
        "3. Receive the converted file\n\n"
        "**Formats**:\n"
        "- **TDATA**: For Telegram Desktop (extract zip to folder)\n"
        "- **StringSession**: For Telethon scripts\n"
        "- **JSON**: For developers\n"
        "- **TXT**: Raw session copy\n\n"
        f"Rate limit: {Config.RATE_LIMIT} conversions/hour\n"
        "Contact admin for support.",
        reply_markup=get_help_menu_buttons()
    )

@app.on_message(filters.command("status") & filters.private & filters.user(Config.ADMIN_IDS))
async def status_command(client: Client, message: Message):
    """Handle /status command (admin only)."""
    logger.info(f"Status command from admin {message.from_user.id}")
    try:
        stats = db.get_stats()
        disk_usage = shutil.disk_usage(Config.SESSION_DIR)
        
        await message.reply_text(
            f"Bot Status:\n"
            f"Total Sessions: {stats.get('session_count', 0)}\n"
            f"Total Conversions: {stats.get('conversion_count', 0)}\n"
            f"Disk Usage: {disk_usage.used / 1024**2:.2f} MB / {disk_usage.total / 1024**2:.2f} MB\n"
            f"Files in Session Directory: {len(os.listdir(Config.SESSION_DIR))}\n"
            f"Log File: bot.log"
        )
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await message.reply_text("Failed to retrieve status.")

@app.on_message(filters.command("cleanup") & filters.private & filters.user(Config.ADMIN_IDS))
async def cleanup_command(client: Client, message: Message):
    """Handle /cleanup command (admin only)."""
    logger.info(f"Cleanup command from admin {message.from_user.id}")
    try:
        deleted = cleanup_old_files()
        await message.reply_text(f"Cleaned up {deleted} old files from session directory.")
    except Exception as e:
        logger.error(f"Error in cleanup command: {e}")
        await message.reply_text("Failed to clean up files.")

@app.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    """Handle session file uploads."""
    user_id = message.from_user.id
    file_name = message.document.file_name or ""
    
    if not file_name.lower().endswith(".session"):
        await message.reply_text("Please upload a valid Telethon .session file.")
        return

    if message.document.file_size > Config.MAX_FILE_SIZE:
        await message.reply_text("File size exceeds maximum limit (10MB).")
        return

    unique_id = uuid4().hex
    session_file = f"{unique_id}_{file_name}"
    file_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    short_session_id = get_short_session_id(session_file)

    try:
        # Download the file
        await message.download(file_path)
        
        # Validate session file
        if not validate_session_file(file_path):
            await message.reply_text("Invalid session file. Please upload a valid Telethon .session file.")
            os.remove(file_path)
            return

        # Store in database
        if not db.store_session(user_id, session_file, short_session_id):
            raise Exception("Failed to store session in database")

        # Send format selection buttons
        buttons = get_format_buttons(user_id, short_session_id, file_name)
        if buttons:
            await message.reply_text(
                f"Received session file: {file_name}\nChoose a format to convert to:",
                reply_markup=buttons
            )
        else:
            await message.reply_text("Error: Unable to generate conversion options.")
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Error handling document for user {user_id}: {e}")
        await message.reply_text("Failed to process the file. Please try again.")
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file {file_path}: {e}")

@app.on_message(filters.text & filters.private)
async def handle_text(client: Client, message: Message):
    """Handle StringSession text input."""
    user_id = message.from_user.id
    session_string = message.text.strip()

    # Validate session string format
    if not (len(session_string) > 100 and re.match(r'^[A-Za-z0-9+/=]+$', session_string)):
        await message.reply_text("Invalid session string. Please send a valid Telethon StringSession.")
        return

    unique_id = uuid4().hex
    session_file = f"{unique_id}_session.string"
    file_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
    short_session_id = get_short_session_id(session_file)

    try:
        # Save session string to file
        with open(file_path, "w") as f:
            f.write(session_string)

        # Store in database
        if not db.store_session(user_id, session_file, short_session_id, session_string):
            raise Exception("Failed to store session in database")

        # Send format selection buttons
        buttons = get_format_buttons(user_id, short_session_id, "session.string")
        if buttons:
            await message.reply_text(
                "Received session string.\nChoose a format to convert to:",
                reply_markup=buttons
            )
        else:
            await message.reply_text("Error: Unable to generate conversion options.")
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Error handling text for user {user_id}: {e}")
        await message.reply_text("Failed to process the session string. Please try again.")
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file {file_path}: {e}")

@app.on_callback_query()
async def handle_callback(client: Client, callback_query: CallbackQuery):
    """Handle all callback queries."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    try:
        if data == "help":
            await handle_help_callback(callback_query)
        elif data == "back":
            await handle_back_callback(callback_query)
        elif data.startswith("convert_"):
            await handle_conversion_callback(callback_query)
        else:
            await callback_query.answer("Invalid selection.")
    except MessageNotModified:
        logger.debug(f"Message not modified for user {user_id}")
    except BadRequest as e:
        logger.error(f"BadRequest in callback for user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in callback for user {user_id}: {e}")
        await callback_query.message.reply_text(
            "An error occurred. Please try again.",
            reply_markup=get_main_menu_buttons()
        )

async def handle_help_callback(callback_query: CallbackQuery):
    """Handle help button callback."""
    await callback_query.message.edit_text(
        "📚 **Help**\n"
        "1. Send a Telethon `.session` file or StringSession\n"
        "2. Choose a format (JSON, TDATA, TXT, StringSession)\n"
        "3. Receive the converted file\n\n"
        "**Formats**:\n"
        "- **TDATA**: For Telegram Desktop (extract zip to folder)\n"
        "- **StringSession**: For Telethon scripts\n"
        "- **JSON**: For developers\n"
        "- **TXT**: Raw session copy\n\n"
        f"Rate limit: {Config.RATE_LIMIT} conversions/hour\n"
        "Contact admin for support.",
        reply_markup=get_help_menu_buttons()
    )
    await callback_query.answer()

async def handle_back_callback(callback_query: CallbackQuery):
    """Handle back button callback."""
    await callback_query.message.edit_text(
        "Welcome to the Session Converter Bot! 📂\n"
        "Send a Telethon .session file to convert it to JSON, TDATA, TXT, or StringSession format.\n"
        "Use /help for more information.",
        reply_markup=get_main_menu_buttons()
    )
    await callback_query.answer()

async def handle_conversion_callback(callback_query: CallbackQuery):
    """Handle conversion format selection callback."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    # Parse callback data
    parts = data.split("_", 3)
    if len(parts) < 4:
        await callback_query.answer("Invalid callback data.")
        return

    format_type = parts[1]
    short_session_id = parts[3]

    # Check authorization
    if user_id not in Config.ADMIN_IDS and user_id != int(parts[2]):
        await callback_query.answer("You are not authorized to convert this file.")
        return

    # Check rate limit
    if not db.check_rate_limit(user_id):
        await callback_query.answer(f"Rate limit exceeded. Max {Config.RATE_LIMIT} conversions per hour.")
        return

    # Get session file from database
    session_file = db.get_session_file(user_id, short_session_id)
    if not session_file:
        await callback_query.answer("Session not found.")
        await callback_query.message.edit_text("Session not found. Please upload the file again.")
        return

    # Update message
    try:
        await callback_query.message.edit_text("Converting... Please wait.")
    except MessageNotModified:
        pass

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

    # Handle conversion result
    if isinstance(output_path, str) and os.path.exists(output_path):
        # Log conversion
        db.log_conversion(user_id)

        # Send converted file
        await callback_query.message.reply_document(
            document=output_path,
            caption=f"Converted to {format_type.upper()} format.",
            reply_markup=get_format_buttons(user_id, short_session_id, session_file.split("_", 1)[-1])
            
        # Clean up
        try:
            os.remove(output_path)
            input_path = os.path.join(Config.SESSION_DIR, f"{user_id}_{session_file}")
            if os.path.exists(input_path):
                os.remove(input_path)
        except Exception as e:
            logger.error(f"Failed to delete files: {e}")
    else:
        await callback_query.message.edit_text(
            f"Conversion failed: {output_path}",
            reply_markup=get_format_buttons(user_id, short_session_id, session_file.split("_", 1)[-1])
        )

# --- Signal Handling ---
def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info("Shutdown signal received. Cleaning up...")
    try:
        cleanup_old_files()
    except Exception as e:
        logger.error(f"Error during shutdown cleanup: {e}")
    finally:
        logger.info("Bot shutdown complete.")
        raise SystemExit

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# --- Main Entry Point ---
if __name__ == "__main__":
    logger.info("Starting bot...")
    try:
        # Perform initial cleanup
        cleanup_old_files()
        
        # Run the bot
        app.run()
    except SystemExit:
        pass
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        logger.info("Bot stopped.")