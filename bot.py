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
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
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
    conn.commit()
    return conn, cursor

conn, cursor = init_db()

# --- FSM States ---
class ReportStates(StatesGroup):
    captcha = State()
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
        await msg.answer(welcome_text, reply_markup=start_buttons())
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
        await msg.answer(stats_text)
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
        "3. Describe the fraud in detail (max 1000 characters).\n"
        "4. Upload proof (JPG/PNG image, max 10MB).\n"
        "5. Provide contact info (Telegram username or phone).\n"
        "6. Confirm and submit your report.\n\n"
        "🔐 Your data is securely stored and only shared with moderators."
    )
    try:
        await cb.message.edit_text(help_text, reply_markup=help_buttons())
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
            await cb.message.edit_text(
                f"⚠️ You've reached the daily limit of {MAX_REPORTS_PER_DAY} reports. Try again tomorrow."
            )
            logger.info(f"User {cb.from_user.id} exceeded daily report limit")
            return

        cursor.execute("SELECT * FROM reports WHERE user_id = ? AND status = 'pending'", (cb.from_user.id,))
        if cursor.fetchone():
            await cb.message.edit_text(
                "⚠️ You have a pending report. Please wait for it to be reviewed before submitting another."
            )
            logger.info(f"User {cb.from_user.id} has pending report")
            return

        num1, num2, op, answer = generate_captcha()
        await state.update_data(captcha_answer=answer)
        await cb.message.edit_text(
            f"Please solve this CAPTCHA to continue:\n<b>{num1} {op} {num2} = ?</b>"
        )
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
            await msg.answer("✅ CAPTCHA passed. Please describe the fraud you experienced (max 1000 characters).")
            await state.set_state(ReportStates.fraud_detail)
            logger.info(f"User {msg.from_user.id} passed CAPTCHA")
        else:
            num1, num2, op, answer = generate_captcha()
            await state.update_data(captcha_answer=answer)
            await msg.answer(f"❌ Incorrect CAPTCHA. Try again:\n<b>{num1} {op} {num2} = ?</b>")
            logger.warning(f"User {msg.from_user.id} failed CAPTCHA")
    except Exception as e:
        logger.error(f"Failed to process CAPTCHA for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing CAPTCHA. Please try again.")

@dp.message(StateFilter(ReportStates.fraud_detail))
async def handle_fraud_detail(msg: Message, state: FSMContext):
    try:
        if len(msg.text) > MAX_FRAUD_DETAIL_LENGTH:
            await msg.answer(f"⚠️ Description too long. Please keep it under {MAX_FRAUD_DETAIL_LENGTH} characters.")
            logger.warning(f"User {msg.from_user.id} submitted too long fraud detail")
            return
        await state.update_data(fraud_detail=msg.text)
        await msg.answer("Please upload proof of the fraud (JPG/PNG image, max 10MB).")
        await state.set_state(ReportStates.proof)
        logger.info(f"User {msg.from_user.id} submitted fraud details")
    except Exception as e:
        logger.error(f"Failed to process fraud details for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing fraud details. Please try again.")

@dp.message(StateFilter(ReportStates.proof))
async def handle_proof(msg: Message, state: FSMContext):
    try:
        if not msg.photo:
            await msg.answer("⚠️ Please send an image as proof (JPG/PNG).")
            logger.warning(f"User {msg.from_user.id} sent non-image proof")
            return
        
        photo = msg.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        if file_info.file_size > MAX_IMAGE_SIZE:
            await msg.answer("⚠️ Image too large. Please upload an image under 10MB.")
            logger.warning(f"User {msg.from_user.id} sent oversized image")
            return
        
        file_ext = file_info.file_path.lower().split('.')[-1]
        if f'.{file_ext}' not in ALLOWED_IMAGE_EXTENSIONS:
            await msg.answer("⚠️ Only JPG and PNG images are allowed.")
            logger.warning(f"User {msg.from_user.id} sent invalid image format")
            return

        await state.update_data(proof_id=photo.file_id)
        await msg.answer("Please provide your contact info (Telegram username starting with @ or phone number).")
        await state.set_state(ReportStates.contact)
        logger.info(f"User {msg.from_user.id} uploaded proof")
    except Exception as e:
        logger.error(f"Failed to process proof for user {msg.from_user.id}: {e}")
        await msg.answer("⚠️ Error processing proof. Please try again.")

@dp.message(StateFilter(ReportStates.contact))
async def handle_contact(msg: Message, state: FSMContext):
    try:
        if not validate_contact(msg.text):
            await msg.answer("⚠️ Invalid contact. Please provide a valid Telegram username (@username) or phone number.")
            logger.warning(f"User {msg.from_user.id} submitted invalid contact")
            return
        
        await state.update_data(contact=msg.text)
        data = await state.get_data()
        report_id = generate_report_id(msg.from_user.id, datetime.now().isoformat())
        await state.update_data(report_id=report_id)
        
        preview = (
            f"<b>Fraud Report Preview:</b>\n\n"
            f"<b>User:</b> @{msg.from_user.username or 'NoUsername'}\n"
            f"<b>Details:</b> {data['fraud_detail'][:100]}...\n"
            f"<b>Contact:</b> {data['contact']}\n\n"
            f"Click below to confirm and send the report."
        )
        await msg.answer_photo(
            photo=data['proof_id'],
            caption=preview,
            reply_markup=confirm_buttons(report_id)
        )
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
            "INSERT INTO reports (report_id, user_id, username, fraud, contact, photo_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                report_id,
                cb.from_user.id,
                cb.from_user.username,
                data["fraud_detail"],
                data["contact"],
                data["proof_id"]
            )
        )
        conn.commit()
        await increment_user_limit(cb.from_user.id)

        report_text = (
            f"<b>New Fraud Report</b> | ID: {report_id}\n\n"
            f"<b>User:</b> @{cb.from_user.username or 'NoUsername'} | ID: {cb.from_user.id}\n"
            f"<b>Details:</b> {data['fraud_detail']}\n"
            f"<b>Contact:</b> {data['contact']}"
        )
        
        for chat_id in [GROUP_ID, CHANNEL_ID]:
            await bot.send_photo(
                chat_id=chat_id,
                photo=data["proof_id"],
                caption=report_text
            )
        await cb.message.edit_text(
            "✅ Your report has been submitted. Thank you for helping us fight fraud!",
            reply_markup=start_buttons()
        )
        logger.info(f"Report {report_id} submitted by user {cb.from_user.id}")
    except Exception as e:
        logger.error(f"Failed to submit report for user {cb.from_user.id}: {e}")
        await cb.message.edit_text(
            "⚠️ An error occurred while submitting your report. Please try again later.",
            reply_markup=start_buttons()
        )
    finally:
        await state.clear()

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(cb: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await cb.message.edit_text(
            "❌ Your report has been cancelled.",
            reply_markup=start_buttons()
        )
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
            await update.message.answer("⚠️ An error occurred. Please try again later.")
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")
    return True

# --- Main ---
async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot started successfully")
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())