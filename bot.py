import os
import logging
import signal
import schedule
import asyncio
import aiosqlite
from datetime import datetime
from typing import List, Optional, Tuple
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import (
    FloodWait,
    RPCError,
    AuthKeyUnregistered,
    SessionRevoked,
    UserDeactivatedBan
)

# Configure logging
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Telegram API credentials
API_ID = 29637547
API_HASH = "13e303a526522f741c0680cfc8cd9c00"
BOT_TOKEN = "7547436649:AAGtnui5bFEr-Dt9Qt1lKjtvwpw4F0cWpKs"
ADMIN_ID = 6257711894

# Initialize bot client
bot = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Directory for session files
SESSION_DIR = "sessions> sessions"
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# SQLite database for schedules
DB_PATH = "schedules.db"

# Initialize database
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                media_path TEXT,
                schedule_time TEXT,
                interval_seconds INTEGER,
                is_recurring BOOLEAN NOT NULL
            )
        """)
        await db.commit()
    logger.info("Database initialized")

# Load session clients
async def load_session_clients() -> List[Client]:
    clients = []
    for session_file in os.listdir(SESSION_DIR):
        if session_file.endswith(".session"):
            session_name = os.path.splitext(session_file)[0]
            client = Client(
                name=os.path.join(SESSION_DIR, session_name),
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=SESSION_DIR
            )
            try:
                await client.start()
                clients.append(client)
                logger.info(f"Loaded session: {session_name}")
            except Exception as e:
                logger.error(f"Failed to load session {session_name}: {e}")
    return clients

# Rotate clients for load balancing
current_client_index = 0
async def get_next_client(clients: List[Client]) -> Optional[Client]:
    global current_client_index
    if not clients:
        return None
    current_client_index = (current_client_index + 1) % len(clients)
    return clients[current_client_index]

# Send message using a session client
async def send_message_with_session(
    clients: List[Client], chat_id: str, text: str, media_path: Optional[str] = None
) -> bool:
    client = await get_next_client(clients)
    if not client:
        logger.error("No active session clients available")
        return False
    try:
        if media_path:
            ext = os.path.splitext(media_path)[1].lower()
            if ext in [".jpg", ".png", ".jpeg"]:
                await client.send_photo(chat_id, media_path, caption=text)
            elif ext in [".mp4", ".avi", ".mkv"]:
                await client.send_video(chat_id, media_path, caption=text)
            else:
                await client.send_document(chat_id, media_path, caption=text)
        else:
            await client.send_message(chat_id, text)
        logger.info(f"Message sent to {chat_id}")
        return True
    except FloodWait as e:
        logger.warning(f"Flood wait: {e.x} seconds")
        await asyncio.sleep(e.x)
        return False
    except RPCError as e:
        logger.error(f"Error sending message to {chat_id}: {e}")
        return False

# Validate a session file
async def validate_session(session_path: str, session_name: str) -> Tuple[bool, str]:
    client = Client(
        name=session_path,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=os.path.dirname(session_path)
    )
    try:
        await client.start()
        await client.stop()
        logger.info(f"Session {session_name} is valid")
        return True, "Session is alive"
    except AuthKeyUnregistered:
        logger.error(f"Session {session_name} is unregistered")
        return False, "Session is unregistered"
    except SessionRevoked:
        logger.error(f"Session {session_name} is revoked")
        return False, "Session is revoked"
    except UserDeactivatedBan:
        logger.error(f"Session {session_name} is banned")
        return False, "Session is banned"
    except Exception as e:
        logger.error(f"Validation error for {session_name}: {e}")
        return False, f"Validation failed: {e}"

# Check and send scheduled messages
async def check_scheduled_messages():
    clients = await load_session_clients()
    current_time = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, chat_id, text, media_path, schedule_time FROM schedules WHERE is_recurring = 0"
        )
        schedules = await cursor.fetchall()
        for schedule_id, chat_id, text, media_path, schedule_time in schedules:
            try:
                schedule_time = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M:%S")
                if schedule_time <= current_time:
                    await send_message_with_session(clients, chat_id, text, media_path)
                    await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
                    await db.commit()
                    logger.info(f"Sent and deleted one-time schedule {schedule_id}")
            except Exception as e:
                logger.error(f"Error processing schedule {schedule_id}: {e}")

# Handle non-admin users
@bot.on_message(~filters.user(ADMIN_ID))
async def handle_non_admin(client, message):
    await message.reply("This is a private bot.")
    logger.info(f"Non-admin user {message.from_user.id} attempted to interact")

# Command to add a session by uploading a file
@bot.on_message(filters.command("addsession") & filters.user(ADMIN_ID))
async def add_session(client, message):
    await message.reply("Please upload a .session file.")
    logger.info("Admin requested to add a session")

# Handle uploaded session file
@bot.on_message(filters.document & filters.user(ADMIN_ID))
async def handle_session_upload(client, message):
    if not message.document.file_name.endswith(".session"):
        await message.reply("Please upload a valid .session file.")
        logger.warning(f"Invalid file uploaded: {message.document.file_name}")
        return
    temp_path = f"temp_{message.document.file_name}"
    try:
        await message.download(file_name=temp_path)
        session_name = f"user_{int(time.time())}"
        session_path = os.path.join(SESSION_DIR, f"{session_name}.session")
        is_valid, status = await validate_session(temp_path, session_name)
        if is_valid:
            os.rename(temp_path, session_path)
            await message.reply(f"Session {session_name}.session is alive and stored!")
            logger.info(f"Stored session {session_name}.session")
        else:
            await message.reply(f"Session is invalid: {status}")
            logger.warning(f"Invalid session uploaded: {status}")
    except Exception as e:
        await message.reply(f"Error processing session: {e}")
        logger.error(f"Error processing session upload: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
            logger.info(f"Cleaned up temporary file {temp_path}")

# Command to send a text message
@bot.on_message(filters.command("send") & filters.user(ADMIN_ID))
async def send_message(client, message):
    try:
        _, chat_id, *text = message.text.split(maxsplit=2)
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        text = text[0] if text else "Hello!"
        clients = await load_session_clients()
        if await send_message_with_session(clients, chat_id, text):
            await message.reply("Message sent!")
        else:
            await message.reply("Failed to send message.")
        logger.info(f"Send command executed for chat {chat_id}")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in send command: {e}")

# Command to send media
@bot.on_message(filters.command("sendmedia") & filters.user(ADMIN_ID))
async def send_media(client, message):
    try:
        _, chat_id, media_path, *caption = message.text.split(maxsplit=3)
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        caption = caption[0] if caption else ""
        if not os.path.exists(media_path):
            await message.reply("Media file not found.")
            logger.warning(f"Media file not found: {media_path}")
            return
        clients = await load_session_clients()
        if await send_message_with_session(clients, chat_id, caption, media_path):
            await message.reply("Media sent!")
        else:
            await message.reply("Failed to send media.")
        logger.info(f"Sendmedia command executed for chat {chat_id}")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in sendmedia command: {e}")

# Command to edit a message
@bot.on_message(filters.command("edit") & filters.user(ADMIN_ID))
async def edit_message(client, message):
    try:
        _, chat_id, message_id, *new_text = message.text.split(maxsplit=3)
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        message_id = int(message_id)
        new_text = new_text[0] if new_text else "Edited message"
        clients = await load_session_clients()
        if clients:
            client = await get_next_client(clients)
            await client.edit_message_text(chat_id, message_id, new_text)
            await message.reply("Message edited!")
            logger.info(f"Edited message {message_id} in chat {chat_id}")
        else:
            await message.reply("No active sessions.")
            logger.warning("No active sessions for edit command")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in edit command: {e}")

# Command to schedule a one-time message
@bot.on_message(filters.command("schedule") & filters.user(ADMIN_ID))
async def schedule_message(client, message):
    try:
        parts = message.text.split(maxsplit=4)
        if len(parts) < 4:
            await message.reply("Usage: /schedule <chat_id> <time> <message> [media_path]")
            return
        _, chat_id, time_str, text, *media_path = parts
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        media_path = media_path[0] if media_path else None
        if media_path and not os.path.exists(media_path):
            await message.reply("Media file not found.")
            logger.warning(f"Media file not found: {media_path}")
            return
        try:
            schedule_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        except ValueError:
            await message.reply("Invalid time format. Use YYYY-MM-DD HH:MM")
            logger.warning(f"Invalid time format: {time_str}")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO schedules (chat_id, text, media_path, schedule_time, is_recurring)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, text, media_path, schedule_time.strftime("%Y-%m-%d %H:%M:%S"), False)
            )
            await db.commit()
        await message.reply("Message scheduled!")
        logger.info(f"One-time message scheduled for {chat_id} at {time_str}")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in schedule command: {e}")

# Command to schedule recurring messages
@bot.on_message(filters.command("recurring") & filters.user(ADMIN_ID))
async def schedule_recurring(client, message):
    try:
        parts = message.text.split(maxsplit=4)
        if len(parts) < 4:
            await message.reply("Usage: /recurring <chat_id> <interval> <message> [media_path]")
            return
        _, chat_id, interval, text, *media_path = parts
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        media_path = media_path[0] if media_path else None
        if media_path and not os.path.exists(media_path):
            await message.reply("Media file not found.")
            logger.warning(f"Media file not found: {media_path}")
            return
        if interval not in ["1m", "30m", "1h"]:
            await message.reply("Interval must be 1m, 30m, or 1h")
            logger.warning(f"Invalid interval: {interval}")
            return
        seconds = {"1m": 60, "30m": 1800, "1h": 3600}[interval]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO schedules (chat_id, text, media_path, interval_seconds, is_recurring)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, text, media_path, seconds, True)
            )
            await db.commit()
        async def recurring_task():
            clients = await load_session_clients()
            await send_message_with_session(clients, chat_id, text, media_path)
        schedule.every(seconds).seconds.do(lambda: asyncio.create_task(recurring_task()))
        await message.reply(f"Recurring message scheduled every {interval}!")
        logger.info(f"Recurring message scheduled for {chat_id} every {interval}")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in recurring command: {e}")

# Command to list schedules
@bot.on_message(filters.command("listschedules") & filters.user(ADMIN_ID))
async def list_schedules(client, message):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT id, chat_id, text, media_path, schedule_time, interval_seconds, is_recurring FROM schedules")
            schedules = await cursor.fetchall()
        if not schedules:
            await message.reply("No active schedules.")
            logger.info("No active schedules found")
            return
        response = "Active Schedules:\n"
        for s in schedules:
            schedule_id, chat_id, text, media_path, schedule_time, interval, is_recurring = s
            if is_recurring:
                interval_str = f"every {interval} seconds"
            else:
                interval_str = f"at {schedule_time}"
            response += f"ID: {schedule_id}, Chat: {chat_id}, Text: {text}, Media: {media_path or 'None'}, Time: {interval_str}\n"
        await message.reply(response)
        logger.info("Listed active schedules")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in listschedules command: {e}")

# Command to cancel a schedule
@bot.on_message(filters.command("cancelschedule") & filters.user(ADMIN_ID))
async def cancel_schedule(client, message):
    try:
        _, schedule_id = message.text.split(maxsplit=1)
        schedule_id = int(schedule_id)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT is_recurring FROM schedules WHERE id = ?", (schedule_id,))
            result = await cursor.fetchone()
            if not result:
                await message.reply("Schedule not found.")
                logger.warning(f"Schedule {schedule_id} not found")
                return
            await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await db.commit()
        schedule.clear()  # Clear and reload recurring schedules
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT chat_id, text, media_path, interval_seconds FROM schedules WHERE is_recurring = 1")
            recurring = await cursor.fetchall()
            for chat_id, text, media_path, seconds in recurring:
                async def recurring_task():
                    clients = await load_session_clients()
                    await send_message_with_session(clients, chat_id, text, media_path)
                schedule.every(seconds).seconds.do(lambda: asyncio.create_task(recurring_task()))
        await message.reply(f"Schedule {schedule_id} cancelled.")
        logger.info(f"Cancelled schedule {schedule_id}")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in cancelschedule command: {e}")

# Command to send a message with inline buttons
@bot.on_message(filters.command("buttons") & filters.user(ADMIN_ID))
async def send_buttons(client, message):
    try:
        _, chat_id, *text = message.text.split(maxsplit=2)
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        text = text[0] if text else "Choose an option:"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Confirm", callback_data="confirm")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")]
        ])
        clients = await load_session_clients()
        if clients:
            client = await get_next_client(clients)
            await client.send_message(chat_id, text, reply_markup=keyboard)
            await message.reply("Message with buttons sent!")
            logger.info(f"Sent message with buttons to {chat_id}")
        else:
            await message.reply("No active sessions.")
            logger.warning("No active sessions for buttons command")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in buttons command: {e}")

# Command to manage sessions
@bot.on_message(filters.command("managesessions") & filters.user(ADMIN_ID))
async def manage_sessions(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Session", callback_data="add_session")],
        [InlineKeyboardButton("Remove Session", callback_data="remove_session")]
    ])
    await message.reply("Manage sessions:", reply_markup=keyboard)
    logger.info("Admin requested to manage sessions")

# Command to check bot status
@bot.on_message(filters.command("status") & filters.user(ADMIN_ID))
async def check_status(client, message):
    try:
        clients = await load_session_clients()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM schedules")
            schedule_count = (await cursor.fetchone())[0]
        sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith(".session")]
        response = (
            f"Bot Status:\n"
            f"Active Sessions: {len(clients)}/{len(sessions)}\n"
            f"Total Schedules: {schedule_count}\n"
            f"Uptime: {datetime.now() - bot_start_time}"
        )
        await message.reply(response)
        logger.info("Status command executed")
    except Exception as e:
        await message.reply(f"Error: {e}")
        logger.error(f"Error in status command: {e}")

# Handle button callbacks
@bot.on_callback_query()
async def handle_callback(client, callback_query):
    data = callback_query.data
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("This is a private bot.")
        logger.info(f"Non-admin {callback_query.from_user.id} attempted callback")
        return
    try:
        if data == "confirm":
            await callback_query.message.edit_text("Confirmed!")
            logger.info("Confirm button clicked")
        elif data == "cancel":
            await callback_query.message.edit_text("Cancelled!")
            logger.info("Cancel button clicked")
        elif data == "add_session":
            await callback_query.message.edit_text("Use /addsession to upload a .session file.")
            logger.info("Add session button clicked")
        elif data == "remove_session":
            sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith(".session")]
            if not sessions:
                await callback_query.message.edit_text("No sessions available to remove.")
                logger.info("No sessions available to remove")
                return
            buttons = [[InlineKeyboardButton(s, callback_data=f"delete_{s}")] for s in sessions]
            keyboard = InlineKeyboardMarkup(buttons)
            await callback_query.message.edit_text("Select a session to remove:", reply_markup=keyboard)
            logger.info("Remove session button clicked")
        elif data.startswith("delete_"):
            session_file = data[len("delete_"):]
            session_path = os.path.join(SESSION_DIR, session_file)
            try:
                os.remove(session_path)
                await callback_query.message.edit_text(f"Session {session_file} removed!")
                logger.info(f"Removed session {session_file}")
            except Exception as e:
                await callback_query.message.edit_text(f"Error removing session: {e}")
                logger.error(f"Error removing session {session_file}: {e}")
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in callback handler: {e}")

# Global start time for uptime tracking
bot_start_time = datetime.now()

# Shutdown handler
async def shutdown(clients: List[Client]):
    logger.info("Shutting down bot...")
    for client in clients:
        try:
            await client.stop()
            logger.info(f"Stopped client {client.name}")
        except Exception as e:
            logger.error(f"Error stopping client {client.name}: {e}")
    await bot.stop()
    logger.info("Bot stopped")

# Signal handler for graceful shutdown
def handle_shutdown(loop, clients):
    logger.info("Received shutdown signal")
    asyncio.run_coroutine_threadsafe(shutdown(clients), loop)
    tasks = [task for task in asyncio.all_tasks(loop) if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    logger.info("Event loop closed")
    exit(0)

# Main function to run bot and scheduler
async def main():
    global clients
    clients = []
    try:
        await init_db()
        # Load recurring schedules from database
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT chat_id, text, media_path, interval_seconds FROM schedules WHERE is_recurring = 1")
            recurring = await cursor.fetchall()
            for chat_id, text, media_path, seconds in recurring:
                async def recurring_task():
                    clients = await load_session_clients()
                    await send_message_with_session(clients, chat_id, text, media_path)
                schedule.every(seconds).seconds.do(lambda: asyncio.create_task(recurring_task()))
        await bot.start()
        clients = await load_session_clients()
        logger.info("Bot started")
        # Set up signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: handle_shutdown(loop, clients))
        while True:
            try:
                await check_scheduled_messages()
                schedule.run_pending()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                logger.info("Main loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
    finally:
        await shutdown(clients)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as e:
        logger.error(f"Startup error: {e}")