import os
import json
import shutil
import logging
import asyncio
import sqlite3
from datetime import datetime
from uuid import uuid4
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.sessions import StringSession

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
ADMIN_IDS = [7584086775]

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

# Database setup for storing user sessions
def init_db():
    try:
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sessions (
                     user_id INTEGER,
                     session_file TEXT,
                     session_string TEXT,
                     created_at TEXT
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

# Inline buttons for format selection
def get_format_buttons(user_id, session_file):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("JSON", callback_data=f"convert_json_{user_id}_{session_file}"),
            InlineKeyboardButton("TDATA", callback_data=f"convert_tdata_{user_id}_{session_file}")
        ],
        [
            InlineKeyboardButton("TXT", callback_data=f"convert_txt_{user_id}_{session_file}"),
            InlineKeyboardButton("StringSession", callback_data=f"convert_string_{user_id}_{session_file}")
        ]
    ])

# Start command
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    logger.info(f"Start command received from user {message.from_user.id}")
    await message.reply_text(
        "Welcome to the Session Converter Bot! 📂\n"
        "Send a session file (.session, tdata folder, or string) to convert it to JSON, TDATA, TXT, or String-denseion.\n"
        "Use /help for more info.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Help", callback_data="help")]
        ])
    )

# Help command
@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    logger.info(f"Help command received from user {message.from_user.id}")
    await message.reply_text(
        "📚 **Help**\n"
        "1. Send a `.session` file, tdata folder, or a session string.\n"
        "2. Choose the format to convert to using inline buttons.\n"
        "3. Receive the converted file.\n\n"
        "Supported formats: JSON, TDATA, TXT, StringSession.\n"
        "Contact admin for support."
    )

# Admin status command
@app.on_message(filters.command("status") & filters.private & filters.user(ADMIN_IDS))
async def status_command(client, message):
    logger.info(f"Status command received from admin {message.from_user.id}")
    conn = sqlite3.connect("sessions.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sessions")
    session_count = c.fetchone()[0]
    conn.close()
    await message.reply_text(
        f"Bot Status:\n"
        f"Total Sessions: {session_count}\n"
        f"Session Directory: {SESSION_DIR}\n"
        f"Log File: bot.log"
    )

# Handle document (session file)
@app.on_message(filters.document & filters.private)
async def handle_document(client, message):
    user_id = message.from_user.id
    file_name = message.document.file_name
    file_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_{file_name}")

    try:
        logger.info(f"Received document from user {user_id}: {file_name}")
        # Download the file
        await message.download(file_path)

        # Store in database
        conn = sqlite3.connect("sessions.db")
        c = conn.cursor()
        c.execute("INSERT INTO sessions (user_id, session_file, created_at) VALUES (?, ?, ?)",
                  (user_id, file_name, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        # Send format selection buttons
        await message.reply_text(
            f"Received session file: {file_name}\nChoose a format to convert to:",
            reply_markup=get_format_buttons(user_id, file_name)
        )
    except Exception as e:
        logger.error(f"Error handling document for user {user_id}: {e}")
        await message.reply_text("Failed to process the file. Please try again.")

# Handle text (session string)
@app.on_message(filters.text & filters.private)
async def handle_text(client, message):
    user_id = message.from_user.id
    session_string = message.text.strip()

    try:
        # Validate session string (basic check for Telethon string session)
        if len(session_string) > 100 and session_string.isascii():
            file_name = f"{user_id}_{uuid4()}_session.string"
            file_path = os.path.join(SESSION_DIR, file_name)

            logger.info(f"Received session string from user {user_id}")

            # Save session string to file
            with open(file_path, "w") as f:
                f.write(session_string)

            # Store in database
            conn = sqlite3.connect("sessions.db")
            c = conn.cursor()
            c.execute("INSERT INTO sessions (user_id, session_file, session_string, created_at) VALUES (?, ?, ?, ?)",
                      (user_id, file_name, session_string, datetime.now().isoformat()))
            conn.commit()
            conn.close()

            # Send format selection buttons
            await message.reply_text(
                "Received session string.\nChoose a format to convert to:",
                reply_markup=get_format_buttons(user_id, file_name)
            )
        else:
            await message.reply_text("Invalid session string. Please send a valid session string or file.")
    except Exception as e:
        logger.error(f"Error handling text for user {user_id}: {e}")
        await message.reply_text("Failed to process the session string. Please try again.")

# Conversion functions
async def convert_to_json(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_{session_file}.json")

    try:
        logger.info(f"Converting to JSON for user {user_id}: {session_file}")
        # Initialize Telethon client to extract session data
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        with open(input_path, "r") as f:
            session_string = f.read().strip()
        client.session.load_session(StringSession(session_string))

        # Extract session data
        session_data = {
            "dc_id": client.session.dc_id,
            "server_address": client.session.server_address,
            "port": client.session.port,
            "auth_key": client.session.auth_key.key.hex() if client.session.auth_key else None,
            "takeout_id": client.session.takeout_id
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

        # Simulate TDATA structure (improved)
        tdata_files = {
            "key_data": input_path,  # Simplified; real TDATA requires more complex structure
            "settings": json.dumps({"version": "1.0", "platform": "unknown"})
        }

        for file_name, content in tdata_files.items():
            file_path = os.path.join(output_dir, file_name)
            if isinstance(content, str) and os.path.exists(content):
                with open(content, "rb") as src, open(file_path, "wb") as dst:
                    dst.write(src.read())
            else:
                with open(file_path, "w") as f:
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
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_{session_file}.txt")

    try:
        logger.info(f"Converting to TXT for user {user_id}: {session_file}")
        shutil.copyfile(input_path, output_path)
        return output_path
    except Exception as e:
        logger.error(f"Error converting to TXT for user {user_id}: {e}")
        return f"Error converting to TXT: {str(e)}"

async def convert_to_string(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{uuid4()}_{session_file}_string.txt")

    try:
        logger.info(f"Converting to StringSession for user {user_id}: {session_file}")
        with open(input_path, "r") as f:
            session_string = f.read().strip()
        with open(output_path, "w") as f:
            f.write(session_string)
        return output_path
    except Exception as e:
        logger.error(f"Error converting to StringSession for user {user_idjn {user_id}: {e}")
        return f"Error converting to StringSession: {str(e)}"

# Callback query handler for format selection
@app.on_callback_query()
async def handle_callback(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id

    try:
        if not data.startswith("convert_"):
            await callback_query.answer("Invalid selection.")
            return

        # Extract format and session file
        parts = data.split("_", 3)
        if len(parts) < 4:
            await callback_query.answer("Invalid callback data.")
            return

        format_type = parts[1]
        session_file = parts[3]

        # Check if user is authorized
        if user_id not in ADMIN_IDS and user_id != int(parts[2]):
            await callback_query.answer("You are not authorized to convert this file.")
            return

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
            # Send converted file
            await callback_query.message.reply_document(
                document=output_path,
                caption=f"Converted to {format_type.upper()}",
                reply_markup=get_format_buttons(user_id, session_file)
            )
            # Clean up
            try:
                os.remove(output_path)
            except Exception as e:
                logger.error(f"Failed to delete file {output_path}: {e}")
        else:
            await callback_query.message.edit_text(
                f"Conversion failed: {output_path}",
                reply_markup=get_format_buttons(user_id, session_file)
            )
    except Exception as e:
        logger.error(f"Error in callback for user {user_id}: {e}")
        await callback_query.message.edit_text(
            f"Error during conversion: {str(e)}",
            reply_markup=get_format_buttons(user_id, session_file)
        )

# Main function to run the bot
if __name__ == "__main__":
    logger.info("Starting bot...")
    app.run()
    logger.info("Bot stopped.")