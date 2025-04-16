import asyncio
import logging
import random
import sqlite3
import os
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import importlib.metadata
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import ReportSpamRequest
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.types import InputPeerUser
import re
import aiosqlite
from functools import lru_cache

# --- Version Check ---
try:
    aiogram_version = importlib.metadata.version("aiogram")
    if tuple(map(int, aiogram_version.split('.'))) < (3, 7, 0):
        logging.warning(f"aiogram version {aiogram_version} detected. Recommend upgrading to >=3.7.0 for full compatibility.")
except importlib.metadata.PackageNotFoundError:
    logging.error("aiogram not installed. Please install with 'pip install aiogram>=3.7.0'.")
    exit(1)

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
GROUP_ID = -1002431056179
CHANNEL_ID = -1002288539987
ADMIN_IDS = [7584086775]
MAX_REPORTS_PER_DAY = 3
MAX_CAPTCHA_ATTEMPTS = 3
MAX_SESSION_UPLOADS_PER_DAY = 5
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
ALLOWED_SESSION_EXTENSION = '.session'
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_FRAUD_DETAIL_LENGTH = 1000
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # seconds
USERNAME_CHECK_INTERVAL = 3600  # seconds (1 hour)
MESSAGE_DELETE_DELAY = 600  # seconds (10 minutes)
CAPTCHA_DELETE_DELAY = 900  # seconds (15 minutes)
REPORT_STATUS_UPDATE_INTERVAL = 600  # seconds (10 minutes)
FRAUD_REPORT_INTERVAL = 1800  # seconds (30 minutes)
SESSION_DIR = "sessions"
DB_PATH = "reports.db"
ANALYTICS_CACHE_TTL = 300  # seconds (5 minutes)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Ensure Session Directory ---
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# --- Bot Setup ---
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())

# --- SQLite Setup with Connection Pooling ---
async def init_db():
    db = await aiosqlite.connect(DB_PATH, check_same_thread=False)
    async with db.execute('PRAGMA journal_mode=WAL;'):
        pass
    async with db.cursor() as cursor:
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                fraud_username TEXT,
                fraud_user_id INTEGER,
                fraud TEXT,
                contact TEXT,
                photo_id TEXT,
                status TEXT DEFAULT 'pending',
                admin_notes TEXT,
                notified INTEGER DEFAULT 0,
                telegram_reported INTEGER DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER PRIMARY KEY,
                report_count INTEGER DEFAULT 0,
                last_report_date TEXT
            )
        """)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_limits (
                admin_id INTEGER PRIMARY KEY,
                upload_count INTEGER DEFAULT 0,
                last_upload_date TEXT
            )
        """)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS fraud_usernames (
                fraud_username TEXT,
                fraud_user_id INTEGER,
                report_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (fraud_username, report_id)
            )
        """)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_stats (
                session_hash TEXT PRIMARY KEY,
                last_used DATETIME,
                use_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0
            )
        """)
        await cursor.execute("CREATE INDEX IF NOT EXISTS idx_fraud_user_id ON fraud_usernames(fraud_user_id)")
        await cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_status ON reports(status)")
        await cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_telegram_reported ON reports(telegram_reported)")
        await cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_stats_last_used ON session_stats(last_used)")
    await db.commit()
    return db

# --- FSM States ---
class ReportStates(StatesGroup):
    captcha = State()
    fraud_username = State()
    fraud_detail = State()
    proof = State()
    contact = State()
    confirm = State()

class AdminReviewStates(StatesGroup):
    select_report = State()
    update_status = State()
    add_notes = State()
    upload_session = State()

# --- Utility Functions ---
def generate_captcha() -> tuple[int, int, str, str]:
    num1, num2 = random.randint(10, 99), random.randint(10, 99)
    operations = ['+', '-', '*']
    op = random.choice(operations)
    answer = str(eval(f"{num1} {op} {num2}"))
    return num1, num2, op, answer

def generate_report_id(user_id: int, timestamp: str) -> str:
    return hashlib.sha256(f"{user_id}{timestamp}".encode()).hexdigest()[:16]

def hash_filename(filename: str, admin_id: int) -> str:
    return hashlib.sha256(f"{filename}{admin_id}{datetime.now().isoformat()}".encode()).hexdigest()[:16] + ALLOWED_SESSION_EXTENSION

def validate_username(username: str) -> bool:
    return bool(re.match(r'^@[\w]{5,32}$', username))

def validate_contact(contact: str) -> bool:
    if contact.startswith('@'):
        return bool(re.match(r'^@[\w]{5,32}$', contact))
    return bool(re.match(r'^\+?\d{10,15}$', contact))

async def check_user_limit(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.cursor() as cursor:
        await cursor.execute("SELECT report_count, last_report_date FROM user_limits WHERE user_id = ?", (user_id,))
        result = await cursor.fetchone()
        today = datetime.now().strftime('%Y-%m-%d')
        
        if not result:
            await cursor.execute(
                "INSERT INTO user_limits (user_id, report_count, last_report_date) VALUES (?, 0, ?)",
                (user_id, today)
            )
            await db.commit()
            return True
        
        count, last_date = result
        if last_date != today:
            await cursor.execute(
                "UPDATE user_limits SET report_count = 0, last_report_date = ? WHERE user_id = ?",
                (today, user_id)
            )
            count = 0
            await db.commit()
        
        return count < MAX_REPORTS_PER_DAY

async def check_session_limit(db: aiosqlite.Connection, admin_id: int) -> bool:
    async with db.cursor() as cursor:
        await cursor.execute("SELECT upload_count, last_upload_date FROM session_limits WHERE admin_id = ?", (admin_id,))
        result = await cursor.fetchone()
        today = datetime.now().strftime('%Y-%m-%d')
        
        if not result:
            await cursor.execute(
                "INSERT INTO session_limits (admin_id, upload_count, last_upload_date) VALUES (?, 0, ?)",
                (admin_id, today)
            )
            await db.commit()
            return True
        
        count, last_date = result
        if last_date != today:
            await cursor.execute(
                "UPDATE session_limits SET upload_count = 0, last_upload_date = ? WHERE admin_id = ?",
                (today, admin_id)
            )
            count = 0
            await db.commit()
        
        return count < MAX_SESSION_UPLOADS_PER_DAY

async def increment_user_limit(db: aiosqlite.Connection, user_id: int):
    async with db.cursor() as cursor:
        await cursor.execute(
            "UPDATE user_limits SET report_count = report_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def increment_session_limit(db: aiosqlite.Connection, admin_id: int):
    async with db.cursor() as cursor:
        await cursor.execute(
            "UPDATE session_limits SET upload_count = upload_count + 1 WHERE admin_id = ?",
            (admin_id,)
        )
        await db.commit()

async def update_session_stats(db: aiosqlite.Connection, session_hash: str, success: bool):
    async with db.cursor() as cursor:
        await cursor.execute(
            "INSERT OR IGNORE INTO session_stats (session_hash, last_used, use_count) "
            "VALUES (?, ?, 0)",
            (session_hash, datetime.now())
        )
        if success:
            await cursor.execute(
                "UPDATE session_stats SET last_used = ?, use_count = use_count + 1, success_count = success_count + 1 "
                "WHERE session_hash = ?",
                (datetime.now(), session_hash)
            )
        else:
            await cursor.execute(
                "UPDATE session_stats SET last_used = ?, use_count = use_count + 1, failure_count = failure_count + 1 "
                "WHERE session_hash = ?",
                (datetime.now(), session_hash)
            )
        await db.commit()

async def retry_api_call(coro, max_attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY):
    for attempt in range(max_attempts):
        try:
            return await coro
        except (TelegramNetworkError, TelegramBadRequest) as e:
            if attempt == max_attempts - 1:
                logger.error(f"Max retries reached: {e}")
                raise
            wait = delay * (2 ** attempt)
            logger.warning(f"API error on attempt {attempt + 1}: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
    raise Exception("Unexpected retry failure")

async def safe_message_action(
    message: Message,
    action: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs
) -> Message:
    try:
        if action == "edit_text" and message.text is None:
            return await retry_api_call(message.answer(text, reply_markup=reply_markup, **kwargs))
        return await retry_api_call(getattr(message, action)(text, reply_markup=reply_markup, **kwargs))
    except TelegramBadRequest as e:
        logger.warning(f"Failed to {action} message: {e}")
        return await retry_api_call(message.answer(text, reply_markup=reply_markup, **kwargs))

async def delete_message_later(message: Message, delay: float = MESSAGE_DELETE_DELAY):
    try:
        await asyncio.sleep(delay)
        await retry_api_call(message.delete())
        logger.info(f"Deleted message {message.message_id} from chat {message.chat.id}")
    except Exception as e:
        logger.warning(f"Failed to delete message {message.message_id}: {e}")

async def check_fraud_username_changes(db: aiosqlite.Connection):
    while True:
        try:
            async with db.cursor() as cursor:
                await cursor.execute("SELECT DISTINCT fraud_username, fraud_user_id FROM fraud_usernames")
                fraudsters = await cursor.fetchall()
            for username, user_id in fraudsters:
                if not user_id:
                    continue
                try:
                    chat = await bot.get_chat(user_id)
                    current_username = f"@{chat.username}" if chat.username else None
                    if current_username and current_username != username:
                        async with db.cursor() as cursor:
                            await cursor.execute(
                                "INSERT OR REPLACE INTO fraud_usernames (fraud_username, fraud_user_id, report_id, timestamp) "
                                "VALUES (?, ?, ?, ?)",
                                (current_username, user_id, generate_report_id(user_id, datetime.now().isoformat()), datetime.now())
                            )
                            await db.commit()
                        try:
                            msg = await bot.send_message(
                                user_id,
                                "🚨 Username change detected. Under investigation."
                            )
                            await delete_message_later(msg, delay=600)
                            logger.info(f"Sent warning to user {user_id} ({current_username})")
                        except TelegramBadRequest as e:
                            logger.warning(f"Failed to message user {user_id}: {e}")
                except TelegramBadRequest as e:
                    logger.warning(f"Failed to fetch chat for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Error in username check: {e}")
        await asyncio.sleep(USERNAME_CHECK_INTERVAL)

async def send_status_updates(db: aiosqlite.Connection):
    while True:
        try:
            async with db.cursor() as cursor:
                await cursor.execute(
                    "SELECT user_id, report_id, status, admin_notes FROM reports WHERE status != 'pending' AND notified = 0"
                )
                reports = await cursor.fetchall()
            for user_id, report_id, status, admin_notes in reports:
                try:
                    msg = await bot.send_message(
                        user_id,
                        f"📢 <b>Report Update</b> | ID: {report_id}\n"
                        f"Status: <b>{status}</b>\n"
                        f"Notes: {admin_notes or 'None'}\n\n"
                        "Thanks for your help!"
                    )
                    async with db.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE reports SET notified = 1 WHERE report_id = ?",
                            (report_id,)
                        )
                        await db.commit()
                    await delete_message_later(msg)
                    logger.info(f"Sent update for report {report_id} to user {user_id}")
                except TelegramBadRequest as e:
                    logger.warning(f"Failed to send update to user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Error in status updates: {e}")
        await asyncio.sleep(REPORT_STATUS_UPDATE_INTERVAL)

async def report_fraud_to_telegram(db: aiosqlite.Connection):
    while True:
        try:
            async with db.cursor() as cursor:
                await cursor.execute(
                    "SELECT session_hash, failure_count FROM session_stats ORDER BY failure_count ASC, last_used DESC"
                )
                session_stats = await cursor.fetchall()
                session_priority = [s[0] for s in session_stats if s[0] in os.listdir(SESSION_DIR)]
                if not session_priority:
                    session_priority = [f for f in os.listdir(SESSION_DIR) if f.endswith(ALLOWED_SESSION_EXTENSION)]
            
            if not session_priority:
                logger.warning("No valid sessions available for fraud reporting")
                await asyncio.sleep(FRAUD_REPORT_INTERVAL)
                continue

            async with db.cursor() as cursor:
                await cursor.execute(
                    "SELECT r.report_id, r.fraud_user_id, r.fraud_username, r.fraud, r.photo_id "
                    "FROM reports r WHERE r.telegram_reported = 0 AND r.fraud_user_id IS NOT NULL LIMIT 5"
                )
                reports = await cursor.fetchall()
            
            if not reports:
                await asyncio.sleep(FRAUD_REPORT_INTERVAL)
                continue

            tasks = []
            for report_id, fraud_user_id, fraud_username, fraud_detail, photo_id in reports:
                session_file = session_priority.pop(0) if session_priority else random.choice(session_priority)
                session_priority.append(session_file)  # Rotate back
                session_path = os.path.join(SESSION_DIR, session_file)
                
                async def process_report():
                    success = False
                    try:
                        async with TelegramClient(session_path, API_ID, API_HASH) as client:
                            await client.start()
                            try:
                                await client(ReportSpamRequest(peer=InputPeerUser(user_id=fraud_user_id, access_hash=0)))
                                await client(ReportRequest(
                                    peer=InputPeerUser(user_id=fraud_user_id, access_hash=0),
                                    message=f"Fraud Report ID: {report_id}\nUsername: {fraud_username}\nDetails: {fraud_detail}",
                                    reason="spam"
                                ))
                                async with db.cursor() as cursor:
                                    await cursor.execute(
                                        "UPDATE reports SET telegram_reported = 1 WHERE report_id = ?",
                                        (report_id,)
                                    )
                                    await db.commit()
                                success = True
                                logger.info(f"Reported user {fraud_user_id} ({fraud_username}) for report {report_id}")
                                
                                async with db.cursor() as cursor:
                                    await cursor.execute("SELECT user_id FROM reports WHERE report_id = ?", (report_id,))
                                    user_id = (await cursor.fetchone())[0]
                                try:
                                    msg = await bot.send_message(
                                        user_id,
                                        f"📢 <b>Update on Report {report_id}</b>\n"
                                        "Action taken. Thanks for keeping Telegram safe!"
                                    )
                                    await delete_message_later(msg)
                                except TelegramBadRequest as e:
                                    logger.warning(f"Failed to notify user {user_id}: {e}")
                            except Exception as e:
                                logger.error(f"Failed to report user {fraud_user_id} for report {report_id}: {e}")
                    except Exception as e:
                        logger.error(f"Failed to use session {session_file} for report {report_id}: {e}")
                    await update_session_stats(db, session_file, success)

                tasks.append(process_report())

            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error in fraud reporting: {e}")
        await asyncio.sleep(FRAUD_REPORT_INTERVAL)

# --- Analytics ---
@lru_cache(maxsize=128)
async def get_analytics(db: aiosqlite.Connection, cache_key: str = "global") -> Dict:
    async with db.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM reports")
        total_reports = (await cursor.fetchone())[0]
        await cursor.execute("SELECT COUNT(*) FROM reports WHERE status = 'pending'")
        pending_reports = (await cursor.fetchone())[0]
        await cursor.execute("SELECT COUNT(*) FROM reports WHERE status = 'resolved'")
        resolved_reports = (await cursor.fetchone())[0]
        await cursor.execute("SELECT COUNT(*) FROM reports WHERE telegram_reported = 1")
        reported_to_telegram = (await cursor.fetchone())[0]
        await cursor.execute(
            "SELECT fraud_username, COUNT(*) as count FROM fraud_usernames GROUP BY fraud_username ORDER BY count DESC LIMIT 5"
        )
        top_fraudsters = await cursor.fetchall()
        await cursor.execute(
            "SELECT session_hash, success_count, failure_count FROM session_stats ORDER BY use_count DESC LIMIT 5"
        )
        session_stats = await cursor.fetchall()
    
    return {
        "total_reports": total_reports,
        "pending_reports": pending_reports,
        "resolved_reports": resolved_reports,
        "reported_to_telegram": reported_to_telegram,
        "top_fraudsters": [(username, count) for username, count in top_fraudsters],
        "session_stats": [
            {"session": session[:8], "success": success, "failure": failure}
            for session, success, failure in session_stats
        ],
        "timestamp": datetime.now().isoformat()
    }

# --- Inline Buttons ---
def start_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Report Fraud", callback_data="start_report")],
        [InlineKeyboardButton(text="📜 My Reports", callback_data="view_reports")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="show_help")]
    ])

def confirm_buttons(report_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Submit", callback_data=f"confirm_report_{report_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_report")]
    ])

def captcha_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 New CAPTCHA", callback_data="resend_captcha")]
    ])

def help_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_start")]
    ])

def admin_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Review Reports", callback_data="review_reports")],
        [InlineKeyboardButton(text="📊 Dashboard", callback_data="view_dashboard")],
        [InlineKeyboardButton(text="📥 Upload Session", callback_data="upload_session")],
        [InlineKeyboardButton(text="🗑️ Manage Sessions", callback_data="manage_sessions")]
    ])

def report_status_buttons(report_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Resolve", callback_data=f"update_status_{report_id}_resolved")],
        [InlineKeyboardButton(text="🔍 Review", callback_data=f"update_status_{report_id}_under_review")],
        [InlineKeyboardButton(text="📝 Notes", callback_data=f"add_notes_{report_id}")],
        [InlineKeyboardButton(text="🔙 Reports", callback_data="review_reports")]
    ])

def report_list_buttons(reports: List[tuple]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            text=f"Report {report_id[:8]}... ({status})",
            callback_data=f"select_report_{report_id}"
        )] for report_id, status in reports
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def session_manage_buttons(sessions: List[str]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            text=f"Session {session[:8]}...",
            callback_data=f"delete_session_{session}"
        )] for session in sessions
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Handlers ---
@dp.message(CommandStart())
async def handle_start(msg: Message, state: FSMContext):
    welcome_text = (
        f"👋 Hello <b>{msg.from_user.full_name}</b>! Welcome to the Fraud Report Bot.\n\n"
        "🔍 Report scams with 'Report Fraud'.\n"
        "📜 Track reports with 'My Reports'.\n"
        "ℹ️ Get help with 'Help'.\n\n"
        "Let's keep Telegram safe! 😊"
    )
    sent_msg = await safe_message_action(msg, "answer", welcome_text, reply_markup=start_buttons())
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.info(f"User {msg.from_user.id} started bot")

@dp.message(Command("cancel"), StateFilter(ReportStates, AdminReviewStates))
async def handle_cancel(msg: Message, state: FSMContext):
    sent_msg = await safe_message_action(
        msg, "answer",
        "❌ Action cancelled. Back to main menu! 😊",
        reply_markup=start_buttons()
    )
    await state.clear()
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.info(f"User {msg.from_user.id} cancelled action")

@dp.message(Command('admin'), lambda msg: msg.from_user.id in ADMIN_IDS)
async def handle_admin(msg: Message):
    admin_text = (
        "🛠️ <b>Admin Dashboard</b>\n\n"
        "Choose an action:\n"
        "- 📋 Review reports\n"
        "- 📊 View analytics\n"
        "- 📥 Upload sessions\n"
        "- 🗑️ Manage sessions"
    )
    sent_msg = await safe_message_action(msg, "answer", admin_text, reply_markup=admin_buttons())
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.info(f"Admin {msg.from_user.id} accessed dashboard")

@dp.callback_query(F.data == "admin_menu")
async def admin_menu(cb: CallbackQuery):
    admin_text = (
        "🛠️ <b>Admin Dashboard</b>\n\n"
        "Choose an action:\n"
        "- 📋 Review reports\n"
        "- 📊 View analytics\n"
        "- 📥 Upload sessions\n"
        "- 🗑️ Manage sessions"
    )
    sent_msg = await safe_message_action(cb.message, "edit_text", admin_text, reply_markup=admin_buttons())
    await delete_message_later(sent_msg)
    logger.info(f"Admin {cb.from_user.id} returned to dashboard")

@dp.callback_query(F.data == "view_dashboard")
async def handle_dashboard(cb: CallbackQuery, db: aiosqlite.Connection = None):
    analytics = await get_analytics(db)
    top_fraudsters = "\n".join([f"- {username}: {count} reports" for username, count in analytics["top_fraudsters"]])
    session_stats = "\n".join([
        f"- Session {s['session']}...: {s['success']} successes, {s['failure']} failures"
        for s in analytics["session_stats"]
    ])
    dashboard_text = (
        f"📊 <b>Analytics Dashboard</b>\n\n"
        f"Total Reports: {analytics['total_reports']}\n"
        f"Pending: {analytics['pending_reports']}\n"
        f"Resolved: {analytics['resolved_reports']}\n"
        f"Reported to Telegram: {analytics['reported_to_telegram']}\n\n"
        f"<b>Top Fraudsters</b>:\n{top_fraudsters or 'None'}\n\n"
        f"<b>Session Performance</b>:\n{session_stats or 'None'}\n\n"
        f"Last Updated: {analytics['timestamp']}"
    )
    sent_msg = await safe_message_action(cb.message, "edit_text", dashboard_text, reply_markup=admin_buttons())
    await delete_message_later(sent_msg)
    logger.info(f"Admin {cb.from_user.id} viewed dashboard")

@dp.callback_query(F.data == "review_reports")
async def review_reports(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    async with db.cursor() as cursor:
        await cursor.execute("SELECT report_id, status FROM reports WHERE status IN ('pending', 'under_review') LIMIT 10")
        reports = await cursor.fetchall()
    if not reports:
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            "🎉 No pending reports! All caught up.",
            reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)
        return
    report_list_text = "📋 <b>Pending Reports</b>\n\nSelect a report:"
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        report_list_text, reply_markup=report_list_buttons(reports)
    )
    await delete_message_later(sent_msg)
    await state.set_state(AdminReviewStates.select_report)
    logger.info(f"Admin {cb.from_user.id} started reviewing reports")

@dp.callback_query(F.data.startswith("select_report_"), StateFilter(AdminReviewStates.select_report))
async def select_report(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    report_id = cb.data.split("_")[2]
    async with db.cursor() as cursor:
        await cursor.execute(
            "SELECT user_id, username, fraud_username, fraud, contact, photo_id, status, admin_notes, telegram_reported "
            "FROM reports WHERE report_id = ?",
            (report_id,)
        )
        report = await cursor.fetchone()
    if not report:
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            "⚠️ Report not found.", reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)
        return
    user_id, username, fraud_username, fraud, contact, photo_id, status, admin_notes, telegram_reported = report
    report_text = (
        f"<b>Report Details</b> | ID: {report_id}\n\n"
        f"<b>User:</b> @{username or 'NoUsername'} | ID: {user_id}\n"
        f"<b>Fraudster:</b> {fraud_username or 'Unknown'}\n"
        f"<b>Details:</b> {fraud}\n"
        f"<b>Contact:</b> {contact}\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Notes:</b> {admin_notes or 'None'}\n"
        f"<b>Reported:</b> {'Yes' if telegram_reported else 'No'}\n\n"
        "Choose an action:"
    )
    sent_msg = await retry_api_call(cb.message.answer_photo(
        photo=photo_id,
        caption=report_text,
        reply_markup=report_status_buttons(report_id)
    ))
    await delete_message_later(sent_msg, delay=600)
    await state.update_data(report_id=report_id)
    logger.info(f"Admin {cb.from_user.id} selected report {report_id}")

@dp.callback_query(F.data.startswith("update_status_"), StateFilter(AdminReviewStates.select_report))
async def update_status(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    report_id = cb.data.split("_")[2]
    new_status = cb.data.split("_")[3]
    async with db.cursor() as cursor:
        await cursor.execute(
            "UPDATE reports SET status = ?, notified = 0 WHERE report_id = ?",
            (new_status, report_id)
        )
        await db.commit()
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        f"✅ Report {report_id[:8]}... updated to <b>{new_status}</b>.",
        reply_markup=report_status_buttons(report_id)
    )
    await delete_message_later(sent_msg)
    logger.info(f"Admin {cb.from_user.id} updated report {report_id} to {new_status}")

@dp.callback_query(F.data.startswith("add_notes_"), StateFilter(AdminReviewStates.select_report))
async def add_notes_prompt(cb: CallbackQuery, state: FSMContext):
    report_id = cb.data.split("_")[2]
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        f"📝 Enter notes for report {report_id[:8]}... (max 500 chars):"
    )
    await delete_message_later(sent_msg)
    await state.update_data(report_id=report_id)
    await state.set_state(AdminReviewStates.add_notes)
    logger.info(f"Admin {cb.from_user.id} prompted notes for report {report_id}")

@dp.message(StateFilter(AdminReviewStates.add_notes))
async def handle_notes(msg: Message, state: FSMContext, db: aiosqlite.Connection = None):
    data = await state.get_data()
    report_id = data.get("report_id")
    if not report_id:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Session expired. Start again.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        await state.clear()
        return
    if len(msg.text) > 500:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Notes too long. Keep under 500 chars.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        return
    async with db.cursor() as cursor:
        await cursor.execute(
            "UPDATE reports SET admin_notes = ?, notified = 0 WHERE report_id = ?",
            (msg.text, report_id)
        )
        await db.commit()
    sent_msg = await safe_message_action(
        msg, "answer",
        f"✅ Notes added to report {report_id[:8]}...!",
        reply_markup=report_status_buttons(report_id)
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    await state.set_state(AdminReviewStates.select_report)
    logger.info(f"Admin {msg.from_user.id} added notes to report {report_id}")

@dp.callback_query(F.data == "upload_session")
async def upload_session_prompt(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    if not await check_session_limit(db, cb.from_user.id):
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            f"⚠️ Daily limit of {MAX_SESSION_UPLOADS_PER_DAY} uploads reached. Try tomorrow!",
            reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)
        logger.info(f"Admin {cb.from_user.id} exceeded session upload limit")
        return
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        "📥 Upload a .session file (Telethon format)."
    )
    await delete_message_later(sent_msg)
    await state.set_state(AdminReviewStates.upload_session)
    logger.info(f"Admin {cb.from_user.id} prompted to upload session")

@dp.message(StateFilter(AdminReviewStates.upload_session))
async def handle_session_upload(msg: Message, state: FSMContext, db: aiosqlite.Connection = None):
    if not msg.document:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Please upload a .session file.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"Admin {msg.from_user.id} sent non-document")
        return
    
    document = msg.document
    file_name = document.file_name
    if not file_name.lower().endswith(ALLOWED_SESSION_EXTENSION):
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Invalid file. Use .session format.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"Admin {msg.from_user.id} sent invalid session format")
        return

    hashed_name = hash_filename(file_name, msg.from_user.id)
    file_path = os.path.join(SESSION_DIR, hashed_name)
    await msg.document.download(destination_file=file_path)

    try:
        async with TelegramClient(file_path, API_ID, API_HASH) as client:
            await client.start()
            logger.info(f"Session file {hashed_name} validated")
    except Exception as e:
        os.remove(file_path)
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Invalid session file. Upload a valid .session.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.error(f"Failed to validate session from admin {msg.from_user.id}: {e}")
        return

    async with db.cursor() as cursor:
        await cursor.execute(
            "INSERT OR IGNORE INTO session_stats (session_hash, last_used, use_count) VALUES (?, ?, 0)",
            (hashed_name, datetime.now())
        )
        await db.commit()
    await increment_session_limit(db, msg.from_user.id)
    sent_msg = await safe_message_action(
        msg, "answer",
        "✅ Session uploaded successfully!",
        reply_markup=admin_buttons()
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.info(f"Admin {msg.from_user.id} uploaded session {hashed_name}")
    await state.clear()

@dp.callback_query(F.data == "manage_sessions")
async def manage_sessions(cb: CallbackQuery, db: aiosqlite.Connection = None):
    sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith(ALLOWED_SESSION_EXTENSION)]
    if not sessions:
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            "📭 No sessions available.",
            reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)
        return
    async with db.cursor() as cursor:
        await cursor.execute(
            "SELECT session_hash, success_count, failure_count FROM session_stats WHERE session_hash IN ({})".format(
                ','.join('?' for _ in sessions)
            ),
            sessions
        )
        stats = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
    session_text = "🗑️ <b>Manage Sessions</b>\n\n"
    for session in sessions:
        success, failure = stats.get(session, (0, 0))
        session_text += f"Session {session[:8]}...: {success} successes, {failure} failures\n"
    session_text += "\nSelect a session to delete:"
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        session_text,
        reply_markup=session_manage_buttons(sessions)
    )
    await delete_message_later(sent_msg)
    logger.info(f"Admin {cb.from_user.id} viewed sessions")

@dp.callback_query(F.data.startswith("delete_session_"))
async def delete_session(cb: CallbackQuery, db: aiosqlite.Connection = None):
    session_file = cb.data.split("_")[2]
    file_path = os.path.join(SESSION_DIR, session_file)
    if os.path.exists(file_path):
        os.remove(file_path)
        async with db.cursor() as cursor:
            await cursor.execute("DELETE FROM session_stats WHERE session_hash = ?", (session_file,))
            await db.commit()
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            f"✅ Session {session_file[:8]}... deleted.",
            reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)
        logger.info(f"Admin {cb.from_user.id} deleted session {session_file}")
    else:
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            "⚠️ Session not found.",
            reply_markup=admin_buttons()
        )
        await delete_message_later(sent_msg)

@dp.callback_query(F.data == "view_reports")
async def view_user_reports(cb: CallbackQuery, db: aiosqlite.Connection = None):
    async with db.cursor() as cursor:
        await cursor.execute(
            "SELECT report_id, fraud_username, status, timestamp, telegram_reported "
            "FROM reports WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5",
            (cb.from_user.id,)
        )
        reports = await cursor.fetchall()
    if not reports:
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            "📭 No reports submitted. Start with 'Report Fraud'!",
            reply_markup=start_buttons()
        )
        await delete_message_later(sent_msg)
        return
    report_text = "<b>Your Reports</b>\n\n"
    for report_id, fraud_username, status, timestamp, telegram_reported in reports:
        report_text += (
            f"📋 <b>ID:</b> {report_id[:8]}...\n"
            f"👤 <b>Fraudster:</b> {fraud_username or 'Unknown'}\n"
            f"📊 <b>Status:</b> {status}\n"
            f"🕒 <b>Date:</b> {timestamp}\n"
            f"🚨 <b>Reported:</b> {'Yes' if telegram_reported else 'No'}\n\n"
        )
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        report_text, reply_markup=start_buttons()
    )
    await delete_message_later(sent_msg)
    logger.info(f"User {cb.from_user.id} viewed reports")

@dp.callback_query(F.data == "show_help")
async def handle_help(cb: CallbackQuery):
    help_text = (
        "ℹ️ <b>Fraud Report Guide</b>\n\n"
        "Here's how to use the bot:\n\n"
        "1. Tap 'Report Fraud' to start.\n"
        "2. Solve a CAPTCHA to verify.\n"
        "3. Enter fraudster's username (@username).\n"
        "4. Describe fraud (max 1000 chars).\n"
        "5. Upload JPG/PNG screenshot (max 10MB).\n"
        "6. Provide contact (username or phone).\n"
        "7. Confirm to submit.\n\n"
        "🔐 Data is secure, shared only with moderators.\n"
        "📜 Check 'My Reports' for status.\n"
        "❌ Use /cancel to stop.\n\n"
        "Need help? We're here! 😊"
    )
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        help_text, reply_markup=help_buttons()
    )
    await delete_message_later(sent_msg)
    logger.info(f"User {cb.from_user.id} viewed help")

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await handle_start(cb.message, state)

@dp.callback_query(F.data == "start_report")
async def handle_report_start(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    if not await check_user_limit(db, cb.from_user.id):
        sent_msg = await safe_message_action(
            cb.message, "edit_text",
            f"⚠️ Daily limit of {MAX_REPORTS_PER_DAY} reports reached. Try tomorrow!",
            reply_markup=start_buttons()
        )
        await delete_message_later(sent_msg)
        logger.info(f"User {cb.from_user.id} exceeded report limit")
        return

    async with db.cursor() as cursor:
        await cursor.execute("SELECT * FROM reports WHERE user_id = ? AND status = 'pending'", (cb.from_user.id,))
        if await cursor.fetchone():
            sent_msg = await safe_message_action(
                cb.message, "edit_text",
                "⚠️ Pending report exists. Wait for review.",
                reply_markup=start_buttons()
            )
            await delete_message_later(sent_msg)
            logger.info(f"User {cb.from_user.id} has pending report")
            return

    num1, num2, op, answer = generate_captcha()
    await state.update_data(captcha_answer=answer, captcha_attempts=0)
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        f"🔒 Solve CAPTCHA:\n<b>{num1} {op} {num2} = ?</b>\n\n"
        "Type answer or tap 'New CAPTCHA'.",
        reply_markup=captcha_buttons()
    )
    await delete_message_later(sent_msg, delay=CAPTCHA_DELETE_DELAY)
    await state.set_state(ReportStates.captcha)
    logger.info(f"User {cb.from_user.id} started report")

@dp.callback_query(F.data == "resend_captcha", StateFilter(ReportStates.captcha))
async def resend_captcha(cb: CallbackQuery, state: FSMContext):
    num1, num2, op, answer = generate_captcha()
    await state.update_data(captcha_answer=answer, captcha_attempts=0)
    sent_msg = await safe_message_action(
        cb.message, "edit_text",
        f"🔒 New CAPTCHA:\n<b>{num1} {op} {num2} = ?</b>\n\n"
        "Type answer or tap 'New CAPTCHA'.",
        reply_markup=captcha_buttons()
    )
    await delete_message_later(sent_msg, delay=CAPTCHA_DELETE_DELAY)
    logger.info(f"User {cb.from_user.id} requested new CAPTCHA")

@dp.message(StateFilter(ReportStates.captcha))
async def handle_captcha_answer(msg: Message, state: FSMContext):
    data = await state.get_data()
    captcha_answer = data.get("captcha_answer")
    captcha_attempts = data.get("captcha_attempts", 0)
    
    if not captcha_answer:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Session expired. Use /start.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        await state.clear()
        return

    if not msg.text or not msg.text.strip().replace('-', '').isdigit():
        captcha_attempts += 1
        if captcha_attempts >= MAX_CAPTCHA_ATTEMPTS:
            sent_msg = await safe_message_action(msg, "answer", "⚠️ Too many attempts. Use /start.")
            await delete_message_later(msg)
            await delete_message_later(sent_msg)
            await state.clear()
            logger.warning(f"User {msg.from_user.id} exceeded CAPTCHA attempts")
            return
        num1, num2, op, answer = generate_captcha()
        await state.update_data(captcha_answer=answer, captcha_attempts=captcha_attempts)
        sent_msg = await safe_message_action(
            msg, "answer",
            f"⚠️ Enter a number:\n<b>{num1} {op} {num2} = ?</b>\n"
            f"Attempts left: {MAX_CAPTCHA_ATTEMPTS - captcha_attempts}",
            reply_markup=captcha_buttons()
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg, delay=CAPTCHA_DELETE_DELAY)
        logger.warning(f"User {msg.from_user.id} sent invalid CAPTCHA input")
        return

    if msg.text.strip() == captcha_answer:
        sent_msg = await safe_message_action(
            msg, "answer",
            "✅ Correct! Enter fraudster's username (e.g., @BadUser)."
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        await state.set_state(ReportStates.fraud_username)
        logger.info(f"User {msg.from_user.id} passed CAPTCHA")
    else:
        captcha_attempts += 1
        if captcha_attempts >= MAX_CAPTCHA_ATTEMPTS:
            sent_msg = await safe_message_action(msg, "answer", "⚠️ Too many attempts. Use /start.")
            await delete_message_later(msg)
            await delete_message_later(sent_msg)
            await state.clear()
            logger.warning(f"User {msg.from_user.id} exceeded CAPTCHA attempts")
            return
        num1, num2, op, answer = generate_captcha()
        await state.update_data(captcha_answer=answer, captcha_attempts=captcha_attempts)
        sent_msg = await safe_message_action(
            msg, "answer",
            f"❌ Incorrect:\n<b>{num1} {op} {num2} = ?</b>\n"
            f"Attempts left: {MAX_CAPTCHA_ATTEMPTS - captcha_attempts}",
            reply_markup=captcha_buttons()
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg, delay=CAPTCHA_DELETE_DELAY)
        logger.warning(f"User {msg.from_user.id} failed CAPTCHA")

@dp.message(StateFilter(ReportStates.fraud_username))
async def handle_fraud_username(msg: Message, state: FSMContext):
    if not validate_username(msg.text):
        sent_msg = await safe_message_action(
            msg, "answer",
            "⚠️ Invalid username. Use @username format."
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent invalid username")
        return
    fraud_username = msg.text.strip()
    fraud_user_id = None
    try:
        chat = await bot.get_chat(fraud_username)
        fraud_user_id = chat.id
    except TelegramBadRequest as e:
        logger.warning(f"Could not fetch user ID for {fraud_username}: {e}")
    await state.update_data(fraud_username=fraud_username, fraud_user_id=fraud_user_id)
    sent_msg = await safe_message_action(
        msg, "answer",
        f"Got it. Describe fraud (max {MAX_FRAUD_DETAIL_LENGTH} chars)."
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    await state.set_state(ReportStates.fraud_detail)
    logger.info(f"User {msg.from_user.id} submitted username {fraud_username}")

@dp.message(StateFilter(ReportStates.fraud_detail))
async def handle_fraud_detail(msg: Message, state: FSMContext):
    if len(msg.text) > MAX_FRAUD_DETAIL_LENGTH:
        sent_msg = await safe_message_action(
            msg, "answer",
            f"⚠️ Too long. Keep under {MAX_FRAUD_DETAIL_LENGTH} chars."
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent long fraud detail")
        return
    await state.update_data(fraud_detail=msg.text)
    sent_msg = await safe_message_action(
        msg, "answer",
        "Thanks! Upload JPG/PNG proof (max 10MB)."
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    await state.set_state(ReportStates.proof)
    logger.info(f"User {msg.from_user.id} submitted fraud details")

@dp.message(StateFilter(ReportStates.proof))
async def handle_proof(msg: Message, state: FSMContext):
    if not msg.photo:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Send JPG/PNG image.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent non-image")
        return
    
    photo = msg.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    if file_info.file_size > MAX_IMAGE_SIZE:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Image too large. Use <10MB.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent oversized image")
        return
    
    file_ext = file_info.file_path.lower().split('.')[-1]
    if f'.{file_ext}' not in ALLOWED_IMAGE_EXTENSIONS:
        sent_msg = await safe_message_action(msg, "answer", "⚠️ Only JPG/PNG allowed.")
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent invalid image format")
        return

    await state.update_data(proof_id=photo.file_id)
    sent_msg = await safe_message_action(
        msg, "answer",
        "Great! Provide contact (username or phone)."
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    await state.set_state(ReportStates.contact)
    logger.info(f"User {msg.from_user.id} uploaded proof")

@dp.message(StateFilter(ReportStates.contact))
async def handle_contact(msg: Message, state: FSMContext):
    if not validate_contact(msg.text):
        sent_msg = await safe_message_action(
            msg, "answer",
            "⚠️ Invalid contact. Use @username or phone."
        )
        await delete_message_later(msg)
        await delete_message_later(sent_msg)
        logger.warning(f"User {msg.from_user.id} sent invalid contact")
        return
    
    await state.update_data(contact=msg.text)
    data = await state.get_data()
    report_id = generate_report_id(msg.from_user.id, datetime.now().isoformat())
    await state.update_data(report_id=report_id)
    
    fraud_username = data.get("fraud_username", "Unknown")
    preview = (
        f"<b>Report Preview</b>\n\n"
        f"<b>Username:</b> @{msg.from_user.username or 'NoUsername'}\n"
        f"<b>Fraudster:</b> {fraud_username}\n"
        f"<b>Details:</b> {data.get('fraud_detail', '')[:100]}...\n"
        f"<b>Contact:</b> {data.get('contact', '')}\n\n"
        "Confirm to submit?"
    )
    sent_msg = await retry_api_call(msg.answer_photo(
        photo=data['proof_id'],
        caption=preview,
        reply_markup=confirm_buttons(report_id)
    ))
    await delete_message_later(msg)
    await delete_message_later(sent_msg, delay=600)
    await state.set_state(ReportStates.confirm)
    logger.info(f"User {msg.from_user.id} submitted contact")

@dp.callback_query(F.data.startswith("confirm_report_"))
async def finish_report(cb: CallbackQuery, state: FSMContext, db: aiosqlite.Connection = None):
    data = await state.get_data()
    report_id = data.get("report_id")
    if not report_id or not data.get("proof_id"):
        sent_msg = await safe_message_action(
            cb.message, "answer",
            "⚠️ Incomplete data. Use /start.",
            reply_markup=start_buttons()
        )
        await delete_message_later(sent_msg)
        await state.clear()
        return
    
    fraud_username = data.get("fraud_username", "Unknown")
    async with db.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO reports (report_id, user_id, username, fraud_username, fraud_user_id, fraud, contact, photo_id, notified, telegram_reported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (
                report_id,
                cb.from_user.id,
                cb.from_user.username,
                fraud_username,
                data.get("fraud_user_id"),
                data.get("fraud_detail", ""),
                data.get("contact", ""),
                data["proof_id"]
            )
        )
        await cursor.execute(
            "INSERT OR IGNORE INTO fraud_usernames (fraud_username, fraud_user_id, report_id) VALUES (?, ?, ?)",
            (fraud_username, data.get("fraud_user_id"), report_id)
        )
        await db.commit()
    await increment_user_limit(db, cb.from_user.id)

    report_text = (
        f"<b>New Fraud Report</b> | ID: {report_id}\n\n"
        f"<b>User:</b> @{cb.from_user.username or 'NoUsername'} | ID: {cb.from_user.id}\n"
        f"<b>Fraudster:</b> {fraud_username}\n"
        f"<b>Details:</b> {data.get('fraud_detail', '')}\n"
        f"<b>Contact:</b> {data.get('contact', '')}"
    )
    
    for chat_id in [GROUP_ID, CHANNEL_ID]:
        sent_msg = await retry_api_call(bot.send_photo(
            chat_id=chat_id,
            photo=data["proof_id"],
            caption=report_text
        ))
        await delete_message_later(sent_msg, delay=86400)
    sent_msg = await safe_message_action(
        cb.message, "answer",
        "✅ Report submitted! We'll update you soon. Thanks! 😊",
        reply_markup=start_buttons()
    )
    await delete_message_later(sent_msg)
    logger.info(f"Report {report_id} submitted by user {cb.from_user.id}")

    for admin_id in ADMIN_IDS:
        try:
            admin_msg = await bot.send_message(
                admin_id,
                f"🚨 <b>New Report</b> | ID: {report_id}\n"
                f"User: @{cb.from_user.username or 'NoUsername'}\n"
                f"Fraudster: {fraud_username}\n"
                f"Use /admin to review."
            )
            await delete_message_later(admin_msg)
        except TelegramBadRequest as e:
            logger.warning(f"Failed to notify admin {admin_id}: {e}")
    
    await state.clear()

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(cb: CallbackQuery, state: FSMContext):
    sent_msg = await safe_message_action(
        cb.message, "answer",
        "❌ Report cancelled. Start anew! 😊",
        reply_markup=start_buttons()
    )
    await delete_message_later(sent_msg)
    await state.clear()
    logger.info(f"User {cb.from_user.id} cancelled report")

@dp.message(StateFilter(ReportStates, AdminReviewStates))
async def handle_unexpected_input(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    sent_msg = await safe_message_action(
        msg, "answer",
        f"⚠️ Unexpected input in {current_state or 'unknown'} state. Follow prompts or /cancel."
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.warning(f"User {msg.from_user.id} sent unexpected input in {current_state}")

@dp.message()
async def handle_unhandled_message(msg: Message):
    sent_msg = await safe_message_action(
        msg, "answer",
        "⚠️ Not sure what you mean. Use /start or /help."
    )
    await delete_message_later(msg)
    await delete_message_later(sent_msg)
    logger.warning(f"User {msg.from_user.id} sent unhandled message")

@dp.errors()
async def error_handler(update, exception):
    logger.error(f"Update {update} caused error: {exception}")
    if hasattr(update, 'message') and isinstance(update.message, Message):
        sent_msg = await safe_message_action(
            update.message, "answer",
            "⚠️ Something broke! Try again later."
        )
        await delete_message_later(sent_msg)
    elif hasattr(update, 'callback_query') and isinstance(update.callback_query, CallbackQuery):
        sent_msg = await safe_message_action(
            update.callback_query.message, "answer",
            "⚠️ Something broke! Try again later."
        )
        await delete_message_later(sent_msg)
    return True

# --- Main ---
async def main():
    db = await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot started successfully")
        asyncio.create_task(check_fraud_username_changes(db))
        asyncio.create_task(send_status_updates(db))
        asyncio.create_task(report_fraud_to_telegram(db))
        await dp.start_polling(bot, db=db)
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
        raise
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())