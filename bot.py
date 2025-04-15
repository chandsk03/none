import asyncio
import logging
import random
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# Logging setup
logging.basicConfig(level=logging.INFO)

# Telegram API credentials
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"

# Target IDs
GROUP_ID = -1002431056179
CHANNEL_ID = -1002288539987

# Admins
ADMIN_IDS = [7584086775]

# Bot setup
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# SQLite DB setup
conn = sqlite3.connect("fraud_reports.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        user_id INTEGER,
        username TEXT,
        fraud_detail TEXT,
        proof_id TEXT,
        contact_info TEXT,
        additional_info TEXT
    )
""")
conn.commit()

# FSM States
class FraudReport(StatesGroup):
    captcha = State()
    fraud_detail = State()
    proof = State()
    contact = State()
    additional_info = State()

# Start command handler
@dp.message(F.text == "/start")
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    username = message.from_user.full_name
    captcha_answer = random.randint(1000, 9999)
    await state.update_data(captcha=captcha_answer)
    
    await message.answer(
        f"Welcome, <b>{username}</b>!\n\n"
        "To begin your fraud report, please solve the CAPTCHA below to verify you're human.\n\n"
        f"<b>Enter this number:</b> <code>{captcha_answer}</code>"
    )
    await state.set_state(FraudReport.captcha)

# Captcha verification
@dp.message(FraudReport.captcha)
async def verify_captcha(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text.strip() == str(data['captcha']):
        await message.answer(
            "CAPTCHA verified successfully!\n\n"
            "Please describe the fraud incident in detail:"
        )
        await state.set_state(FraudReport.fraud_detail)
    else:
        await message.answer("Incorrect CAPTCHA. Please try again.")

# Fraud detail
@dp.message(FraudReport.fraud_detail)
async def receive_fraud_detail(message: Message, state: FSMContext):
    await state.update_data(fraud_detail=message.text)
    await message.answer("Please send a screenshot or image as proof of the fraud:")
    await state.set_state(FraudReport.proof)

# Proof of fraud
@dp.message(FraudReport.proof, F.photo)
async def receive_proof(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(proof_id=photo_id)
    await message.answer("Enter the fraudster's Telegram username or phone number:")
    await state.set_state(FraudReport.contact)

# Contact info
@dp.message(FraudReport.contact)
async def receive_contact(message: Message, state: FSMContext):
    await state.update_data(contact_info=message.text)
    await message.answer("Any additional important information you'd like to share?")
    await state.set_state(FraudReport.additional_info)

# Additional info + confirmation
@dp.message(FraudReport.additional_info)
async def finish_report(message: Message, state: FSMContext):
    await state.update_data(additional_info=message.text)
    data = await state.get_data()

    report_text = (
        f"<b>New Fraud Report</b>\n\n"
        f"<b>From:</b> @{message.from_user.username or 'N/A'} | ID: <code>{message.from_user.id}</code>\n"
        f"<b>Fraud Details:</b>\n{data['fraud_detail']}\n\n"
        f"<b>Fraudster Contact:</b> {data['contact_info']}\n"
        f"<b>Additional Info:</b>\n{data['additional_info'] or 'None'}"
    )

    # Send to group and channel
    try:
        await bot.send_photo(chat_id=GROUP_ID, photo=data['proof_id'], caption=report_text)
        await bot.send_photo(chat_id=CHANNEL_ID, photo=data['proof_id'], caption=report_text)
    except Exception as e:
        await message.answer("There was an error sending your report to the group/channel.")
        logging.error(f"Failed to send report: {e}")
        return

    # Save in DB
    cursor.execute(
        "INSERT INTO reports (user_id, username, fraud_detail, proof_id, contact_info, additional_info) VALUES (?, ?, ?, ?, ?, ?)",
        (
            message.from_user.id,
            message.from_user.username,
            data['fraud_detail'],
            data['proof_id'],
            data['contact_info'],
            data['additional_info'],
        )
    )
    conn.commit()

    await message.answer("✅ Your report has been submitted successfully. Thank you!")
    await state.clear()

# Admin command to export DB
@dp.message(F.text == "/export")
async def export_db(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await bot.send_document(chat_id=message.chat.id, document=FSInputFile("fraud_reports.db"))
    else:
        await message.answer("You are not authorized to use this command.")

# Start polling
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
