import os
import json
import shutil
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio
import sqlite3
from datetime import datetime

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
ADMIN_IDS = [7584086775]

# Initialize Pyrogram Client
app = Client("session_converter_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Database setup for storing user sessions
def init_db():
    conn = sqlite3.connect("sessions.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
                 user_id INTEGER,
                 session_file TEXT,
                 session_string TEXT,
                 created_at TEXT
                 )''')
    conn.commit()
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
    await message.reply_text(
        "Welcome to the Session Converter Bot! 📂\n"
        "Send a session file (.session, tdata folder, or string) to convert it to JSON, TDATA, TXT, or StringSession.\n"
        "Use /help for more info.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Help", callback_data="help")]
        ])
    )

# Help command
@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    await message.reply_text(
        "📚 **Help**\n"
        "1. Send a `.session` file, tdata folder, or a session string.\n"
        "2. Choose the format to convert to using inline buttons.\n"
        "3. Receive the converted file.\n\n"
        "Supported formats: JSON, TDATA, TXT, StringSession.\n"
        "Contact admin for support."
    )

# Handle document (session file)
@app.on_message(filters.document & filters.private)
async def handle_document(client, message):
    user_id = message.from_user.id
    file_name = message.document.file_name
    file_path = os.path.join(SESSION_DIR, f"{user_id}_{file_name}")

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

# Handle text (session string)
@app.on_message(filters.text & filters.private)
async def handle_text(client, message):
    user_id = message.from_user.id
    session_string = message.text.strip()
    
    # Validate session string (basic check)
    if len(session_string) > 100:  # Assuming session string is long
        file_name = f"{user_id}_session.string"
        file_path = os.path.join(SESSION_DIR, file_name)

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

# Conversion functions
async def convert_to_json(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}.json")
    
    try:
        # Initialize Telethon client to extract session data
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        with open(input_path, "r") as f:
            session_string = f.read()
        client.session.load_session(StringSession(session_string))
        
        # Extract session data
        session_data = {
            "dc_id": client.session.dc_id,
            "server_address": client.session.server_address,
            "port": client.session.port,
            "auth_key": client.session.auth_key.key.hex(),
            "takeout_id": client.session.takeout_id
        }
        
        # Save to JSON
        with open(output_path, "w") as f:
            json.dump(session_data, f, indent=4)
        
        return output_path
    except Exception as e:
        return f"Error converting to JSON: {str(e)}"

async def convert_to_tdata(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_dir = os.path.join(SESSION_DIR, f"{user_id}_tdata")
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Simulate TDATA structure (simplified)
        with open(os.path.join(output_dir, "key_data"), "wb") as f:
            with open(input_path, "r") as sf:
                f.write(sf.read().encode())
        shutil.make_archive(output_dir, "zip", output_dir)
        return f"{output_dir}.zip"
    except Exception as e:
        return f"Error converting to TDATA: {str(e)}"

async def convert_to_txt(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}.txt")
    
    try:
        # Copy session string to TXT
        shutil.copyfile(input_path, output_path)
        return output_path
    except Exception as e:
        return f"Error converting to TXT: {str(e)}"

async def convert_to_string(session_file, user_id):
    input_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}")
    output_path = os.path.join(SESSION_DIR, f"{user_id}_{session_file}_string.txt")
    
    try:
        # Read session string
        with open(input_path, "r") as f:
            session_string = f.read()
        with open(output_path, "w") as f:
            f.write(session_string)
        return output_path
    except Exception as e:
        return f"Error converting to StringSession: {str(e)}"

# Callback query handler for format selection
@app.on_callback_query()
async def handle_callback(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    if not data.startswith("convert_"):
        await callback_query.answer("Invalid selection.")
        return

    # Extract format and session file
    parts = data.split("_")
    if len(parts) < 3:
        await callback_query.answer("Invalid callback data.")
        return

    format_type = parts[1]
    session_file = "_".join(parts[2:])
    
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
        os.remove(output_path)
    else:
        await callback_query.message.edit_text(
            f"Conversion failed: {output_path}",
            reply_markup=get_format_buttons(user_id, session_file)
        )

# Main function to run the bot
async def main():
    await app.start()
    print("Bot is running...")
    await app.idle()

if __name__ == "__main__":
    asyncio.run(main())