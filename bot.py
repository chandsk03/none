import asyncio
import logging
import random
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.filters import CommandStart, StateFilter

# --- Config ---
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGaPSv_bgfiJ6f_gMLhXfL0HOpaAfYsCEI"
GROUP_ID = -1002431056179
CHANNEL_ID = -1002288539987
ADMIN_IDS = [7584086775]

# --- Logging ---
logging.basicConfig(level=logging.INFO)

# --- Bot Setup ---
bot = Bot(token=BOT_TOKEN, default=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# --- SQLite Setup ---
conn = sqlite3.connect("reports.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        fraud TEXT,
        contact TEXT,
        photo_id TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

# --- FSM States ---
class ReportStates(StatesGroup):
    captcha = State()
    fraud_detail = State()
    proof = State()
    contact = State()
    confirm = State()

# --- CAPTCHA ---
def generate_captcha():
    num1, num2 = random.randint(1, 9), random.randint(1, 9)
    return num1, num2, str(num1 + num2)

# --- Inline Buttons ---
def start_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Report a Fraud", callback_data="start_report")]
    ])

def confirm_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm & Send", callback_data="confirm_report")],
        [InlineKeyboardButton(text="Cancel", callback_data="cancel_report")]
    ])

# --- Start Handler ---
@dp.message(CommandStart())
async def handle_start(msg: Message, state: FSMContext):
    await msg.answer(f"Welcome, <b>{msg.from_user.full_name}</b>!\n\n"
                     "Use the button below to report a fraud.", 
                     reply_markup=start_buttons())

# --- Start Report ---
@dp.callback_query(F.data == "start_report")
async def handle_report_start(cb: CallbackQuery, state: FSMContext):
    cursor.execute("SELECT * FROM reports WHERE user_id = ?", (cb.from_user.id,))
    if cursor.fetchone():
        await cb.message.edit_text("⚠️ You've already submitted a report. Thank you!")
        return
    num1, num2, answer = generate_captcha()
    await state.update_data(captcha_answer=answer)
    await cb.message.edit_text(f"Please solve this CAPTCHA to continue:\n<b>{num1} + {num2} = ?</b>")
    await state.set_state(ReportStates.captcha)

# --- CAPTCHA Response ---
@dp.message(StateFilter(ReportStates.captcha))
async def handle_captcha_answer(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text.strip() == data.get("captcha_answer"):
        await msg.answer("✅ CAPTCHA passed. Please describe the fraud you experienced.")
        await state.set_state(ReportStates.fraud_detail)
    else:
        await msg.answer("❌ Incorrect CAPTCHA. Try again.")
        await handle_report_start(msg, state)

# --- Fraud Detail ---
@dp.message(StateFilter(ReportStates.fraud_detail))
async def handle_fraud_detail(msg: Message, state: FSMContext):
    await state.update_data(fraud_detail=msg.text)
    await msg.answer("Please upload any proof of the fraud (photo, screenshot, etc.).")
    await state.set_state(ReportStates.proof)

# --- Proof Handler ---
@dp.message(StateFilter(ReportStates.proof))
async def handle_proof(msg: Message, state: FSMContext):
    if not msg.photo:
        await msg.answer("⚠️ Please send a photo as proof.")
        return
    photo_id = msg.photo[-1].file_id
    await state.update_data(proof_id=photo_id)
    await msg.answer("Now send your contact info (Telegram username or phone number).")
    await state.set_state(ReportStates.contact)

# --- Contact Info ---
@dp.message(StateFilter(ReportStates.contact))
async def handle_contact(msg: Message, state: FSMContext):
    await state.update_data(contact=msg.text)
    data = await state.get_data()
    preview = (f"<b>Fraud Report Preview:</b>\n\n"
               f"<b>User:</b> @{msg.from_user.username or 'NoUsername'}\n"
               f"<b>Details:</b> {data['fraud_detail']}\n"
               f"<b>Contact:</b> {data['contact']}\n\n"
               f"Click below to confirm and send the report.")
    await msg.answer_photo(photo=data['proof_id'], caption=preview, reply_markup=confirm_buttons())
    await state.set_state(ReportStates.confirm)

# --- Confirm Report ---
@dp.callback_query(F.data == "confirm_report")
async def finish_report(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cursor.execute("INSERT INTO reports (user_id, username, fraud, contact, photo_id) VALUES (?, ?, ?, ?, ?)", (
        cb.from_user.id,
        cb.from_user.username,
        data["fraud_detail"],
        data["contact"],
        data["proof_id"]
    ))
    conn.commit()

    report_text = (f"<b>New Fraud Report Received</b>\n\n"
                   f"<b>User:</b> @{cb.from_user.username or 'NoUsername'} | ID: {cb.from_user.id}\n"
                   f"<b>Details:</b> {data['fraud_detail']}\n"
                   f"<b>Contact:</b> {data['contact']}")
    
    try:
        await bot.send_photo(chat_id=GROUP_ID, photo=data["proof_id"], caption=report_text)
        await bot.send_photo(chat_id=CHANNEL_ID, photo=data["proof_id"], caption=report_text)
    except Exception as e:
        logging.error(f"Failed to send report: {e}")

    await cb.message.edit_text("✅ Your report has been submitted. Thank you for helping us fight fraud!")
    await state.clear()

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Your report has been cancelled.")

# --- Main ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
