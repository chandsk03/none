import asyncio
import logging
import random
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
import re
import hashlib

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
GROUP_ID = -1002431056179
CHANNEL_ID = -1002288539987
ADMIN_IDS = [7584086775]
MAX_REPORTS_PER_DAY = 3
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_FRAUD_DETAIL_LENGTH = 1000
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # seconds
USERNAME_CHECK_INTERVAL = 3600  # seconds (1 hour)

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

# --- Bot Setup ---
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# --- SQLite Setup ---
def init_db():
    conn = sqlite3.connect("reports.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            fraud_username TEXT,
            fraud TEXT,
            contact TEXT,
            photo_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_limits (
            user_id INTEGER PRIMARY KEY,
            report_count INTEGER DEFAULT 0,
            last_report_date TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fraud_usernames (
            fraud_username TEXT,
            fraud_user_id INTEGER,
            report_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (fraud_username, report_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fraud_user_id ON fraud_usernames(fraud_user_id)")
    conn.commit()
    return conn, cursor

conn, cursor = init_db()

# --- FSM States ---
class ReportStates(StatesGroup):
    captcha = State()
    fraud_username = State()
    fraud_detail = State()
    proof = State()
    contact = State()
    confirm = State()

# --- Utility Functions ---
def generate_captcha():
    num1, num2 = random.randint(10, 99), random.randint(10, 99)
    operations = ['+', '-', '*']
    op = random.choice(operations)
    if op == '+':
        answer = str(num1 + num2)
    elif op == '-':
        answer = str(num1 - num2)
    else:
        answer = str(num1 * num2)
    return num1, num2, op, answer

def generate_report_id(user_id, timestamp):
    return hashlib.sha256(f"{user_id}{timestamp}".encode()).hexdigest()[:16]

def validate_username(username):
    return bool(re.match(r'^@[\w]{5,32}$', username))

def validate_contact(contact):
    if contact.startswith('@'):
        return bool(re.match(r'^@[\w]{5,32}$', contact))
    return bool(re.match(r'^\+?\d{10,15}$', contact))

async def check_user_limit(user_id):
    cursor.execute("SELECT report_count, last_report_date FROM user_limits WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    today = datetime.now().strftime('%Y-%m-%d')
    
    if not result:
        cursor.execute("INSERT INTO user_limits (user_id, report_count, last_report_date) VALUES (?, 0, ?)",
                      (user_id, today))
        conn.commit()
        return True
    
    count, last_date = result
    if last_date != today:
        cursor.execute("UPDATE user_limits SET report_count = 0, last_report_date = ? WHERE user_id = ?",
                      (today, user_id))
        count = 0
    
    if count >= MAX_REPORTS_PER_DAY:
        return False
    
    return True

async def increment_user_limit(user_id):
    cursor.execute("UPDATE user_limits SET report_count = report_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()

async def retry_api_call(coro, max_attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY):
    for attempt in range(max_attempts):
        try:
            return await coro
        except TelegramNetworkError as e:
            if attempt == max_attempts - 1:
                raise
            logger.warning(f"Network error on attempt {attempt + 1}: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay * (2 ** attempt))
    raise Exception("Max retry attempts reached")

async def check_fraud_username_changes():
    while True:
        try:
            cursor.execute("SELECT DISTINCT fraud_username, fraud_user_id FROM fraud_usernames")
            fraudsters = cursor.fetchall()
            for username, user_id in fraudsters:
                if not user_id:
                    continue
                try:
                    chat = await bot.get_chat(user_id)
                    current_username = f"@{chat.username}" if chat.username else None
                    if current_username and current_username != username:
                        cursor.execute(
                            "INSERT OR REPLACE INTO fraud_usernames (fraud_username, fraud_user_id, report_id, timestamp) "
                            "VALUES (?, ?, ?, ?)",
                            (current_username, user_id, generate_report_id(user_id, datetime.now().isoformat()), datetime.now())
                        )
                        conn.commit()
                        try:
                            await bot.send_message(
                                user_id,
                                "🚨 We noticed you changed your username. Please be aware that fraudulent activities are being monitored."
                            )
                            logger.info(f"Sent username change warning to user {user_id} ({current_username})")
                        except TelegramBadRequest as e:
                            logger.warning(f"Failed to message user {user_id}: {e}")
                except TelegramBadRequest as e:
                    logger.warning(f"Failed to fetch chat for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Error in username change check: {e}")
        await asyncio.sleep(USERNAME_CHECK_INTERVAL)

# --- Inline Buttons ---
def start_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Report a Fraud", callback_data="start_report")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="show_help")]
    ])

def confirm_buttons(report_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Send", callback_data=f"confirm_report_{report_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_report")]
    ])

def help_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_start")]
    ])

# --- Handlers ---
@dp.message(CommandStart())
async def handle_start(msg: Message, state: FSMContext):
    welcome_text = (
        f"👋 Welcome, <b>{msg.from_user.full_name}</b>!\n\n"
        "This bot helps you report fraudulent activities securely.\n"
        "Use the buttons below to start a report or get help."
    )
    try:
        await retry_api_call(msg.answer(welcome_text, reply_markup=start_buttons()))
        logger.info(f"User {msg.from_user.id} started the bot")
    except Exception as e:
        logger.error(f"Failed to send welcome message to user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Unable to start the bot. Please try again later.")

@dp.message(Command('stats'), lambda msg: msg.from_user.id in ADMIN_IDS)
async def handle_stats(msg: Message):
    try:
        cursor.execute("SELECT COUNT(*) FROM reports")
        total_reports = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM reports WHERE status = 'pending'")
        pending_reports = cursor.fetchone()[0]
        stats_text = (
            f"📊 Bot Statistics:\n"
            f"Total Reports: {total_reports}\n"
            f"Pending Reports: {pending_reports}"
        )
        await retry_api_call(msg.answer(stats_text))
        logger.info(f"Admin {msg.from_user.id} requested stats")
    except Exception as e:
        logger.error(f"Failed to fetch stats for admin {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Unable to fetch statistics. Please try again later.")

@dp.callback_query(F.data == "show_help")
async def handle_help(cb: CallbackQuery):
    help_text = (
        "ℹ️ <b>How to use this bot:</b>\n\n"
        "1. Click 'Report a Fraud' to start.\n"
        "2. Pass a CAPTCHA to verify you're not a bot.\n"
        "3. Enter the fraudster's Telegram username.\n"
        "4. Describe the fraud in detail (max 1000 characters).\n"
        "5. Upload proof (JPG/PNG image, max 10MB).\n"
        "6. Provide contact info (Telegram username or phone).\n"
        "7. Confirm and submit your report.\n\n"
        "🔐 Your data is securely stored and only shared with moderators."
    )
    try:
        await retry_api_call(cb.message.edit_text(help_text, reply_markup=help_buttons()))
        logger.info(f"User {cb.from_user.id} viewed help")
    except Exception as e:
        logger.error(f"Failed to send help message to user {cb.from_user.id}: {e}")
        await cb.message.answer("⚠️ Unable to show help. Please try again.")

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(cb: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await handle_start(cb.message, state)
    except Exception as e:
        logger.error(f"Failed to return to start for user {cb.from_user.id}: {e}")
        await cb.message.answer("⚠️ Unable to return to start. Please use /start.")

@dp.callback_query(F.data == "start_report")
async def handle_report_start(cb: CallbackQuery, state: FSMContext):
    try:
        if not await check_user_limit(cb.from_user.id):
            await retry_api_call(cb.message.edit_text(
                f"⚠️ You've reached the daily limit of {MAX_REPORTS_PER_DAY} reports. Try again tomorrow."
            ))
            logger.info(f"User {cb.from_user.id} exceeded daily report limit")
            return

        cursor.execute("SELECT * FROM reports WHERE user_id = ? AND status = 'pending'", (cb.from_user.id,))
        if cursor.fetchone():
            await retry_api_call(cb.message.edit_text(
                "⚠️ You have a pending report. Please wait for it to be reviewed before submitting another."
            ))
            logger.info(f"User {cb.from_user.id} has pending report")
            return

        num1, num2, op, answer = generate_captcha()
        await state.update_data(captcha_answer=answer)
        await retry_api_call(cb.message.edit_text(
            f"Please solve this CAPTCHA to continue:\n<b>{num1} {op} {num2} = ?</b>"
        ))
        await state.set_state(ReportStates.captcha)
        logger.info(f"User {cb.from_user.id} started report process")
    except Exception as e:
        logger.error(f"Failed to start report for user {cb.from_user.id}: {e}")
        await cb.message.answer("⚠️ Unable to start report. Please try again.")

@dp.message(StateFilter(ReportStates.captcha))
async def handle_captcha_answer(msg: Message, state: FSMContext):
    try:
        data = await state.get_data()
        if msg.text.strip() == data.get("captcha_answer"):
            await retry_api_call(msg.answer("✅ CAPTCHA passed. Please enter the fraudster's Telegram username (e.g., @username)."))
            await state.set_state(ReportStates.fraud_username)
            logger.info(f"User {msg.from_user.id} passed CAPTCHA")
        else:
            num1, num2, op, answer = generate_captcha()
            await state.update_data(captcha_answer=answer)
            await retry_api_call(msg.answer(f"❌ Incorrect CAPTCHA. Try again:\n<b>{num1} {op} {num2} = ?</b>"))
            logger.warning(f"User {msg.from_user.id} failed CAPTCHA")
    except Exception as e:
        logger.error(f"Failed to process CAPTCHA for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing CAPTCHA. Please try again.")

@dp.message(StateFilter(ReportStates.fraud_username))
async def handle_fraud_username(msg: Message, state: FSMContext):
    try:
        if not validate_username(msg.text):
            await retry_api_call(msg.answer("⚠️ Invalid username. Please provide a valid Telegram username starting with @ (e.g., @username)."))
            logger.warning(f"User {msg.from_user.id} submitted invalid fraud username")
            return
        fraud_username = msg.text.strip()
        fraud_user_id = None
        try:
            chat = await bot.get_chat(fraud_username)
            fraud_user_id = chat.id
        except TelegramBadRequest as e:
            logger.warning(f"Could not fetch user ID for {fraud_username}: {e}")
        await state.update_data(fraud_username=fraud_username, fraud_user_id=fraud_user_id)
        await retry_api_call(msg.answer("Please describe the fraud you experienced (max 1000 characters)."))
        await state.set_state(ReportStates.fraud_detail)
        logger.info(f"User {msg.from_user.id} submitted fraud username {fraud_username}")
    except Exception as e:
        logger.error(f"Failed to process fraud username for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing fraud username. Please try again.")

@dp.message(StateFilter(ReportStates.fraud_detail))
async def handle_fraud_detail(msg: Message, state: FSMContext):
    try:
        if len(msg.text) > MAX_FRAUD_DETAIL_LENGTH:
            await retry_api_call(msg.answer(f"⚠️ Description too long. Please keep it under {MAX_FRAUD_DETAIL_LENGTH} characters."))
            logger.warning(f"User {msg.from_user.id} submitted too long fraud detail")
            return
        await state.update_data(fraud_detail=msg.text)
        await retry_api_call(msg.answer("Please upload proof of the fraud (JPG/PNG image, max 10MB)."))
        await state.set_state(ReportStates.proof)
        logger.info(f"User {msg.from_user.id} submitted fraud details")
    except Exception as e:
        logger.error(f"Failed to process fraud details for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing fraud details. Please try again.")

@dp.message(StateFilter(ReportStates.proof))
async def handle_proof(msg: Message, state: FSMContext):
    try:
        if not msg.photo:
            await retry_api_call(msg.answer("⚠️ Please send an image as proof (JPG/PNG)."))
            logger.warning(f"User {msg.from_user.id} sent non-image proof")
            return
        
        photo = msg.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        if file_info.file_size > MAX_IMAGE_SIZE:
            await retry_api_call(msg.answer("⚠️ Image too large. Please upload an image under 10MB."))
            logger.warning(f"User {msg.from_user.id} sent oversized image")
            return
        
        file_ext = file_info.file_path.lower().split('.')[-1]
        if f'.{file_ext}' not in ALLOWED_IMAGE_EXTENSIONS:
            await retry_api_call(msg.answer("⚠️ Only JPG and PNG images are allowed."))
            logger.warning(f"User {msg.from_user.id} sent invalid image format")
            return

        await state.update_data(proof_id=photo.file_id)
        await retry_api_call(msg.answer("Please provide your contact info (Telegram username starting with @ or phone number)."))
        await state.set_state(ReportStates.contact)
        logger.info(f"User {msg.from_user.id} uploaded proof")
    except Exception as e:
        logger.error(f"Failed to process proof for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing proof. Please try again.")

@dp.message(StateFilter(ReportStates.contact))
async def handle_contact(msg: Message, state: FSMContext):
    try:
        if not validate_contact(msg.text):
            await retry_api_call(msg.answer("⚠️ Invalid contact. Please provide a valid Telegram username (@username) or phone number."))
            logger.warning(f"User {msg.from_user.id} submitted invalid contact")
            return
        
        await state.update_data(contact=msg.text)
        data = await state.get_data()
        report_id = generate_report_id(msg.from_user.id, datetime.now().isoformat())
        await state.update_data(report_id=report_id)
        
        preview = (
            f"<b>Fraud Report Preview:</b>\n\n"
            f"<b>User:</b> @{msg.from_user.username or 'NoUsername'}\n"
            f"<b>Fraudster:</b> {data['fraud_username']}\n"
            f"<b>Details:</b> {data['fraud_detail'][:100]}...\n"
            f"<b>Contact:</b> {data['contact']}\n\n"
            f"Click below to confirm and send the report."
        )
        await retry_api_call(msg.answer_photo(
            photo=data['proof_id'],
            caption=preview,
            reply_markup=confirm_buttons(report_id)
        ))
        await state.set_state(ReportStates.confirm)
        logger.info(f"User {msg.from_user.id} submitted contact")
    except Exception as e:
        logger.error(f"Failed to process contact for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing contact. Please try again.")

@dp.callback_query(F.data.startswith("confirm_report_"))
async def finish_report(cb: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        report_id = data.get("report_id")
        
        cursor.execute(
            "INSERT INTO reports (report_id, user_id, username, fraud_username, fraud, contact, photo_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                cb.from_user.id,
                cb.from_user.username,
                data["fraud_username"],
                data["fraud_detail"],
                data["contact"],
                data["proof_id"]
            )
        )
        cursor.execute(
            "INSERT INTO fraud_usernames (fraud_username, fraud_user_id, report_id) VALUES (?, ?, ?)",
            (data["fraud_username"], data.get("fraud_user_id"), report_id)
        )
        conn.commit()
        await increment_user_limit(cb.from_user.id)

        report_text = (
            f"<b>New Fraud Report</b> | ID: {report_id}\n\n"
            f"<b>User:</b> @{cb.from_user.username or 'NoUsername'} | ID: {cb.from_user.id}\n"
            f"<b>Fraudster:</b> {data['fraud_username']}\n"
            f"<b>Details:</b> {data['fraud_detail']}\n"
            f"<b>Contact:</b> {data['contact']}"
        )
        
        for chat_id in [GROUP_ID, CHANNEL_ID]:
            await retry_api_call(bot.send_photo(
                chat_id=chat_id,
                photo=data["proof_id"],
                caption=report_text
            ))
        await retry_api_call(cb.message.edit_text(
            "✅ Your report has been submitted. Thank you for helping us fight fraud!",
            reply_markup=start_buttons()
        ))
        logger.info(f"Report {report_id} submitted by user {cb.from_user.id}")
    except Exception as e:
        logger.error(f"Failed to submit report for user {cb.from_user.id}: {e}")
        await retry_api_call(cb.message.edit_text(
            "⚠️ An error occurred while submitting your report. Please try again later.",
            reply_markup=start_buttons()
        ))
    finally:
        await state.clear()

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(cb: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await retry_api_call(cb.message.edit_text(
            "❌ Your report has been cancelled.",
            reply_markup=start_buttons()
        ))
        logger.info(f"User {cb.from_user.id} cancelled report")
    except Exception as e:
        logger.error(f"Failed to cancel report for user {cb.from_user.id}: {e}")
        await cb.message.answer("⚠️ Error cancelling report. Please use /start.")

# --- Error Handler ---
@dp.errors()
async def error_handler(update, exception):
    logger.error(f"Update {update} caused error: {exception}")
    if hasattr(update, 'message') and isinstance(update.message, Message):
        try:
            await retry_api_call(update.message.answer("⚠️ An error occurred. Please try again later."))
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")
    return True

# --- Main ---
async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot started successfully")
        # Start username change monitoring task
        asyncio.create_task(check_fraud_username_changes())
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())